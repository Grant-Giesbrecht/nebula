"""
Nebula Navigator -- data model (GUI-toolkit-independent).

This layer turns an archive into the plain data the Navigator renders: a
list of sessions, and for each session a list of *items*, where one item is
a logical artefact = the (data file, sidecar) pair keyed by artefact name.
Its whole job is to classify each pair's status so the view can draw the
right box:

    paired   -- data file + sidecar both present
    orphan   -- data file present, sidecar MISSING
    stray    -- sidecar present, data file MISSING
    drifted  -- both present but the file no longer matches its sha256

Nothing here imports a GUI toolkit, so it lifts cleanly into the standalone
Navigator repo and stays unit-testable without a display.
"""

from __future__ import annotations

import datetime
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from nebula import annotations
from nebula.index import _iter_session_dirs
from nebula.refs import Ref, format_ref
from nebula.registry import get_registry, resolve_archive
from nebula.session import _hold_active, orphan_artifacts_in
from nebula.sidecar import (
    SESSION_FILE,
    SIDECAR_SUFFIX,
    SidecarMeta,
    read_session_yaml,
    read_sidecar,
    sha256_file,
    sidecar_path_for,
)

PAIRED = "paired"
ORPHAN = "orphan"
STRAY = "stray"
DRIFTED = "drifted"

STATUS_LABEL = {
    PAIRED: "OK",
    ORPHAN: "no metadata",
    STRAY: "data missing",
    DRIFTED: "modified",
}


def resolve(archive_arg) -> "tuple[Path, str]":
    """Lenient resolution for the GUI, mirroring the CLI: a registered name
    wins, otherwise treat the argument as a filesystem path. Returns
    (root, label)."""
    registry = get_registry()
    if isinstance(archive_arg, str):
        cfg = registry.try_get(archive_arg)
        if cfg is not None:
            return cfg.root, archive_arg
    return Path(archive_arg), str(archive_arg)


@dataclass
class Item:
    name: str
    status: str
    has_artifact: bool
    has_sidecar: bool
    source: Optional[str] = None       # "script" | "external" | "?" (unreadable)
    origin: Optional[str] = None
    size: Optional[int] = None         # bytes, if the artefact is present
    sha256: Optional[str] = None
    timestamp: Optional[str] = None    # sidecar 'created', else file mtime (ISO)
    artifact_path: Optional[Path] = None
    sidecar_path: Optional[Path] = None
    # produced_by's git fields, carried on the item so a list view can show
    # "what built this" without opening the sidecar panel per file.
    repo: Optional[str] = None
    commit: Optional[str] = None
    dirty: Optional[bool] = None
    entry_point: Optional[str] = None
    n_derived_from: int = 0            # how many sources it declares
    # Mutable, user-authored -- deliberately kept apart from everything
    # above, which was recorded at creation time and never changes.
    user_tags: List[str] = field(default_factory=list)
    user_comment: str = ""
    # Duplicate grouping: `name` is what is on disk (raw-001.csv), while
    # `display_name` is what was asked for (raw.csv). position/total say
    # which write this was, so the view can show "2 of 3".
    original_name: Optional[str] = None
    position: int = 1
    total: int = 1

    @property
    def display_name(self) -> str:
        return self.original_name or self.name

    @property
    def is_duplicate(self) -> bool:
        return self.total > 1

    @property
    def status_label(self) -> str:
        return STATUS_LABEL.get(self.status, self.status)

    @property
    def detail(self) -> str:
        bits = [f"{self.name}  —  {STATUS_LABEL.get(self.status, self.status)}"]
        if self.status == ORPHAN:
            bits.append("no sidecar; run reconcile to record provenance")
        elif self.status == STRAY:
            bits.append("sidecar has no data file; recover it or remove the sidecar")
        elif self.status == DRIFTED:
            bits.append("bytes changed since the sidecar was written (sha256 mismatch)")
        if self.source:
            src = self.source if self.source != "?" else "unreadable sidecar"
            bits.append(f"source: {src}")
        if self.origin:
            bits.append(f"origin: {self.origin}")
        if self.size is not None:
            bits.append(f"size: {_human_size(self.size)}")
        return "\n".join(bits)


@dataclass
class SessionInfo:
    run_id: str
    path: Path
    created: str
    status: str
    tags: List[str] = field(default_factory=list)
    description: str = ""
    held: bool = False
    n_items: int = 0
    n_problems: int = 0
    user_tags: List[str] = field(default_factory=list)


def list_sessions(archive) -> List[SessionInfo]:
    """Every session in the archive, newest first, with a per-session count
    of items and how many have a problem (so the sidebar can flag them)."""
    archive_root, _ = resolve_archive(archive)
    out: List[SessionInfo] = []
    for session_dir in _iter_session_dirs(archive_root):
        try:
            meta = read_session_yaml(session_dir)
        except Exception:
            continue
        items = list_items(session_dir)
        problems = sum(1 for it in items if it.status != PAIRED)
        out.append(SessionInfo(
            run_id=meta.run_id, path=session_dir, created=meta.created,
            status=meta.status, tags=meta.tags, description=meta.description,
            held=_hold_active(meta), n_items=len(items), n_problems=problems,
            user_tags=annotations.get(session_dir)["tags"],
        ))
    out.sort(key=lambda s: s.created or "", reverse=True)
    return out


def _appendable(status: str, created: str, held: bool) -> bool:
    today = datetime.date.today().isoformat()
    return status == "open" or (created or "")[:10] == today or bool(held)


def _is_appendable(s: "SessionInfo") -> bool:
    """A session you can import into without a deliberate reopen: still
    open, created today, or held (mirrors session.append_to's rule)."""
    return _appendable(s.status, s.created, s.held)


def importable_sessions(archive) -> List[SessionInfo]:
    """Sessions a drag-and-drop import may target directly -- i.e. not
    frozen (closed on a previous day)."""
    return [s for s in list_sessions(archive) if _is_appendable(s)]


def frozen_sessions(archive) -> List[SessionInfo]:
    """Sessions closed on a previous day -- importable only with a
    deliberate reopen (allow_frozen)."""
    return [s for s in list_sessions(archive) if not _is_appendable(s)]


def list_items(session_dir, *, verify_checksums: bool = False) -> List[Item]:
    """One Item per logical artefact in a session (the union of data files
    and sidecars). verify_checksums re-hashes present files to detect drift
    -- off by default since it can be slow on large data."""
    session_dir = Path(session_dir)
    notes = annotations.read_annotations(session_dir).get("artifacts") or {}
    artefacts: set = set()
    sidecar_bases: set = set()
    for entry in session_dir.iterdir():
        if not entry.is_file() or entry.name.startswith("."):
            continue
        if entry.name in (SESSION_FILE, annotations.ANNOTATIONS_FILE):
            continue
        if entry.name.endswith(SIDECAR_SUFFIX):
            sidecar_bases.add(entry.name[: -len(SIDECAR_SUFFIX)])
        else:
            artefacts.add(entry.name)

    items: List[Item] = []
    for name in sorted(artefacts | sidecar_bases):
        has_a = name in artefacts
        has_s = name in sidecar_bases
        art_path = session_dir / name
        sc_path = sidecar_path_for(art_path)

        source = origin = sha = created = None
        original_name = None
        duplicate_index = None
        repo = commit = entry_point = None
        dirty = None
        n_derived = 0
        if has_s:
            try:
                meta = read_sidecar(art_path)
                source = meta.produced_by.source
                origin = meta.produced_by.origin
                repo = meta.produced_by.repo
                commit = meta.produced_by.commit
                dirty = meta.produced_by.dirty
                entry_point = meta.produced_by.entry_point
                n_derived = len(meta.derived_from)
                original_name = meta.original_name
                duplicate_index = meta.duplicate_index
                sha = meta.sha256
                created = meta.created
            except Exception:
                source = "?"  # sidecar present but unparseable

        size = None
        if has_a:
            try:
                st = art_path.stat()
                size = st.st_size
                if created is None:  # no sidecar timestamp -> use file mtime
                    created = datetime.datetime.fromtimestamp(
                        st.st_mtime).astimezone().isoformat(timespec="seconds")
            except OSError:
                pass

        if has_a and has_s:
            status = PAIRED
            if verify_checksums and sha:
                try:
                    if sha256_file(art_path) != sha:
                        status = DRIFTED
                except OSError:
                    pass
        elif has_a:
            status = ORPHAN
        else:
            status = STRAY

        items.append(Item(
            name=name, status=status, has_artifact=has_a, has_sidecar=has_s,
            source=source, origin=origin, size=size, sha256=sha,
            timestamp=created,
            artifact_path=art_path if has_a else None,
            sidecar_path=sc_path if has_s else None,
            repo=repo, commit=commit, dirty=dirty, entry_point=entry_point,
            n_derived_from=n_derived,
            user_tags=list((notes.get(name) or {}).get("tags") or []),
            user_comment=(notes.get(name) or {}).get("comment") or "",
            original_name=original_name,
            position=(duplicate_index or 0) + 1,
        ))

    return _group_duplicates(items)


def _sort_key(it: "Item"):
    """Newest first -- the run you just did is the one you want to see.

    (The Navigator can re-sort this however the user asks; this is the
    default order every consumer gets.)

    Timestamps are parsed rather than compared as strings, since a sidecar
    'created' carries a UTC offset and two files either side of a DST
    change would otherwise sort wrongly. Undated items (an unreadable
    sidecar with no mtime to fall back on) go last rather than to 1970.

    Ties -- common, since timestamps have second resolution -- fall back to
    the requested name and then write order, so a duplicate group stays
    together and in order instead of interleaving.
    """
    ts = _parse_timestamp(it.timestamp)
    # Negated rather than reverse=True so the tie-breakers stay ascending:
    # a duplicate group must read 1, 2, 3 even though the list is newest-first.
    return (ts is None, -(ts or 0.0), it.display_name, it.position)


def _parse_timestamp(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts).timestamp()
    except (ValueError, OSError):
        return None


def _group_duplicates(items: List[Item]) -> List[Item]:
    """Number each duplicate group, then order everything by creation
    time (see _sort_key)."""
    groups: dict = {}
    for it in items:
        groups.setdefault(it.display_name, []).append(it)
    for name, members in groups.items():
        for it in members:
            it.total = len(members)
    return sorted(items, key=_sort_key)


def sidecar_display(sidecar_path) -> str:
    """The sidecar's contents as pretty JSON for the side panel. Falls back
    to the raw text if it doesn't parse (so an unreadable sidecar is still
    inspectable), or an error note if it can't be read at all."""
    path = Path(sidecar_path)
    try:
        raw = path.read_text()
    except OSError as e:
        return f"(could not read {path.name}: {e})"
    try:
        return json.dumps(json.loads(raw), indent=2, sort_keys=True)
    except Exception:
        return raw


def _ref_dict(r: Ref) -> dict:
    """A ref as both its compact string form and its parts, so the view can
    show the string and still key off the pieces."""
    try:
        text = format_ref(r)
    except ValueError:          # a ref with neither session nor file
        text = "(empty ref)"
    return {"ref": text, "file": r.file, "session": r.session, "archive": r.archive}


def sidecar_info(sidecar_path) -> dict:
    """The sidecar as *structured* data for the detail panel, rather than
    the pretty-printed blob ``sidecar_display`` returns.

    Always succeeds: an unreadable or malformed sidecar comes back with
    ``ok: False`` and whatever raw text we could get, because a broken
    sidecar is exactly the case you most want to look at in the GUI.
    """
    path = Path(sidecar_path)
    out: dict = {"ok": False, "name": path.name, "path": str(path),
                 "raw": "", "error": None}
    try:
        raw = path.read_text()
    except OSError as e:
        out["error"] = f"could not read {path.name}: {e}"
        return out

    try:
        data = json.loads(raw)
        out["raw"] = json.dumps(data, indent=2, sort_keys=True)
    except Exception as e:      # noqa: BLE001 -- any parse failure is reportable
        out["raw"] = raw
        out["error"] = f"not valid JSON: {e}"
        return out

    try:
        meta = SidecarMeta.from_dict(data)
    except Exception as e:      # noqa: BLE001 -- e.g. missing 'created'
        out["error"] = f"unrecognised sidecar layout: {e}"
        return out

    pb = meta.produced_by
    # ProducedBy.source defaults to "script", so a sidecar written before
    # that field existed *looks* like it claims "script" when it actually
    # says nothing. Tell the view which it is, so a GUI can stop presenting
    # an assumption as a recorded fact.
    raw_pb = data.get("produced_by") or {}
    out.update({
        "ok": True,
        "created": meta.created,
        "sha256": meta.sha256,
        "source_recorded": "source" in raw_pb,
        "produced_by": {
            "source": pb.source,
            "origin": pb.origin,
            "repo": pb.repo,
            "commit": pb.commit,
            "dirty": pb.dirty,
            "entry_point": pb.entry_point,
            "imported_by": pb.imported_by,
            "imported_at": pb.imported_at,
            "code": pb.code,
            "repos": dict(pb.repos or {}),
            # Anything a newer nebula wrote that this one doesn't model.
            "extra": dict(pb.extra or {}),
        },
        "derived_from": [_ref_dict(r) for r in meta.derived_from_refs()],
        "inputs": meta.inputs,
        "extra": meta.extra,
    })
    return out


def session_info(session_dir) -> dict:
    """Everything session.yaml knows about a session, plus the few derived
    facts the GUI would otherwise have to recompute (hold state, whether
    it's still appendable, item/problem counts, total artefact size)."""
    session_dir = Path(session_dir)
    path = session_dir / SESSION_FILE
    out: dict = {"ok": False, "path": str(path), "session_path": str(session_dir),
                 "raw": "", "error": None}
    try:
        out["raw"] = path.read_text()
    except OSError as e:
        out["error"] = f"could not read {SESSION_FILE}: {e}"
        return out

    try:
        meta = read_session_yaml(session_dir)
    except Exception as e:      # noqa: BLE001 -- malformed YAML is displayable
        out["error"] = f"could not parse {SESSION_FILE}: {e}"
        return out

    items = list_items(session_dir)
    held = _hold_active(meta)
    out.update({
        "ok": True,
        "run_id": meta.run_id,
        "created": meta.created,
        "status": meta.status,
        "tags": list(meta.tags),
        "description": meta.description,
        "held": held,
        "hold_until": meta.hold_until,
        "appendable": _appendable(meta.status, meta.created, held),
        # Resolved, not just formatted: the panel marks a missing session
        # and an unreachable archive differently, and only navigates to
        # something that is actually there (roadmap item 1).
        "related_runs": [
            _resolve_ref(r, archive_root=session_dir.parents[2],
                         archive_label=session_dir.parents[2].name,
                         run_id=meta.run_id)
            for r in meta.related_run_refs()
        ],
        "history": list(meta.history),
        "n_items": len(items),
        "n_problems": sum(1 for it in items if it.status != PAIRED),
        "user_tags": annotations.get(session_dir)["tags"],
        "user_comment": annotations.get(session_dir)["comment"],
        "size": sum(it.size or 0 for it in items),
    })
    out["size_human"] = _human_size(out["size"])
    return out


def _find_session_dir(archive_root: Path, run_id: str) -> Optional[Path]:
    """The folder for a run id inside an archive, or None if it isn't
    there (a ref into a session that was moved, deleted, or never
    existed)."""
    for session_dir in _iter_session_dirs(Path(archive_root)):
        if session_dir.name == run_id:
            return session_dir
    return None


def _resolve_ref(ref: Ref, *, archive_root: Path, archive_label: str,
                 run_id: str) -> dict:
    """Turn one derived_from ref into something a view can render and
    click: where it points, whether that file is actually there, and -- if
    not -- why (missing file vs. an archive we can't reach)."""
    out = _ref_dict(ref)
    target_run = ref.session or run_id
    out.update({"run_id": target_run, "filename": ref.file,
                "whole_session": ref.file is None,
                "path": None, "session_path": None,
                "exists": False, "resolved": True, "note": None})

    root = archive_root
    if ref.archive is not None and ref.archive != archive_label:
        cfg = get_registry().try_get(ref.archive)
        if cfg is None:
            out.update({"resolved": False,
                        "note": f"archive {ref.archive!r} is not registered here"})
            return out
        root = cfg.root
        if not root.is_dir():
            out.update({"resolved": False,
                        "note": f"archive {ref.archive!r} is not mounted"})
            return out

    session_dir = _find_session_dir(root, target_run)
    if session_dir is None:
        out["note"] = f"session {target_run} not found"
        return out
    out["session_path"] = str(session_dir)
    if ref.file is None:                       # whole-session reference
        out["exists"] = True
        return out
    target = session_dir / ref.file
    out["path"] = str(target)
    out["exists"] = target.is_file()
    if not out["exists"]:
        out["note"] = "file is missing"
    return out


def lineage(archive, session_path, filename: str) -> dict:
    """Both directions of one artefact's provenance graph:

        upstream   -- what it declares it was derived from (its sidecar)
        downstream -- artefacts elsewhere that declare they came from it

    Downstream comes from :func:`nebula.check.dependents_of`, which scans
    sidecars on disk rather than the SQLite index -- so this answers
    correctly even when the index is stale, and without requiring the user
    to have run ``nebula rebuild``. Cross-archive dependents can't be seen
    from here (nothing records back-links), which ``complete`` reports.
    """
    from nebula.check import dependents_of  # local: avoids an import cycle

    archive_root, label = resolve(archive)
    session_dir = Path(session_path)
    try:
        run_id = read_session_yaml(session_dir).run_id
    except Exception:
        run_id = session_dir.name

    upstream: List[dict] = []
    art_path = session_dir / filename
    if sidecar_path_for(art_path).is_file():
        try:
            meta = read_sidecar(art_path)
            upstream = [
                _resolve_ref(r, archive_root=archive_root, archive_label=label,
                             run_id=run_id)
                for r in meta.derived_from_refs()
            ]
        except Exception:
            upstream = []   # unparseable sidecar; sidecar_info reports why

    downstream: List[dict] = []
    for hit in dependents_of(archive_root, run_id, filename):
        dep_run, _, dep_file = hit.partition("/")
        dep_dir = _find_session_dir(archive_root, dep_run)
        dep_path = (dep_dir / dep_file) if dep_dir else None
        downstream.append({
            "ref": hit, "run_id": dep_run, "filename": dep_file,
            "session_path": str(dep_dir) if dep_dir else None,
            "path": str(dep_path) if dep_path else None,
            "exists": bool(dep_path and dep_path.is_file()),
            "same_session": dep_run == run_id,
        })

    return {
        "run_id": run_id, "filename": filename,
        "upstream": upstream, "downstream": downstream,
        "complete": True,   # downstream is same-archive only, by construction
    }


def resolve_refs(archive, run_id: str, refs: List[str]) -> List[dict]:
    """Check derived_from refs before they are written.

    Each entry comes back with whether it parses at all, and -- if it does
    -- whether it points at something that exists, using the same
    resolution as :func:`lineage`. A ref is *allowed* to dangle (the CLI
    permits it, and a target may legitimately arrive later), so this
    reports rather than rejects; the caller decides how loudly to warn.
    """
    from nebula.refs import parse_ref

    archive_root, label = resolve(archive)
    out: List[dict] = []
    for text in refs:
        entry = {"text": text, "valid": False, "error": None}
        try:
            ref = parse_ref(text)
        except ValueError as e:
            entry["error"] = str(e)
            out.append(entry)
            continue
        entry["valid"] = True
        entry.update(_resolve_ref(ref, archive_root=archive_root,
                                  archive_label=label, run_id=run_id))
        out.append(entry)
    return out


ITEM_SEARCH_FIELDS = ("filename", "tags", "origin", "session",
                      "user_tags", "comments")


def _in_date_range(timestamp, date_from, date_to) -> bool:
    """Compare an ISO timestamp's date part against YYYY-MM-DD bounds
    (inclusive). ISO dates sort lexicographically, so string comparison is
    both correct and cheap. An item with no timestamp is only kept when no
    bound was asked for -- an unknown date can't be claimed to be in range.
    """
    if not date_from and not date_to:
        return True
    day = (timestamp or "")[:10]
    if not day:
        return False
    if date_from and day < date_from:
        return False
    if date_to and day > date_to:
        return False
    return True


def search_items(
    archive,
    query: str = "",
    *,
    fields=None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 1000,
) -> dict:
    """Search artefacts across every session in an archive.

    ``query`` is split on whitespace and every term must match somewhere in
    the enabled ``fields`` (AND, case-insensitive substring) -- so "diode
    csv" finds a csv in a diode-tagged session. Fields:

        filename -- the artefact's own name
        tags     -- its *session's* tags (tags live on sessions, not files)
        origin   -- the sidecar's free-text origin, plus script/external
        session  -- the run id and session description

    ``date_from``/``date_to`` are inclusive YYYY-MM-DD bounds on the item's
    timestamp (its sidecar 'created', else the file's mtime).

    An empty query with no date bounds matches nothing rather than
    everything: the caller is asking to search, not to list the archive.
    """
    fields = set(fields or ITEM_SEARCH_FIELDS)
    terms = [t for t in (query or "").lower().split() if t]
    if not terms and not date_from and not date_to:
        return {"items": [], "truncated": False, "n_sessions": 0, "n_scanned": 0}

    root, _ = resolve(archive)
    out: List[dict] = []
    n_sessions = n_scanned = 0
    truncated = False

    for s in list_sessions(root):
        n_sessions += 1
        session_notes = annotations.get(s.path)
        for it in list_items(s.path):
            n_scanned += 1
            if not _in_date_range(it.timestamp, date_from, date_to):
                continue
            if terms:
                hay: List[str] = []
                if "filename" in fields:
                    hay.append(it.name)
                if "tags" in fields:
                    hay.extend(s.tags)
                if "origin" in fields:
                    hay.append(it.origin or "")
                    hay.append(it.source or "")
                if "session" in fields:
                    hay.append(s.run_id)
                    hay.append(s.description)
                if "user_tags" in fields:
                    hay.extend(it.user_tags)
                    hay.extend(session_notes["tags"])
                if "comments" in fields:
                    hay.append(it.user_comment)
                    hay.append(session_notes["comment"])
                blob = " ".join(hay).lower()
                if not all(t in blob for t in terms):
                    continue
            if len(out) >= limit:
                truncated = True
                break
            out.append({
                "item": it, "run_id": s.run_id, "session_path": str(s.path),
                "session_description": s.description, "tags": list(s.tags),
                "session_created": s.created,
                "session_user_tags": list(session_notes["tags"]),
            })
        if truncated:
            break

    return {"items": out, "truncated": truncated,
            "n_sessions": n_sessions, "n_scanned": n_scanned}


#: Where to look for a repo by name when resolving an entry point to a
#: local checkout. Machine-local, so it is env-configurable rather than
#: stored in the archive (which is shared between machines).
DEFAULT_REPO_SEARCH_PATHS = (
    "~/Documents/GitHub", "~/GitHub", "~/git", "~/src", "~/code",
    "~/Projects", "~/projects", "~/repos", "~/dev",
)
REPO_PATHS_ENV = "NEBULA_REPO_PATHS"


def repo_search_paths() -> List[Path]:
    raw = os.environ.get(REPO_PATHS_ENV)
    parts = raw.split(os.pathsep) if raw else DEFAULT_REPO_SEARCH_PATHS
    return [Path(os.path.expanduser(p)) for p in parts if p]


def _find_repo_checkout(repo: str) -> Optional[Path]:
    for base in repo_search_paths():
        candidate = base / repo
        if (candidate / ".git").exists():
            return candidate
    return None


def _remote_url(repo_root: Path) -> Optional[str]:
    """The repo's origin as a browsable https URL, from the checkout
    itself -- more reliable than assuming a naming convention."""
    from nebula.session import _git

    raw = _git(["remote", "get-url", "origin"], cwd=repo_root)
    if not raw:
        return None
    url = raw.strip()
    if url.startswith("git@"):                      # git@github.com:org/repo.git
        host, _, path = url[4:].partition(":")
        url = f"https://{host}/{path}"
    if url.endswith(".git"):
        url = url[:-4]
    return url if url.startswith("http") else None


def entry_point_link(archive, item: dict) -> dict:
    """Work out how to open the script that produced an artifact.

    Two independent answers, because neither is always available or always
    right: the local checkout (what you can edit, but possibly at a
    different commit) and a hosted URL pinned to the recorded commit (exact
    for that commit, but wrong when the tree was dirty, and only if the
    commit was pushed).
    """
    entry = item.get("entry_point")
    repo = item.get("repo")
    commit = item.get("commit")
    out: dict = {"entry_point": entry, "repo": repo, "commit": commit,
                 "dirty": item.get("dirty"), "local": None, "remote": None,
                 "note": None}
    if not entry:
        out["note"] = "no entry point recorded"
        return out

    # No repo means capture_provenance stored an absolute path.
    if not repo:
        path = Path(entry)
        out["local"] = {"path": str(path), "exists": path.is_file(),
                        "repo_root": None, "matches_commit": None}
        if not path.is_file():
            out["note"] = "the script is not at that path on this machine"
        return out

    root = _find_repo_checkout(repo)
    if root is None:
        out["note"] = (f"no checkout of {repo!r} found; set {REPO_PATHS_ENV} if it "
                       f"lives outside the usual places")
    else:
        from nebula.session import _git

        path = root / entry
        head = _git(["rev-parse", "HEAD"], cwd=root)
        out["local"] = {
            "path": str(path),
            "exists": path.is_file(),
            "repo_root": str(root),
            # False means the file on disk is *not* the recorded version.
            "matches_commit": (head == commit) if (head and commit) else None,
        }
        if not path.is_file():
            out["note"] = f"{entry} is not in the {repo} checkout at this commit"

        url = _remote_url(root)
        if url and commit:
            out["remote"] = {"url": f"{url}/blob/{commit}/{entry}", "from": "origin"}

    if out["remote"] is None and commit:
        # Fall back to the registry's git_org when there is no checkout to
        # read a remote from.
        cfg = get_registry().try_get(archive) if isinstance(archive, str) else None
        if cfg is not None and cfg.git_org:
            out["remote"] = {
                "url": f"https://github.com/{cfg.git_org}/{repo}/blob/{commit}/{entry}",
                "from": "registry git_org",
            }

    if out["remote"] and item.get("dirty"):
        out["remote"]["warning"] = (
            "the working tree was dirty, so the file at this commit is not "
            "exactly what ran")
    return out


def code_info(archive, code: str) -> dict:
    """Stats for one artifact's captured-source snapshot, for the
    provenance panel."""
    from nebula import codestore

    root, _ = resolve(archive)
    stats = codestore.manifest_stats(root, code)
    if stats is None:
        return {"ok": False, "id": code, "short": (code or "")[:12],
                "error": "this snapshot is not in the archive's code store"}
    stats["ok"] = True
    stats["error"] = None
    stats["store_dir"] = str(root / codestore.CODE_DIR)
    return stats


def restore_code(archive, code: str, dest_parent) -> dict:
    """Restore a captured-source snapshot into a fresh folder under
    `dest_parent`, named after the snapshot so two restores never collide."""
    from nebula import codestore

    root, _ = resolve(archive)
    parent = Path(os.path.expanduser(str(dest_parent)))
    base = f"nebula-code-{(code or '')[:12]}"
    dest = parent / base
    n = 2
    while dest.exists():          # a second restore of the same snapshot
        dest = parent / f"{base}-{n}"
        n += 1
    return codestore.restore(root, code, dest)


def activity(archive) -> dict:
    """Sessions and artifacts per calendar day, for the activity strip.

    Days are *local* dates parsed from the timestamp, not string slices: a
    session created at 23:40 with a +02:00 offset belongs to the day its
    clock showed, and slicing the ISO string would file it under the wrong
    one either side of a DST change.
    """
    root, _ = resolve(archive)
    days: dict = {}
    for s in list_sessions(root):
        ts = _parse_timestamp(s.created)
        if ts is None:
            continue
        day = datetime.datetime.fromtimestamp(ts).date().isoformat()
        bucket = days.setdefault(day, {"sessions": 0, "items": 0, "problems": 0})
        bucket["sessions"] += 1
        bucket["items"] += s.n_items
        bucket["problems"] += s.n_problems

    ordered = sorted(days)
    return {
        "days": days,
        "first": ordered[0] if ordered else None,
        "last": ordered[-1] if ordered else None,
        "busiest": max((d["sessions"] for d in days.values()), default=0),
        "today": datetime.date.today().isoformat(),
    }


def archive_stats(archive) -> dict:
    """Everything the archive-management panel shows at a glance: size of
    the archive, when the index was last rebuilt, what the code store holds,
    and any sessions still marked open long after they should be."""
    from nebula import codestore
    from nebula.config import ARCHIVE_CONFIG_FILE, read_settings

    root, label = resolve(archive)
    sessions = list_sessions(root)
    n_items = sum(s.n_items for s in sessions)
    n_problems = sum(s.n_problems for s in sessions)

    from nebula import index as index_mod

    # Report the index as it is; don't sweep it here. This panel is a status
    # display, and a status display that silently repairs what it is
    # describing can never show you a problem.
    index_info = index_mod.status(root)
    index_info["human"] = _human_size(index_info["size"] or 0)
    index_info["stale"] = None
    if index_info["exists"]:
        # Signatures, not session counts: an edited or resealed sidecar
        # leaves the count identical, and reporting that as fresh would be
        # a confident lie.
        pending = index_mod.pending_changes(root)
        index_info["stale"] = pending["stale"]
        index_info["pending"] = {k: pending[k] for k in
                                 ("added", "updated", "removed", "reason")}
        index_info["skipped_years"] = pending["skipped_years"]

    blobs = list(codestore._iter_stored(root, codestore.BLOBS))
    manifests = list(codestore._iter_stored(root, codestore.MANIFESTS))
    code_bytes = 0
    for path in blobs + manifests:
        try:
            code_bytes += path.stat().st_size
        except OSError:
            pass

    settings = read_settings(root)
    return {
        "label": label, "root": str(root),
        "n_sessions": len(sessions), "n_items": n_items, "n_problems": n_problems,
        "size": sum(_session_size(s.path) for s in sessions),
        "index": index_info,
        "code": {"blobs": len(blobs), "manifests": len(manifests),
                 "bytes": code_bytes, "human": _human_size(code_bytes),
                 "dir": str(root / codestore.CODE_DIR)},
        "settings": {"on_overwrite": settings.on_overwrite,
                     "capture_code": settings.capture_code,
                     "config_file": str(root / ARCHIVE_CONFIG_FILE),
                     "config_exists": (root / ARCHIVE_CONFIG_FILE).is_file()},
        # Still "open" but not from today: almost always a script that
        # died before close() rather than work in progress.
        "stale_open": [
            {"run_id": s.run_id, "created": s.created, "path": str(s.path)}
            for s in sessions
            if s.status == "open"
            and (s.created or "")[:10] != datetime.date.today().isoformat()
        ],
    }


def _session_size(session_dir) -> int:
    total = 0
    for entry in Path(session_dir).iterdir():
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return total


def _human_size_public(n: int) -> str:
    return _human_size(n)


def registered_archives() -> List[dict]:
    """The machine's archive registry (~/.nebula/archives.yaml), so the
    archive switcher can offer known archives without the user hunting for
    the directory. ``exists`` is False for a root that isn't mounted."""
    out = []
    for name, cfg in sorted(get_registry().all().items()):
        out.append({"name": name, "root": str(cfg.root), "exists": cfg.root.is_dir()})
    return out


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"
