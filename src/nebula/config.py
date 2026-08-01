"""
Per-archive settings (``<archive>/archive.yaml``).

Distinct from the *registry* (~/.nebula/archives.yaml), which is
machine-local config about where archives live. These settings belong to
the archive itself and travel with it, so every machine writing into a
shared archive agrees on how it behaves.

Missing file means defaults -- an archive created before this existed, or
one nobody has configured, still works.

A root-level *file* is safe with the session layout: the year/month walk
only descends directories whose names are numeric, so archive.yaml (like
index.db) is simply ignored by it.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Dict

import yaml

from nebula.codestore import DEFAULT_MAX_FILE_BYTES

ARCHIVE_CONFIG_FILE = "archive.yaml"

#: Env override, mostly for CI and tests: 0/false disables code capture
#: regardless of what the archive says.
CAPTURE_ENV = "NEBULA_CAPTURE_CODE"


#: What to do when a write would land on an existing artifact.
OVERWRITE_POLICIES = ("duplicate", "overwrite", "cancel")
DEFAULT_OVERWRITE_POLICY = "duplicate"


@dataclass
class ArchiveSettings:
    #: duplicate -- write alongside as <name>-001.<ext>, recording the name
    #:              that was asked for (the default: nothing is ever lost)
    #: overwrite -- replace the existing file
    #: cancel    -- refuse the write and raise
    on_overwrite: str = DEFAULT_OVERWRITE_POLICY
    #: Snapshot first-party source into <archive>/.code on every save.
    capture_code: bool = True
    #: Per-file ceiling for that snapshot.
    code_max_file_bytes: int = DEFAULT_MAX_FILE_BYTES

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ArchiveSettings":
        known = {f.name for f in fields(cls)}
        got = cls(**{k: v for k, v in (d or {}).items() if k in known})
        # A typo here would silently change how writes behave, so fall back
        # to the safe policy rather than trusting it.
        if got.on_overwrite not in OVERWRITE_POLICIES:
            got.on_overwrite = DEFAULT_OVERWRITE_POLICY
        return got

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def config_path(archive_root) -> Path:
    return Path(archive_root) / ARCHIVE_CONFIG_FILE


def read_settings(archive_root, *, apply_env: bool = True) -> ArchiveSettings:
    """Settings for an archive, falling back to defaults for a missing or
    unreadable file -- configuration trouble must not stop a measurement
    script from saving its data.

    apply_env=False reads what the *file* says, ignoring the environment
    override. Anything about to write settings back must use it: otherwise
    a one-off NEBULA_CAPTURE_CODE=0 in the shell would be persisted into
    the archive as though the user had chosen it.
    """
    settings = ArchiveSettings()
    try:
        raw = yaml.safe_load(config_path(archive_root).read_text())
        settings = ArchiveSettings.from_dict(raw or {})
    except (OSError, yaml.YAMLError, TypeError):
        pass

    if apply_env:
        override = env_override()
        if override is not None:
            settings.capture_code = override
    return settings


def env_override() -> "bool | None":
    """The NEBULA_CAPTURE_CODE override, or None if it isn't set."""
    raw = os.environ.get(CAPTURE_ENV)
    if raw is None:
        return None
    return raw.strip().lower() not in ("0", "false", "no", "")


def write_settings(archive_root, settings: ArchiveSettings) -> Path:
    path = config_path(archive_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(settings.to_dict(), f, sort_keys=True)
    return path
