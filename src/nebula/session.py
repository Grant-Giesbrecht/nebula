"""
Session lifecycle: creating, appending to, and closing session folders.

A session is a directory: <archive_root>/data/<year>/S-<yy>-<nnnn>/

The archive root holds only archive.yaml, index.db, code/ and data/, so it
stays browsable by hand. Under data/ there is one folder per year -- no
month nesting -- and the session id carries its own two-digit year, so a
folder name is self-describing wherever it ends up.

Ids are zero-padded and restart at 0001 each year, which is what lets the
CLI accept a bare number ("0012") and resolve it against the current year
(see resolve_run_id).

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

from nebula.annotations import ANNOTATIONS_FILE
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


def _resolve_caller(caller_frame_depth: Optional[int]) -> Optional[str]:
    """_caller_source_file with capture_provenance's depth contract, for
    callers that need the path itself and not just a ProducedBy.

    _caller_source_file's fixed-depth mode assumes exactly one frame sits
    between it and the caller doing the counting (historically
    capture_provenance). This function is that frame -- which is why it
    must stay a thin one-liner and must not be inlined into its callers:
    removing it would silently shift every depth by one.
    """
    return _caller_source_file(caller_frame_depth)


def capture_provenance(caller_frame_depth: Optional[int] = 2) -> ProducedBy:
    """Inspect the call stack to find the source file of whichever script
    called into nebula, then capture its repo/commit/dirty state.

    caller_frame_depth is tuned by callers of this function based on how
    many frames separate them from the actual user script; see Session
    methods below for usage. Pass None to auto-detect the caller instead
    of relying on a fixed frame offset.
    """
    return provenance_for(_caller_source_file(caller_frame_depth))


def provenance_for(caller_file: Optional[str]) -> ProducedBy:
    """capture_provenance for an already-resolved source file. Split out so
    a caller that also needs the path (to snapshot the code) resolves the
    stack once instead of twice."""
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

#: S-<yy>-<nnnn> (or I-<yy>-<nnnn> in an intake archive), e.g. S-26-0012.
#: The prefix, year and number are all captured, so a folder name alone
#: tells you which kind of archive minted it, when, and its number within
#: that year. An I- id is provisional by construction: merging replaces it
#: and records what it became, so an I- id found in a standard archive is a
#: merge that did not finish.
_ID_RE = re.compile(rf"^([SI])-(\d{{2}})-(\d{{{ID_WIDTH},}})$")

#: Every prefix an id may legitimately carry, for parsing.
ID_PREFIXES = ("S-", "I-")

#: Everything a session lives under, so the archive root stays readable.
DATA_DIR = "data"


def data_root(archive_root: Path) -> Path:
    return Path(archive_root) / DATA_DIR


def year_dir(archive_root: Path, year: int) -> Path:
    return data_root(archive_root) / f"{year:04d}"


def _format_id(year2: int, n: int, prefix: str = SESSION_PREFIX) -> str:
    return f"{prefix}{year2:02d}-{n:0{ID_WIDTH}d}"


def id_prefix(run_id: str) -> Optional[str]:
    """The prefix a session id carries ("S-" or "I-"), or None."""
    m = _ID_RE.match(run_id or "")
    return f"{m.group(1)}-" if m else None


def is_provisional(run_id: str) -> bool:
    """True for an intake id -- one that is expected to be replaced by a
    merge, and so must never be cited as though it were permanent."""
    return id_prefix(run_id) == "I-"


def id_year(run_id: str) -> Optional[int]:
    """The four-digit year encoded in a session id, or None if it doesn't
    parse. Two-digit years are read as 20xx -- this format is not intended
    to outlive that assumption."""
    m = _ID_RE.match(run_id)
    return 2000 + int(m.group(2)) if m else None


def resolve_run_id(text: str, *, now: Optional[datetime.datetime] = None,
                   prefix: str = SESSION_PREFIX) -> str:
    """Expand a user-typed session id to its canonical form.

        S-26-0012 -> S-26-0012      (already canonical)
        26-0012   -> S-26-0012      (missing prefix)
        0012 / 12 -> S-<this year>-0012

    Ids restart each year, so a bare number is only meaningful against a
    year; the current one is the useful default. `prefix` supplies the one
    to assume when the text doesn't carry it -- callers pass the archive's
    own, so a bare "12" typed at an intake archive means I-26-0012.

    Deliberately CLI-facing: the library keeps taking exact ids, the same
    way resolve_archive is strict for the API and lenient for the terminal.
    """
    raw = (text or "").strip().upper()
    if not raw:
        raise ValueError("empty session id")
    if _ID_RE.match(raw):
        return raw
    m = re.match(r"^([SI]-)?(\d{2})-(\d+)$", raw)
    if m:
        return _format_id(int(m.group(2)), int(m.group(3)), m.group(1) or prefix)
    if raw.isdigit():
        now = now or datetime.datetime.now().astimezone()
        return _format_id(now.year % 100, int(raw), prefix)
    raise ValueError(
        f"{text!r} is not a session id; expected S-<yy>-<nnnn> (e.g. S-26-0012), "
        f"I-<yy>-<nnnn> in an intake archive, or a bare number for the current "
        f"year (e.g. 0012)"
    )


def _existing_ids(archive_root: Path, year: int) -> List[int]:
    """Session numbers already used in one year. Numbering is per-year, so
    only that year's folder matters.

    Prefix-blind on purpose: an S- and an I- session may not share a number
    within a year. They should never coexist (an archive mints one kind),
    but if a merge left one behind, reusing its number would put two
    different sessions at one coordinate.
    """
    ids = []
    ydir = year_dir(archive_root, year)
    if not ydir.is_dir():
        return ids
    for session_dir in ydir.iterdir():
        m = _ID_RE.match(session_dir.name)
        if m and int(m.group(2)) == year % 100:
            ids.append(int(m.group(3)))
    return ids


def _allocate_new_id(archive_root: Path, year: int, prefix: str = SESSION_PREFIX):
    """Next free id for the given year. The folder listing is the source of
    truth -- no separate counter file to keep in sync. Collisions (e.g. two
    processes racing) are resolved by retrying with the next id if folder
    creation fails because the target already exists; see new()."""
    existing = _existing_ids(archive_root, year)
    return _format_id(year % 100, (max(existing) + 1) if existing else 1, prefix)


# ---------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------

def orphan_artifacts_in(session_dir: Path) -> List[Path]:
    """Artifact files in a session folder that have no sidecar. Excludes
    session.yaml, annotations.yaml, the sidecars themselves, hidden files
    (including temp files left by an interrupted atomic write), and
    subdirectories."""
    orphans = []
    for entry in sorted(Path(session_dir).iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        if name.startswith("."):
            continue
        if name in (SESSION_FILE, ANNOTATIONS_FILE) or name.endswith(SIDECAR_SUFFIX):
            continue
        if not sidecar_path_for(entry).exists():
            orphans.append(entry)
    return orphans


# ---------------------------------------------------------------------
# Overwrite protection
# ---------------------------------------------------------------------

#: Width of the automatic duplicate suffix: raw.csv -> raw-001.csv
DUPLICATE_WIDTH = 3


def duplicate_name(filename: str, n: int) -> str:
    """The nth duplicate of a filename, keeping the extension where a
    human expects it: raw.csv -> raw-001.csv, data.tar.gz -> data.tar-001.gz."""
    path = Path(filename)
    return f"{path.stem}-{n:0{DUPLICATE_WIDTH}d}{path.suffix}"


def resolve_write_target(session_dir: Path, filename: str, policy: str):
    """Where a write should actually land, given the archive's overwrite
    policy. Returns (path, original_name, duplicate_index) where the last
    two are None unless the file was renamed to avoid clobbering.

    Called when the path is handed to the caller, not at close: by the time
    a with-block exits the bytes are already on disk.
    """
    session_dir = Path(session_dir)
    target = session_dir / filename
    if not target.exists():
        return target, None, None

    if policy == "overwrite":
        return target, None, None
    if policy == "cancel":
        raise FileExistsError(
            f"{filename!r} already exists in {session_dir.name} and this archive's "
            f"on_overwrite policy is 'cancel'. Write under a different name, or "
            f"change the policy with 'nebula config <archive> --on-overwrite duplicate'."
        )

    for n in range(1, 1000):
        candidate = session_dir / duplicate_name(filename, n)
        if not candidate.exists():
            return candidate, filename, n
    raise RuntimeError(f"more than 999 duplicates of {filename!r} in {session_dir}")


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
        self._settings = None
        # What this session's writes actually landed as: requested name ->
        # name on disk. Only differs when overwrite protection renamed one.
        # See _redirect_ref for why a same-session reference has to follow.
        self._written: Dict[str, str] = {}

    @property
    def id(self) -> str:
        return self.meta.run_id

    @property
    def archive_root(self) -> Path:
        """The archive this session lives in. Sessions are always
        <root>/data/<year>/<id>, the same assumption _find_session_dir and
        the index walk already make."""
        return self.path.parents[2]

    def _attach_code(self, meta: SidecarMeta, caller_file: Optional[str]) -> None:
        """Snapshot the first-party source behind this save into the
        archive's code store and record its manifest id on the sidecar.

        Capture happens per artifact write rather than at close() so the
        sidecar is written once and never read-modify-written, and so it
        stays self-describing if the file is copied elsewhere. Repeated
        captures in one process are nearly free (see codestore's cache).

        Never fatal: losing a code snapshot must not cost the user their
        measurement data.
        """
        if not caller_file:
            return
        try:
            from nebula import codestore
            from nebula.config import read_settings

            settings = read_settings(self.archive_root)
            if not settings.capture_code:
                return
            got = codestore.capture(
                self.archive_root, caller_file,
                max_file_bytes=settings.code_max_file_bytes,
            )
            if got:
                meta.produced_by.code = got["code"]
                meta.produced_by.repos = got["repos"]
        except Exception:  # noqa: BLE001 -- provenance is best-effort
            pass

    def artifact_path(self, filename: str) -> Path:
        """The path a name maps to, verbatim. Deliberately *not*
        overwrite-aware: it is a pure helper, and callers use it to look
        files up as well as to write them. Overwrite protection lives in
        artifact(), the front door that hands out a path to write to."""
        return self.path / filename

    @property
    def settings(self):
        """This archive's settings, read once per session."""
        if self._settings is None:
            from nebula.config import read_settings

            self._settings = read_settings(self.archive_root)
        return self._settings

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
        caller_file = _resolve_caller(2)
        produced_by = provenance_for(caller_file)
        path, original_name, duplicate_index = resolve_write_target(
            self.path, filename, self.settings.on_overwrite)
        if original_name:
            self._note_write(original_name, path.name)
        return _ArtifactWriter(
            self,
            path,
            original_name=original_name,
            duplicate_index=duplicate_index,
            produced_by=produced_by,
            derived_from=derived_from,
            inputs=inputs or {},
            extra=extra,
            caller_file=caller_file,
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
        caller_file = _resolve_caller(caller_frame_depth)
        meta = SidecarMeta(
            created=_now_iso(),
            produced_by=provenance_for(caller_file),
            inputs=inputs or {},
            extra=extra,
        )
        for ref in derived_from or []:
            self._add_derived_from(meta, ref)
        self._attach_code(meta, caller_file)
        return write_sidecar(self.artifact_path(artifact_filename), meta)

    def _note_write(self, requested: str, actual: str) -> None:
        self._written[requested] = actual

    def _add_derived_from(self, meta: SidecarMeta, ref: "str | Ref") -> None:
        """Record one derived_from entry, pinning it first if it names an
        asset.

        An asset is mutable, so storing "derives from AF-26-0017" alone
        would name a file whose bytes are free to change afterwards --
        exactly the lineage-points-at-the-wrong-data failure that
        _redirect_ref exists to prevent, one level up. Pinning happens
        here, at write time, because that is the only moment the bytes the
        session actually saw are still identifiable.

        A failure to pin must not fail the artifact write: the measurement
        is the thing that cannot be recreated. Fall back to recording the
        plain ref, which check reports as unpinned.
        """
        from nebula.refs import Ref as _Ref, parse_ref

        parsed = ref if isinstance(ref, _Ref) else parse_ref(ref)
        if isinstance(parsed, _Ref) and parsed.asset and parsed.archive is None:
            from nebula import assets

            try:
                meta.derived_from.append(
                    assets.reference(self.archive_root, parsed.asset))
                return
            except Exception:
                pass
        meta.add_derived_from(self._redirect_ref(ref))

    def _redirect_ref(self, ref: "str | Ref") -> "str | Ref":
        """Point a same-session reference at the file this session actually
        wrote under that name.

        Overwrite protection can rename a write (raw.csv -> raw-001.csv)
        after the caller has already decided what to call it. A later
        `derived_from=["raw.csv"]` in the same session then names a file
        that *does* exist -- the one from an earlier run -- so nothing
        errors and the lineage quietly points at the wrong data. Within one
        session the intent is unambiguous: it means the file just written.

        Only same-archive, same-session, bare references are redirected,
        and only for names this session actually renamed; anything naming
        another session or archive is left exactly as written.
        """
        from nebula.refs import Ref as _Ref, parse_ref

        if not self._written:
            return ref
        parsed = parse_ref(ref) if isinstance(ref, str) else ref
        if not isinstance(parsed, _Ref):
            return ref
        if parsed.archive is not None or parsed.session is not None:
            return ref
        actual = self._written.get(parsed.file)
        if not actual or actual == parsed.file:
            return ref
        return _Ref(archive=None, session=None, file=actual, user=parsed.user)

    def add_related_run(self, ref: "str | Ref") -> None:
        self.meta.add_related_run(ref)
        self._save_meta()

    def _save_meta(self) -> None:
        write_session_yaml(self.path, self.meta)

    def find_orphan_artifacts(self) -> List[Path]:
        """Return artifact files in this session folder that have no
        sidecar. See the module-level orphan_artifacts_in()."""
        return orphan_artifacts_in(self.path)

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
        self._note_in_index()

    def mark_crashed(self) -> None:
        self.meta.status = "crashed"
        self._save_meta()
        self._note_in_index()

    def _note_in_index(self) -> None:
        """Re-index just this session, so a reader finds nothing to do.

        Purely an optimisation: index.ensure_fresh() would spot the same
        change on the next read from the session directory's signature.
        That makes it safe to be entirely best-effort -- by now the data
        and its sidecars are on disk, and no cache update is worth turning
        a finished measurement into a traceback.
        """
        try:
            if not self.settings.auto_index:
                return
            from nebula import index

            index.update_session(self.archive_root, self.path)
        except Exception:       # noqa: BLE001 -- the cache is never worth raising for
            pass

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
        caller_file: Optional[str] = None,
        original_name: Optional[str] = None,
        duplicate_index: Optional[int] = None,
    ):
        self._session = session
        self.path = path
        self._original_name = original_name
        self._duplicate_index = duplicate_index
        self._produced_by = produced_by
        self._caller_file = caller_file
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
            original_name=self._original_name,
            duplicate_index=self._duplicate_index,
            extra=self._extra,
        )
        for ref in self._derived_from:
            self._session._add_derived_from(meta, ref)
        self._session._attach_code(meta, self._caller_file)
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
    including the data/year/id path. See registry.resolve_archive() for
    the exact resolution rule.

    archive_name overrides the label recorded on the returned Session
    (e.g. to give an unregistered/ad hoc Path a friendly name); normally
    you don't need this -- a registered name resolves its own label.
    """
    archive_root, resolved_name = resolve_archive(archive)
    name = archive_name or resolved_name or "local"

    from nebula.config import read_settings

    settings = read_settings(archive_root, apply_env=False)
    _refuse_if_unwritable(archive_root, settings)

    now = datetime.datetime.now().astimezone()
    ydir = year_dir(archive_root, now.year)
    ydir.mkdir(parents=True, exist_ok=True)

    with _lock:
        for _ in range(10):  # small retry budget for cross-process races
            run_id = _allocate_new_id(archive_root, now.year, settings.prefix)
            session_dir = ydir / run_id
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


class ArchiveNotWritable(PermissionError):
    """A write aimed at an archive that is not supposed to receive one."""


def _refuse_if_unwritable(archive_root, settings) -> None:
    """Guard the two archive kinds that must not be written to.

    A fragment is someone else's excerpt: adding a session to it would
    produce an archive that is neither theirs nor yours, under ids only one
    of you owns. A merged intake is worse -- data written after a merge
    looks merged, so it can be pruned away having never been copied
    anywhere. Both are recoverable by an explicit unlock, never by accident.
    """
    if settings.kind == "fragment":
        raise ArchiveNotWritable(
            f"{archive_root} is a fragment (an excerpt of another archive) and "
            f"cannot be written to. Adopt what you need into your own archive "
            f"with 'nebula adopt', or write into a standard archive.")
    if settings.locked:
        raise ArchiveNotWritable(
            f"{archive_root} was merged into {settings.merged_to or 'another archive'} "
            f"on {settings.merged_at} and is locked, so nothing written now could "
            f"be mistaken for data that has already been merged. "
            f"Run 'nebula unlock {archive_root}' if you really mean to keep using it.")


def _find_session_dir(archive_root: Path, run_id: str) -> Path:
    """Locate a session folder. The id encodes its year, so this is a
    direct path join rather than a walk -- the scan below is only a
    fallback for a folder someone filed under the wrong year by hand."""
    archive_root = Path(archive_root)
    year = id_year(run_id)
    if year is not None:
        candidate = year_dir(archive_root, year) / run_id
        if candidate.is_dir() and (candidate / SESSION_FILE).exists():
            return candidate

    droot = data_root(archive_root)
    for ydir in sorted(droot.iterdir()) if droot.is_dir() else []:
        if not ydir.is_dir() or not ydir.name.isdigit():
            continue
        candidate = ydir / run_id
        if candidate.is_dir() and (candidate / SESSION_FILE).exists():
            return candidate
    raise FileNotFoundError(f"no session {run_id!r} found under {droot}")


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
