"""
Session lifecycle: creating, appending to, and closing S-XXXX folders.

A session is a directory: <archive_root>/<year>/<month>/S-XXXX/
The numeric id is a bare, zero-padded, monotonically increasing decimal
counter, global to the archive (not per-day). The folder's location in the
year/month hierarchy records its creation date; the id itself carries no
date information, so it stays short in derived_from/related_runs refs.

Provenance (git repo/commit/dirty flag/entry point) is captured
automatically at artifact-write time by walking up from the caller's
source file to find a .git directory -- callers don't need to pass this
in manually.
"""

from __future__ import annotations

import contextlib
import datetime
import inspect
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Optional

from nebula.refs import Ref, format_ref, parse_ref, SESSION_PREFIX
from nebula.registry import Registry, resolve_archive
from nebula.sidecar import (
    ProducedBy,
    SessionMeta,
    SidecarMeta,
    read_session_yaml,
    write_session_yaml,
    write_sidecar,
)

ID_WIDTH = 4  # S-0001 .. S-9999 before needing a width bump

_lock = threading.Lock()  # guards folder creation / id allocation on this process


# ---------------------------------------------------------------------
# Git provenance capture
# ---------------------------------------------------------------------

def _find_git_root(start: Path) -> Optional[Path]:
    cur = start.resolve()
    if cur.is_file():
        cur = cur.parent
    for parent in [cur, *cur.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _git(args: List[str], cwd: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def capture_provenance(caller_frame_depth: int = 2) -> ProducedBy:
    """Inspect the call stack to find the source file of whichever script
    called into nebula, then capture its repo/commit/dirty state.

    caller_frame_depth is tuned by callers of this function based on how
    many frames separate them from the actual user script; see Session
    methods below for usage.
    """
    stack = inspect.stack()
    caller_file = None
    if len(stack) > caller_frame_depth:
        caller_file = stack[caller_frame_depth].filename
    if not caller_file or not os.path.exists(caller_file):
        return ProducedBy()

    git_root = _find_git_root(Path(caller_file))
    if git_root is None:
        return ProducedBy(entry_point=caller_file)

    commit = _git(["rev-parse", "HEAD"], cwd=git_root)
    dirty_output = _git(["status", "--porcelain"], cwd=git_root)
    repo_name = git_root.name

    rel_entry = os.path.relpath(caller_file, git_root)
    return ProducedBy(
        repo=repo_name,
        commit=commit,
        dirty=(bool(dirty_output) if dirty_output is not None else None),
        entry_point=rel_entry,
    )


# ---------------------------------------------------------------------
# Session id allocation
# ---------------------------------------------------------------------

_ID_RE = re.compile(rf"^{re.escape(SESSION_PREFIX)}(\d{{{ID_WIDTH},}})$")


def _format_id(n: int) -> str:
    return f"{SESSION_PREFIX}{n:0{ID_WIDTH}d}"


def _existing_ids(archive_root: Path) -> List[int]:
    ids = []
    if not archive_root.exists():
        return ids
    for year_dir in archive_root.iterdir():
        if not year_dir.is_dir():
            continue
        for month_dir in year_dir.iterdir():
            if not month_dir.is_dir():
                continue
            for session_dir in month_dir.iterdir():
                m = _ID_RE.match(session_dir.name)
                if m:
                    ids.append(int(m.group(1)))
    return ids


def _allocate_new_id(archive_root: Path) -> str:
    """Scan the archive for the highest existing id and return the next one.
    The folder listing is the source of truth -- no separate counter file
    to keep in sync. Collisions (e.g. two processes racing) are resolved
    by retrying with the next id if folder creation fails because the
    target already exists; see Session.new()."""
    existing = _existing_ids(archive_root)
    return _format_id((max(existing) + 1) if existing else 1)


# ---------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------

class Session:
    """A handle to an open (or reopened) session folder.

    Not usually constructed directly -- use new(), append_to(), reopen(),
    or the session() convenience context manager instead.
    """

    def __init__(self, path: Path, meta: SessionMeta, archive: Optional[str] = None):
        self.path = Path(path)
        self.meta = meta
        self.archive = archive
        self._closed_cleanly = False

    @property
    def id(self) -> str:
        return self.meta.run_id

    def artifact_path(self, filename: str) -> Path:
        return self.path / filename

    def write_meta_for(
        self,
        artifact_filename: str,
        *,
        derived_from: Optional[List["str | Ref"]] = None,
        inputs: Optional[Dict] = None,
        caller_frame_depth: int = 2,
        **extra,
    ) -> Path:
        """Write the sidecar for one artifact this session just produced.

        derived_from accepts compact ref strings or Ref objects; bare
        filenames ("scope_trace_raw.csv") are resolved as same-session
        refs automatically by parse_ref.
        """
        produced_by = capture_provenance(caller_frame_depth=caller_frame_depth)
        meta = SidecarMeta(
            created=_now_iso(),
            produced_by=produced_by,
            inputs=inputs or {},
            extra=extra,
        )
        for ref in derived_from or []:
            meta.add_derived_from(ref)
        return write_sidecar(self.artifact_path(artifact_filename), meta)

    def add_related_run(self, ref: "str | Ref") -> None:
        self.meta.add_related_run(ref)
        self._save_meta()

    def _save_meta(self) -> None:
        write_session_yaml(self.path, self.meta)

    def close(self) -> None:
        self.meta.status = "closed"
        self._save_meta()
        self._closed_cleanly = True

    def mark_crashed(self) -> None:
        self.meta.status = "crashed"
        self._save_meta()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.mark_crashed()
        else:
            self.close()
        # Don't suppress exceptions.
        return None


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def new(
    archive: "str | Path",
    *,
    tags: Optional[List[str]] = None,
    description: str = "",
    archive_name: Optional[str] = None,
) -> Session:
    """Create a brand-new session folder and return an open Session.

    `archive` is either a registered archive name (str) -- looked up in
    ~/.nebula/archives.yaml -- or a literal filesystem root (Path), not
    including the year/month/id path. See registry.resolve_archive() for
    the exact resolution rule.

    archive_name overrides the label recorded on the returned Session
    (e.g. to give an unregistered/ad hoc Path a friendly name); normally
    you don't need this -- a registered name resolves its own label.
    """
    archive_root, resolved_name = resolve_archive(archive)
    name = archive_name or resolved_name or "local"

    now = datetime.datetime.now().astimezone()
    year_month_dir = archive_root / f"{now.year:04d}" / f"{now.month:02d}"
    year_month_dir.mkdir(parents=True, exist_ok=True)

    with _lock:
        for _ in range(10):  # small retry budget for cross-process races
            run_id = _allocate_new_id(archive_root)
            session_dir = year_month_dir / run_id
            try:
                session_dir.mkdir(parents=False, exist_ok=False)
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError(
                "could not allocate a unique session id after 10 attempts; "
                "check for a stale/broken folder in the archive"
            )

    meta = SessionMeta(
        run_id=run_id,
        created=_now_iso(),
        status="open",
        tags=tags or [],
        description=description,
    )
    write_session_yaml(session_dir, meta)
    return Session(session_dir, meta, archive=name)


def _find_session_dir(archive_root: Path, run_id: str) -> Path:
    archive_root = Path(archive_root)
    for year_dir in archive_root.iterdir() if archive_root.exists() else []:
        if not year_dir.is_dir():
            continue
        for month_dir in year_dir.iterdir():
            candidate = month_dir / run_id
            if candidate.is_dir() and (candidate / "session.yaml").exists():
                return candidate
    raise FileNotFoundError(f"no session {run_id!r} found under {archive_root}")


def append_to(
    archive: "str | Path", run_id: str, *, archive_name: Optional[str] = None
) -> Session:
    """Reattach to an existing OPEN session to write more artifacts into
    it. Raises if the session is closed -- closed sessions are immutable
    by policy; use related_runs / derived_from to link new work to old
    sessions instead of reopening them.

    `archive` follows the same str-name-vs-Path-literal resolution as
    new()."""
    archive_root, resolved_name = resolve_archive(archive)
    name = archive_name or resolved_name or "local"
    session_dir = _find_session_dir(archive_root, run_id)
    meta = read_session_yaml(session_dir)
    if meta.status != "open":
        raise RuntimeError(
            f"session {run_id!r} is {meta.status!r}, not open. "
            f"Closed sessions are immutable by policy -- create a new "
            f"session and reference this one via related_runs instead."
        )
    return Session(session_dir, meta, archive=name)


def reopen(
    archive: "str | Path", run_id: str, *, archive_name: Optional[str] = None
) -> Session:
    """Explicitly reopen a session regardless of its current status (e.g.
    a crashed session resuming from a checkpoint after a machine reboot).
    Distinct from append_to() so that 'accidentally reopening a closed
    session' requires deliberate intent, not just a typo'd status check.
    """
    archive_root, resolved_name = resolve_archive(archive)
    name = archive_name or resolved_name or "local"
    session_dir = _find_session_dir(archive_root, run_id)
    meta = read_session_yaml(session_dir)
    meta.status = "open"
    write_session_yaml(session_dir, meta)
    return Session(session_dir, meta, archive=name)


@contextlib.contextmanager
def session(
    archive: "str | Path",
    *,
    run_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
    description: str = "",
    archive_name: Optional[str] = None,
):
    """Convenience context manager: creates a new session if run_id is
    None, otherwise appends to the given (open) session. Closes/marks
    crashed automatically on exit.

        with nebula.session("postdoc", tags=["RP23D"], description="...") as s:
            ...
            s.write_meta_for("raw.graf", derived_from=["scope_trace.csv"])

    `archive` may be a registered archive name (str) or a literal Path.
    """
    if run_id is None:
        s = new(archive, tags=tags, description=description, archive_name=archive_name)
    else:
        s = append_to(archive, run_id, archive_name=archive_name)
    try:
        yield s
    except BaseException:
        s.mark_crashed()
        raise
    else:
        s.close()
