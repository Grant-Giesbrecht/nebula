"""
Content-addressed store for asset snapshots.

Deliberately separate from :mod:`nebula.codestore`, which holds captured
source. The two stores have the same *shape* and nothing else in common:

* **Different lifecycles.** A code blob is live exactly while some sidecar
  names its manifest. An asset blob is live while an asset retains the
  snapshot *or* a session pinned those bytes -- two independent claims,
  one of which (the session pin) deliberately outranks the other.
* **Different sizes.** Code blobs are capped at a megabyte by
  construction. Asset blobs are the 100 GB video.

They shared one store briefly, and it produced exactly the bug that
arrangement invites: ``codestore.gc`` computed liveness from sidecar
``produced_by.code`` alone, saw every asset blob as unreachable, and
deleted bytes a session had pinned. Separating the stores makes that class
of mistake structurally impossible rather than a thing each new reachability
walk has to remember -- code gc now cannot reach asset bytes at all.

There are no manifests here. A manifest exists in the code store because a
snapshot is a *set* of files; an asset snapshot is one file, so its digest
is the whole of its identity::

    asset-store/blobs/<aa>/<bb>/<sha256>

Not hidden, for the same reason the code store is not: the archive is meant
to be browsable by hand when something needs troubleshooting.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

STORE_DIR = "asset-store"
BLOBS = "blobs"


class UnreadableAssetRecord(Exception):
    """An asset record could not be parsed, so what it references is
    unknown. Raised only where that ignorance would otherwise be read as
    "references nothing" -- i.e. by gc, before it deletes anything."""


def blobs_root(archive_root) -> Path:
    return Path(archive_root) / STORE_DIR / BLOBS


def blob_path(archive_root, digest: str) -> Path:
    """Two-level fanout, as in the code store: one directory per asset
    version would be brutal to sync through Dropbox/MEGA."""
    return blobs_root(archive_root) / digest[:2] / digest[2:4] / digest


def store_blob_file(archive_root, src: "str | Path", *,
                    digest: Optional[str] = None, _chunk: int = 1 << 20) -> str:
    """Store a file's bytes without ever holding them all in memory.

    `digest` skips the hashing pass when the caller already knows it, which
    the asset path always does: it has to hash to decide whether a snapshot
    is needed at all, and re-reading 100 GB to learn the same answer twice
    is the difference between usable and not.
    """
    src = Path(src)
    if digest is None:
        h = hashlib.sha256()
        with open(src, "rb") as f:
            for block in iter(lambda: f.read(_chunk), b""):
                h.update(block)
        digest = h.hexdigest()

    path = blob_path(archive_root, digest)
    if path.exists():
        return digest          # the name is the hash: present means identical
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp.")
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as f:
            for block in iter(lambda: f.read(_chunk), b""):
                out.write(block)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return digest


def _iter_blobs(archive_root: Path):
    base = blobs_root(archive_root)
    if not base.is_dir():
        return
    for path in base.rglob("*"):
        # Skip our own temp files and whatever the OS drops in here
        # (.DS_Store arrives via Finder/MEGA): not store objects, and
        # reporting them as collectable garbage is noise.
        if path.is_file() and not path.name.startswith("."):
            yield path


def referenced_blobs(archive_root, *, include_trash: bool = True) -> Dict[str, List[str]]:
    """{digest: [what claims it]} -- every asset blob something still wants.

    Two independent claims, and a blob survives on either:

    * a **retained snapshot** on an asset, i.e. one no storage cap has
      evicted. An evicted record (``pending_gc``) makes no claim its bytes
      survive -- that is precisely what lets a cap reclaim anything.
    * a **pinned derived_from edge** on a session sidecar. This outranks
      the asset's own cap: the asset may have evicted that version, but a
      session recorded that it was built from those exact bytes, and
      honouring that is the entire reason pinning exists.

    Raises :class:`UnreadableAssetRecord` if any record cannot be parsed.
    A record that will not parse cannot be asked what it claims, and
    treating "I could not read it" as "it claims nothing" is what turns one
    corrupt file into deleted bytes.
    """
    from nebula import assets as assets_mod
    from nebula.codestore import _iter_sidecar_files
    import json

    archive_root = Path(archive_root)
    out: Dict[str, List[str]] = {}

    for asset_id in assets_mod.list_assets(archive_root):
        try:
            meta = assets_mod.read_asset(archive_root, asset_id)
        except assets_mod.AssetError as e:
            raise UnreadableAssetRecord(
                f"cannot determine what {asset_id} references: {e}") from None
        for snap in meta.snapshots:
            if not snap.pending_gc:
                out.setdefault(snap.sha256, []).append(asset_id)

    for path in _iter_sidecar_files(archive_root, include_trash=include_trash):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for entry in data.get("derived_from") or []:
            sha = entry.get("sha256")
            if entry.get("asset") and sha and entry.get("fidelity") == "pinned":
                out.setdefault(sha, []).append(str(path))
    return out


def gc(archive_root, *, dry_run: bool = True, include_trash: bool = True) -> dict:
    """Delete asset blobs nothing claims (mark and sweep).

    Dry run by default. Nothing outside asset-store/ is ever touched --
    in particular this cannot reach the code store, which is the point of
    them being separate.
    """
    archive_root = Path(archive_root)
    try:
        live = set(referenced_blobs(archive_root, include_trash=include_trash))
    except UnreadableAssetRecord as e:
        # Refuse rather than guess: the blobs whose safety is unknown are
        # exactly the ones we would be deleting.
        return {"dry_run": dry_run, "blobs": [], "bytes": 0, "live_blobs": 0,
                "skipped": str(e)}

    dead, freed = [], 0
    for path in _iter_blobs(archive_root):
        if path.name not in live:
            dead.append(path)

    for path in dead:
        try:
            freed += path.stat().st_size
        except OSError:
            pass
        if not dry_run:
            try:
                path.unlink()
            except OSError:
                pass

    return {"dry_run": dry_run, "blobs": [p.name for p in dead], "bytes": freed,
            "live_blobs": len(live), "skipped": None}
