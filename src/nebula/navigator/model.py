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
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from nebula.index import _iter_session_dirs
from nebula.registry import get_registry, resolve_archive
from nebula.session import _hold_active, orphan_artifacts_in
from nebula.sidecar import (
    SESSION_FILE,
    SIDECAR_SUFFIX,
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
        ))
    out.sort(key=lambda s: s.created or "", reverse=True)
    return out


def _is_appendable(s: "SessionInfo") -> bool:
    """A session you can import into without a deliberate reopen: still
    open, created today, or held (mirrors session.append_to's rule)."""
    today = datetime.date.today().isoformat()
    return s.status == "open" or (s.created or "")[:10] == today or s.held


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
    artefacts: set = set()
    sidecar_bases: set = set()
    for entry in session_dir.iterdir():
        if not entry.is_file() or entry.name.startswith("."):
            continue
        if entry.name == SESSION_FILE:
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
        if has_s:
            try:
                meta = read_sidecar(art_path)
                source = meta.produced_by.source
                origin = meta.produced_by.origin
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
        ))
    return items


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


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"
