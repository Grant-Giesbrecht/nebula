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
from typing import Dict, List, Optional

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
    #: The rail shows only *that* a comment exists, never its text -- a
    #: paragraph does not belong in a list row. Carried in full anyway so
    #: the indicator can show it on hover.
    user_comment: str = ""


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
            user_comment=annotations.get(session_dir)["comment"],
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
    out["index"] = session_index_state(session_dir, meta.run_id)
    return out


def session_index_state(session_dir, run_id: str) -> dict:
    """What the index believes about this session, next to what is actually
    on disk -- so the two can be compared rather than taken on faith.

    Cheap by construction: one scandir for the live signature, one indexed
    row, and the year's seal file if there is one. Nothing is swept or
    repaired here; a panel that silently fixed what it was describing could
    never show a problem.
    """
    from nebula import index as index_mod

    session_dir = Path(session_dir)
    archive_root = session_dir.parents[2]
    year = session_dir.parent.name
    out = {
        "live_sig": index_mod.session_signature(session_dir),
        "indexed_sig": None, "in_sync": None, "indexed": False,
        "index_exists": False, "index_usable": False, "built": None,
        "year": year, "sealed": False, "seal": None, "skipped_by_seal": False,
    }
    target = index_mod.index_path_for(archive_root)
    out["index_exists"] = target.is_file()

    seal = index_mod.read_seal(archive_root, year)
    if seal:
        out["sealed"] = True
        out["seal"] = {"digest": seal.get("digest"), "sealed": seal.get("sealed"),
                       "sessions": seal.get("sessions")}

    if not out["index_exists"]:
        return out
    try:
        conn = index_mod.open_index(archive_root)
    except Exception:           # noqa: BLE001 -- an unusable index is a fact to show
        return out
    try:
        out["index_usable"] = index_mod.status(archive_root)["usable"]
        row = conn.execute("SELECT sig FROM sessions WHERE run_id = ?",
                           (run_id,)).fetchone()
        if row is not None:
            out["indexed"] = True
            out["indexed_sig"] = row["sig"]
            out["in_sync"] = row["sig"] == out["live_sig"]
        seal_row = conn.execute("SELECT digest FROM year_seals WHERE year = ?",
                                (year,)).fetchone()
        # A sweep only skips this session's year when the index has already
        # verified it against exactly this seal.
        out["skipped_by_seal"] = bool(
            seal and seal_row and seal_row["digest"] == str(seal.get("digest")))
    except Exception:           # noqa: BLE001
        pass
    finally:
        conn.close()
    return out


#: Tables the index browser can show, in the order it offers them.
INDEX_TABLES = ("sessions", "artifacts", "derived_from", "related_runs",
                "year_seals", "meta")


def index_view(archive, *, table: str = "sessions", query: str = "",
               run_id: str = "", limit: int = 200, offset: int = 0) -> dict:
    """A read-only look at what is actually in index.db.

    Deliberately a dump rather than a report: the point is to see what the
    index holds, including the columns (rel_path, sig, year) that exist to
    make it self-maintaining and portable. Paged, because "show me the
    index" must stay answerable on an archive with a hundred thousand rows.
    """
    from nebula import index as index_mod

    root, label = resolve(archive)
    status = index_mod.status(root)
    out = {"archive": label, "root": str(root), "status": status,
           "table": table, "tables": [], "columns": [], "rows": [],
           "total": 0, "limit": limit, "offset": offset,
           "query": query, "run_id": run_id, "error": None}
    if not status["exists"]:
        out["error"] = "no index yet"
        return out
    if not status["usable"]:
        out["error"] = ("this index was written by a different version of "
                        "nebula; it will be rebuilt the next time it is read")
        return out
    if table not in INDEX_TABLES:
        table = "sessions"
        out["table"] = table

    try:
        conn = index_mod.open_index(root)
    except Exception as e:      # noqa: BLE001
        out["error"] = f"could not open the index: {e}"
        return out
    try:
        for name in INDEX_TABLES:
            try:
                n = conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            except Exception:   # noqa: BLE001 -- a table the schema dropped
                n = None
            out["tables"].append({"name": name, "rows": n})

        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        out["columns"] = cols

        where, params = [], []
        if run_id and "run_id" in cols:
            where.append("run_id = ?")
            params.append(run_id)
        if query:
            # A plain contains-match across the text columns: this is a
            # magnifying glass, not a query language.
            like = [f"CAST({c} AS TEXT) LIKE ?" for c in cols]
            where.append("(" + " OR ".join(like) + ")")
            params.extend([f"%{query}%"] * len(cols))
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        out["total"] = conn.execute(
            f"SELECT count(*) FROM {table}{clause}", params).fetchone()[0]
        order = " ORDER BY created DESC" if "created" in cols else (
            " ORDER BY run_id" if "run_id" in cols else "")
        rows = conn.execute(
            f"SELECT * FROM {table}{clause}{order} LIMIT ? OFFSET ?",
            params + [max(1, min(1000, limit)), max(0, offset)]).fetchall()
        out["rows"] = [{c: row[c] for c in cols} for row in rows]
    except Exception as e:      # noqa: BLE001
        out["error"] = f"could not read the index: {e}"
    finally:
        conn.close()
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


#: How many hops the tree view asks for unless told otherwise. Depth is a
#: display choice, not a performance one: with the index swept and the
#: reverse edge indexed, hops are cheap -- but a screen full of ancestors
#: nobody asked for is still a worse answer than three and an "expand".
DEFAULT_TREE_DEPTH = 3


class _Lineage:
    """One archive's provenance edges, answered index-first.

    The index is swept before use (index.ensure_fresh), so it is normally
    exact. When it can't be used at all -- no index, unreadable, a
    filesystem we can only read -- this falls back to scanning sidecars,
    which is slower but always right. Callers are told which happened via
    ``source``, because "fast" and "trustworthy" are different promises
    and a view should not quietly imply the wrong one.

    Answers are memoised for the life of one query: a diamond in the graph
    asks the same question twice, and the tree is walked breadth-first.
    """

    def __init__(self, archive_root: Path):
        self.root = Path(archive_root)
        self.source = "index"
        self._conn = None
        self._up: Dict[tuple, list] = {}
        self._down: Dict[tuple, list] = {}
        try:
            from nebula import index as index_mod

            self._conn = index_mod.open_fresh(self.root)
        except Exception:       # noqa: BLE001 -- any index trouble: scan instead
            self._conn = None
            self.source = "scan"

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:   # noqa: BLE001
                pass
            self._conn = None

    def parents(self, run_id: str, filename: str) -> List[dict]:
        """Raw derived_from refs of one artefact, as dicts."""
        key = (run_id, filename)
        if key not in self._up:
            self._up[key] = (self._parents_indexed(run_id, filename)
                             if self._conn is not None
                             else self._parents_scanned(run_id, filename))
        return self._up[key]

    def children(self, run_id: str, filename: str) -> List[dict]:
        """Same-archive artefacts declaring they came from this one."""
        key = (run_id, filename)
        if key not in self._down:
            self._down[key] = (self._children_indexed(run_id, filename)
                               if self._conn is not None
                               else self._children_scanned(run_id, filename))
        return self._down[key]

    # -- index-backed ----------------------------------------------------
    def _parents_indexed(self, run_id, filename):
        rows = self._conn.execute(
            "SELECT ref_archive, ref_session, ref_file FROM derived_from "
            "WHERE run_id = ? AND filename = ?", (run_id, filename)).fetchall()
        return [{"archive": r["ref_archive"], "session": r["ref_session"],
                 "file": r["ref_file"]} for r in rows]

    def _children_indexed(self, run_id, filename):
        # The reverse edge, which idx_derived_from_ref exists for. A child's
        # ref may name this session explicitly or leave it implicit (same
        # session), and may or may not name this archive.
        rows = self._conn.execute(
            """
            SELECT run_id, filename FROM derived_from
            WHERE ref_file = ?
              AND ref_archive IS NULL
              AND (ref_session = ? OR (ref_session IS NULL AND run_id = ?))
            """, (filename, run_id, run_id)).fetchall()
        return [{"run_id": r["run_id"], "filename": r["filename"]} for r in rows]

    # -- filesystem fallback ---------------------------------------------
    def _parents_scanned(self, run_id, filename):
        session_dir = _find_session_dir(self.root, run_id)
        if session_dir is None:
            return []
        art = session_dir / filename
        if not sidecar_path_for(art).is_file():
            return []
        try:
            meta = read_sidecar(art)
        except Exception:       # noqa: BLE001
            return []
        return [_ref_dict(r) for r in meta.derived_from_refs()]

    def _children_scanned(self, run_id, filename):
        from nebula.check import dependents_of

        out = []
        for hit in dependents_of(self.root, run_id, filename):
            dep_run, _, dep_file = hit.partition("/")
            out.append({"run_id": dep_run, "filename": dep_file})
        return out


def _tree_node(archive_root: Path, label: str, run_id: str, filename: str,
               *, note=None, resolved=True) -> dict:
    """One artefact as the tree renders it: what it is, where it is, and
    whether it is actually there."""
    session_dir = _find_session_dir(archive_root, run_id) if resolved else None
    path = (session_dir / filename) if (session_dir and filename) else None
    return {
        "ref": f"{run_id}/{filename}" if filename else run_id,
        "run_id": run_id, "filename": filename, "archive": label,
        "session_path": str(session_dir) if session_dir else None,
        "path": str(path) if path else None,
        "exists": bool(path and path.is_file()),
        "resolved": resolved, "note": note,
        "children": [], "truncated": False, "seen": False,
    }


def provenance_tree(archive, run_id: str, filename: Optional[str] = None, *,
                    direction: str = "both", depth: int = DEFAULT_TREE_DEPTH) -> dict:
    """A nested provenance tree, for the relations view.

    Rooted either at one artefact (``filename`` given) or at a whole
    session, in which case every artefact the session produced becomes a
    top-level branch -- the "what did this run produce, and what came of
    it" question that nothing answered before.

    ``direction`` is "up", "down" or "both". ``depth`` caps the hops; a
    node whose children were not expanded because of the cap is marked
    ``truncated`` so the view can offer to go further, and a node already
    shown elsewhere in the tree is marked ``seen`` rather than being
    expanded twice -- an indented tree cannot draw a diamond, so it should
    at least admit to one.
    """
    archive_root, label = resolve(archive)
    lin = _Lineage(archive_root)
    try:
        def walk(node: dict, direction: str, left: int, seen: set) -> None:
            # `seen` is shared across the whole branch, not per path. That
            # catches cycles, and it is also what makes a diamond honest: an
            # indented tree cannot show that two paths reconverge, so the
            # second appearance is marked instead of being expanded again --
            # which additionally stops a wide DAG from blowing up.
            key = (node["run_id"], node["filename"])
            if key in seen:
                node["seen"] = True
                return
            seen.add(key)
            if left <= 0:
                edges = (lin.parents(*key) if direction == "up"
                         else lin.children(*key))
                node["truncated"] = bool(edges)
                return
            if direction == "up":
                for ref in lin.parents(*key):
                    child = _parent_node(archive_root, label, node["run_id"], ref)
                    node["children"].append(child)
                    if child["resolved"] and child["filename"]:
                        walk(child, "up", left - 1, seen)
            else:
                for edge in lin.children(*key):
                    child = _tree_node(archive_root, label,
                                       edge["run_id"], edge["filename"])
                    node["children"].append(child)
                    walk(child, "down", left - 1, seen)

        roots = []
        if filename:
            roots = [filename]
        else:
            session_dir = _find_session_dir(archive_root, run_id)
            if session_dir is not None:
                roots = [it.name for it in list_items(session_dir)
                         if it.has_sidecar]

        out = {"archive": label, "root": str(archive_root), "run_id": run_id,
               "filename": filename, "direction": direction, "depth": depth,
               "source": lin.source, "branches": []}
        for name in roots:
            branch = {"item": _tree_node(archive_root, label, run_id, name),
                      "upstream": [], "downstream": []}
            if direction in ("up", "both"):
                up = _tree_node(archive_root, label, run_id, name)
                walk(up, "up", depth, set())
                branch["upstream"] = up["children"]
                branch["item"]["truncated_up"] = up["truncated"]
            if direction in ("down", "both"):
                down = _tree_node(archive_root, label, run_id, name)
                walk(down, "down", depth, set())
                branch["downstream"] = down["children"]
                branch["item"]["truncated_down"] = down["truncated"]
            out["branches"].append(branch)
        out["session"] = _session_summary(archive_root, run_id)
        return out
    finally:
        lin.close()


def _parent_node(archive_root: Path, label: str, from_run: str, ref: dict) -> dict:
    """An upstream edge, which -- unlike a downstream one -- may point into
    another archive, and so may not be resolvable at all."""
    target_run = ref.get("session") or from_run
    other = ref.get("archive")
    if other is not None and other != label:
        cfg = get_registry().try_get(other)
        if cfg is None or not Path(cfg.root).is_dir():
            node = _tree_node(archive_root, other, target_run, ref.get("file"),
                              resolved=False,
                              note=(f"archive {other!r} is not registered here"
                                    if cfg is None else
                                    f"archive {other!r} is not mounted"))
            return node
        node = _tree_node(Path(cfg.root), other, target_run, ref.get("file"))
        return node
    node = _tree_node(archive_root, label, target_run, ref.get("file"))
    if not node["session_path"]:
        node["note"] = f"session {target_run} not found"
    elif not node["exists"]:
        node["note"] = "file is missing"
    return node


def _session_summary(archive_root: Path, run_id: str) -> Optional[dict]:
    session_dir = _find_session_dir(archive_root, run_id)
    if session_dir is None:
        return None
    try:
        meta = read_session_yaml(session_dir)
    except Exception:           # noqa: BLE001
        return None
    return {"run_id": meta.run_id, "description": meta.description,
            "created": meta.created, "status": meta.status,
            "path": str(session_dir)}


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

    from nebula.config import archive_identity

    settings = read_settings(root)
    ident = archive_identity(root)
    return {
        "label": label, "root": str(root), "identity": ident,
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


# ---------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------

#: Extensions the asset browser will render inline. Kept small and
#: format-explicit rather than sniffing bytes: a thumbnail grid is the
#: main way a figure library gets navigated, so what previews and what
#: does not should be predictable.
PREVIEWABLE = {".svg": "image/svg+xml", ".png": "image/png",
               ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}

#: Ceiling on an inlined preview. The sidecar protocol is line-delimited
#: JSON over a pipe, so a large base64 payload would stall every other
#: request queued behind it.
MAX_PREVIEW_BYTES = 2 << 20


def _asset_to_dict(archive_root: Path, meta, settings) -> dict:
    from nebula import assets as assets_mod

    live = assets_mod.live_file(archive_root, meta.id)
    retained = [s for s in meta.snapshots if not s.pending_gc]
    ext = Path(meta.name or "").suffix.lower()
    return {
        "id": meta.id,
        "name": meta.name,
        "path": str(live) if live else None,
        "missing": live is None,
        "size": meta.size,
        "size_human": _human_size(meta.size or 0),
        "sha256": meta.sha256,
        "created": meta.created,
        "scanned_at": meta.scanned_at,
        "origin": meta.origin,
        "imported_by": meta.imported_by,
        # Both, always: the GUI shows the declared value in the policy
        # control and the resolved one as the effect, so "auto" is never
        # mistaken for a policy that does nothing.
        "policy": meta.policy or "auto",
        "policy_resolved": meta.effective_policy(settings),
        "period_days": meta.effective_period_days(settings),
        "max_snapshots": meta.max_snapshots,
        "max_snapshot_bytes": meta.max_snapshot_bytes,
        "derived_from": list(meta.derived_from),
        "n_snapshots": len(retained),
        "n_snapshots_total": len(meta.snapshots),
        "renames": list(meta.renames),
        "previewable": ext in PREVIEWABLE and (meta.size or 0) <= MAX_PREVIEW_BYTES,
    }


def list_assets(archive, *, policy: str = "", sort: str = "recent",
                query: str = "") -> List[dict]:
    """Every asset, newest-touched first by default.

    Recency leads because assets are re-used rather than discovered: the
    twenty figures you keep coming back to are the ones you touched last,
    and no taxonomy beats that for the common case.
    """
    from nebula import assets as assets_mod
    from nebula.config import read_settings

    root, _ = resolve(archive)
    settings = read_settings(root, apply_env=False)
    out: List[dict] = []
    needle = (query or "").strip().lower()
    for asset_id in assets_mod.list_assets(root):
        try:
            meta = assets_mod.read_asset(root, asset_id)
        except assets_mod.AssetError:
            continue
        row = _asset_to_dict(root, meta, settings)
        if policy and row["policy_resolved"] != policy:
            continue
        if needle and needle not in f"{row['id']} {row['name'] or ''}".lower():
            continue
        out.append(row)

    if sort == "name":
        out.sort(key=lambda r: (r["name"] or "").lower())
    elif sort == "size":
        out.sort(key=lambda r: r["size"] or 0, reverse=True)
    elif sort == "created":
        out.sort(key=lambda r: r["created"] or "", reverse=True)
    else:
        out.sort(key=lambda r: r["scanned_at"] or r["created"] or "", reverse=True)
    return out


def asset_info(archive, asset_id: str) -> dict:
    """One asset in full, including its version history and the sessions
    that used it."""
    from nebula import assets as assets_mod
    from nebula import assetstore
    from nebula.config import read_settings

    root, label = resolve(archive)
    settings = read_settings(root, apply_env=False)
    meta = assets_mod.read_asset(root, asset_id)
    info = _asset_to_dict(root, meta, settings)
    info["archive"] = label
    info["snapshots"] = [
        {
            "sha256": s.sha256,
            "at": s.at,
            "by": s.by,
            "bytes": s.bytes,
            "bytes_human": _human_size(s.bytes or 0),
            "note": s.note,
            "trigger": s.trigger,
            "pending_gc": s.pending_gc,
            # An evicted record makes no claim its bytes survive, so the
            # GUI must not offer to restore one that is gone.
            "recoverable": assetstore.blob_path(root, s.sha256).is_file(),
        }
        for s in reversed(meta.snapshots)
    ]
    info["used_by"] = asset_downstream(root, asset_id)
    return info


def asset_downstream(archive, asset_id: str) -> List[dict]:
    """Which session artifacts derive from this asset, and at what
    fidelity. Read from the index -- this is the reverse edge, and
    scanning every sidecar for it would make opening an asset slow."""
    from nebula import index as index_mod

    root, _ = resolve(archive)
    try:
        conn = index_mod.open_fresh(root)
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT run_id, filename, ref_sha256, ref_fidelity FROM derived_from "
            "WHERE ref_asset = ? ORDER BY run_id, filename", (asset_id,)
        ).fetchall()
        return [{"run_id": r["run_id"], "filename": r["filename"],
                 "sha256": r["ref_sha256"], "fidelity": r["ref_fidelity"]}
                for r in rows]
    finally:
        conn.close()


def asset_preview(archive, asset_id: str) -> dict:
    """A data: URI for the asset's current bytes, or a reason it has none.

    Inlined rather than served as a file:// URL because the webview's
    origin rules block local files, and because this keeps the front-end
    from needing filesystem access at all.
    """
    import base64

    from nebula import assets as assets_mod

    root, _ = resolve(archive)
    path = assets_mod.live_file(root, asset_id)
    if path is None:
        return {"uri": None, "reason": "file missing"}
    ext = path.suffix.lower()
    mime = PREVIEWABLE.get(ext)
    if mime is None:
        return {"uri": None, "reason": f"no preview for {ext or 'this type'}"}
    size = path.stat().st_size
    if size > MAX_PREVIEW_BYTES:
        return {"uri": None,
                "reason": f"too large to preview ({_human_size(size)})"}
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"uri": f"data:{mime};base64,{data}", "reason": None}


def asset_import_defaults(archive, paths: List[str]) -> dict:
    """What the import dialog should pre-fill for these files.

    The policy is pre-selected from size *and the reason is returned with
    it*, so the size ladder is something the user learns rather than
    something that silently happens to them.
    """
    from nebula import assets as assets_mod
    from nebula.config import read_settings

    root, _ = resolve(archive)
    settings = read_settings(root, apply_env=False)
    files = []
    for p in paths or []:
        path = Path(p)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        policy = assets_mod.default_policy_for_size(size, settings)
        files.append({
            "path": str(path),
            "name": path.name,
            "size": size,
            "size_human": _human_size(size),
            "policy": policy,
            "reason": _policy_reason(policy, size, settings),
            "previewable": path.suffix.lower() in PREVIEWABLE,
        })
    return {
        "files": files,
        "settings": asset_settings(root),
    }


def _policy_reason(policy: str, size: int, settings) -> str:
    if policy == "manual":
        return (f"{_human_size(size)} is over the "
                f"{_human_size(settings.asset_manual_above)} manual threshold")
    if policy == "periodic":
        return (f"{_human_size(size)} is over the "
                f"{_human_size(settings.asset_periodic_above)} periodic threshold")
    return f"{_human_size(size)} is below the size thresholds"


def asset_settings(archive) -> dict:
    """The archive-level asset defaults, for the settings panel."""
    from nebula.config import ASSET_CAP_ACTIONS, ASSET_POLICIES, read_settings

    root, _ = resolve(archive)
    s = read_settings(root, apply_env=False)
    return {
        "policy": s.asset_policy,
        "periodic_above": s.asset_periodic_above,
        "periodic_above_human": _human_size(s.asset_periodic_above),
        "manual_above": s.asset_manual_above,
        "manual_above_human": _human_size(s.asset_manual_above),
        "period_days": s.asset_period_days,
        "max_snapshots": s.asset_max_snapshots,
        "max_snapshot_bytes": s.asset_max_snapshot_bytes,
        "cap_action": s.asset_cap_action,
        "policies": list(ASSET_POLICIES),
        "cap_actions": list(ASSET_CAP_ACTIONS),
    }


#: Settings key -> ArchiveSettings field. One table, used by both the
#: writer and the preview, so the two cannot disagree about what is
#: settable.
_ASSET_SETTING_FIELDS = {
    "policy": "asset_policy",
    "periodic_above": "asset_periodic_above",
    "manual_above": "asset_manual_above",
    "period_days": "asset_period_days",
    "max_snapshots": "asset_max_snapshots",
    "max_snapshot_bytes": "asset_max_snapshot_bytes",
    "cap_action": "asset_cap_action",
}

_ASSET_INT_KEYS = ("periodic_above", "manual_above", "period_days",
                   "max_snapshots", "max_snapshot_bytes")


class AssetSettingsError(ValueError):
    """Proposed asset defaults that would not mean what they say."""


def _coerced_asset_settings(settings, changes: dict):
    """Apply `changes` onto a copy of `settings`, validating as we go.

    Validation lives here rather than in the form because the form is not
    the only caller and can be stale. The ladder check is the important
    one: with `periodic_above` at or above `manual_above` the periodic rung
    is unreachable, so the setting would be accepted and then silently do
    nothing -- exactly the quiet failure this project is trying not to have.
    """
    from nebula.config import (ASSET_CAP_ACTIONS, ASSET_POLICIES,
                               AUTO_ASSET_POLICY)
    import copy

    got = copy.copy(settings)
    changes = changes or {}

    for key in _ASSET_INT_KEYS:
        if key not in changes:
            continue
        raw = changes[key]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise AssetSettingsError(f"{key} must be a whole number, got {raw!r}")
        if value < 0:
            raise AssetSettingsError(f"{key} cannot be negative, got {value}")
        setattr(got, _ASSET_SETTING_FIELDS[key], value)

    if "policy" in changes:
        policy = changes["policy"]
        if policy == AUTO_ASSET_POLICY:
            # "auto" resolves against the ladder whose bottom rung this is,
            # so allowing it here would make the default define itself.
            raise AssetSettingsError(
                "'auto' is not a valid archive-wide default: it is resolved "
                "from this setting, so it cannot also be this setting")
        if policy not in ASSET_POLICIES:
            raise AssetSettingsError(f"unknown asset policy {policy!r}")
        got.asset_policy = policy

    if "cap_action" in changes:
        action = changes["cap_action"]
        if action not in ASSET_CAP_ACTIONS:
            raise AssetSettingsError(f"unknown cap action {action!r}")
        got.asset_cap_action = action

    if got.asset_periodic_above >= got.asset_manual_above:
        raise AssetSettingsError(
            f"the size thresholds are a ladder: 'periodic above' "
            f"({_human_size(got.asset_periodic_above)}) must be below "
            f"'manual above' ({_human_size(got.asset_manual_above)}), or no "
            "asset ever gets the periodic policy")

    if got.asset_period_days < 1:
        raise AssetSettingsError("period_days must be at least 1")

    return got


def asset_settings_preview(archive, changes: dict) -> dict:
    """Which assets would change policy if `changes` were saved.

    Assets whose own policy is "auto" re-resolve against the ladder on
    every read, so moving a threshold silently rewrites their behaviour.
    That is intended, but it is a far bigger blast radius than a settings
    form usually has, so it is shown before saving rather than discovered
    afterwards.
    """
    from nebula.assets import AssetError, list_assets, read_asset
    from nebula.config import read_settings

    root, _ = resolve(archive)
    current = read_settings(root, apply_env=False)
    try:
        proposed = _coerced_asset_settings(current, changes)
    except AssetSettingsError as e:
        return {"ok": False, "error": str(e)}

    moved, auto_total = [], 0
    for asset_id in list_assets(root):
        try:
            meta = read_asset(root, asset_id)
        except AssetError:
            # An unreadable record is `check`'s problem to report, not a
            # reason to refuse a settings preview.
            continue
        if (meta.policy or "auto") != "auto":
            continue
        auto_total += 1
        was = meta.effective_policy(current)
        now = meta.effective_policy(proposed)
        if was != now:
            moved.append({
                "id": meta.id,
                "name": meta.name,
                "bytes": meta.size or 0,
                "size_human": _human_size(meta.size or 0),
                "from": was,
                "to": now,
            })
    moved.sort(key=lambda m: -m["bytes"])
    return {"ok": True, "auto_assets": auto_total, "changed": len(moved),
            "moved": moved}


def set_asset_settings(archive, changes: dict) -> dict:
    """Update the archive's asset defaults.

    Only known keys are applied and every one is validated, so a stale
    front-end cannot write nonsense into archive.yaml.
    """
    from nebula.config import read_settings, write_settings

    root, _ = resolve(archive)
    settings = read_settings(root, apply_env=False)
    write_settings(root, _coerced_asset_settings(settings, changes))
    return asset_settings(root)
