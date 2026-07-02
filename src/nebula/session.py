"""
Session lifecycle: creating, appending to, and closing S-XXXX folders.

A session is a directory: <store_root>/<year>/<month>/S-XXXX/
The numeric id is a bare, zero-padded, monotonically increasing decimal
counter, global to the store (not per-day). The folder's location in the
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


def _existing_ids(store_root: Path) -> List[int]:
    ids = []
    if not store_root.exists():
        return ids
    for year_dir in store_root.iterdir():
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


def _allocate_new_id(store_root: Path) -> str:
    """Scan the store for the highest existing id and return the next one.
    The folder listing is the source of truth -- no separate counter file
    to keep in sync. Collisions (e.g. two processes racing) are resolved
    by retrying with the next id if folder creation fails because the
    target already exists; see Session.new()."""
    existing = _existing_ids(store_root)
    return _format_id((max(existing) + 1) if existing else 1)


# ---------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------

class Session:
    """A handle to an open (or reopened) session folder.

    Not usually constructed directly -- use new(), append_to(), reopen(),
    or the session() convenience context manager instead.
    """

    def __init__(self, path: Path, meta: SessionMeta, store: Optional[str] = None):
        self.path = Path(path)
        self.meta = meta
        self.store = store
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
    store_root: Path,
    *,
    tags: Optional[List[str]] = None,
    description: str = "",
    store: Optional[str] = None,
) -> Session:
    """Create a brand-new session folder under store_root and return an
    open Session. store_root is the top-level directory for this store
    (e.g. /nas/nist-data), not including the year/month/id path."""
    store_root = Path(store_root)
    now = datetime.datetime.now().astimezone()
    year_month_dir = store_root / f"{now.year:04d}" / f"{now.month:02d}"
    year_month_dir.mkdir(parents=True, exist_ok=True)

    with _lock:
        for _ in range(10):  # small retry budget for cross-process races
            run_id = _allocate_new_id(store_root)
            session_dir = year_month_dir / run_id
            try:
                session_dir.mkdir(parents=False, exist_ok=False)
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError(
                "could not allocate a unique session id after 10 attempts; "
                "check for a stale/broken folder in the store"
            )

    meta = SessionMeta(
        run_id=run_id,
        created=_now_iso(),
        status="open",
        tags=tags or [],
        description=description,
    )
    write_session_yaml(session_dir, meta)
    return Session(session_dir, meta, store=store)


def _find_session_dir(store_root: Path, run_id: str) -> Path:
    store_root = Path(store_root)
    for year_dir in store_root.iterdir() if store_root.exists() else []:
        if not year_dir.is_dir():
            continue
        for month_dir in year_dir.iterdir():
            candidate = month_dir / run_id
            if candidate.is_dir() and (candidate / "session.yaml").exists():
                return candidate
    raise FileNotFoundError(f"no session {run_id!r} found under {store_root}")


def append_to(store_root: Path, run_id: str, *, store: Optional[str] = None) -> Session:
    """Reattach to an existing OPEN session to write more artifacts into
    it. Raises if the session is closed -- closed sessions are immutable
    by policy; use related_runs / derived_from to link new work to old
    sessions instead of reopening them."""
    session_dir = _find_session_dir(store_root, run_id)
    meta = read_session_yaml(session_dir)
    if meta.status != "open":
        raise RuntimeError(
            f"session {run_id!r} is {meta.status!r}, not open. "
            f"Closed sessions are immutable by policy -- create a new "
            f"session and reference this one via related_runs instead."
        )
    return Session(session_dir, meta, store=store)


def reopen(store_root: Path, run_id: str, *, store: Optional[str] = None) -> Session:
    """Explicitly reopen a session regardless of its current status (e.g.
    a crashed session resuming from a checkpoint after a machine reboot).
    Distinct from append_to() so that 'accidentally reopening a closed
    session' requires deliberate intent, not just a typo'd status check.
    """
    session_dir = _find_session_dir(store_root, run_id)
    meta = read_session_yaml(session_dir)
    meta.status = "open"
    write_session_yaml(session_dir, meta)
    return Session(session_dir, meta, store=store)


@contextlib.contextmanager
def session(
    store_root: Path,
    *,
    run_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
    description: str = "",
    store: Optional[str] = None,
):
    """Convenience context manager: creates a new session if run_id is
    None, otherwise appends to the given (open) session. Closes/marks
    crashed automatically on exit.

        with nebula.session(ROOT, tags=["RP23D"], description="...") as s:
            ...
            s.write_meta_for("raw.graf", derived_from=["scope_trace.csv"])
    """
    if run_id is None:
        s = new(store_root, tags=tags, description=description, store=store)
    else:
        s = append_to(store_root, run_id, store=store)
    try:
        yield s
    except BaseException:
        s.mark_crashed()
        raise
    else:
        s.close()
