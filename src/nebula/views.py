"""
Saved searches ("views"): the computed counterpart to a collection.

A collection is curated -- you put things in it. A view is a stored query
that answers itself every time you open it: *everything tagged shows-drift
in 2026 that still has problems*. Neither moves a file; both are additive
views over immutable storage.

One file per view, beside collections and for the same reasons (add, remove
and share them individually)::

    <archive>/saved-searches/needs-attention.yaml

    name: needs-attention
    title: Runs that still have problems
    query: drift
    fields: [filename, user_tags, comments]
    date_from: 2026-01-01
    date_to: null

The stored fields are exactly the arguments of
:func:`nebula.navigator.model.search_items`, so running a view is one call
with no translation layer -- and any search you can build in the GUI can be
saved verbatim.
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

VIEWS_DIR = "saved-searches"
VERSION = 1

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ViewError(ValueError):
    """A view that cannot be named or stored."""


def clean_name(name: str) -> str:
    value = (name or "").strip()
    if not _NAME_RE.match(value):
        raise ViewError(
            f"invalid view name {name!r}: use letters, digits, '.', '-' and "
            f"'_', starting with a letter or digit")
    return value


@dataclass
class View:
    name: str
    title: str = ""
    query: str = ""
    fields: List[str] = field(default_factory=list)
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    created: Optional[str] = None
    modified: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"version": VERSION, "name": self.name}
        if self.title:
            out["title"] = self.title
        out["query"] = self.query
        if self.fields:
            out["fields"] = list(self.fields)
        if self.date_from:
            out["date_from"] = self.date_from
        if self.date_to:
            out["date_to"] = self.date_to
        if self.created:
            out["created"] = self.created
        if self.modified:
            out["modified"] = self.modified
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any], *, name: str) -> "View":
        fields_raw = d.get("fields")
        return cls(
            name=d.get("name") or name,
            title=str(d.get("title") or ""),
            query=str(d.get("query") or ""),
            fields=[str(f) for f in fields_raw] if isinstance(fields_raw, list) else [],
            date_from=d.get("date_from"),
            date_to=d.get("date_to"),
            created=d.get("created"),
            modified=d.get("modified"),
        )

    def search_args(self) -> Dict[str, Any]:
        """Exactly what search_items() takes."""
        return {"query": self.query, "fields": self.fields or None,
                "date_from": self.date_from, "date_to": self.date_to}


def views_dir(archive_root) -> Path:
    return Path(archive_root) / VIEWS_DIR


def path_for(archive_root, name: str) -> Path:
    return views_dir(archive_root) / f"{clean_name(name)}.yaml"


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def list_names(archive_root) -> List[str]:
    d = views_dir(archive_root)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.yaml") if not p.name.startswith("."))


def read(archive_root, name: str) -> Optional[View]:
    path = path_for(archive_root, name)
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return View(name=clean_name(name))
    return View.from_dict(raw, name=clean_name(name))


def list_all(archive_root) -> List[View]:
    out = []
    for name in list_names(archive_root):
        got = read(archive_root, name)
        if got is not None:
            out.append(got)
    return out


def write(archive_root, view: View) -> Path:
    view.name = clean_name(view.name)
    view.created = view.created or _now()
    view.modified = _now()
    path = path_for(archive_root, view.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{view.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(view.to_dict(), f, sort_keys=False, allow_unicode=True,
                           default_flow_style=False, width=88)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def save(archive_root, name: str, *, query: str = "", title: str = "",
         fields=None, date_from=None, date_to=None) -> View:
    """Create or overwrite a view. Overwriting is the point: a saved search
    is meant to be tweaked."""
    existing = read(archive_root, name)
    view = View(
        name=clean_name(name), title=title or (existing.title if existing else ""),
        query=query, fields=list(fields or []),
        date_from=date_from, date_to=date_to,
        created=existing.created if existing else None,
    )
    write(archive_root, view)
    return view


def delete(archive_root, name: str) -> bool:
    try:
        path_for(archive_root, name).unlink()
        return True
    except OSError:
        return False


def run(archive_root, name: str, *, limit: int = 1000) -> dict:
    """Execute a saved view. Returns search_items()'s result plus the view."""
    from nebula.navigator import model

    view = read(archive_root, name)
    if view is None:
        raise ViewError(f"no view {name!r} in this archive")
    args = view.search_args()
    res = model.search_items(Path(archive_root), args["query"],
                             fields=args["fields"], date_from=args["date_from"],
                             date_to=args["date_to"], limit=limit)
    res["view"] = view.to_dict()
    return res
