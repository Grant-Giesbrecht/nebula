"""
Mutable assets: files you keep editing, that sessions may derive from.

Everything in a session is sealed -- written once, checksummed, and any
later drift is an integrity error. That is the right model for a
measurement, and the wrong one for a figure. A schematic SVG, a CAD file,
a poster template: these are edited for years, and asking the user to
`reseal` after every edit turns the archive into a nag.

So assets live outside sessions and are *live by construction*. `check`
never reports drift on them, because drift is what they are for. What
makes that safe -- rather than a hole in the archive's provenance -- is
that a reference to an asset is pinned to specific bytes:

    pinned    a blob of the exact content was stored; fully recoverable
    observed  the sha256 was recorded but no blob; proves later drift,
              cannot recover what was there

A session's ``derived_from`` therefore keeps meaning something precise
even though the file it names keeps changing.

Identity, not filenames
-----------------------
An asset's identity is an opaque, never-reused id (``AF-26-0017``), and
its directory is named for that id::

    assets/26/00/AF-26-0017/
        asset.json                      identity, policy, history
        apl_paper_figures_v2_good.svg   the live file, named whatever

One directory per asset is what makes renaming safe. Users rename files
casually and reuse old names for new files; if identity came from the
path, a new ``figure.svg`` would silently inherit the provenance of the
old one -- a wrong link, which is worse than a broken one. Here a rename
is just "the single file in this directory has a different name than last
scan", and a different asset is always a different directory, so the
ambiguity cannot arise. The id also keeps an asset's snapshot history
continuous across renames, which is exactly when you most want to read it.

The id never appears in anything a human types. Refs carry both the
readable name and the id (see :mod:`nebula.refs`); resolution falls back
to the id only when the name misses.

Layout
------
Fanned out by id, the way :mod:`nebula.codestore` fans out by digest, and
for the same reason its header gives: one huge directory is painful to
sync through Dropbox/MEGA, and expensive to stat-scan for index
freshness. The bucket is derived arithmetically from the id, so an
asset's path is computable with no lookup and no counter file to desync
between machines. Numbering restarts each year and comes from the folder
listing, exactly like session ids.

There are deliberately no asset *groups*. Ids are unique archive-wide, so
a container adds nothing to identity, and organisation is better served
by the mechanisms that already exist and allow many-membership:
collections, tags, and saved searches.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nebula import assetstore
from nebula.config import (
    ASSET_POLICIES,
    AUTO_ASSET_POLICY,
    DEFAULT_ASSET_POLICY,
    ArchiveSettings,
    read_settings,
)
from nebula.sidecar import sha256_file

ASSETS_DIR = "assets"
ASSET_FILE = "asset.json"
ASSET_PREFIX = "AF-"
ID_WIDTH = 4

#: Assets per bucket directory. Chosen to keep a bucket small enough to
#: scan and sync cheaply while keeping the number of buckets per year
#: modest -- with ID_WIDTH=4 this is at most 100 buckets holding 100 each.
BUCKET_SIZE = 100

_ID_RE = re.compile(rf"^{re.escape(ASSET_PREFIX)}(\d{{2}})-(\d+)$")


class AssetError(ValueError):
    """An asset that cannot be trusted to be what it claims."""


# ---------------------------------------------------------------------
# Ids and paths
# ---------------------------------------------------------------------

def format_asset_id(year2: int, n: int) -> str:
    return f"{ASSET_PREFIX}{year2:02d}-{n:0{ID_WIDTH}d}"


def parse_asset_id(asset_id: str) -> Tuple[int, int]:
    """(two-digit year, number) for a well-formed id. Raises otherwise --
    a mis-parsed id would point at the wrong directory, and silently
    resolving to the wrong asset is the failure this whole scheme exists
    to prevent."""
    m = _ID_RE.match((asset_id or "").strip())
    if not m:
        raise AssetError(
            f"malformed asset id {asset_id!r}; expected "
            f"{ASSET_PREFIX}<yy>-<{'n' * ID_WIDTH}>")
    return int(m.group(1)), int(m.group(2))


def is_asset_id(text: str) -> bool:
    return bool(_ID_RE.match((text or "").strip()))


def assets_root(archive_root) -> Path:
    return Path(archive_root) / ASSETS_DIR


def asset_dir(archive_root, asset_id: str) -> Path:
    """Where an asset lives, computed from its id alone -- no index
    lookup, no directory search."""
    year2, n = parse_asset_id(asset_id)
    bucket = n // BUCKET_SIZE
    return assets_root(archive_root) / f"{year2:02d}" / f"{bucket:02d}" / asset_id


def _year_dir(archive_root, year2: int) -> Path:
    return assets_root(archive_root) / f"{year2:02d}"


def existing_ids(archive_root, year2: int) -> List[int]:
    """Asset numbers already used in one year, read from the folder
    listing. The filesystem is the source of truth here for the same
    reason it is for session ids: a counter file would desync the moment
    two machines wrote into a synced archive."""
    out: List[int] = []
    ydir = _year_dir(archive_root, year2)
    if not ydir.is_dir():
        return out
    for bucket in ydir.iterdir():
        if not bucket.is_dir():
            continue
        for entry in bucket.iterdir():
            if not entry.is_dir():
                continue
            m = _ID_RE.match(entry.name)
            if m and int(m.group(1)) == year2:
                out.append(int(m.group(2)))
    return sorted(out)


def allocate_asset_id(archive_root, *, now: Optional[datetime.datetime] = None) -> str:
    now = now or datetime.datetime.now()
    year2 = now.year % 100
    used = existing_ids(archive_root, year2)
    return format_asset_id(year2, (max(used) + 1) if used else 1)


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_iso(text: Optional[str]) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.fromisoformat(text) if text else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------

@dataclass
class Snapshot:
    """One stored version of an asset's bytes."""
    sha256: str
    at: str
    by: Optional[str] = None
    bytes: Optional[int] = None
    #: Free text: the user's reason for a manual commit ("figure as
    #: submitted to APL"), quotable alongside the sha in notes.
    note: Optional[str] = None
    #: What caused this snapshot: commit | reference | change | import.
    trigger: str = "commit"
    #: Set when a storage cap evicted this version. The record survives --
    #: it is a few hundred bytes and keeps the history readable -- but its
    #: blob is a candidate for `nebula gc` once nothing else references it.
    pending_gc: bool = False

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Snapshot":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class AssetMeta:
    """``asset.json``: everything about an asset except its bytes.

    Deliberately keyed by nothing -- the file's own directory names the
    asset, so this record stays valid across any rename of the file
    inside it.
    """
    id: str
    created: str
    #: The live file's current name on disk. A label, never an identity.
    name: str = ""
    #: Snapshot policy. "auto" (the default) defers to the archive's size
    #: ladder and is re-evaluated every time it is asked, so retuning the
    #: archive moves every asset that never made its own choice -- and an
    #: asset that grows past a threshold moves with it. Named rather than
    #: left empty so `asset show` can always print what it is.
    policy: str = AUTO_ASSET_POLICY
    #: Per-asset overrides; None means "use the archive setting".
    period_days: Optional[int] = None
    max_snapshots: Optional[int] = None
    max_snapshot_bytes: Optional[int] = None
    #: Ceiling above which an automatic snapshot downgrades to observed
    #: rather than failing. None means "use the archive setting".
    auto_max_bytes: Optional[int] = None

    #: Where the asset itself came from. A figure is very often derived
    #: from sacrosanct session data, and that link is knowable only at
    #: import time -- unlike tags or collections, it cannot be
    #: reconstructed later, so it is captured here from the start.
    derived_from: List[Dict[str, Optional[str]]] = field(default_factory=list)
    origin: Optional[str] = None
    imported_by: Optional[str] = None

    #: Cheap change detection: what the file looked like at the last scan.
    #: Lets an `observed` reference reuse a known sha instead of rehashing
    #: a huge file that has not moved.
    size: Optional[int] = None
    mtime: Optional[float] = None
    sha256: Optional[str] = None
    scanned_at: Optional[str] = None

    snapshots: List[Snapshot] = field(default_factory=list)
    #: Every name this asset has had: {at, from, to}. The id makes refs
    #: survive a rename; this makes the rename legible to a human reading
    #: the history.
    renames: List[Dict[str, str]] = field(default_factory=list)

    #: Keys written by a newer nebula. Preserved verbatim on rewrite, so an
    #: older checkout never strands a newer archive. Same contract as
    #: SidecarMeta.extra.
    extra: Dict[str, Any] = field(default_factory=dict)

    # -- policy resolution ------------------------------------------------

    def effective_policy(self, settings: ArchiveSettings) -> str:
        """The concrete policy in force right now. Resolves "auto" against
        the size ladder using the last scanned size, so the answer tracks
        the file rather than freezing at import."""
        pol = self.policy or AUTO_ASSET_POLICY
        if pol == AUTO_ASSET_POLICY:
            return default_policy_for_size(self.size or 0, settings)
        return pol if pol in ASSET_POLICIES else DEFAULT_ASSET_POLICY

    def effective_period_days(self, settings: ArchiveSettings) -> int:
        return (self.period_days if self.period_days is not None
                else settings.asset_period_days)

    def effective_auto_max_bytes(self, settings: ArchiveSettings) -> int:
        return (self.auto_max_bytes if self.auto_max_bytes is not None
                else settings.asset_manual_above)

    def latest_snapshot(self) -> Optional[Snapshot]:
        """The newest snapshot whose bytes are still claimed. Evicted
        records are skipped: they say a version existed, not that it can
        still be got back, and callers here are asking the latter."""
        for snap in reversed(self.snapshots):
            if not snap.pending_gc:
                return snap
        return None

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        extra = d.pop("extra")
        d.update(extra)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AssetMeta":
        known = {f.name for f in fields(cls)} - {"extra"}
        vals = {k: v for k, v in d.items() if k in known}
        vals["snapshots"] = [Snapshot.from_dict(s) for s in d.get("snapshots") or []]
        vals.setdefault("id", d.get("id", ""))
        vals.setdefault("created", d.get("created", ""))
        return cls(**vals, extra={k: v for k, v in d.items() if k not in known})


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Same contract as sidecar._atomic_write_json: a crash mid-write must
    never leave a half-written record."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_path(archive_root, asset_id: str) -> Path:
    return asset_dir(archive_root, asset_id) / ASSET_FILE


def read_asset(archive_root, asset_id: str) -> AssetMeta:
    path = record_path(archive_root, asset_id)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise AssetError(f"no such asset: {asset_id}") from None
    except ValueError as e:
        raise AssetError(f"unreadable {path}: {e}") from None
    return AssetMeta.from_dict(raw)


def write_asset(archive_root, meta: AssetMeta) -> Path:
    path = record_path(archive_root, meta.id)
    _atomic_write_json(path, meta.to_dict())
    return path


def live_file(archive_root, asset_id: str) -> Optional[Path]:
    """The asset's editable file: the one entry in its directory that is
    not the record. Returns None if the file is missing (deleted by hand),
    which callers report rather than treating as an empty asset."""
    d = asset_dir(archive_root, asset_id)
    if not d.is_dir():
        return None
    for entry in sorted(d.iterdir()):
        if entry.name == ASSET_FILE or entry.name.startswith("."):
            continue
        if entry.is_file():
            return entry
    return None


def list_assets(archive_root) -> List[str]:
    """Every asset id in the archive, newest year first. Reads the folder
    tree, not the index -- callers wanting speed should go through the
    index; this is the ground truth it is built from."""
    root = assets_root(archive_root)
    if not root.is_dir():
        return []
    out: List[str] = []
    for ydir in sorted(root.iterdir(), reverse=True):
        if not (ydir.is_dir() and ydir.name.isdigit()):
            continue
        for bucket in sorted(ydir.iterdir()):
            if not bucket.is_dir():
                continue
            for entry in sorted(bucket.iterdir()):
                if entry.is_dir() and _ID_RE.match(entry.name):
                    out.append(entry.name)
    return out


# ---------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------

def default_policy_for_size(size: int, settings: ArchiveSettings) -> str:
    """The policy an asset of this size gets unless the user says
    otherwise. A ladder rather than a single threshold, so medium files
    keep periodic history instead of falling straight to manual."""
    if size >= settings.asset_manual_above:
        return "manual"
    if size >= settings.asset_periodic_above:
        return "periodic"
    return settings.asset_policy or DEFAULT_ASSET_POLICY


def import_asset(
    archive_root,
    src: "str | Path",
    *,
    name: Optional[str] = None,
    policy: Optional[str] = None,
    derived_from: Optional[List[Any]] = None,
    origin: Optional[str] = None,
    by: Optional[str] = None,
    move: bool = False,
    snapshot_now: bool = True,
    now: Optional[datetime.datetime] = None,
) -> AssetMeta:
    """Bring a file under management as a new asset.

    `policy` of None means "derive it from the file's size", which is what
    the GUI pre-selects and what a bare CLI import gets.

    `snapshot_now` stores the imported bytes as the asset's first version
    unless the resolved policy is manual -- an asset with no snapshot at
    all has nothing for an early reference to pin to.
    """
    from nebula.sidecar import _ref_to_dict          # local: avoids a cycle
    from nebula.refs import parse_ref

    src = Path(src)
    if not src.is_file():
        raise AssetError(f"not a file: {src}")
    if policy is not None and policy not in ASSET_POLICIES:
        raise AssetError(
            f"unknown asset policy {policy!r}; expected one of "
            f"{', '.join(ASSET_POLICIES)}")

    settings = read_settings(archive_root, apply_env=False)
    size = src.stat().st_size
    resolved_policy = policy or default_policy_for_size(size, settings)

    asset_id = allocate_asset_id(archive_root, now=now)
    dest_dir = asset_dir(archive_root, asset_id)
    dest_dir.mkdir(parents=True, exist_ok=False)
    dest = dest_dir / (name or src.name)

    if move:
        shutil.move(str(src), str(dest))
    else:
        shutil.copy2(str(src), str(dest))

    refs = []
    for r in derived_from or []:
        refs.append(_ref_to_dict(parse_ref(r) if isinstance(r, str) else r))

    meta = AssetMeta(
        id=asset_id,
        created=_now_iso(),
        name=dest.name,
        # An unspecified policy is recorded as "auto", not as the value
        # the ladder happens to give today: the asset is choosing to
        # follow the archive, and that is a different statement from
        # choosing the policy the archive currently has.
        policy=policy or AUTO_ASSET_POLICY,
        derived_from=refs,
        origin=origin,
        imported_by=by or _default_user(),
    )
    _refresh_stat(dest, meta, sha=sha256_file(dest))
    write_asset(archive_root, meta)

    if snapshot_now and resolved_policy != "manual":
        commit(archive_root, asset_id, by=by, trigger="import",
               note="imported", settings=settings)
        meta = read_asset(archive_root, asset_id)
    return meta


def _default_user() -> Optional[str]:
    try:
        from nebula import identity
        return identity.get_user() or None
    except Exception:
        return None


def _refresh_stat(path: Path, meta: AssetMeta, *, sha: Optional[str]) -> None:
    st = path.stat()
    meta.name = path.name
    meta.size = st.st_size
    meta.mtime = st.st_mtime
    meta.scanned_at = _now_iso()
    if sha is not None:
        meta.sha256 = sha


# ---------------------------------------------------------------------
# Scanning: renames and edits
# ---------------------------------------------------------------------

def scan(archive_root, asset_id: str, *, rehash: bool = True) -> Dict[str, Any]:
    """Reconcile one asset's record with what is on disk.

    Returns what changed: ``{"renamed": (old, new) | None, "changed":
    bool, "missing": bool, "sha256": str | None}``.

    A rename here is unambiguous by construction -- the directory *is* the
    asset, so whatever single file is in it is this asset under a new
    name. No sha matching, no heuristics, and no way for an unrelated file
    that reuses an old name to inherit this identity.
    """
    meta = read_asset(archive_root, asset_id)
    path = live_file(archive_root, asset_id)
    out: Dict[str, Any] = {"renamed": None, "changed": False,
                           "missing": False, "sha256": meta.sha256}
    if path is None:
        out["missing"] = True
        return out

    st = path.stat()
    renamed = bool(meta.name) and path.name != meta.name
    # stat is the cheap gate: rehashing a 100 GB file on every scan would
    # make scanning cost more than the thing it protects.
    touched = (meta.size != st.st_size or meta.mtime != st.st_mtime
               or meta.sha256 is None)

    sha = meta.sha256
    if touched and rehash:
        sha = sha256_file(path)
        out["changed"] = sha != meta.sha256

    if renamed:
        meta.renames.append({"at": _now_iso(), "from": meta.name, "to": path.name})
        out["renamed"] = (meta.name, path.name)

    _refresh_stat(path, meta, sha=sha)
    out["sha256"] = sha
    if renamed or touched:
        write_asset(archive_root, meta)
    return out


# ---------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------

def commit(
    archive_root,
    asset_id: str,
    *,
    note: Optional[str] = None,
    by: Optional[str] = None,
    force: bool = False,
    trigger: str = "commit",
    settings: Optional[ArchiveSettings] = None,
) -> Optional[Snapshot]:
    """Store the asset's current bytes as a snapshot. Returns the new
    Snapshot, or None if the bytes are already stored.

    This is the deliberate save -- the git-commit-shaped verb -- so it
    works under *every* policy, including manual and never. A policy
    governs what happens unasked; it must not stand between a user and a
    version they explicitly want kept.

    `force` overrides the size ceiling that otherwise downgrades a big
    automatic snapshot to observed-only.
    """
    settings = settings or read_settings(archive_root, apply_env=False)
    meta = read_asset(archive_root, asset_id)
    path = live_file(archive_root, asset_id)
    if path is None:
        raise AssetError(f"{asset_id} has no file on disk")

    size = path.stat().st_size
    ceiling = meta.effective_auto_max_bytes(settings)
    # An explicit commit is allowed past the ceiling; an automatic one is
    # not, unless forced. Refusing to *record* the reference was never on
    # the table -- that is the failure mode this whole design avoids.
    if trigger != "commit" and not force and ceiling and size > ceiling:
        return None

    sha = sha256_file(path)
    latest = meta.latest_snapshot()
    if latest is not None and latest.sha256 == sha and not force:
        return None                      # already stored; nothing changed

    assetstore.store_blob_file(archive_root, path, digest=sha)
    snap = Snapshot(sha256=sha, at=_now_iso(), by=by or _default_user(),
                    bytes=size, note=note, trigger=trigger)
    meta.snapshots.append(snap)
    _refresh_stat(path, meta, sha=sha)
    _enforce_caps(meta, settings)
    write_asset(archive_root, meta)
    return snap


def _enforce_caps(meta: AssetMeta, settings: ArchiveSettings) -> None:
    """Evict the oldest snapshots past the asset's caps.

    Eviction never deletes a blob here. Blobs are content-addressed and
    may be shared with a session's pinned reference, so removing one at
    commit time could break a provenance link that has nothing to do with
    this cap -- the same reason git reclaims in `gc` rather than at commit.
    What eviction does is mark the record, leaving `nebula gc` to drop the
    bytes once nothing else claims them.

    So the cap bounds what this asset is *asking* to keep, not the
    archive's size on disk: a version some session pinned survives the cap
    it was evicted by, and it should.
    """
    max_n = (meta.max_snapshots if meta.max_snapshots is not None
             else settings.asset_max_snapshots)
    max_bytes = (meta.max_snapshot_bytes if meta.max_snapshot_bytes is not None
                 else settings.asset_max_snapshot_bytes)
    live = [s for s in meta.snapshots if not s.pending_gc]

    evict: List[Snapshot] = []
    if max_n and len(live) > max_n:
        evict.extend(live[:-max_n])
        live = live[-max_n:]

    if max_bytes:
        total = 0
        keep: List[Snapshot] = []
        # Walk newest-first so the cap always keeps the most recent
        # versions, which are the ones anyone is likely to want back.
        for snap in reversed(live):
            total += snap.bytes or 0
            if keep and total > max_bytes:
                evict.append(snap)
                continue
            keep.append(snap)

    for snap in evict:
        snap.pending_gc = True
    if settings.asset_cap_action == "drop":
        meta.snapshots = [s for s in meta.snapshots if not s.pending_gc]


def _due_for_periodic(meta: AssetMeta, settings: ArchiveSettings,
                      now: datetime.datetime) -> bool:
    latest = meta.latest_snapshot()
    if latest is None:
        return True
    at = _parse_iso(latest.at)
    if at is None:
        return True
    days = meta.effective_period_days(settings)
    return (now - at) >= datetime.timedelta(days=days)


def reference(
    archive_root,
    asset_id: str,
    *,
    by: Optional[str] = None,
    pin: Optional[bool] = None,
    now: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Record that something is about to derive from this asset, and
    return the ref dict to store in the referrer's sidecar.

    The returned dict carries the readable name *and* the id, so
    provenance stays legible while surviving a rename, plus the fidelity
    actually achieved:

        {"asset": "AF-26-0017", "file": "figure.svg",
         "sha256": "...", "fidelity": "pinned" | "observed"}

    `pin` forces (True) or suppresses (False) the snapshot regardless of
    policy; None means "follow the policy".
    """
    now = now or datetime.datetime.now().astimezone()
    settings = read_settings(archive_root, apply_env=False)
    state = scan(archive_root, asset_id)
    if state["missing"]:
        raise AssetError(f"{asset_id} has no file on disk")
    meta = read_asset(archive_root, asset_id)
    policy = meta.effective_policy(settings)

    if pin is None:
        want = (policy in ("on_reference", "every_change")
                or (policy == "periodic" and _due_for_periodic(meta, settings, now)))
    else:
        want = pin

    snap = None
    if want:
        snap = commit(archive_root, asset_id, by=by, trigger="reference",
                      force=(pin is True), settings=settings)
        if snap is None:
            # Either the bytes are already stored, or the size ceiling
            # declined the automatic write. The first is as good as a new
            # snapshot; the second is the downgrade to observed.
            meta = read_asset(archive_root, asset_id)
            latest = meta.latest_snapshot()
            if latest is not None and latest.sha256 == meta.sha256:
                snap = latest

    return {
        "asset": asset_id,
        "file": meta.name,
        "sha256": meta.sha256,
        "fidelity": "pinned" if snap is not None else "observed",
    }


def history(archive_root, asset_id: str) -> List[Snapshot]:
    """Every stored version of an asset, oldest first -- continuous across
    renames, which is the point of having an id at all."""
    return read_asset(archive_root, asset_id).snapshots


def set_policy(
    archive_root,
    asset_id: str,
    policy: Optional[str] = None,
    *,
    period_days: Optional[int] = None,
    max_snapshots: Optional[int] = None,
    max_snapshot_bytes: Optional[int] = None,
) -> AssetMeta:
    """Change one asset's policy and caps. Passing a value of -1 for any
    numeric field clears the override, returning the asset to the archive
    default rather than pinning it to today's value."""
    meta = read_asset(archive_root, asset_id)
    if policy is not None:
        if policy not in ASSET_POLICIES:
            raise AssetError(
                f"unknown asset policy {policy!r}; expected one of "
                f"{', '.join(ASSET_POLICIES)}")
        meta.policy = policy
    for name, val in (("period_days", period_days),
                      ("max_snapshots", max_snapshots),
                      ("max_snapshot_bytes", max_snapshot_bytes)):
        if val is not None:
            setattr(meta, name, None if val < 0 else val)
    write_asset(archive_root, meta)
    return meta
