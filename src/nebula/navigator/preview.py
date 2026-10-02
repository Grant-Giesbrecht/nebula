"""
File previews for the Navigator's space / F2 popup.

Everything here returns plain JSON-able dicts keyed by ``kind`` so the front
end only has to switch on it:

    text   -- {"text", "truncated"}                  (txt, py, md, json, ...)
    table  -- {"columns", "rows", "truncated"}       (csv / tsv)
    image  -- {"uri"}                                 (png, jpeg, gif, ...)
    hdf    -- {"nodes", "truncated"}                  (HDF5 and tome files)
    none   -- {"reason"}                              (nothing sensible to show)

A tome file *is* an HDF5 file (see stardust's tome format), so both go
through the same tree; the ``__pytype__`` tags that make a tome a tome show
up as ordinary attributes. Datasets are read lazily (:func:`hdf_node`) -- the
tree itself carries only names, shapes and dtypes, so opening a big file is
cheap.

h5py, matplotlib and graf are optional: a missing one turns into a ``none``
preview with an install hint rather than an error. The sidecar protocol is
line-delimited JSON over a pipe, so every payload here is capped (see the
``MAX_*`` constants) rather than letting one preview stall the queue.
"""

from __future__ import annotations

import base64
import csv
import io
from pathlib import Path
from typing import Any, Dict, List

MAX_TEXT_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 4 << 20
MAX_TABLE_ROWS = 500
MAX_TABLE_COLS = 64
MAX_HDF_NODES = 5000

TEXT_EXTS = {".txt", ".py", ".md", ".json", ".yaml", ".yml", ".toml", ".ini",
             ".cfg", ".log", ".m", ".sh", ".js", ".html", ".css", ".xml",
             ".tex", ".rst", ".cpp", ".c", ".h", ".rs", ".dat", ".meta"}
TABLE_EXTS = {".csv": ",", ".tsv": "\t"}
IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}
HDF_EXTS = {".h5", ".hdf5", ".hdf", ".he5", ".tome"}


def _none(reason: str) -> Dict[str, Any]:
    return {"kind": "none", "reason": reason}


def preview_file(path) -> Dict[str, Any]:
    """Pick a preview by extension (case-insensitive) and build it."""
    p = Path(path)
    if not p.is_file():
        return _none("file not found")
    ext = p.suffix.lower()
    try:
        if ext == ".graf":
            return _preview_graf(p)
        if ext in HDF_EXTS:
            return _preview_hdf(p)
        if ext in IMAGE_MIME:
            return _preview_image(p, ext)
        if ext in TABLE_EXTS:
            return _preview_table(p, TABLE_EXTS[ext])
        if ext in TEXT_EXTS or ext == "" or p.name.endswith(".meta.json"):
            return _preview_text(p)
    except Exception as e:  # a corrupt file must not take the bridge down
        return _none(f"could not preview: {e}")
    return _none(f"no preview for {ext or 'this type'}")


def _read_head(p: Path, limit: int) -> "tuple[bytes, bool]":
    with open(p, "rb") as fh:
        data = fh.read(limit + 1)
    return data[:limit], len(data) > limit


def _preview_text(p: Path) -> Dict[str, Any]:
    data, truncated = _read_head(p, MAX_TEXT_BYTES)
    if b"\x00" in data[:4096]:
        return _none("binary file")
    return {"kind": "text", "text": data.decode("utf-8", errors="replace"),
            "truncated": truncated}


def _preview_table(p: Path, delim: str) -> Dict[str, Any]:
    data, cut = _read_head(p, MAX_TEXT_BYTES)
    text = data.decode("utf-8", errors="replace")
    rows: List[List[str]] = []
    truncated = cut
    for row in csv.reader(io.StringIO(text), delimiter=delim):
        if len(rows) >= MAX_TABLE_ROWS:
            truncated = True
            break
        if len(row) > MAX_TABLE_COLS:
            truncated = True
            row = row[:MAX_TABLE_COLS]
        rows.append(row)
    if cut and rows:
        rows.pop()          # the last line is probably sliced mid-row
    if not rows:
        return {"kind": "table", "columns": [], "rows": [], "truncated": False}
    return {"kind": "table", "columns": rows[0], "rows": rows[1:],
            "truncated": truncated}


def _preview_image(p: Path, ext: str) -> Dict[str, Any]:
    if p.stat().st_size > MAX_IMAGE_BYTES:
        return _none(f"image too large to preview ({p.stat().st_size >> 20} MB)")
    return {"kind": "image", "uri": _data_uri(IMAGE_MIME[ext], p.read_bytes())}


def _data_uri(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _preview_graf(p: Path) -> Dict[str, Any]:
    try:
        import matplotlib
        matplotlib.use("Agg")           # render only; never open a window
        import matplotlib.pyplot as plt
        import graf
    except ImportError as e:
        return _none(f"graf preview needs matplotlib and graf installed ({e})")
    import warnings
    with warnings.catch_warnings():     # old-format notices are not for here
        warnings.simplefilter("ignore")
        fig = graf.load_graf(str(p))
    try:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100)
    finally:
        plt.close(fig)
    return {"kind": "image", "uri": _data_uri("image/png", buf.getvalue())}


# ---------------------------------------------------------------------
# HDF5 / tome
# ---------------------------------------------------------------------

def _h5py():
    try:
        import h5py
        return h5py
    except ImportError:
        return None


def _scalar_str(v: Any) -> str:
    """Render one HDF5 value for a table cell."""
    import numpy as np

    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    if isinstance(v, np.bytes_):
        return bytes(v).decode("utf-8", errors="replace")
    if isinstance(v, (float, np.floating)):
        return f"{float(v):.6g}"
    if isinstance(v, (complex, np.complexfloating)):
        return f"{complex(v):.6g}"
    if isinstance(v, np.ndarray):
        return "[" + ", ".join(_scalar_str(x) for x in v.ravel()[:8]) + \
               (", …]" if v.size > 8 else "]")
    return str(v)


def _preview_hdf(p: Path) -> Dict[str, Any]:
    h5py = _h5py()
    if h5py is None:
        return _none("HDF5 preview needs h5py installed")
    nodes: List[Dict[str, Any]] = []
    truncated = False
    with h5py.File(p, "r") as fh:
        def describe(name: str, obj) -> Dict[str, Any]:
            is_ds = isinstance(obj, h5py.Dataset)
            pytype = obj.attrs.get("__pytype__")
            return {
                "path": "/" + name if name else "/",
                "name": name.rsplit("/", 1)[-1] if name else "/",
                "kind": "dataset" if is_ds else "group",
                "shape": list(obj.shape) if is_ds else None,
                "dtype": str(obj.dtype) if is_ds else None,
                "n_attrs": len(obj.attrs),
                "pytype": _scalar_str(pytype) if pytype is not None else None,
            }

        nodes.append(describe("", fh))

        def visit(name, obj):
            nonlocal truncated
            if len(nodes) >= MAX_HDF_NODES:
                truncated = True
                return True         # stop visiting
            nodes.append(describe(name, obj))
            return None

        fh.visititems(visit)
    return {"kind": "hdf", "nodes": nodes, "truncated": truncated}


def hdf_node(path, node_path: str, max_rows: int = 200,
             max_cols: int = 32) -> Dict[str, Any]:
    """Attributes and (for a dataset) a table of values for one tree node."""
    import numpy as np

    h5py = _h5py()
    if h5py is None:
        return {"error": "h5py not installed"}
    with h5py.File(path, "r") as fh:
        obj = fh[node_path]
        attrs = [[k, _scalar_str(v)] for k, v in obj.attrs.items()]
        out: Dict[str, Any] = {"path": node_path, "attrs": attrs}
        if not isinstance(obj, h5py.Dataset):
            out["members"] = sorted(obj.keys())[:500]
            return out
        out.update(shape=list(obj.shape), dtype=str(obj.dtype))
        note = ""
        if obj.shape == ():
            out["scalar"] = _scalar_str(obj[()])
            return out
        if obj.size == 0:
            out["columns"], out["rows"] = [], []
            return out
        dt = obj.dtype
        if dt.names:                                    # compound: one column per field
            data = obj[:max_rows]
            cols = ["#"] + list(dt.names)
            rows = [[str(i)] + [_scalar_str(r[n]) for n in dt.names]
                    for i, r in enumerate(data)]
            n_total = obj.shape[0]
        else:
            nd = obj.ndim
            if nd == 1:
                data = obj[:max_rows].reshape(-1, 1)
                n_total = obj.shape[0]
            else:
                lead = (0,) * (nd - 2)
                if lead:
                    note = f"showing [{', '.join('0' for _ in lead)}, :, :] of {tuple(obj.shape)}"
                data = np.asarray(obj[lead + (slice(0, max_rows), slice(0, max_cols))]) \
                    if nd >= 2 else data
                n_total = obj.shape[nd - 2]
            ncols = data.shape[1]
            cols = ["#"] + ([f"{i}" for i in range(ncols)] if ncols > 1 or nd > 1
                            else ["value"])
            rows = [[str(i)] + [_scalar_str(x) for x in r] for i, r in enumerate(data)]
            if nd >= 2 and obj.shape[-1] > max_cols:
                note = (note + "; " if note else "") + f"first {max_cols} of {obj.shape[-1]} columns"
        if n_total > max_rows:
            note = (note + "; " if note else "") + f"first {max_rows} of {n_total} rows"
        out.update(columns=cols, rows=rows, note=note)
        return out
