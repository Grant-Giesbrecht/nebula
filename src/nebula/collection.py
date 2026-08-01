"""
Collections: named, hand-curated sets of things, nestable like folders.

The organisational unit nebula deliberately does *not* get from its layout.
Sessions are time buckets (``data/<year>/S-<yy>-<nnnn>/``) and a ref is a
(session, filename) pair, so moving files into topic folders would silently
break provenance. A collection is the alternative: it *points* at things
instead of holding them, so a file can be in five collections at once, in
none, or in a colleague's, and nothing on disk moves.

One file per collection, so they can be added, removed, versioned and
mailed to a colleague individually::

    <archive>/collections/paper-2026.yaml

    name: paper-2026
    title: Figures for the 2026 paper
    description: |
      Everything that ends up in the manuscript.
    entries:
      - ref: S-26-0031/raw.tome
        note: the good warm-up run
      - ref: S-26-0034                       # a whole session
      - ref: collections/rp23d-campaign      # a nested collection
      - ref: nebula://kai@lab/shared/S-26-0002/cal.json

Entries are ordinary refs, so anything :mod:`nebula.refs` accepts works --
including full ``nebula://`` URIs into someone else's archive. Nesting is by
reference rather than containment: a sub-collection is a pointer, so the
same collection can appear in several parents, and cycles are possible and
therefore detected (see :func:`tree`).

Membership is stored in the collection, never on the member. Nothing here
touches a sidecar or session.yaml.
"""

from __future__ import annotations

import datetime
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from nebula.refs import COLLECTIONS_SEGMENT, Ref, format_ref, parse_ref

COLLECTIONS_DIR = "collections"
VERSION = 1

#: A collection's name is both its filename and a ref segment
#: ("collections/<name>", which is how nesting is stored), so it is the one
#: string that has to survive a filesystem and a ref parser. Spaces and most
#: punctuation are fine; these are not:
#:
#:   /  \  |   ref and path separators
#:   :        illegal in Windows filenames (and the classic Mac separator)
#:   * ? " < >  illegal in Windows filenames
#:
#: Windows also strips trailing dots and spaces, and reserves a handful of
#: device names, so those are rejected rather than silently mangled.
_ILLEGAL = set('/\\|:*?"<>')
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


class CollectionError(ValueError):
    """A collection that cannot be named, stored, or resolved."""


def slugify(display: str) -> str:
    """Coerce a string into a usable collection name.

    Names are permissive now, so this only has to remove what clean_name
    rejects -- it is a fallback for text arriving from elsewhere, not
    something the user should have to think about.
    """
    text = (display or "").strip()
    for ch in _ILLEGAL:
        text = text.replace(ch, "-")
    text = "".join(c for c in text if ord(c) >= 32).strip().rstrip(".")
    text = re.sub(r"\s{2,}", " ", text)
    if not text or text.split(".")[0].upper() in _RESERVED:
        text = f"{text}-collection".strip("-") if text else "collection"
    return text[:120]


def clean_name(name: str) -> str:
    """Validate a collection name, or explain exactly what is wrong with it."""
    value = (name or "").strip()
    if not value:
        raise CollectionError("a collection needs a name")
    bad = sorted(_ILLEGAL & set(value))
    if bad:
        raise CollectionError(
            f"collection names cannot contain {' '.join(repr(c) for c in bad)}: {name!r}")
    if any(ord(c) < 32 for c in value):
        raise CollectionError(f"collection names cannot contain control characters: {name!r}")
    if value.endswith("."):
        raise CollectionError(f"collection names cannot end with '.': {name!r}")
    if value.split(".")[0].upper() in _RESERVED:
        raise CollectionError(f"{value!r} is a reserved filename on Windows")
    if len(value) > 120:
        raise CollectionError("collection name is too long (120 characters max)")
    return value


@dataclass
class Entry:
    ref: str                       # as written, in either spelling
    note: str = ""

    @property
    def parsed(self) -> Ref:
        return parse_ref(self.ref)

    @property
    def kind(self) -> str:
        try:
            return self.parsed.kind
        except ValueError:
            return "invalid"


@dataclass
class Collection:
    name: str
    title: str = ""
    description: str = ""
    entries: List[Entry] = field(default_factory=list)
    created: Optional[str] = None
    modified: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"version": VERSION, "name": self.name}
        if self.title:
            out["title"] = self.title
        if self.description:
            out["description"] = self.description
        if self.created:
            out["created"] = self.created
        if self.modified:
            out["modified"] = self.modified
        out["entries"] = [
            ({"ref": e.ref, "note": e.note} if e.note else {"ref": e.ref})
            for e in self.entries
        ]
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any], *, name: str) -> "Collection":
        entries = []
        for raw in (d.get("entries") or []):
            if isinstance(raw, str):            # tolerate a bare list of refs
                entries.append(Entry(ref=raw))
            elif isinstance(raw, dict) and raw.get("ref"):
                entries.append(Entry(ref=str(raw["ref"]), note=str(raw.get("note") or "")))
        return cls(
            name=d.get("name") or name,
            title=str(d.get("title") or ""),
            description=str(d.get("description") or ""),
            entries=entries,
            created=d.get("created"),
            modified=d.get("modified"),
        )


# ---------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------

def collections_dir(archive_root) -> Path:
    return Path(archive_root) / COLLECTIONS_DIR


def path_for(archive_root, name: str) -> Path:
    return collections_dir(archive_root) / f"{clean_name(name)}.yaml"


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def list_names(archive_root) -> List[str]:
    d = collections_dir(archive_root)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.yaml") if not p.name.startswith("."))


def read(archive_root, name: str) -> Optional[Collection]:
    """A collection, or None if there isn't one by that name. A malformed
    file reads as an empty collection rather than raising: these are
    hand-editable, and one bad file must not break the whole listing."""
    path = path_for(archive_root, name)
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return Collection(name=clean_name(name))
    return Collection.from_dict(raw, name=clean_name(name))


def list_all(archive_root) -> List[Collection]:
    out = []
    for name in list_names(archive_root):
        got = read(archive_root, name)
        if got is not None:
            out.append(got)
    return out


def write(archive_root, coll: Collection) -> Path:
    coll.name = clean_name(coll.name)
    coll.created = coll.created or _now()
    coll.modified = _now()
    path = path_for(archive_root, coll.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{coll.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(coll.to_dict(), f, sort_keys=False, allow_unicode=True,
                           default_flow_style=False, width=88)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def create(archive_root, name: str, *, title: str = "",
           description: str = "") -> Collection:
    name = clean_name(name)
    if path_for(archive_root, name).exists():
        raise CollectionError(f"collection {name!r} already exists")
    coll = Collection(name=name, title=title, description=description)
    write(archive_root, coll)
    return coll


def delete(archive_root, name: str) -> bool:
    """Remove a collection file. Members are untouched -- a collection is
    only a pointer list, so deleting one never deletes data."""
    path = path_for(archive_root, name)
    try:
        path.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------
# editing
# ---------------------------------------------------------------------

def _require(archive_root, name: str) -> Collection:
    coll = read(archive_root, name)
    if coll is None:
        raise CollectionError(f"no collection {name!r} in this archive")
    return coll


def add(archive_root, name: str, ref: str, *, note: str = "") -> Collection:
    """Add one ref. Rejects a ref that doesn't parse, a duplicate, and a
    collection that would contain itself."""
    coll = _require(archive_root, name)
    parsed = parse_ref(ref)                    # raises ValueError if malformed
    canonical = format_ref(parsed)

    if parsed.kind == "collection" and parsed.archive is None and parsed.user is None:
        if parsed.collection == coll.name:
            raise CollectionError(f"a collection cannot contain itself ({coll.name!r})")
        if _reaches(archive_root, parsed.collection, coll.name):
            raise CollectionError(
                f"adding {parsed.collection!r} to {coll.name!r} would make a cycle "
                f"({parsed.collection!r} already contains {coll.name!r})")

    if any(format_ref(e.parsed) == canonical for e in coll.entries
           if e.kind != "invalid"):
        raise CollectionError(f"{canonical} is already in {coll.name!r}")

    coll.entries.append(Entry(ref=canonical, note=note))
    write(archive_root, coll)
    return coll


def remove(archive_root, name: str, ref: str) -> Collection:
    coll = _require(archive_root, name)
    try:
        canonical = format_ref(parse_ref(ref))
    except ValueError:
        canonical = ref.strip()
    before = len(coll.entries)
    coll.entries = [
        e for e in coll.entries
        if not (e.ref == canonical
                or (e.kind != "invalid" and format_ref(e.parsed) == canonical))
    ]
    if len(coll.entries) == before:
        raise CollectionError(f"{ref} is not in {coll.name!r}")
    write(archive_root, coll)
    return coll


def move(archive_root, src: str, dst: str, ref: str, *, note: str = "") -> None:
    """Move one entry from one collection to another.

    Adds first, then removes: if the add is refused (a cycle, a duplicate)
    nothing has been lost, and if the remove somehow fails the entry is in
    both places -- visible and fixable -- rather than gone. Collections are
    pointer lists, so neither step touches the thing being moved.
    """
    src, dst = clean_name(src), clean_name(dst)
    if src == dst:
        return
    note = note or next((e.note for e in _require(archive_root, src).entries
                         if e.kind != "invalid" and format_ref(e.parsed) == format_ref(parse_ref(ref))),
                        "")
    add(archive_root, dst, ref, note=note)
    try:
        remove(archive_root, src, ref)
    except CollectionError:
        pass          # already gone from the source; the add is what matters


def rename(archive_root, old: str, new: Optional[str] = None, *,
           title: Optional[str] = None) -> Collection:
    """Rename a collection and/or retitle it.

    Renaming the *name* also rewrites every `collections/<old>` ref in the
    archive, since nesting is by reference -- otherwise renaming a folder
    would orphan it from its parent. Retitling touches nothing else, which
    is why the GUI edits the title by default.
    """
    coll = _require(archive_root, old)
    if title is not None:
        coll.title = title.strip()

    if new and clean_name(new) != coll.name and title is None:
        # A real rename supersedes any leftover alias. Collections are a
        # scratch organisational tool -- carrying an old name around only
        # produces the "why does this have two names?" confusion.
        coll.title = ""

    if new and clean_name(new) != coll.name:
        new = clean_name(new)
        if path_for(archive_root, new).exists():
            raise CollectionError(f"collection {new!r} already exists")
        old_name = coll.name
        coll.name = new
        write(archive_root, coll)
        try:
            path_for(archive_root, old_name).unlink()
        except OSError:
            pass
        # Fix up every parent that pointed at the old name.
        for other in list_all(archive_root):
            changed = False
            for entry in other.entries:
                if entry.kind != "collection":
                    continue
                ref = entry.parsed
                if ref.archive or ref.user or ref.collection != old_name:
                    continue
                entry.ref = f"{COLLECTIONS_SEGMENT}/{new}"
                changed = True
            if changed:
                write(archive_root, other)
        return coll

    write(archive_root, coll)
    return coll


def _reaches(archive_root, start: str, target: str, _seen=None) -> bool:
    """True if collection `start` contains `target`, at any depth."""
    _seen = _seen or set()
    if start in _seen:
        return False
    _seen.add(start)
    coll = read(archive_root, start)
    if coll is None:
        return False
    for entry in coll.entries:
        if entry.kind != "collection":
            continue
        ref = entry.parsed
        if ref.archive or ref.user:            # another archive: not our cycle
            continue
        if ref.collection == target or _reaches(archive_root, ref.collection,
                                                target, _seen):
            return True
    return False


# ---------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------

def resolve_entry(archive_root, entry: Entry, *, local_user=None,
                  local_archive=None) -> dict:
    """Where an entry points and whether it is actually there.

    Never raises: a collection full of dangling refs is exactly what you
    want to *see*, and `nebula check` reports them.
    """
    out = {"ref": entry.ref, "note": entry.note, "kind": entry.kind,
           "exists": False, "resolved": True, "note_error": None,
           "path": None, "target": None, "foreign": False}
    try:
        ref = entry.parsed
    except ValueError as e:
        out.update({"kind": "invalid", "resolved": False, "note_error": str(e)})
        return out

    root = Path(archive_root)
    if ref.user or ref.archive:
        from nebula.identity import get_user
        from nebula.registry import get_registry

        mine = (ref.user is None or ref.user == (local_user or get_user()))
        same_archive = (ref.archive is None or ref.archive == local_archive)
        if not (mine and same_archive):
            out["foreign"] = True
            cfg = (get_registry().find(ref.archive, ref.user)
                   if ref.archive else None)
            if cfg is None or not Path(cfg.root).is_dir():
                out.update({"resolved": False,
                            "note_error": f"archive {ref.archive!r} is not "
                                          f"registered or not mounted here"})
                return out
            root = Path(cfg.root)

    if ref.kind == "collection":
        target = path_for(root, ref.collection)
        out.update({"target": ref.collection, "path": str(target),
                    "exists": target.is_file()})
        if not out["exists"]:
            out["note_error"] = f"no collection {ref.collection!r} there"
        return out

    if ref.kind == "archive":
        out.update({"target": ref.archive, "path": str(root), "exists": root.is_dir()})
        return out

    from nebula.session import _find_session_dir

    try:
        session_dir = _find_session_dir(root, ref.session)
    except (FileNotFoundError, ValueError):
        out.update({"target": ref.session,
                    "note_error": f"session {ref.session} not found"})
        return out

    if ref.kind == "session":
        out.update({"target": ref.session, "path": str(session_dir), "exists": True})
        return out

    target = session_dir / ref.file
    out.update({"target": f"{ref.session}/{ref.file}", "path": str(target),
                "exists": target.is_file()})
    if not out["exists"]:
        out["note_error"] = "file is missing"
    return out


def tree(archive_root, name: str, *, max_depth: int = 8,
         _stack=None) -> dict:
    """The collection and everything under it, resolved.

    Nested collections are expanded in place. A cycle (or a collection
    already on this branch) is reported rather than followed -- entries are
    references, so cycles are possible however carefully add() guards
    against making them here.
    """
    _stack = _stack or []
    coll = read(archive_root, name)
    if coll is None:
        return {"name": name, "missing": True, "entries": []}

    node = {"name": coll.name, "title": coll.title,
            "description": coll.description, "missing": False, "entries": []}
    if name in _stack:
        node["cycle"] = True
        return node
    if len(_stack) >= max_depth:
        node["truncated"] = True
        return node

    for entry in coll.entries:
        resolved = resolve_entry(archive_root, entry)
        if (resolved["kind"] == "collection" and resolved["exists"]
                and not resolved["foreign"]):
            resolved["child"] = tree(archive_root, entry.parsed.collection,
                                     max_depth=max_depth, _stack=_stack + [name])
        node["entries"].append(resolved)
    return node


def containing(archive_root, ref: str) -> List[str]:
    """Which collections list this ref -- the "what is this file part of?"
    lookup a file view needs."""
    try:
        canonical = format_ref(parse_ref(ref))
    except ValueError:
        return []
    out = []
    for coll in list_all(archive_root):
        for entry in coll.entries:
            if entry.kind == "invalid":
                continue
            if format_ref(entry.parsed) == canonical:
                out.append(coll.name)
                break
    return sorted(out)
