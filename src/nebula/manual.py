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

import datetime
import getpass
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from nebula.check import dependents_of, inbound_to_session
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
    read_sidecar,
    sha256_file,
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


# ---------------------------------------------------------------------
# delete / replace  (soft-delete to a per-session .trash/)
# ---------------------------------------------------------------------

TRASH_DIRNAME = ".trash"


def _timestamp_slug() -> str:
    return datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def _move_to_trash(trash_dir: Path, path: Path, stamp: str) -> Path:
    """Move a file into trash under a timestamp-prefixed name, dodging
    collisions if the same name is trashed twice in one second."""
    trash_dir.mkdir(exist_ok=True)
    dest = trash_dir / f"{stamp}-{path.name}"
    n = 1
    while dest.exists():
        dest = trash_dir / f"{stamp}-{n}-{path.name}"
        n += 1
    shutil.move(str(path), str(dest))
    return dest


def delete_file(
    archive: "str | Path",
    run_id: str,
    filename: str,
    *,
    reason: Optional[str] = None,
    by: Optional[str] = None,
    force: bool = False,
) -> Path:
    """Soft-delete an artifact: move it (and its sidecar) into the
    session's .trash/, and log the deletion. Refuses if another artifact
    still derives from it, unless force=True. Returns the trashed path."""
    archive_root, _ = resolve_archive(archive)
    session_dir = _find_session_dir(archive_root, run_id)
    target = session_dir / filename
    sidecar = sidecar_path_for(target)
    # Tolerate a stray sidecar with no artifact (the "missing_artifact" case
    # from check) so `rm` can clear it -- but require that *something* exists.
    if not target.is_file() and not sidecar.exists():
        raise FileNotFoundError(f"no artifact or sidecar {filename!r} in {run_id}")

    deps = dependents_of(archive_root, run_id, filename)
    if deps and not force:
        raise RuntimeError(
            f"{filename!r} is still derived from by: {', '.join(deps)}. "
            f"Delete/repoint those first, or pass force=True to break the link."
        )

    by = by or _default_user()
    if sidecar.exists():
        old_sha = read_sidecar(target).sha256
    elif target.is_file():
        old_sha = sha256_file(target)
    else:
        old_sha = None
    trash = session_dir / TRASH_DIRNAME
    stamp = _timestamp_slug()
    trashed = None
    if target.is_file():
        trashed = _move_to_trash(trash, target, stamp)
    if sidecar.exists():
        moved_sidecar = _move_to_trash(trash, sidecar, stamp)
        trashed = trashed or moved_sidecar
    _record_history(
        session_dir, "delete", file=filename, note=reason, by=by,
        sha256=old_sha, trashed_to=trashed.name,
        broke_links=deps or None,
    )
    return trashed


def replace_file(
    archive: "str | Path",
    run_id: str,
    filename: str,
    new_src: "str | Path",
    *,
    reason: Optional[str] = None,
    origin: Optional[str] = None,
    by: Optional[str] = None,
    move: bool = False,
) -> Path:
    """Replace an artifact's bytes with a new file, keeping the same name
    and its derived_from/inputs. The old version is soft-deleted to
    .trash/, and the swap is logged with both checksums. The new sidecar is
    marked external (a human, not a script, put these bytes here)."""
    archive_root, _ = resolve_archive(archive)
    session_dir = _find_session_dir(archive_root, run_id)
    target = session_dir / filename
    new_src = Path(new_src)
    if not target.is_file():
        raise FileNotFoundError(f"no artifact {filename!r} in {run_id} to replace")
    if not new_src.is_file():
        raise FileNotFoundError(f"no such replacement file: {new_src}")

    by = by or _default_user()
    sidecar = sidecar_path_for(target)
    old_meta = read_sidecar(target) if sidecar.exists() else None
    old_sha = old_meta.sha256 if old_meta else sha256_file(target)

    trash = session_dir / TRASH_DIRNAME
    stamp = _timestamp_slug()
    _move_to_trash(trash, target, stamp)
    if sidecar.exists():
        _move_to_trash(trash, sidecar, stamp)

    if move:
        shutil.move(str(new_src), str(target))
    else:
        shutil.copy2(str(new_src), str(target))

    produced_by = ProducedBy(
        source="external", origin=origin or reason,
        imported_by=by, imported_at=_now_iso(),
    )
    meta = SidecarMeta(
        created=_now_iso(),
        produced_by=produced_by,
        derived_from=(old_meta.derived_from if old_meta else []),
        inputs=(old_meta.inputs if old_meta else {}),
        extra={"replaced_sha256": old_sha, "replaced_at": _now_iso()},
    )
    write_sidecar(target, meta)  # fills new sha256
    _record_history(
        session_dir, "replace", file=filename, note=reason, by=by,
        old_sha256=old_sha, new_sha256=meta.sha256,
    )
    return target


def reseal(
    archive: "str | Path",
    run_id: str,
    filename: str,
    *,
    by: Optional[str] = None,
) -> str:
    """Re-record an artifact's checksum from its current bytes -- the
    blessed fix for a `check` checksum_mismatch when the edit was
    intentional. Logs the reseal (old -> new sha256). Returns the new hash.
    Raises if there's no sidecar (use reconcile to create one first)."""
    archive_root, _ = resolve_archive(archive)
    session_dir = _find_session_dir(archive_root, run_id)
    target = session_dir / filename
    if not target.is_file():
        raise FileNotFoundError(f"no artifact {filename!r} in {run_id}")
    if not sidecar_path_for(target).exists():
        raise FileNotFoundError(
            f"{filename!r} has no sidecar; run 'nebula reconcile' to create one"
        )
    meta = read_sidecar(target)
    old_sha = meta.sha256
    new_sha = sha256_file(target)
    if new_sha == old_sha:
        return new_sha  # already sealed to the current bytes; nothing to do
    meta.sha256 = new_sha
    write_sidecar(target, meta)
    _record_history(
        session_dir, "reseal", file=filename, by=by or _default_user(),
        old_sha256=old_sha, new_sha256=new_sha,
    )
    return new_sha


def delete_session(
    archive: "str | Path",
    run_id: str,
    *,
    reason: Optional[str] = None,
    by: Optional[str] = None,
    force: bool = False,
) -> Path:
    """Soft-delete a whole session: record a tombstone in its history, then
    move the entire folder to an archive-level .trash/. Refuses if another
    session still references it (derived_from / related_runs) unless
    force=True. Returns the trashed folder path."""
    archive_root, _ = resolve_archive(archive)
    session_dir = _find_session_dir(archive_root, run_id)

    inbound = inbound_to_session(archive_root, run_id)
    if inbound and not force:
        raise RuntimeError(
            f"session {run_id!r} is still referenced by: {', '.join(inbound)}. "
            f"Repoint those first, or pass force=True to delete anyway."
        )

    by = by or _default_user()
    # Write the tombstone into the session's own history before it moves,
    # so the trashed folder carries the record of why it was removed.
    _record_history(
        session_dir, "delete-session", note=reason, by=by,
        broke_links=inbound or None,
    )
    archive_trash = archive_root / TRASH_DIRNAME
    archive_trash.mkdir(parents=True, exist_ok=True)
    dest = archive_trash / f"{run_id}-{_timestamp_slug()}"
    shutil.move(str(session_dir), str(dest))
    return dest


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
