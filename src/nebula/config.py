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

#: What an archive is for, which decides whose session ids it mints and what
#: may be done with it. All three share one on-disk format; only policy
#: differs.
#:
#:   standard -- the real thing. Ids are permanent and sacred.
#:   intake   -- a staging archive (a lab bench, an untrusted machine). Its
#:               ids are provisional (I-...) and are reallocated when it is
#:               merged into a standard archive, which is the point of it.
#:   fragment -- an excerpt of someone's standard archive, exported to be
#:               read. Ids belong to the source archive and are preserved
#:               exactly, so a citation stays valid; never merged, and not
#:               written to.
KINDS = ("standard", "intake", "fragment")
DEFAULT_KIND = "standard"

#: Session id prefix per kind. Only intake differs: an I- id marks a
#: *different identity* (it will be replaced on merge, and the merge records
#: what it became), where a fragment's id is the same identity in a
#: different place -- which the archive segment of a URI already says.
KIND_PREFIX = {"standard": "S-", "intake": "I-", "fragment": "S-"}

#: Env override, mostly for CI and tests: 0/false disables code capture
#: regardless of what the archive says.
CAPTURE_ENV = "NEBULA_CAPTURE_CODE"


#: What to do when a write would land on an existing artifact.
OVERWRITE_POLICIES = ("duplicate", "overwrite", "cancel")
DEFAULT_OVERWRITE_POLICY = "duplicate"


class ConfigError(ValueError):
    """An archive.yaml that cannot be trusted to describe the archive."""


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
    #: Re-index a session in <archive>/index.db as it closes. Off just
    #: means readers do that work instead (index.ensure_fresh finds the
    #: change either way); worth turning off only if the index lives on
    #: storage slow enough that closing a session should not touch it.
    auto_index: bool = True

    # ---- identity: travels with the archive, unlike the registry --------
    #: What this archive calls itself. This is the name in a nebula URI, so
    #: it is recorded here rather than being supplied by whoever registers
    #: it -- a fragment must resolve under the name its author used.
    name: str = ""
    #: Who created it, for the user segment of a URI. Empty means "the local
    #: identity", i.e. mine.
    user: str = ""
    kind: str = DEFAULT_KIND
    created: str = ""
    #: Set on an intake archive once it has been merged: further writes are
    #: refused until it is explicitly unlocked, so a second merge cannot be
    #: fed data that was written after the first one.
    merged_at: str = ""
    merged_to: str = ""

    @property
    def prefix(self) -> str:
        return KIND_PREFIX.get(self.kind, "S-")

    @property
    def locked(self) -> bool:
        return bool(self.merged_at)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ArchiveSettings":
        known = {f.name for f in fields(cls)}
        got = cls(**{k: v for k, v in (d or {}).items() if k in known})
        # A typo here would silently change how writes behave, so fall back
        # to the safe policy rather than trusting it.
        if got.on_overwrite not in OVERWRITE_POLICIES:
            got.on_overwrite = DEFAULT_OVERWRITE_POLICY
        # An unknown kind must not silently become "standard": that would
        # hand a fragment permission to mint ids. Fail loudly instead.
        if got.kind not in KINDS:
            raise ConfigError(
                f"unknown archive kind {got.kind!r} in {ARCHIVE_CONFIG_FILE}; "
                f"expected one of {', '.join(KINDS)}")
        return got

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        # Don't write empty identity fields: an archive that predates this,
        # or one nobody has named, should look untouched rather than
        # sprouting blank keys.
        for key in ("name", "user", "created", "merged_at", "merged_to"):
            if not out.get(key):
                out.pop(key, None)
        return out


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
    except ConfigError:
        raise
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


def archive_identity(archive_root) -> Dict[str, Any]:
    """Name, owner and kind as the archive itself declares them.

    Falls back to the directory name and the local identity, so an archive
    created before any of this existed still answers -- but what is written
    in the file always wins, because that is what travels with a copy.
    """
    from nebula import identity

    root = Path(archive_root)
    settings = read_settings(root, apply_env=False)
    return {
        "name": settings.name or root.name,
        "user": settings.user or identity.get_user() or "",
        "kind": settings.kind,
        "created": settings.created,
        "declared": bool(settings.name),
        "locked": settings.locked,
        "merged_at": settings.merged_at,
        "merged_to": settings.merged_to,
        "root": str(root),
    }
