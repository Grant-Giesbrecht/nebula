"""
Moving data between archives: export, merge and adopt.

Three operations, one shape. Each first builds a **plan** -- what would be
copied, what would be renamed, what would dangle -- and only then acts. The
plan is the dry run, the GUI confirmation dialog and the CLI's ``--dry-run``
all at once, which is the only honest way to offer an operation whose cost
is measured in gigabytes.

    export  standard -> fragment   ids preserved, so a citation stays valid
    merge   intake   -> standard   ids reallocated, and what they became is
                                   recorded on both sides
    adopt   fragment -> standard   ids reallocated; the source is untouched

What makes any of this more than copying files is that *references travel*.
A session id is only meaningful inside its archive, so whenever ids change,
every ref naming them has to change with them:

  * ``derived_from`` in sidecars (bare refs are same-session and need
    nothing; explicit ones must be rewritten)
  * ``related_runs`` in session.yaml
  * the session's own run_id, and its folder name
  * collection entries

A half-rewritten archive is worse than a failed transfer, so a merge marks
each source session as it lands (``merged_to``), skips anything already
marked when re-run, and never deletes anything: pruning is a separate,
verified step.
"""

from __future__ import annotations

import datetime
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from nebula import codestore
from nebula.config import (
    ARCHIVE_CONFIG_FILE,
    ArchiveSettings,
    KIND_PREFIX,
    archive_identity,
    read_settings,
    write_settings,
)
from nebula.refs import Ref, format_ref, parse_ref
from nebula.registry import get_registry
from nebula.session import (
    DATA_DIR,
    _allocate_new_id,
    _find_session_dir,
    id_year,
    year_dir,
)
from nebula.sidecar import (
    SESSION_FILE,
    SIDECAR_SUFFIX,
    read_session_yaml,
    read_sidecar,
    sha256_file,
    write_session_yaml,
    write_sidecar,
)

#: Timestamped, so an intake archive's name is a coordinate that survives in
#: a paper notebook: "saved in intake_2026_07_31_190230/I-26-0001" resolves
#: to a permanent id forever, because the merge records the pair.
INTAKE_NAME_FORMAT = "intake_%Y_%m_%d_%H%M%S"


class TransferError(RuntimeError):
    """A transfer that must not proceed."""


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _resolve(archive) -> Tuple[Path, str]:
    """Resolve an archive leniently, as the CLI and the Navigator both do.

    resolve_archive() is deliberately strict for the *library* API -- a
    typo'd name there would otherwise create a session folder under a
    relative path. Transfers are different: every argument names an archive
    that already exists, and both front ends hand over literal paths. Being
    strict here just turned a path into "unknown archive".
    """
    if isinstance(archive, str):
        cfg = get_registry().try_get(archive)
        if cfg is not None:
            return Path(cfg.root), archive
        return Path(archive), archive
    return Path(archive), str(archive)


# ---------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------

@dataclass
class SessionPlan:
    run_id: str                     # id in the source
    new_run_id: str                 # id in the destination (== run_id for export)
    path: Path
    files: List[str] = field(default_factory=list)
    bytes: int = 0
    partial: bool = False           # only some of the session's artefacts
    omitted: int = 0
    foreign: bool = False           # came from a fragment, not from this archive
    archive: str = ""               # which archive it came from
    note: str = ""


@dataclass
class TransferPlan:
    op: str
    source: str
    source_root: Path
    dest: str
    dest_root: Optional[Path]
    sessions: List[SessionPlan] = field(default_factory=list)
    manifests: List[str] = field(default_factory=list)
    collections: List[dict] = field(default_factory=list)
    dangling: List[dict] = field(default_factory=list)
    skipped: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return sum(s.bytes for s in self.sessions)

    @property
    def foreign_bytes(self) -> int:
        return sum(s.bytes for s in self.sessions if s.foreign)

    def to_dict(self) -> dict:
        return {
            "op": self.op, "source": self.source, "source_root": str(self.source_root),
            "dest": self.dest, "dest_root": str(self.dest_root) if self.dest_root else None,
            "sessions": [
                {"run_id": s.run_id, "new_run_id": s.new_run_id, "path": str(s.path),
                 "files": list(s.files), "bytes": s.bytes, "partial": s.partial,
                 "omitted": s.omitted, "foreign": s.foreign, "archive": s.archive,
                 "note": s.note}
                for s in self.sessions
            ],
            "manifests": list(self.manifests),
            "collections": list(self.collections),
            "dangling": list(self.dangling),
            "skipped": list(self.skipped),
            "warnings": list(self.warnings),
            "n_sessions": len(self.sessions),
            "n_files": sum(len(s.files) for s in self.sessions),
            "bytes": self.bytes,
            "foreign_bytes": self.foreign_bytes,
            "renames": {s.run_id: s.new_run_id for s in self.sessions
                        if s.run_id != s.new_run_id},
        }


# ---------------------------------------------------------------------
# Selection and closure
# ---------------------------------------------------------------------

def _session_files(session_dir: Path) -> List[str]:
    """Artefacts in a session (not sidecars, not session.yaml)."""
    out = []
    for entry in sorted(session_dir.iterdir()):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        if entry.name == SESSION_FILE or entry.name.endswith(SIDECAR_SUFFIX):
            continue
        if entry.name == "annotations.yaml":
            continue
        out.append(entry.name)
    return out


def _expand_selection(archive_root: Path, label: str, *, sessions=None,
                      refs=None, collection=None) -> Dict[str, Set[str]]:
    """Turn a user's selection into {run_id: {filenames}}.

    An empty filename set means "the whole session". Collections are the
    intended way in: a collection is already a curated set of sessions,
    files and nested folders, so exporting one needs no new selection
    mechanism.
    """
    picked: Dict[str, Set[str]] = {}

    def add(run_id: str, filename: Optional[str]) -> None:
        if run_id not in picked:
            picked[run_id] = set()
        if filename is None:
            picked[run_id] = set()          # whole session wins over a subset
        elif picked[run_id] or run_id not in picked or picked[run_id] != set():
            picked[run_id].add(filename)
        else:
            picked[run_id].add(filename)

    whole: Set[str] = set()
    for run_id in sessions or []:
        whole.add(run_id)
        picked.setdefault(run_id, set())

    for text in refs or []:
        ref = parse_ref(text) if isinstance(text, str) else text
        if ref.session is None:
            raise TransferError(f"{text!r} does not name a session")
        if ref.file is None:
            whole.add(ref.session)
            picked.setdefault(ref.session, set())
        else:
            picked.setdefault(ref.session, set()).add(ref.file)

    if collection:
        from nebula import collection as collection_mod

        # tree() nests children in place, so walk it rather than expecting a
        # flat list -- a collection of collections is the normal case.
        for entry in _walk_collection(
                collection_mod.tree(archive_root, collection)):
            ref_text = entry.get("ref")
            if not ref_text:
                continue
            try:
                ref = parse_ref(ref_text)
            except ValueError:
                continue
            if ref.archive and ref.archive != label:
                continue        # foreign refs are pulled in by closure, not here
            if ref.session is None:
                continue
            if ref.file is None:
                whole.add(ref.session)
                picked.setdefault(ref.session, set())
            else:
                picked.setdefault(ref.session, set()).add(ref.file)

    for run_id in whole:
        picked[run_id] = set()
    return picked


def _walk_collection(node: dict):
    """Every entry in a collection tree, children included."""
    for entry in node.get("entries") or []:
        yield entry
        child = entry.get("child")
        if isinstance(child, dict):
            yield from _walk_collection(child)


def _upstream_closure(archive_root: Path, label: str,
                      picked: Dict[str, Set[str]]) -> Tuple[Dict[str, Set[str]], List[dict]]:
    """Pull in everything the selection was derived from.

    A figure without its raw data is a picture, not a reference, so lineage
    is included by default. Refs into *other* archives are returned
    separately: whether to embed a colleague's data is a decision about
    permission, not about bytes.
    """
    foreign: List[dict] = []
    frontier = [(r, f) for r, files in picked.items()
                for f in (files or _files_in(archive_root, r))]
    seen: Set[Tuple[str, str]] = set()

    while frontier:
        run_id, filename = frontier.pop()
        if (run_id, filename) in seen:
            continue
        seen.add((run_id, filename))
        session_dir = _find_session_dir(archive_root, run_id)
        if session_dir is None:
            continue
        art = session_dir / filename
        try:
            meta = read_sidecar(art)
        except Exception:       # noqa: BLE001 -- no sidecar, nothing to follow
            continue
        for ref in meta.derived_from_refs():
            if ref.archive is not None and ref.archive != label:
                foreign.append({"archive": ref.archive, "user": ref.user,
                                "session": ref.session, "file": ref.file,
                                "from": f"{run_id}/{filename}"})
                continue
            target_run = ref.session or run_id
            if ref.file is None:
                picked.setdefault(target_run, set())
                continue
            existing = picked.get(target_run)
            if existing is None:
                picked[target_run] = {ref.file}
            elif existing:          # a subset: widen it
                existing.add(ref.file)
            # an empty set means the whole session is already included
            frontier.append((target_run, ref.file))
    return picked, foreign


def _files_in(archive_root: Path, run_id: str) -> List[str]:
    session_dir = _find_session_dir(archive_root, run_id)
    return _session_files(session_dir) if session_dir else []


def _manifests_for(archive_root: Path, picked: Dict[str, Set[str]]) -> List[str]:
    """Code-store manifests behind the selected artefacts.

    Content-addressed, so this is a set of hashes: copying them into another
    archive can never conflict, because identical ids mean identical bytes.
    """
    out: Set[str] = set()
    for run_id, files in picked.items():
        session_dir = _find_session_dir(archive_root, run_id)
        if session_dir is None:
            continue
        for name in (files or _session_files(session_dir)):
            try:
                meta = read_sidecar(session_dir / name)
            except Exception:   # noqa: BLE001
                continue
            if meta.produced_by and meta.produced_by.code:
                out.add(meta.produced_by.code)
    return sorted(out)


# ---------------------------------------------------------------------
# Export: standard -> fragment
# ---------------------------------------------------------------------

def plan_export(archive, dest, *, sessions=None, refs=None, collection=None,
                include_foreign: bool = True) -> TransferPlan:
    """What an export would copy, without copying it."""
    archive_root, _ = _resolve(archive)
    ident = archive_identity(archive_root)
    label = ident["name"]

    if ident["kind"] == "intake":
        raise TransferError(
            "an intake archive's ids are provisional (I-...), so a fragment cut "
            "from one could not be cited. Merge it into a standard archive first.")

    picked = _expand_selection(archive_root, label, sessions=sessions,
                               refs=refs, collection=collection)
    if not picked:
        raise TransferError("nothing selected to export")
    picked, foreign = _upstream_closure(archive_root, label, picked)

    plan = TransferPlan(op="export", source=label, source_root=archive_root,
                        dest=str(dest), dest_root=Path(dest))
    for run_id in sorted(picked):
        session_dir = _find_session_dir(archive_root, run_id)
        if session_dir is None:
            plan.dangling.append({"ref": run_id, "note": "session not found"})
            continue
        all_files = _session_files(session_dir)
        want = sorted(picked[run_id]) if picked[run_id] else all_files
        missing = [f for f in want if not (session_dir / f).is_file()]
        for f in missing:
            plan.dangling.append({"ref": f"{run_id}/{f}", "note": "file is missing"})
        want = [f for f in want if f not in missing]
        plan.sessions.append(SessionPlan(
            run_id=run_id, new_run_id=run_id,          # ids are preserved
            path=session_dir, files=want,
            bytes=sum((session_dir / f).stat().st_size for f in want),
            partial=len(want) < len(all_files),
            omitted=len(all_files) - len(want),
            archive=label,
        ))
    plan.manifests = _manifests_for(archive_root, picked)
    _plan_export_collections(plan, archive_root)

    for item in foreign:
        embedded = include_foreign and _add_foreign(plan, item)
        if not embedded:
            plan.dangling.append({
                "ref": format_ref(Ref(archive=item["archive"], session=item["session"],
                                      file=item["file"], user=item.get("user"))),
                "note": ("not included -- it belongs to another archive"
                         if not include_foreign else
                         f"archive {item['archive']!r} is not available here"),
                "from": item["from"],
            })
    return plan


def _plan_export_collections(plan: TransferPlan, archive_root: Path) -> None:
    """Collections that describe any of what is being exported.

    The grouping is part of what you are sending -- "these twelve files are
    the paper" is information the recipient cannot reconstruct. Entries
    naming things left out are dropped rather than shipped dangling, and a
    collection with nothing left is not shipped at all.
    """
    from nebula import collection as collection_mod

    included = {(s.run_id, f) for s in plan.sessions for f in s.files}
    sessions = {s.run_id for s in plan.sessions}
    for name in collection_mod.list_names(archive_root):
        coll = collection_mod.read(archive_root, name)
        if coll is None:
            continue
        kept, dropped = [], 0
        for entry in coll.entries:
            try:
                ref = entry.parsed
            except ValueError:
                continue
            if getattr(ref, "collection", None):
                kept.append(entry.ref)          # resolved when the child ships
            elif ref.file is None and ref.session in sessions:
                kept.append(entry.ref)
            elif (ref.session, ref.file) in included:
                kept.append(entry.ref)
            else:
                dropped += 1
        if kept:
            plan.collections.append({"name": name, "new_name": name, "renamed": False,
                                     "entries": kept, "dropped": dropped})


def _add_foreign(plan: TransferPlan, item: dict) -> bool:
    """Include a session from a fragment we hold, so the export stands alone.

    Whether this is appropriate is a question about permission rather than
    bytes, which is why the caller decides and the plan always reports it
    separately.
    """
    cfg = get_registry().find(item["archive"], item.get("user"))
    if cfg is None or not Path(cfg.root).is_dir():
        return False
    other_root = Path(cfg.root)
    session_dir = _find_session_dir(other_root, item["session"])
    if session_dir is None:
        return False
    if any(s.run_id == item["session"] and s.archive == item["archive"]
           for s in plan.sessions):
        return True
    files = ([item["file"]] if item["file"] else _session_files(session_dir))
    files = [f for f in files if (session_dir / f).is_file()]
    ident = archive_identity(other_root)
    plan.sessions.append(SessionPlan(
        run_id=item["session"], new_run_id=item["session"], path=session_dir,
        files=files, bytes=sum((session_dir / f).stat().st_size for f in files),
        partial=len(files) < len(_session_files(session_dir)),
        omitted=len(_session_files(session_dir)) - len(files),
        foreign=True, archive=ident["name"],
        note=f"from {ident['user'] or 'unknown'}/{ident['name']}",
    ))
    return True


def export(archive, dest, *, sessions=None, refs=None, collection=None,
           include_foreign: bool = True, plan: Optional[TransferPlan] = None,
           title: str = "") -> TransferPlan:
    """Write a fragment: an excerpt of this archive that keeps its ids."""
    plan = plan or plan_export(archive, dest, sessions=sessions, refs=refs,
                               collection=collection, include_foreign=include_foreign)
    dest_root = Path(dest)
    if dest_root.exists() and any(dest_root.iterdir()):
        raise TransferError(f"{dest_root} already exists and is not empty")
    dest_root.mkdir(parents=True, exist_ok=True)

    ident = archive_identity(Path(plan.source_root))
    settings = read_settings(plan.source_root, apply_env=False)
    settings.kind = "fragment"
    settings.name = ident["name"]
    settings.user = ident["user"]
    settings.created = _now()
    settings.merged_at = settings.merged_to = ""
    write_settings(dest_root, settings)

    # Foreign sessions keep their own archive's identity, so they are written
    # into a nested fragment rather than pretending to be ours.
    for sp in plan.sessions:
        if sp.foreign:
            root = dest_root / "fragments" / sp.archive
            _ensure_nested_fragment(root, sp)
        else:
            root = dest_root
        _copy_session(sp, root, keep_id=True)

    _copy_code(plan, Path(plan.source_root), dest_root)
    _write_export_collections(plan, dest_root)
    for sp in plan.sessions:
        if sp.foreign:
            src = _foreign_root(sp)
            if src is not None:
                for manifest in _manifests_for(src, {sp.run_id: set(sp.files)}):
                    codestore.copy_manifest(src, dest_root / "fragments" / sp.archive,
                                            manifest)
    return plan


def _write_export_collections(plan: TransferPlan, dest_root: Path) -> None:
    from nebula import collection as collection_mod

    for item in plan.collections:
        entries = [collection_mod.Entry(ref=r) for r in item.get("entries", [])]
        if not entries:
            continue
        collection_mod.write(dest_root, collection_mod.Collection(
            name=item["new_name"], entries=entries))


def _foreign_root(sp: SessionPlan) -> Optional[Path]:
    cfg = get_registry().try_get(sp.archive)
    return Path(cfg.root) if cfg else None


def _ensure_nested_fragment(root: Path, sp: SessionPlan) -> None:
    if (root / ARCHIVE_CONFIG_FILE).is_file():
        return
    root.mkdir(parents=True, exist_ok=True)
    src = _foreign_root(sp)
    ident = archive_identity(src) if src else {"name": sp.archive, "user": ""}
    write_settings(root, ArchiveSettings(
        kind="fragment", name=ident["name"], user=ident.get("user") or "",
        created=_now()))


def _copy_session(sp: SessionPlan, dest_root: Path, *, keep_id: bool,
                  verify: bool = True) -> Path:
    """Copy one session's selected artefacts, their sidecars, session.yaml
    and annotations. Checksums are verified as the bytes cross, because this
    is the one moment data leaves the machine that wrote it."""
    year = id_year(sp.new_run_id) or datetime.datetime.now().year
    target = year_dir(dest_root, year) / sp.new_run_id
    target.mkdir(parents=True, exist_ok=True)

    for name in sp.files:
        src = sp.path / name
        shutil.copy2(src, target / name)
        if verify and sha256_file(src) != sha256_file(target / name):
            raise TransferError(f"copy of {sp.run_id}/{name} does not match the original")
        sidecar = sp.path / f"{name}{SIDECAR_SUFFIX}"
        if sidecar.is_file():
            shutil.copy2(sidecar, target / sidecar.name)

    for extra in (SESSION_FILE, "annotations.yaml"):
        if (sp.path / extra).is_file():
            shutil.copy2(sp.path / extra, target / extra)
    return target


# ---------------------------------------------------------------------
# Merge: intake -> standard
# ---------------------------------------------------------------------

def plan_merge(source, dest) -> TransferPlan:
    """What merging an intake archive would do, including the rename map."""
    src_root, _ = _resolve(source)
    dst_root, _ = _resolve(dest)
    src_ident = archive_identity(src_root)
    dst_ident = archive_identity(dst_root)

    if src_ident["kind"] == "fragment":
        raise TransferError(
            f"{src_ident['name']} is a fragment: its ids belong to "
            f"{src_ident['user'] or 'someone else'} and must keep their names. "
            f"Register it and reference it, or use 'nebula adopt' to take a copy.")
    if dst_ident["kind"] != "standard":
        raise TransferError(
            f"can only merge into a standard archive; {dst_ident['name']} is a "
            f"{dst_ident['kind']} archive")
    if src_root.resolve() == dst_root.resolve():
        raise TransferError("source and destination are the same archive")

    plan = TransferPlan(op="merge", source=src_ident["name"], source_root=src_root,
                        dest=dst_ident["name"], dest_root=dst_root)
    if src_ident["locked"]:
        plan.warnings.append(
            f"{src_ident['name']} was already merged into {src_ident['merged_to']} "
            f"on {src_ident['merged_at']}; only sessions added since then are new")

    from nebula.index import _iter_session_dirs

    taken: Dict[int, Set[int]] = {}
    for session_dir in _iter_session_dirs(src_root):
        try:
            meta = read_session_yaml(session_dir)
        except Exception:       # noqa: BLE001
            plan.skipped.append({"run_id": session_dir.name,
                                 "note": "unreadable session.yaml"})
            continue
        already = (meta.extra or {}).get("merged_to") if hasattr(meta, "extra") else None
        already = already or _merged_marker(session_dir)
        if already:
            plan.skipped.append({"run_id": meta.run_id,
                                 "note": f"already merged as {already}"})
            continue
        if meta.status == "open":
            plan.skipped.append({"run_id": meta.run_id,
                                 "note": "still open -- close it before merging"})
            continue

        # Keep the year the work happened in, not the year it was filed.
        year = id_year(meta.run_id) or datetime.datetime.now().year
        new_id = _next_free(dst_root, year, taken)
        files = _session_files(session_dir)
        plan.sessions.append(SessionPlan(
            run_id=meta.run_id, new_run_id=new_id, path=session_dir, files=files,
            bytes=sum((session_dir / f).stat().st_size for f in files),
            archive=src_ident["name"],
        ))

    _warn_about_seals(plan, dst_root)
    plan.manifests = _manifests_for(src_root, {s.run_id: set() for s in plan.sessions})
    _plan_collections(plan, src_root, dst_root)
    _warn_about_strays(plan, src_root)
    return plan


def _plan_collections(plan: TransferPlan, src_root: Path, dst_root: Path) -> None:
    """Collections in the source come too, with their refs remapped.

    A name already in use is *not* merged into: someone's curated set should
    not silently gain entries, so the incoming one is suffixed with the
    source archive's name -- which for an intake is a timestamp, and so is
    unique and traceable.
    """
    from nebula import collection as collection_mod

    existing = set(collection_mod.list_names(dst_root))
    for name in collection_mod.list_names(src_root):
        new_name = name
        if new_name in existing:
            new_name = collection_mod.slugify(f"{name}-{plan.source}")
            i = 2
            while new_name in existing:
                new_name = collection_mod.slugify(f"{name}-{plan.source}-{i}")
                i += 1
        existing.add(new_name)
        plan.collections.append({"name": name, "new_name": new_name,
                                 "renamed": new_name != name})


def _warn_about_strays(plan: TransferPlan, src_root: Path) -> None:
    """Fragments sitting inside the source aren't ours to file: they belong
    to whoever wrote them, under NEBULA_HOME/fragments."""
    nested = _nested_fragments(src_root)
    if nested:
        plan.warnings.append(
            f"{len(nested)} fragment(s) inside {plan.source} are not merged -- they "
            f"belong to their own authors. Install them with 'nebula receive "
            f"{src_root}' instead.")


def _merged_marker(session_dir: Path) -> Optional[str]:
    """Whether this session has already been merged, from its session.yaml.
    Kept in `history` so it survives without a schema change."""
    try:
        meta = read_session_yaml(session_dir)
    except Exception:           # noqa: BLE001
        return None
    for entry in reversed(meta.history or []):
        if entry.get("action") == "merged" and entry.get("note"):
            return entry["note"]
    return None


def _next_free(dst_root: Path, year: int, taken: Dict[int, Set[int]]) -> str:
    """Allocate the next id for a year, remembering what this plan already
    handed out -- the folders don't exist yet, so the archive can't tell us."""
    from nebula.session import _existing_ids

    used = taken.setdefault(year, set(_existing_ids(dst_root, year)))
    n = (max(used) + 1) if used else 1
    used.add(n)
    return f"{KIND_PREFIX['standard']}{year % 100:02d}-{n:04d}"


def _warn_about_seals(plan: TransferPlan, dst_root: Path) -> None:
    """A sealed year that is about to gain sessions is a contradiction: the
    seal says "finished", and freshness sweeps skip it."""
    from nebula import index as index_mod

    years = {id_year(s.new_run_id) for s in plan.sessions}
    for year in sorted(y for y in years if y):
        if index_mod.read_seal(dst_root, str(year)):
            plan.warnings.append(
                f"{year} is sealed in {plan.dest}; merging into it would leave the "
                f"seal wrong. Run 'nebula unseal {plan.dest} {year}' first.")


def merge(source, dest, *, plan: Optional[TransferPlan] = None,
          verify: bool = True, lock: bool = True) -> TransferPlan:
    """Merge an intake archive into a standard one, renaming its sessions.

    Idempotent: every source session is marked as it lands, and a re-run
    skips what is already marked. Nothing is deleted -- see :func:`prune`.
    """
    plan = plan or plan_merge(source, dest)
    dst_root = Path(plan.dest_root)
    src_root = Path(plan.source_root)

    for warning in plan.warnings:
        if "is sealed" in warning:
            raise TransferError(warning)

    rename = {s.run_id: s.new_run_id for s in plan.sessions}
    for sp in plan.sessions:
        target = _copy_session(sp, dst_root, keep_id=False, verify=verify)
        _rewrite_session(target, sp.new_run_id, rename,
                         source_label=plan.source, dest_label=plan.dest)
        _record_import(target, sp, plan)
        _mark_merged(sp.path, f"{plan.dest}|{sp.new_run_id}")

    _copy_code(plan, src_root, dst_root)
    _copy_collections(plan, src_root, dst_root, rename)

    if lock and plan.sessions:
        settings = read_settings(src_root, apply_env=False)
        settings.merged_at = _now()
        settings.merged_to = plan.dest
        write_settings(src_root, settings)

    _reindex(dst_root)
    return plan


def _rewrite_session(session_dir: Path, new_run_id: str, rename: Dict[str, str],
                     *, source_label: str, dest_label: str) -> None:
    """Point a copied session's references at their new ids.

    Two rewrites happen here. Ids that changed are remapped. And a ref that
    named the *destination* archive explicitly -- an intake on a lab machine
    referring back to the main archive -- collapses to a local ref, since
    after the merge it is local.
    """
    def fix(ref: Ref) -> Ref:
        archive = ref.archive
        if archive is not None and archive == dest_label:
            archive = None                      # now the same archive
        session = ref.session
        if archive is None and session in rename:
            session = rename[session]
        return Ref(archive=archive, session=session, file=ref.file, user=ref.user)

    meta = read_session_yaml(session_dir)
    meta.run_id = new_run_id
    if meta.related_runs:
        meta.related_runs = [_ref_dict(fix(r)) for r in meta.related_run_refs()]
    write_session_yaml(session_dir, meta)

    for sidecar in sorted(session_dir.glob(f"*{SIDECAR_SUFFIX}")):
        artifact = session_dir / sidecar.name[: -len(SIDECAR_SUFFIX)]
        try:
            sc = read_sidecar(artifact)
        except Exception:       # noqa: BLE001
            continue
        if not sc.derived_from:
            continue
        sc.derived_from = [_ref_dict(fix(r)) for r in sc.derived_from_refs()]
        write_sidecar(artifact, sc)


def _ref_dict(ref: Ref) -> dict:
    out = {"archive": ref.archive, "session": ref.session, "file": ref.file}
    if ref.user:
        out["user"] = ref.user
    return out


def _record_import(session_dir: Path, sp: SessionPlan, plan: TransferPlan) -> None:
    """Record on the destination session where it came from.

    This is what makes "saved in intake_2026_07_31_190230/I-26-0001" resolve
    forever: the id changed, but the coordinate it had is kept.
    """
    from nebula import identity

    meta = read_session_yaml(session_dir)
    meta.history = list(meta.history or [])
    meta.history.append({
        "action": "merged", "at": _now(), "by": identity.get_user() or None,
        "file": None,
        "note": f"{plan.op} from {plan.source}/{sp.run_id}",
    })
    write_session_yaml(session_dir, meta)


def _mark_merged(session_dir: Path, destination: str) -> None:
    """Mark a source session so a re-run skips it."""
    meta = read_session_yaml(session_dir)
    meta.history = list(meta.history or [])
    meta.history.append({"action": "merged", "at": _now(), "by": None,
                         "file": None, "note": destination})
    write_session_yaml(session_dir, meta)


def _copy_code(plan: TransferPlan, src_root: Path, dst_root: Path) -> dict:
    """Carry the captured source across.

    Content addressing makes this the easy half: a blob already in the
    destination is *by definition* the same bytes, so it is skipped rather
    than compared or renamed. The sidecar's `produced_by.code` needs no
    rewriting either -- unlike a session id, a code id means the same thing
    in every archive.
    """
    got = {"manifests": 0, "blobs_copied": 0, "blobs_present": 0, "missing": []}
    for digest in plan.manifests:
        res = codestore.copy_manifest(src_root, dst_root, digest)
        got["manifests"] += 1
        got["blobs_copied"] += res["blobs_copied"]
        got["blobs_present"] += res["blobs_present"]
        got["missing"].extend(res["missing"])
    if got["missing"]:
        plan.warnings.append(
            f"{len(got['missing'])} code blob(s) referenced by these sessions are "
            f"missing from {plan.source}, so those snapshots arrive incomplete; "
            f"'nebula check' in the destination will list them.")
    return got


def _copy_collections(plan: TransferPlan, src_root: Path, dst_root: Path,
                      rename: Dict[str, str]) -> None:
    """Copy the source's collections, remapping every ref they hold.

    A collection is a list of references, so it is exactly the thing that
    breaks when ids change: left alone, every entry would point at an id
    that no longer exists.
    """
    from nebula import collection as collection_mod

    coll_rename = {c["name"]: c["new_name"] for c in plan.collections}
    for item in plan.collections:
        src = collection_mod.read(src_root, item["name"])
        if src is None:
            continue
        entries = []
        for entry in src.entries:
            try:
                ref = entry.parsed
            except ValueError:
                continue                        # a ref that never parsed
            entries.append(collection_mod.Entry(
                ref=format_ref(_remap_ref(ref, rename, coll_rename, plan.dest)),
                note=entry.note))
        collection_mod.write(dst_root, collection_mod.Collection(
            name=item["new_name"], title=src.title,
            description=(src.description or "")
            + (f"\n(from {plan.source})" if item["renamed"] else ""),
            entries=entries))


def _remap_ref(ref: Ref, rename: Dict[str, str], coll_rename: Dict[str, str],
               dest_label: str) -> Ref:
    """One ref, moved to the destination's names."""
    archive = ref.archive
    if archive is not None and archive == dest_label:
        archive = None
    if getattr(ref, "collection", None):
        return Ref(archive=archive, user=ref.user,
                   collection=coll_rename.get(ref.collection, ref.collection))
    session = ref.session
    if archive is None and session in rename:
        session = rename[session]
    return Ref(archive=archive, session=session, file=ref.file, user=ref.user)


def _reindex(archive_root: Path) -> None:
    from nebula import index as index_mod

    try:
        if index_mod.index_path_for(archive_root).is_file():
            index_mod.ensure_fresh(archive_root)
    except Exception:           # noqa: BLE001 -- the index is a cache
        pass


# ---------------------------------------------------------------------
# Adopt: fragment -> standard
# ---------------------------------------------------------------------

def plan_adopt(source, dest, *, sessions=None) -> TransferPlan:
    """What adopting sessions out of a fragment would do."""
    src_root, _ = _resolve(source)
    dst_root, _ = _resolve(dest)
    src_ident = archive_identity(src_root)
    dst_ident = archive_identity(dst_root)
    if dst_ident["kind"] != "standard":
        raise TransferError(
            f"can only adopt into a standard archive; {dst_ident['name']} is a "
            f"{dst_ident['kind']} archive")

    plan = TransferPlan(op="adopt", source=src_ident["name"], source_root=src_root,
                        dest=dst_ident["name"], dest_root=dst_root)
    from nebula.index import _iter_session_dirs

    taken: Dict[int, Set[int]] = {}
    wanted = set(sessions or [])
    for session_dir in _iter_session_dirs(src_root):
        try:
            meta = read_session_yaml(session_dir)
        except Exception:       # noqa: BLE001
            continue
        if wanted and meta.run_id not in wanted:
            continue
        origin = f"nebula://{src_ident['user'] or 'unknown'}/{src_ident['name']}/{meta.run_id}"
        already = _already_adopted(dst_root, origin)
        if already:
            plan.skipped.append({"run_id": meta.run_id,
                                 "note": f"already adopted as {already}"})
            continue
        year = id_year(meta.run_id) or datetime.datetime.now().year
        files = _session_files(session_dir)
        plan.sessions.append(SessionPlan(
            run_id=meta.run_id, new_run_id=_next_free(dst_root, year, taken),
            path=session_dir, files=files,
            bytes=sum((session_dir / f).stat().st_size for f in files),
            foreign=True, archive=src_ident["name"],
            note=f"from {src_ident['user'] or 'unknown'}/{src_ident['name']}",
        ))
    _warn_about_seals(plan, dst_root)
    plan.manifests = _manifests_for(src_root, {s.run_id: set() for s in plan.sessions})
    if plan.sessions:
        _plan_collections(plan, src_root, dst_root)
    return plan


def _already_adopted(dst_root: Path, origin: str) -> Optional[str]:
    """Whether this exact source session is already here, by the origin
    recorded at adoption time. Cheap duplicate detection that a hand-copy
    could never offer."""
    from nebula.index import _iter_session_dirs

    for session_dir in _iter_session_dirs(dst_root):
        try:
            meta = read_session_yaml(session_dir)
        except Exception:       # noqa: BLE001
            continue
        for entry in meta.history or []:
            if entry.get("action") == "adopted" and origin in (entry.get("note") or ""):
                return meta.run_id
    return None


def adopt(source, dest, *, sessions=None, plan: Optional[TransferPlan] = None,
          verify: bool = True) -> TransferPlan:
    """Take a copy of sessions from a fragment into a standard archive.

    The fragment is left exactly as it was -- it is someone else's excerpt,
    and its ids are the ones they cited.
    """
    plan = plan or plan_adopt(source, dest, sessions=sessions)
    dst_root = Path(plan.dest_root)
    src_ident = archive_identity(Path(plan.source_root))
    rename = {s.run_id: s.new_run_id for s in plan.sessions}

    for warning in plan.warnings:
        if "is sealed" in warning:
            raise TransferError(warning)

    for sp in plan.sessions:
        target = _copy_session(sp, dst_root, keep_id=False, verify=verify)
        # Refs to sessions that came along are remapped; refs to the rest of
        # the source archive keep pointing there, as full URIs.
        _rewrite_session(target, sp.new_run_id, rename,
                         source_label=plan.source, dest_label=plan.dest)
        _record_adoption(target, sp, src_ident)

    _copy_code(plan, Path(plan.source_root), dst_root)
    _copy_collections(plan, Path(plan.source_root), dst_root, rename)
    _reindex(dst_root)
    return plan


def _record_adoption(session_dir: Path, sp: SessionPlan, src_ident: dict) -> None:
    from nebula import identity

    origin = (f"nebula://{src_ident['user'] or 'unknown'}/{src_ident['name']}/"
              f"{sp.run_id}")
    meta = read_session_yaml(session_dir)
    meta.history = list(meta.history or [])
    meta.history.append({"action": "adopted", "at": _now(),
                         "by": identity.get_user() or None, "file": None,
                         "note": f"adopted from {origin}"})
    write_session_yaml(session_dir, meta)


# ---------------------------------------------------------------------
# Receiving fragments
# ---------------------------------------------------------------------

def plan_receive(source, *, home=None) -> List[dict]:
    """Where an incoming fragment (and anything nested inside it) belongs.

    A fragment John sent may itself carry an excerpt of Jane's archive.
    Those are filed under *Jane*, not under John: two deliveries of the
    same source archive -- one via John, one via Bill -- should land in one
    place, because they are the same archive.
    """
    from nebula.registry import fragment_dir

    src = Path(source)
    if not (src / ARCHIVE_CONFIG_FILE).is_file():
        raise TransferError(f"{src} is not an archive (no {ARCHIVE_CONFIG_FILE})")

    out = []
    for root in [src] + sorted(_nested_fragments(src)):
        ident = archive_identity(root)
        if ident["kind"] != "fragment":
            raise TransferError(
                f"{root} is a {ident['kind']} archive, not a fragment; "
                f"'nebula register' it in place instead")
        dest = fragment_dir(ident["user"], ident["name"])
        if home is not None:
            dest = Path(home) / "fragments" / (ident["user"] or "unknown") / ident["name"]
        out.append({"source": str(root), "dest": str(dest), "name": ident["name"],
                    "user": ident["user"], "exists": dest.is_dir(),
                    "nested": root != src})
    return out


def _nested_fragments(root: Path) -> List[Path]:
    frags = root / "fragments"
    if not frags.is_dir():
        return []
    out = []
    for child in sorted(frags.iterdir()):
        if (child / ARCHIVE_CONFIG_FILE).is_file():
            out.append(child)
        elif child.is_dir():        # fragments/<user>/<archive>
            out.extend(p for p in sorted(child.iterdir())
                       if (p / ARCHIVE_CONFIG_FILE).is_file())
    return out


def receive(source, *, home=None, overwrite_foreign: bool = False,
            register: bool = True) -> dict:
    """File an incoming fragment where refs into it will resolve.

    Union rather than replace: sessions already present are compared by
    checksum. Identical content is skipped; *different* content is kept as
    it is and reported, because silently replacing data a colleague may
    already have cited is the worst outcome available here.
    """
    plans = plan_receive(source, home=home)
    result = {"installed": [], "conflicts": [], "skipped": 0, "added": 0}
    for item in plans:
        src, dest = Path(item["source"]), Path(item["dest"])
        dest.mkdir(parents=True, exist_ok=True)
        if not (dest / ARCHIVE_CONFIG_FILE).is_file():
            shutil.copy2(src / ARCHIVE_CONFIG_FILE, dest / ARCHIVE_CONFIG_FILE)

        from nebula.index import _iter_session_dirs

        for session_dir in _iter_session_dirs(src):
            year = session_dir.parent.name
            target = dest / DATA_DIR / year / session_dir.name
            if target.is_dir():
                same, differing = _compare_sessions(session_dir, target)
                if differing and not overwrite_foreign:
                    result["conflicts"].append({
                        "archive": item["name"], "run_id": session_dir.name,
                        "files": differing,
                        "note": "already here with different content; kept what "
                                "was already installed",
                    })
                    result["skipped"] += 1
                    continue
                if not differing and same:
                    result["skipped"] += 1
                    continue
            target.mkdir(parents=True, exist_ok=True)
            for entry in sorted(session_dir.iterdir()):
                if entry.is_file():
                    shutil.copy2(entry, target / entry.name)
            result["added"] += 1

        for digest in _all_manifests(src):
            codestore.copy_manifest(src, dest, digest)

        if register:
            try:
                get_registry().register_archive(dest)
            except Exception:       # noqa: BLE001 -- filing it matters more
                pass
        result["installed"].append({"name": item["name"], "user": item["user"],
                                    "dest": str(dest), "nested": item["nested"]})
    return result


def _compare_sessions(src: Path, dest: Path) -> Tuple[List[str], List[str]]:
    """(identical, differing) artefact names between two copies of a session."""
    same, differ = [], []
    for name in _session_files(src):
        target = dest / name
        if not target.is_file():
            differ.append(name)
            continue
        if sha256_file(src / name) == sha256_file(target):
            same.append(name)
        else:
            differ.append(name)
    return same, differ


def _all_manifests(archive_root: Path) -> List[str]:
    from nebula.index import _iter_session_dirs

    picked = {d.name: set() for d in _iter_session_dirs(archive_root)}
    return _manifests_for(archive_root, picked)


# ---------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------

def unmerged_sessions(archive_root) -> List[str]:
    from nebula.index import _iter_session_dirs

    out = []
    for session_dir in _iter_session_dirs(Path(archive_root)):
        if not _merged_marker(session_dir):
            out.append(session_dir.name)
    return out


def prune(archive, *, force: bool = False) -> dict:
    """Delete a merged intake archive, once every session in it has landed.

    Verifies rather than trusting the lock: deletion is the irreversible
    step, and the whole point of an intake archive is that its contents
    exist nowhere else until they have been merged.
    """
    root, _ = _resolve(archive)
    ident = archive_identity(root)
    if ident["kind"] != "intake":
        raise TransferError(f"{ident['name']} is a {ident['kind']} archive; "
                            f"prune only removes merged intake archives")
    left = unmerged_sessions(root)
    if left and not force:
        raise TransferError(
            f"{len(left)} session(s) in {ident['name']} have not been merged "
            f"({', '.join(left[:5])}{'...' if len(left) > 5 else ''}). "
            f"Merge them first, or pass force=True to delete them anyway.")
    shutil.rmtree(root)
    return {"removed": str(root), "sessions": len(left), "forced": bool(left)}


def unlock(archive) -> dict:
    """Clear the post-merge lock on an intake archive."""
    root, _ = _resolve(archive)
    settings = read_settings(root, apply_env=False)
    was = {"merged_at": settings.merged_at, "merged_to": settings.merged_to}
    settings.merged_at = settings.merged_to = ""
    write_settings(root, settings)
    return {"root": str(root), "was": was}


def new_intake(parent, *, label: str = "", user: str = "") -> Path:
    """Create a timestamped intake archive under `parent`.

    The name is the coordinate: "intake_2026_07_31_190230/I-26-0001" written
    in a notebook resolves to a permanent id after the merge, because the
    merge records the pair.
    """
    from nebula import identity

    stamp = datetime.datetime.now().strftime(INTAKE_NAME_FORMAT)
    name = f"{stamp}_{label}" if label else stamp
    root = Path(parent) / name
    if root.exists():
        raise TransferError(f"{root} already exists")
    (root / DATA_DIR).mkdir(parents=True)
    write_settings(root, ArchiveSettings(
        kind="intake", name=name, user=user or identity.get_user() or "",
        created=_now()))
    return root


def init_archive(root, *, kind: str = "standard", name: str = "",
                 user: str = "") -> Path:
    """Create an archive that knows its own name, owner and kind."""
    from nebula import identity
    from nebula.config import KINDS

    if kind not in KINDS:
        raise TransferError(f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}")
    root = Path(root)
    if (root / ARCHIVE_CONFIG_FILE).is_file():
        raise TransferError(f"{root} is already an archive")
    (root / DATA_DIR).mkdir(parents=True, exist_ok=True)
    write_settings(root, ArchiveSettings(
        kind=kind, name=name or root.name,
        user=user or identity.get_user() or "", created=_now()))
    return root
