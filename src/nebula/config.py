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


#: How an asset gets snapshotted into the blob store without being asked.
#:
#:   auto         -- resolve from the archive's size ladder at the moment
#:                   the question is asked. A named policy rather than an
#:                   absent one: "what is this asset set to?" must always
#:                   have an answer the user can read back, and a file
#:                   that grows past a threshold should move policy on its
#:                   own rather than staying on whatever its size was at
#:                   import.
#:   every_change -- snapshot whenever nebula *observes* the bytes change.
#:                   Not "every save": nebula is not a daemon, so several
#:                   edits between two scans collapse into one snapshot.
#:   on_reference -- snapshot when a session records deriving from it.
#:   periodic     -- as on_reference, but at most once per asset_period_days.
#:   manual       -- never automatically.
#:
#: Every policy still accepts an explicit `nebula asset commit`: a policy
#: governs what happens *unasked*, and must never block a deliberate save.
ASSET_POLICIES = ("auto", "every_change", "on_reference", "periodic", "manual")
AUTO_ASSET_POLICY = "auto"
DEFAULT_ASSET_POLICY = "on_reference"

#: What a storage cap does to the snapshots it evicts.
#:
#:   mark -- flag them for `nebula gc` and keep the record. A snapshot
#:           record is a few hundred bytes; the blob is the expensive
#:           part. Keeping the record means the asset's history stays
#:           readable ("there was a version at this sha on this date")
#:           even once the bytes are gone.
#:   drop -- forget the record too.
#:
#: Either way the cap bounds what *this asset* holds onto, not the
#: archive's total bytes: a blob a session pinned stays, because that
#: pin is a stronger claim than this asset's cap.
ASSET_CAP_ACTIONS = ("mark", "drop")
DEFAULT_ASSET_CAP_ACTION = "mark"

#: Size ladder deciding a newly imported asset's default policy. Size is
#: overwhelmingly what predicts the answer -- a 100 GB video wants `manual`
#: because it is enormous, not because of what it contains -- so the
#: default is derived from it and the user overrides the exceptions.
DEFAULT_ASSET_PERIODIC_ABOVE = 256 << 20      # 256 MiB -> periodic
DEFAULT_ASSET_MANUAL_ABOVE = 8 << 30         # 8 GiB   -> manual
DEFAULT_ASSET_PERIOD_DAYS = 7


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

    # ---- assets: archive-wide defaults, overridable per asset -----------
    #: Default snapshot policy for an asset whose size falls below
    #: asset_periodic_above. The two thresholds form a ladder; see the
    #: module constants for why size is the right predictor.
    asset_policy: str = DEFAULT_ASSET_POLICY
    asset_periodic_above: int = DEFAULT_ASSET_PERIODIC_ABOVE
    asset_manual_above: int = DEFAULT_ASSET_MANUAL_ABOVE
    #: Minimum gap between automatic snapshots under the periodic policy.
    asset_period_days: int = DEFAULT_ASSET_PERIOD_DAYS
    #: Per-asset storage caps, evicting oldest snapshots first. 0 = no cap.
    #: These bound the archive's growth directly, which is what the user
    #: actually fears -- frequency only proxies for it.
    asset_max_snapshots: int = 0
    asset_max_snapshot_bytes: int = 0
    #: Whether hitting a cap forgets the snapshot record or only flags its
    #: blob for collection. See ASSET_CAP_ACTIONS.
    asset_cap_action: str = DEFAULT_ASSET_CAP_ACTION

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
        # Same reasoning for the asset default: a typo must not silently
        # change how much history gets kept. Fall back to the safe value
        # (the one that snapshots more), rather than the one that loses
        # bytes nobody asked to lose.
        # "auto" is meaningless here and would recurse: the ladder's own
        # bottom rung *is* this setting.
        if (got.asset_policy not in ASSET_POLICIES
                or got.asset_policy == AUTO_ASSET_POLICY):
            got.asset_policy = DEFAULT_ASSET_POLICY
        if got.asset_cap_action not in ASSET_CAP_ACTIONS:
            got.asset_cap_action = DEFAULT_ASSET_CAP_ACTION
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
