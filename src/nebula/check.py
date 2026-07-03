"""
Archive integrity check (fsck) and the reference-scanning helpers that the
delete guards share.

Everything here reads the filesystem directly -- the sidecars and
session.yaml files, never the index -- because this is exactly the tool you
reach for when you suspect the archive has been mucked with by hand and the
index might be stale or lying. It answers:

  - which files have no sidecar (orphans)?
  - which sidecars point at a file that's gone?
  - which files no longer match the sha256 their sidecar recorded (drift)?
  - which derived_from / related_runs refs point at something missing?
  - which session folders are missing session.yaml, or share an id?

The same derived_from scan powers delete_file/delete_session's "is anything
still pointing at this?" guard (see manual.py).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from nebula.registry import get_registry, resolve_archive
from nebula.session import HOLD_FOREVER, _ID_RE, orphan_artifacts_in
from nebula.sidecar import (
    SESSION_FILE,
    SIDECAR_SUFFIX,
    read_session_yaml,
    read_sidecar,
    sha256_file,
    sidecar_path_for,
)

_VALID_STATUS = {"open", "closed", "crashed"}


@dataclass
class CheckIssue:
    kind: str
    session: Optional[str]
    file: Optional[str]
    detail: str
    fix: Optional[str] = None       # suggested remediation (often a nebula command)
    severity: str = "error"         # "error" (a real inconsistency) | "info" (FYI)

    def __str__(self) -> str:
        where = self.session or "-"
        if self.file:
            where = f"{where}/{self.file}"
        return f"[{self.severity}] {self.kind} {where}: {self.detail}"


def _iter_all_session_dirs(archive_root: Path) -> Iterator[Path]:
    """Every S-XXXX folder under the archive, regardless of whether it has
    a session.yaml (so missing-yaml folders are still visited). Skips the
    archive-level .trash (its name isn't a numeric year)."""
    if not archive_root.exists():
        return
    for year in sorted(archive_root.iterdir()):
        if not year.is_dir() or not year.name.isdigit():
            continue
        for month in sorted(year.iterdir()):
            if not month.is_dir() or not month.name.isdigit():
                continue
            for d in sorted(month.iterdir()):
                if d.is_dir() and _ID_RE.match(d.name):
                    yield d


def _read_sidecars(session_dir: Path):
    """Yield (artifact_filename, SidecarMeta) for each sidecar in a session
    folder (top level only -- trashed files under .trash are excluded)."""
    for sc in sorted(session_dir.glob(f"*{SIDECAR_SUFFIX}")):
        artifact = sc.name[: -len(SIDECAR_SUFFIX)]
        try:
            meta = read_sidecar(session_dir / artifact)
        except Exception:
            continue
        yield artifact, meta


# ---------------------------------------------------------------------
# Reference scans (also used by the delete guards)
# ---------------------------------------------------------------------

def dependents_of(archive: "str | Path", run_id: str, filename: str) -> List[str]:
    """Same-archive artifacts whose derived_from points at run_id/filename.
    Scans the filesystem (not the index), so it's trustworthy even when the
    index is stale. Cross-archive dependents can't be found from here."""
    archive_root, _ = resolve_archive(archive)
    hits: List[str] = []
    for session_dir in _iter_all_session_dirs(archive_root):
        r = session_dir.name
        for artifact, meta in _read_sidecars(session_dir):
            for ref in meta.derived_from_refs():
                if ref.archive is not None or ref.file != filename:
                    continue
                target_session = ref.session or r  # None = same session
                if target_session == run_id:
                    hits.append(f"{r}/{artifact}")
    return hits


def inbound_to_session(archive: "str | Path", run_id: str) -> List[str]:
    """Things in OTHER same-archive sessions that reference this session --
    via an artifact's derived_from or a session's related_runs."""
    archive_root, _ = resolve_archive(archive)
    hits: List[str] = []
    for session_dir in _iter_all_session_dirs(archive_root):
        r = session_dir.name
        if r == run_id:
            continue
        for artifact, meta in _read_sidecars(session_dir):
            for ref in meta.derived_from_refs():
                if ref.archive is None and ref.session == run_id:
                    hits.append(f"{r}/{artifact} derives from {run_id}/{ref.file}")
        if (session_dir / SESSION_FILE).exists():
            try:
                smeta = read_session_yaml(session_dir)
            except Exception:
                continue
            for rr in smeta.related_run_refs():
                if rr.archive is None and rr.session == run_id:
                    hits.append(f"{r} related_run -> {run_id}")
    return hits


# ---------------------------------------------------------------------
# Full integrity check
# ---------------------------------------------------------------------

def check(
    archive: "str | Path",
    *,
    verify_checksums: bool = True,
    archive_label: Optional[str] = None,
) -> List[CheckIssue]:
    """Walk the archive and return every integrity problem found, each with
    a suggested fix. An empty list means the archive is internally
    consistent. `archive_label` is the name to use in suggested commands
    (defaults to the resolved archive name, or "<archive>")."""
    archive_root, resolved = resolve_archive(archive)
    label = archive_label or resolved or "<archive>"
    registry = get_registry()
    issues: List[CheckIssue] = []

    # First pass: inventory what exists, and flag structural problems.
    id_to_dirs: dict = {}
    existing_sessions: set = set()
    existing_files: set = set()  # (run_id, filename)

    for session_dir in _iter_all_session_dirs(archive_root):
        run_id = session_dir.name
        id_to_dirs.setdefault(run_id, []).append(session_dir)
        if not (session_dir / SESSION_FILE).exists():
            issues.append(CheckIssue(
                "missing_session_yaml", run_id, None,
                f"folder has no {SESSION_FILE}",
                fix=f"restore {SESSION_FILE}, or move the folder out of the archive"))
            continue
        existing_sessions.add(run_id)
        for entry in sorted(session_dir.iterdir()):
            if entry.is_file() and not entry.name.startswith(".") \
                    and entry.name != SESSION_FILE \
                    and not entry.name.endswith(SIDECAR_SUFFIX):
                existing_files.add((run_id, entry.name))

    for run_id, dirs in id_to_dirs.items():
        if len(dirs) > 1:
            issues.append(CheckIssue(
                "duplicate_id", run_id, None,
                f"{len(dirs)} folders share this id: {[str(d) for d in dirs]}",
                fix="renumber or remove the extra folder(s)"))

    # Second pass: per-session content checks.
    for session_dir in _iter_all_session_dirs(archive_root):
        run_id = session_dir.name
        if not (session_dir / SESSION_FILE).exists():
            continue

        smeta = None
        try:
            smeta = read_session_yaml(session_dir)
        except Exception as e:
            issues.append(CheckIssue(
                "unreadable_session_yaml", run_id, None, str(e),
                fix=f"fix the YAML syntax in {run_id}/{SESSION_FILE}"))

        if smeta is not None:
            issues.extend(_check_session_meta(run_id, smeta, label))

        # Artifacts with no sidecar.
        for orphan in orphan_artifacts_in(session_dir):
            issues.append(CheckIssue(
                "orphan", run_id, orphan.name, "file has no sidecar",
                fix=f"nebula reconcile {label} {run_id}"))

        # Every sidecar: parse it, then check its artifact + refs.
        for sc in sorted(session_dir.glob(f"*{SIDECAR_SUFFIX}")):
            artifact = sc.name[: -len(SIDECAR_SUFFIX)]
            try:
                meta = read_sidecar(session_dir / artifact)
            except Exception as e:
                issues.append(CheckIssue(
                    "unreadable_sidecar", run_id, artifact,
                    f"can't parse sidecar: {e}",
                    fix=f"fix the JSON in {run_id}/{artifact}{SIDECAR_SUFFIX}"))
                continue

            artifact_path = session_dir / artifact
            if not artifact_path.is_file():
                issues.append(CheckIssue(
                    "missing_artifact", run_id, artifact,
                    "sidecar exists but the artifact file is gone",
                    fix=(f"recover it from {run_id}/.trash/, or run "
                         f"'nebula rm {label} {run_id} {artifact}' to clear the stray sidecar")))
                continue
            if verify_checksums and meta.sha256:
                actual = sha256_file(artifact_path)
                if actual != meta.sha256:
                    issues.append(CheckIssue(
                        "checksum_mismatch", run_id, artifact,
                        f"sha256 {actual[:12]}... != recorded {meta.sha256[:12]}...",
                        fix=(f"if the edit was intentional: 'nebula reseal {label} {run_id} "
                             f"{artifact}'; otherwise restore the original bytes")))
            for ref in meta.derived_from_refs():
                issues.extend(_check_ref(
                    ref, kind="derived_from", run_id=run_id, file=artifact,
                    existing_sessions=existing_sessions, existing_files=existing_files,
                    registry=registry, label=label))

        # Session-level related_runs.
        if smeta is not None:
            for rr in smeta.related_run_refs():
                issues.extend(_check_ref(
                    rr, kind="related_run", run_id=run_id, file=None,
                    existing_sessions=existing_sessions, existing_files=existing_files,
                    registry=registry, label=label))

    return issues


def _check_session_meta(run_id, smeta, label) -> List[CheckIssue]:
    out = []
    if smeta.run_id != run_id:
        out.append(CheckIssue(
            "id_mismatch", run_id, None,
            f"session.yaml run_id is {smeta.run_id!r} but the folder is {run_id!r}",
            fix=f"set 'run_id: {run_id}' in {run_id}/{SESSION_FILE}, or rename the folder"))
    if smeta.status not in _VALID_STATUS:
        out.append(CheckIssue(
            "invalid_status", run_id, None,
            f"status {smeta.status!r} is not one of {sorted(_VALID_STATUS)}",
            fix=f"set a valid status in {run_id}/{SESSION_FILE}"))
    if smeta.hold_until and smeta.hold_until != HOLD_FOREVER:
        try:
            datetime.datetime.fromisoformat(smeta.hold_until)
        except (ValueError, TypeError):
            out.append(CheckIssue(
                "garbled_hold_until", run_id, None,
                f"hold_until {smeta.hold_until!r} is neither {HOLD_FOREVER!r} nor an ISO timestamp",
                fix=f"nebula release {label} {run_id}"))
    return out


def _check_ref(ref, *, kind, run_id, file, existing_sessions, existing_files,
               registry, label) -> List[CheckIssue]:
    """Verify one derived_from / related_run ref. Same-archive refs are
    checked against what exists; cross-archive refs are reported as info
    (we can't verify another archive's contents from here)."""
    out: List[CheckIssue] = []
    dangling = "dangling_derived_from" if kind == "derived_from" else "dangling_related_run"

    if ref.archive is not None:
        cfg = registry.try_get(ref.archive)
        if cfg is None or not cfg.root.exists():
            out.append(CheckIssue(
                "unresolved_cross_archive_ref", run_id, file,
                f"{kind} points into archive {ref.archive!r}, which isn't "
                f"registered/mounted here -- not verified",
                fix=f"nebula register {ref.archive} <root>  (so check can verify it)",
                severity="info"))
        return out

    target_session = ref.session or run_id

    if kind == "derived_from" and ref.file == file and target_session == run_id:
        out.append(CheckIssue(
            "self_reference", run_id, file,
            "derived_from lists the file itself",
            fix=f"remove the self-reference from {run_id}/{file}{SIDECAR_SUFFIX}"))
        return out

    if ref.file is None:
        if target_session not in existing_sessions:
            out.append(CheckIssue(
                dangling, run_id, file,
                f"{kind} points at missing session {target_session}",
                fix=f"restore session {target_session}, or drop the ref"))
    elif (target_session, ref.file) not in existing_files:
        out.append(CheckIssue(
            dangling, run_id, file,
            f"{kind} points at missing {target_session}/{ref.file}",
            fix=(f"restore {target_session}/{ref.file} (check {target_session}/.trash/), "
                 f"or drop the ref")))
    return out
