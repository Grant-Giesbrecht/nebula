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
import warnings
from pathlib import Path
from typing import Dict, List, Optional

from nebula.refs import Ref, format_ref, parse_ref, SESSION_PREFIX
from nebula.registry import Registry, resolve_archive
from nebula.sidecar import (
    ProducedBy,
    SessionMeta,
    SidecarMeta,
    SESSION_FILE,
    SIDECAR_SUFFIX,
    read_session_yaml,
    sidecar_path_for,
    write_session_yaml,
    write_sidecar,
)

ID_WIDTH = 4  # S-0001 .. S-9999 before needing a width bump

_lock = threading.Lock()  # guards folder creation / id allocation on this process

# Absolute path to nebula's own source directory, used to skip nebula's
# internal frames when auto-detecting which user script is the caller.
_NEBULA_DIR = os.path.dirname(os.path.abspath(__file__))

# Policies for what close() does about artifacts left without a sidecar.
#   "stub+warn" -- auto-write a provenance stub AND warn (default): nothing
#                  is ever left un-tracked, but you still hear about it so
#                  the missing inputs/derived_from don't slip by unnoticed.
#   "stub"      -- auto-write a provenance stub, silently.
#   "warn"      -- warn only; the orphan stays an orphan.
#   "raise"     -- fail the close() loudly.
_MISSING_META_POLICIES = ("stub+warn", "stub", "warn", "raise")
_DEFAULT_MISSING_META = "stub+warn"


class MissingMetadataError(RuntimeError):
    """Raised at close() when on_missing_meta='raise' and one or more
    artifacts in the session folder have no sidecar."""


class MissingMetadataWarning(UserWarning):
    """Emitted at close() when on_missing_meta includes 'warn' and one or
    more artifacts in the session folder have no sidecar."""


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


def _caller_source_file(caller_frame_depth: Optional[int]) -> Optional[str]:
    """Locate the user script that called into nebula.

    If caller_frame_depth is an int, use that fixed offset into the stack
    (the historical behaviour, cheap and predictable for callers a known
    number of frames from the user script). If it is None, auto-detect by
    walking outward to the first frame whose file lives outside nebula's
    own source directory -- robust when the number of intervening frames
    isn't fixed (e.g. the stub path invoked from close()).
    """
    stack = inspect.stack()
    if caller_frame_depth is not None:
        # +1 to skip this helper's own frame, so caller_frame_depth stays
        # measured relative to capture_provenance (our caller), preserving
        # the historical fixed-depth contract.
        idx = caller_frame_depth + 1
        if len(stack) > idx:
            return stack[idx].filename
        return None
    for frame in stack[1:]:  # skip _caller_source_file itself
        fn = frame.filename
        if not fn or fn.startswith("<"):  # <string>, <frozen ...>, etc.
            continue
        if os.path.dirname(os.path.abspath(fn)) == _NEBULA_DIR:
            continue
        return fn
    return None


def capture_provenance(caller_frame_depth: Optional[int] = 2) -> ProducedBy:
    """Inspect the call stack to find the source file of whichever script
    called into nebula, then capture its repo/commit/dirty state.

    caller_frame_depth is tuned by callers of this function based on how
    many frames separate them from the actual user script; see Session
    methods below for usage. Pass None to auto-detect the caller instead
    of relying on a fixed frame offset.
    """
    caller_file = _caller_source_file(caller_frame_depth)
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

    def __init__(
        self,
        path: Path,
        meta: SessionMeta,
        archive: Optional[str] = None,
        on_missing_meta: str = _DEFAULT_MISSING_META,
    ):
        self.path = Path(path)
        self.meta = meta
        self.archive = archive
        if on_missing_meta not in _MISSING_META_POLICIES:
            raise ValueError(
                f"on_missing_meta must be one of {_MISSING_META_POLICIES!r}, "
                f"got {on_missing_meta!r}"
            )
        self.on_missing_meta = on_missing_meta
        self._closed_cleanly = False

    @property
    def id(self) -> str:
        return self.meta.run_id

    def artifact_path(self, filename: str) -> Path:
        return self.path / filename

    def artifact(
        self,
        filename: str,
        *,
        derived_from: Optional[List["str | Ref"]] = None,
        inputs: Optional[Dict] = None,
        **extra,
    ) -> "_ArtifactWriter":
        """Context manager that pairs writing an artifact with writing its
        sidecar, so the two can't drift apart:

            with s.artifact("raw.tome", inputs={"gain": 10}) as fn:
                dict_to_tome(data, fn)
            # sidecar written automatically on block exit

        Yields the path to write to. On clean exit it captures provenance
        and writes the sidecar; if the file was never actually created it
        raises, surfacing a silently-failed write instead of leaving an
        un-tracked hole. On an exception it writes nothing and does not
        suppress the error.

        This is the preferred front door; artifact_path() +
        write_meta_for() remain as a lower-level escape hatch (and the
        close() audit still covers anything written that way).
        """
        # Capture provenance now, while the user script is the direct
        # caller (fixed depth 2), rather than at block-exit time where the
        # frame layout is murkier.
        produced_by = capture_provenance(caller_frame_depth=2)
        return _ArtifactWriter(
            self,
            self.artifact_path(filename),
            produced_by=produced_by,
            derived_from=derived_from,
            inputs=inputs or {},
            extra=extra,
        )

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

    def find_orphan_artifacts(self) -> List[Path]:
        """Return artifact files in the session folder that have no
        sidecar. Excludes session.yaml, the sidecars themselves, hidden
        files (including the temp files left by an interrupted atomic
        write), and subdirectories."""
        orphans = []
        for entry in sorted(self.path.iterdir()):
            if not entry.is_file():
                continue
            name = entry.name
            if name.startswith("."):
                continue
            if name == SESSION_FILE or name.endswith(SIDECAR_SUFFIX):
                continue
            if not sidecar_path_for(entry).exists():
                orphans.append(entry)
        return orphans

    def _reconcile_missing_meta(self) -> None:
        """Apply the on_missing_meta policy to any artifacts left without a
        sidecar. Called on clean close only -- a crashed session's orphans
        are honest and shouldn't be papered over."""
        orphans = self.find_orphan_artifacts()
        if not orphans:
            return

        names = ", ".join(o.name for o in orphans)
        policy = self.on_missing_meta

        if policy == "raise":
            raise MissingMetadataError(
                f"session {self.id} has artifacts with no sidecar: {names}. "
                f"Write metadata for them (s.artifact(...) or "
                f"s.write_meta_for(...)), or open the session with "
                f"on_missing_meta='stub' to auto-record provenance."
            )

        # "warn" and "stub+warn" both surface the orphans; only the stub
        # variants also write the recovery sidecar.
        if "warn" in policy:
            warnings.warn(
                f"session {self.id} has artifacts with no sidecar: {names}",
                MissingMetadataWarning,
                stacklevel=2,
            )
        if "stub" in policy:
            # Write a provenance-only sidecar so nothing is left un-tracked.
            # The rich inputs/derived_from are still missing, but a stub is
            # recoverable (edit it later) where an orphan is invisible.
            produced_by = capture_provenance(caller_frame_depth=None)
            for orphan in orphans:
                meta = SidecarMeta(created=_now_iso(), produced_by=produced_by)
                meta.extra["auto_stub"] = True
                write_sidecar(orphan, meta)

    def close(self) -> None:
        self._reconcile_missing_meta()
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


class _ArtifactWriter:
    """Context manager returned by Session.artifact(). Yields the artifact
    path on enter; on clean exit verifies the file exists and writes its
    sidecar. Not constructed directly."""

    def __init__(
        self,
        session: "Session",
        path: Path,
        *,
        produced_by: ProducedBy,
        derived_from: Optional[List["str | Ref"]],
        inputs: Dict,
        extra: Dict,
    ):
        self._session = session
        self.path = path
        self._produced_by = produced_by
        self._derived_from = derived_from or []
        self._inputs = inputs
        self._extra = extra

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            # The write failed; leave no sidecar and don't suppress.
            return None
        if not self.path.exists():
            raise FileNotFoundError(
                f"s.artifact({self.path.name!r}) block finished but no file "
                f"was written to {self.path}; nothing to record metadata for"
            )
        meta = SidecarMeta(
            created=_now_iso(),
            produced_by=self._produced_by,
            inputs=self._inputs,
            extra=self._extra,
        )
        for ref in self._derived_from:
            meta.add_derived_from(ref)
        write_sidecar(self.path, meta)
        return None


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def new(
    archive: "str | Path",
    *,
    tags: Optional[List[str]] = None,
    description: str = "",
    archive_name: Optional[str] = None,
    on_missing_meta: str = _DEFAULT_MISSING_META,
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
    return Session(session_dir, meta, archive=name, on_missing_meta=on_missing_meta)


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


def _created_today(meta: SessionMeta) -> bool:
    """True if the session's recorded start date is today. A session
    guarantees *when it was started*, not that only one script ever wrote
    to it -- so same-day work can keep flowing into it."""
    today = datetime.date.today().isoformat()
    return (meta.created or "")[:10] == today


# ---------------------------------------------------------------------
# Holds: keeping a session appendable past its creation day
# ---------------------------------------------------------------------

HOLD_FOREVER = "forever"  # sentinel stored in hold_until for indefinite holds

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> float:
    """Parse a hold duration like '2h', '90m', '45s', '1.5d', or a bare
    number of seconds, into seconds. Raises ValueError on anything else."""
    text = text.strip().lower()
    if not text:
        raise ValueError("empty duration")
    if text[-1] in _DURATION_UNITS:
        return float(text[:-1]) * _DURATION_UNITS[text[-1]]
    return float(text)  # bare number = seconds


def _hold_value_active(hold_until: Optional[str], *, now: Optional[datetime.datetime] = None) -> bool:
    """True if a hold_until value represents a currently-active hold. Works
    off the raw stored value, so callers with an index row (not a full
    SessionMeta) can use it too."""
    if not hold_until:
        return False
    if hold_until == HOLD_FOREVER:
        return True
    now = now or datetime.datetime.now().astimezone()
    try:
        return now < datetime.datetime.fromisoformat(hold_until)
    except ValueError:
        return False  # a garbled timestamp is treated as no hold


def _hold_active(meta: SessionMeta, *, now: Optional[datetime.datetime] = None) -> bool:
    """True if the session currently has an unexpired hold."""
    return _hold_value_active(meta.hold_until, now=now)


def hold(
    archive: "str | Path",
    run_id: str,
    *,
    seconds: Optional[float] = None,
) -> str:
    """Place a hold on a session so it stays appendable across day
    boundaries (e.g. a run of related measurements spanning midnight),
    even after a script closes it. Pass seconds=None for an indefinite
    hold, or a number of seconds for a timed one. Returns the stored
    hold_until value ("forever" or an ISO timestamp).

    Release it with release(); a hold does not otherwise change the
    session's open/closed status."""
    archive_root, _ = resolve_archive(archive)
    session_dir = _find_session_dir(archive_root, run_id)
    meta = read_session_yaml(session_dir)
    if seconds is None:
        meta.hold_until = HOLD_FOREVER
    else:
        until = datetime.datetime.now().astimezone() + datetime.timedelta(seconds=seconds)
        meta.hold_until = until.isoformat(timespec="seconds")
    write_session_yaml(session_dir, meta)
    return meta.hold_until


def release(archive: "str | Path", run_id: str) -> bool:
    """Clear any hold on a session. Returns True if there had been one.
    Safe to call when no hold is set."""
    archive_root, _ = resolve_archive(archive)
    session_dir = _find_session_dir(archive_root, run_id)
    meta = read_session_yaml(session_dir)
    had_hold = meta.hold_until is not None
    meta.hold_until = None
    write_session_yaml(session_dir, meta)
    return had_hold


def append_to(
    archive: "str | Path",
    run_id: str,
    *,
    archive_name: Optional[str] = None,
    on_missing_meta: str = _DEFAULT_MISSING_META,
) -> Session:
    """Reattach to a session to write more artifacts into it, so several
    related measurements can share one folder.

    A session's guarantee is the date it was *started*, not single-writer
    exclusivity. So appending is allowed when the session is still OPEN,
    when it was CREATED TODAY (even if a previous script already closed
    it -- reopening same-day work is free, and the status flips back to
    open), or when it has an active HOLD (see hold(), for work spanning
    midnight). A session CLOSED ON A PREVIOUS DAY with no hold is frozen:
    this raises, and you must reopen() it deliberately (the picker's
    /reopen --force).

    `archive` follows the same str-name-vs-Path-literal resolution as
    new()."""
    archive_root, resolved_name = resolve_archive(archive)
    name = archive_name or resolved_name or "local"
    session_dir = _find_session_dir(archive_root, run_id)
    meta = read_session_yaml(session_dir)
    if meta.status != "open" and not _created_today(meta) and not _hold_active(meta):
        raise RuntimeError(
            f"session {run_id!r} is {meta.status!r} and was started on "
            f"{(meta.created or '?')[:10]}, not today. Sessions closed on a "
            f"previous day are frozen -- put a hold on it (nebula hold "
            f"{run_id}) before midnight, reopen() it explicitly, or use the "
            f"picker's /reopen {run_id} --force."
        )
    if meta.status != "open":
        # Same-day resume: the session is active again, so record that
        # honestly rather than leaving a folder that claims to be done.
        meta.status = "open"
        write_session_yaml(session_dir, meta)
    return Session(session_dir, meta, archive=name, on_missing_meta=on_missing_meta)


def reopen(
    archive: "str | Path",
    run_id: str,
    *,
    archive_name: Optional[str] = None,
    on_missing_meta: str = _DEFAULT_MISSING_META,
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
    return Session(session_dir, meta, archive=name, on_missing_meta=on_missing_meta)


@contextlib.contextmanager
def session(
    archive: "str | Path",
    *,
    run_id: Optional[str] = None,
    new_session: bool = False,
    tags: Optional[List[str]] = None,
    description: str = "",
    archive_name: Optional[str] = None,
    on_missing_meta: str = _DEFAULT_MISSING_META,
):
    """Convenience context manager. Closes/marks crashed automatically on
    exit.

        with nebula.session("postdoc", tags=["RP23D"], description="...") as s:
            ...
            s.write_meta_for("raw.graf", derived_from=["scope_trace.csv"])

    Which session it opens:
      - run_id given         -> append to that (open) session;
      - new_session=True     -> create a fresh session, no questions asked;
      - otherwise            -> present the interactive CLI session picker
                                (nebula.session_select.select_session), so
                                the user can append to a session in progress
                                instead of accidentally spraying data across
                                many one-shot sessions. In a non-interactive
                                context the picker just makes a new session.

    Pass new_session=True in unattended scripts that should always start
    clean without prompting.

    `archive` may be a registered archive name (str) or a literal Path.
    """
    if run_id is not None:
        s = append_to(
            archive,
            run_id,
            archive_name=archive_name,
            on_missing_meta=on_missing_meta,
        )
    elif new_session:
        s = new(
            archive,
            tags=tags,
            description=description,
            archive_name=archive_name,
            on_missing_meta=on_missing_meta,
        )
    else:
        # Imported lazily: select_session imports back from this module, and
        # it pulls in the terminal-UI helpers that batch code needn't load.
        from nebula.session_select import select_session

        s = select_session(
            archive,
            tags=tags,
            description=description,
            archive_name=archive_name,
            on_missing_meta=on_missing_meta,
        )
    try:
        yield s
    except BaseException:
        s.mark_crashed()
        raise
    else:
        s.close()
