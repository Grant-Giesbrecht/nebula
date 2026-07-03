"""
Sidecar file I/O.

Two kinds of metadata file live in a session folder:

  - <artifact>.meta.json  -- one per data file, machine-written, atomic,
                              never read-modify-write.
  - session.yaml          -- one per session folder, human-editable,
                              read-modify-write is fine here since it's
                              edited rarely and by one person at a time.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from nebula.refs import Ref, format_ref, parse_ref

SIDECAR_SUFFIX = ".meta.json"
SESSION_FILE = "session.yaml"


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: "str | Path", _chunk: int = 1 << 20) -> str:
    """Streaming SHA-256 of a file's bytes, so integrity of hand-added or
    replaced files can be verified later (and drift detected)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_chunk), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------
# Per-artifact sidecar (JSON, atomic, machine-written)
# ---------------------------------------------------------------------

@dataclass
class ProducedBy:
    repo: Optional[str] = None
    commit: Optional[str] = None
    dirty: Optional[bool] = None
    entry_point: Optional[str] = None
    # Where the file came from. "script" (default) means a tracked script
    # run produced it and the git fields above are authoritative. "external"
    # means it was brought in by hand -- a coworker's emailed dataset, a
    # manual instrument export, a file adopted by `reconcile` -- so the git
    # fields are empty and the ones below describe its origin instead.
    # `origin` is deliberately free text: a catch-all for whatever context
    # is worth keeping ("emailed by Jane 2026-07-01", a ticket link, ...).
    source: str = "script"
    origin: Optional[str] = None
    imported_by: Optional[str] = None   # OS user who ran the import
    imported_at: Optional[str] = None   # ISO 8601


@dataclass
class SidecarMeta:
    created: str  # ISO 8601 timestamp
    produced_by: ProducedBy = field(default_factory=ProducedBy)
    derived_from: List[Dict[str, Optional[str]]] = field(default_factory=list)
    inputs: Dict[str, Any] = field(default_factory=dict)
    # Hex SHA-256 of the artifact bytes, auto-filled by write_sidecar. Lets
    # `check` verify a hand-added file hasn't silently drifted.
    sha256: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # extra's contents get merged up to top level on write, so ad hoc
        # fields don't get buried under a generic "extra" key in the file.
        extra = d.pop("extra")
        d.update(extra)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SidecarMeta":
        known = {"created", "produced_by", "derived_from", "inputs", "sha256"}
        extra = {k: v for k, v in d.items() if k not in known}
        produced_by = ProducedBy(**d.get("produced_by", {}))
        return cls(
            created=d["created"],
            produced_by=produced_by,
            derived_from=d.get("derived_from", []),
            inputs=d.get("inputs", {}),
            sha256=d.get("sha256"),
            extra=extra,
        )

    def derived_from_refs(self) -> List[Ref]:
        """Decode the stored ref dicts back into Ref objects."""
        return [
            Ref(file=r.get("file"), session=r.get("session"), archive=r.get("archive"))
            for r in self.derived_from
        ]

    def add_derived_from(self, ref: "str | Ref") -> None:
        """Accepts either a compact ref string ('diode.graf',
        'S-0152/diode.graf', 'postdoc|S-0152/diode.graf') or a Ref."""
        if isinstance(ref, str):
            ref = parse_ref(ref)
        self.derived_from.append(
            {"archive": ref.archive, "session": ref.session, "file": ref.file}
        )


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON atomically: write to a temp file in the same directory,
    then os.replace. This means a crash mid-write never leaves a
    half-written sidecar, and concurrent writers to *different* sidecars
    in the same folder never interfere with each other."""
    path = Path(path)
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def sidecar_path_for(artifact_path: Path) -> Path:
    return Path(artifact_path).with_suffix(Path(artifact_path).suffix + SIDECAR_SUFFIX)


def write_sidecar(artifact_path: Path, meta: SidecarMeta) -> Path:
    """Write (or overwrite) the sidecar for a given artifact file.
    Overwriting is allowed -- e.g. to append a derived_from entry
    discovered after the fact -- but each write is still a fresh atomic
    replace, never an in-place mutation.

    If the artifact file exists and meta has no checksum yet, its SHA-256
    is computed and stored -- so every path that writes a sidecar (script
    saves, manual imports, reconcile stubs) records one."""
    artifact_path = Path(artifact_path)
    if meta.sha256 is None and artifact_path.is_file():
        meta.sha256 = sha256_file(artifact_path)
    sidecar_path = sidecar_path_for(artifact_path)
    _atomic_write_json(sidecar_path, meta.to_dict())
    return sidecar_path


def read_sidecar(artifact_path: Path) -> SidecarMeta:
    sidecar_path = sidecar_path_for(artifact_path)
    with open(sidecar_path, "r") as f:
        return SidecarMeta.from_dict(json.load(f))


# ---------------------------------------------------------------------
# session.yaml (human-editable, read-modify-write is acceptable)
# ---------------------------------------------------------------------

@dataclass
class SessionMeta:
    run_id: str
    created: str  # ISO 8601 timestamp, session open time
    status: str = "open"  # "open" | "closed" | "crashed"
    tags: List[str] = field(default_factory=list)
    description: str = ""
    related_runs: List[Dict[str, Optional[str]]] = field(default_factory=list)
    # A "hold" keeps a session appendable across day boundaries even after
    # it's been closed (see session.hold()). None = no hold; the sentinel
    # "forever" = indefinite; otherwise an ISO 8601 expiry timestamp.
    hold_until: Optional[str] = None
    # Append-only audit log of manual operations on this session (imports,
    # reconciles, and later replaces/deletes), so a hand-edited session
    # carries its own record of what changed, when, by whom, and why.
    history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SessionMeta":
        return cls(
            run_id=d["run_id"],
            created=d["created"],
            status=d.get("status", "open"),
            tags=d.get("tags", []),
            description=d.get("description", ""),
            related_runs=d.get("related_runs", []),
            hold_until=d.get("hold_until"),
            history=d.get("history", []),
        )

    def related_run_refs(self) -> List[Ref]:
        return [
            Ref(file=r.get("file"), session=r.get("session"), archive=r.get("archive"))
            for r in self.related_runs
        ]

    def add_related_run(self, ref: "str | Ref") -> None:
        if isinstance(ref, str):
            ref = parse_ref(ref)
        entry = {"archive": ref.archive, "session": ref.session, "file": ref.file}
        if entry not in self.related_runs:
            self.related_runs.append(entry)

    def add_history(
        self,
        action: str,
        *,
        file: Optional[str] = None,
        note: Optional[str] = None,
        by: Optional[str] = None,
        at: Optional[str] = None,
        **extra: Any,
    ) -> None:
        """Append one entry to the session's manual-operation history."""
        entry: Dict[str, Any] = {
            "action": action,
            "at": at or _now_iso(),
            "by": by,
            "file": file,
            "note": note,
        }
        entry.update(extra)
        self.history.append(entry)


def write_session_yaml(session_dir: Path, meta: SessionMeta) -> Path:
    path = Path(session_dir) / SESSION_FILE
    # YAML doesn't need the same atomic-write rigor as the sidecars since
    # it's edited by one human (or the owning process) at a time, but we
    # still write-then-replace to avoid truncating the file on a crash.
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=".session.yaml.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(meta.to_dict(), f, sort_keys=False)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


def read_session_yaml(session_dir: Path) -> SessionMeta:
    path = Path(session_dir) / SESSION_FILE
    with open(path, "r") as f:
        return SessionMeta.from_dict(yaml.safe_load(f))
