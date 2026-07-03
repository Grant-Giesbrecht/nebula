"""
Manual archive operations: bringing files in by hand, and adopting files
already dropped into a session folder.

These are the deliberate, out-of-band counterparts to the measurement-script
flow -- for data a coworker emailed you, a manual instrument export, or a
new session seeded from files someone sent. Every path here still writes a
proper sidecar, but with honest "external" provenance (source="external"
plus a free-text origin) rather than a faked git commit, so a hand-added
file is a first-class citizen in the archive instead of an orphan.

Each operation also appends an entry to the session's history log (in
session.yaml), so a hand-edited session records what changed and why.

This module holds the reusable functions; the CLI (`nebula import`,
`nebula reconcile`) and, later, a GUI drive them.
"""

from __future__ import annotations

import getpass
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from nebula.registry import resolve_archive
from nebula.session import (
    Session,
    _created_today,
    _find_session_dir,
    _hold_active,
    _now_iso,
    new,
    orphan_artifacts_in,
)
from nebula.sidecar import (
    ProducedBy,
    SidecarMeta,
    read_session_yaml,
    sidecar_path_for,
    write_session_yaml,
    write_sidecar,
)


def _default_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _write_external_sidecar(
    dest: Path,
    *,
    origin: Optional[str],
    imported_by: str,
    derived_from: Optional[List] = None,
    inputs: Optional[Dict] = None,
    extra: Optional[Dict] = None,
) -> Path:
    """Write a sidecar recording that `dest` came from outside a tracked
    script. write_sidecar fills in the sha256."""
    produced_by = ProducedBy(
        source="external",
        origin=origin,
        imported_by=imported_by,
        imported_at=_now_iso(),
    )
    meta = SidecarMeta(
        created=_now_iso(),
        produced_by=produced_by,
        inputs=inputs or {},
        extra=extra or {},
    )
    for ref in derived_from or []:
        meta.add_derived_from(ref)
    return write_sidecar(dest, meta)


def _record_history(session_dir: Path, action: str, **fields) -> None:
    """Append a history entry to session.yaml (read-modify-write)."""
    meta = read_session_yaml(session_dir)
    meta.add_history(action, **fields)
    write_session_yaml(session_dir, meta)


def _ensure_writable(meta, run_id: str, allow_frozen: bool) -> None:
    """Enforce the same freeze rule as append_to: a session closed on a
    previous day (with no hold) can't be written to unless you opt in."""
    frozen = (
        meta.status != "open"
        and not _created_today(meta)
        and not _hold_active(meta)
    )
    if frozen and not allow_frozen:
        raise RuntimeError(
            f"session {run_id!r} was closed on a previous day; pass "
            f"allow_frozen=True (CLI: --reopen) to import into it anyway."
        )


def _place_file(
    session_dir: Path,
    src: "str | Path",
    *,
    move: bool = False,
    dest_name: Optional[str] = None,
    origin: Optional[str] = None,
    imported_by: str,
    derived_from: Optional[List] = None,
    inputs: Optional[Dict] = None,
) -> Path:
    """Copy (or move) a source file into a session folder and write its
    external sidecar. Does NOT touch session.yaml -- the caller records
    history, so the new-session path can batch it into a single close."""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"no such file to import: {src}")
    dest = session_dir / (dest_name or src.name)
    if dest.exists():
        raise FileExistsError(
            f"{dest.name!r} already exists in {session_dir.name}; import it "
            f"under a different --as name (replacing files comes later)."
        )
    if move:
        shutil.move(str(src), str(dest))
    else:
        shutil.copy2(str(src), str(dest))
    _write_external_sidecar(
        dest,
        origin=origin,
        imported_by=imported_by,
        derived_from=derived_from,
        inputs=inputs,
    )
    return dest


def import_file(
    archive: "str | Path",
    run_id: str,
    src: "str | Path",
    *,
    dest_name: Optional[str] = None,
    origin: Optional[str] = None,
    imported_by: Optional[str] = None,
    derived_from: Optional[List] = None,
    inputs: Optional[Dict] = None,
    move: bool = False,
    allow_frozen: bool = False,
) -> Path:
    """Import one external file into an existing session, writing its
    sidecar and logging the import. Honors the previous-day-closed freeze
    (pass allow_frozen=True to override). Returns the destination path.

    Importing does not change the session's open/closed status -- it's a
    discrete, out-of-band edit, not a resumption of the session."""
    archive_root, _ = resolve_archive(archive)
    session_dir = _find_session_dir(archive_root, run_id)
    meta = read_session_yaml(session_dir)
    _ensure_writable(meta, run_id, allow_frozen)

    imported_by = imported_by or _default_user()
    dest = _place_file(
        session_dir, src, move=move, dest_name=dest_name, origin=origin,
        imported_by=imported_by, derived_from=derived_from, inputs=inputs,
    )
    _record_history(
        session_dir, "import", file=dest.name, note=origin, by=imported_by,
        source=str(Path(src)),
    )
    return dest


def import_new(
    archive: "str | Path",
    srcs: List["str | Path"],
    *,
    tags: Optional[List[str]] = None,
    description: str = "",
    origin: Optional[str] = None,
    imported_by: Optional[str] = None,
    move: bool = False,
    archive_name: Optional[str] = None,
) -> Session:
    """Create a new session seeded with external files (e.g. a dataset a
    coworker emailed). Returns the (now closed) Session."""
    imported_by = imported_by or _default_user()
    s = new(archive, tags=tags, description=description, archive_name=archive_name)
    try:
        for src in srcs:
            dest = _place_file(
                s.path, src, move=move, origin=origin, imported_by=imported_by,
            )
            # Add history to the in-memory meta so the single close() below
            # persists it -- writing session.yaml out-of-band here would be
            # clobbered by that close.
            s.meta.add_history(
                "import", file=dest.name, note=origin, by=imported_by,
                source=str(Path(src)),
            )
    finally:
        s.close()
    return s


def adopt_file(
    path: "str | Path",
    *,
    origin: Optional[str] = None,
    imported_by: Optional[str] = None,
    derived_from: Optional[List] = None,
    inputs: Optional[Dict] = None,
) -> Path:
    """Write an external sidecar for a file that's ALREADY sitting in a
    session folder (dropped in by hand). Unlike import_file it doesn't move
    any bytes -- it just adopts the file in place. Used by `reconcile`."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"no such file: {path}")
    if sidecar_path_for(path).exists():
        raise FileExistsError(f"{path.name!r} already has a sidecar")
    imported_by = imported_by or _default_user()
    _write_external_sidecar(
        path, origin=origin, imported_by=imported_by,
        derived_from=derived_from, inputs=inputs, extra={"reconciled": True},
    )
    _record_history(
        path.parent, "reconcile", file=path.name, note=origin, by=imported_by,
    )
    return path


def find_orphan_files(
    archive: "str | Path", run_id: Optional[str] = None
) -> List[Path]:
    """Files in the archive (or one session) that have no sidecar -- the
    candidates `reconcile` offers to adopt."""
    archive_root, _ = resolve_archive(archive)
    if run_id is not None:
        session_dirs = [_find_session_dir(archive_root, run_id)]
    else:
        from nebula.index import _iter_session_dirs

        session_dirs = list(_iter_session_dirs(archive_root))
    orphans: List[Path] = []
    for session_dir in session_dirs:
        orphans.extend(orphan_artifacts_in(session_dir))
    return orphans
