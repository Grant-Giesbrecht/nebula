"""
User annotations: mutable tags and comments for sessions and artifacts.

Deliberately separate from everything written at creation time. A sidecar
records what happened -- provenance, checksum, inputs -- is machine-written
and atomic, and is never read-modify-written. Session tags are chosen when
the session is created. Both are claims about the run, and stay sacred.

Annotations are the opposite: notes you add later and change whenever you
like ("this is the run that showed the drift", "figure 3 in the paper").
They live in one file per session::

    <session>/annotations.yaml

    version: 1
    session:
      tags: [paper-2026, thesis-ch3]
      comment: |
        Warm-up drifted for the first 20 min.
    artifacts:
      vccs_warm_up.tome:
        tags: [shows-drift]
        comment: the run that showed the phenomenon

YAML rather than JSON because comments are long, multi-line and
hand-edited; a block scalar beats a string full of \\n escapes.

Concurrency is deliberately simple: write-then-replace so a crash can't
truncate the file, and last-write-wins between machines. Two people
annotating the same session through a syncing folder at the same moment
will lose one side's edit -- the file is small and rarely contested, and
merging is a problem for another day.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ANNOTATIONS_FILE = "annotations.yaml"
VERSION = 1

#: Tags are for finding things, so they stay simple: no commas (the
#: separator everywhere they are typed) and no newlines. Colons, hyphens
#: and underscores are explicitly fine, for namespaced tags like
#: "paper:2026" or "rp23-warmup".
_FORBIDDEN = (",", "\n", "\r", "\t")


class TagError(ValueError):
    """A tag that can't be stored as written."""


def clean_tag(text: str) -> str:
    """Normalise one tag, or raise TagError explaining why it can't be.

    Outer whitespace is stripped and internal runs collapsed; case is
    preserved, since "RP23D" is not "rp23d".
    """
    if text is None:
        raise TagError("empty tag")
    raw = str(text)
    for bad in _FORBIDDEN:
        if bad in raw:
            name = {",": "commas", "\n": "newlines", "\r": "newlines",
                    "\t": "tabs"}[bad]
            raise TagError(f"tags cannot contain {name}: {raw!r}")
    cleaned = re.sub(r"\s+", " ", raw).strip()
    if not cleaned:
        raise TagError("empty tag")
    return cleaned


def clean_tags(tags) -> List[str]:
    """Normalise a list of tags, dropping duplicates but keeping order."""
    out: List[str] = []
    for tag in tags or []:
        cleaned = clean_tag(tag)
        if cleaned not in out:
            out.append(cleaned)
    return out


def split_tags(text: str) -> List[str]:
    """Parse the comma-separated form used by the CLI and the GUI field."""
    return clean_tags([part for part in (text or "").split(",") if part.strip()])


# ---------------------------------------------------------------------
# file I/O
# ---------------------------------------------------------------------

def annotations_path(session_dir) -> Path:
    return Path(session_dir) / ANNOTATIONS_FILE


def _blank() -> Dict[str, Any]:
    return {"version": VERSION, "session": {}, "artifacts": {}}


def read_annotations(session_dir) -> Dict[str, Any]:
    """The session's annotations, or an empty structure if there are none.

    Never raises for a missing or malformed file: annotations are a
    convenience layer, and a broken one must not stop the Navigator from
    showing the session.
    """
    path = annotations_path(session_dir)
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return _blank()
    if not isinstance(raw, dict):
        return _blank()

    out = _blank()
    session = raw.get("session")
    if isinstance(session, dict):
        out["session"] = _clean_entry(session)
    artifacts = raw.get("artifacts")
    if isinstance(artifacts, dict):
        for name, entry in artifacts.items():
            if isinstance(entry, dict):
                cleaned = _clean_entry(entry)
                if cleaned:
                    out["artifacts"][str(name)] = cleaned
    out["version"] = raw.get("version", VERSION)
    return out


def _clean_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    tags = entry.get("tags")
    if isinstance(tags, list):
        kept = []
        for tag in tags:                      # tolerate hand-edited junk
            try:
                kept.append(clean_tag(tag))
            except TagError:
                continue
        if kept:
            out["tags"] = list(dict.fromkeys(kept))
    comment = entry.get("comment")
    if isinstance(comment, str) and comment.strip():
        out["comment"] = comment.strip()
    return out


def write_annotations(session_dir, data: Dict[str, Any]) -> Optional[Path]:
    """Persist annotations, atomically. Writing an empty set removes the
    file rather than leaving an empty one in every session folder."""
    path = annotations_path(session_dir)
    payload = {"version": data.get("version", VERSION)}
    if data.get("session"):
        payload["session"] = data["session"]
    if data.get("artifacts"):
        payload["artifacts"] = dict(sorted(data["artifacts"].items()))

    if len(payload) == 1:                     # nothing but the version left
        try:
            path.unlink()
        except OSError:
            pass
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{ANNOTATIONS_FILE}.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True,
                           default_flow_style=False, width=88)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# ---------------------------------------------------------------------
# editing
# ---------------------------------------------------------------------

def get(session_dir, filename: Optional[str] = None) -> Dict[str, Any]:
    """Annotations for one target: the session (filename=None) or one
    artifact."""
    data = read_annotations(session_dir)
    if filename is None:
        entry = data.get("session") or {}
    else:
        entry = (data.get("artifacts") or {}).get(filename) or {}
    return {"tags": list(entry.get("tags") or []),
            "comment": entry.get("comment") or ""}


def set_annotation(session_dir, filename: Optional[str] = None, *,
                   tags=None, comment: Optional[str] = None) -> Dict[str, Any]:
    """Replace the tags and/or comment of one target. Passing None leaves
    that field alone; passing [] or "" clears it."""
    data = read_annotations(session_dir)
    entry = dict((data["artifacts"].get(filename) or {}) if filename is not None
                 else (data.get("session") or {}))

    if tags is not None:
        cleaned = clean_tags(tags)
        if cleaned:
            entry["tags"] = cleaned
        else:
            entry.pop("tags", None)
    if comment is not None:
        text = str(comment).strip()
        if text:
            entry["comment"] = text
        else:
            entry.pop("comment", None)

    if filename is None:
        data["session"] = entry
    elif entry:
        data["artifacts"][filename] = entry
    else:
        data["artifacts"].pop(filename, None)

    write_annotations(session_dir, data)
    return {"tags": list(entry.get("tags") or []),
            "comment": entry.get("comment") or ""}


def add_tags(session_dir, filename: Optional[str], tags) -> Dict[str, Any]:
    current = get(session_dir, filename)["tags"]
    return set_annotation(session_dir, filename, tags=current + list(tags or []))


def remove_tags(session_dir, filename: Optional[str], tags) -> Dict[str, Any]:
    drop = {clean_tag(t) for t in (tags or [])}
    current = get(session_dir, filename)["tags"]
    return set_annotation(session_dir, filename,
                          tags=[t for t in current if t not in drop])


def append_comment(session_dir, filename: Optional[str], text: str) -> Dict[str, Any]:
    """Add a line to the existing comment rather than replacing it -- the
    bulk editor's use case: noting the same thing on several files without
    clobbering whatever was already written on each one. A blank ``text``
    is a no-op, so callers can pass through an empty bulk-edit field
    unconditionally."""
    line = (text or "").strip()
    if not line:
        return get(session_dir, filename)
    current = get(session_dir, filename)["comment"]
    combined = f"{current}\n{line}" if current else line
    return set_annotation(session_dir, filename, comment=combined)


def annotated_files(session_dir) -> List[str]:
    """Artifact names this session has annotations for -- including any
    that no longer exist, which `check` reports."""
    return sorted((read_annotations(session_dir).get("artifacts") or {}))
