"""
Disposable SQLite index over an archive's sessions and artifacts.

This is a cache, not a database of record: every row is a copy of
something that already exists in a session.yaml or a *.meta.json on disk.
Delete it and you lose nothing; rebuild it and you get it back. Nothing
here is ever written by a measurement script.

Freshness is a *read-side* property. Any writer that had to remember to
update the index would eventually forget -- a crashed run never reaches
close(), manual operations edit sidecars directly, another machine syncs
in whole sessions, and a person can always edit a file by hand. So
instead of trusting writers, :func:`ensure_fresh` compares what the index
recorded against a cheap stat signature of each session directory and
re-indexes only what actually changed. Readers call it and stop caring.

Three things keep that affordable on a large archive:

* **Signatures, not contents.** A session's signature covers the names,
  sizes and mtimes of its session.yaml and *.meta.json -- one scandir, no
  file reads. Sidecars are written atomically (mkstemp + os.replace into
  the same directory), so any change to one necessarily shows up here.
  Artifact data files and annotations.yaml are deliberately excluded:
  neither contributes to the index, and including them would re-index a
  session every time a tag was edited or a big data file was rewritten.
* **Year seals.** A finished year can be sealed (see :func:`seal_year`),
  recording a digest of its sessions in ``data/<year>/.year-seal.yaml``.
  A sealed year whose seal still matches what the index recorded is
  skipped whole -- one small file read instead of a stat per session.
* **Per-year batching.** The sweep holds one year in memory and commits
  per year, so peak memory doesn't grow with the archive.

Paths are stored *relative to the archive root*, so an archive can be
moved between machines, or synced, without invalidating its index.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import yaml

from nebula.registry import resolve_archive
from nebula.session import DATA_DIR
from nebula.sidecar import SESSION_FILE, SIDECAR_SUFFIX, read_session_yaml

INDEX_FILE = "index.db"

#: Bumped whenever the schema changes shape. A mismatch triggers a full
#: rebuild rather than a subtly-wrong query against old columns.
SCHEMA_VERSION = 3

#: Written into data/<year>/ by seal_year().
SEAL_FILE = ".year-seal.yaml"
SEAL_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    run_id TEXT PRIMARY KEY,
    rel_path TEXT NOT NULL,    -- relative to the archive root: the archive is movable
    created TEXT NOT NULL,
    status TEXT NOT NULL,
    tags TEXT NOT NULL,        -- JSON list
    description TEXT NOT NULL,
    hold_until TEXT,           -- NULL, "forever", or an ISO expiry timestamp
    history TEXT,              -- JSON list of manual-operation entries
    year TEXT NOT NULL,        -- the data/<year>/ bucket it lives in
    sig TEXT NOT NULL          -- stat signature of session.yaml + sidecars
);

CREATE TABLE IF NOT EXISTS related_runs (
    run_id TEXT NOT NULL,
    ref_archive TEXT,
    ref_session TEXT,
    ref_file TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    run_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    created TEXT,
    repo TEXT,
    commit_hash TEXT,
    dirty INTEGER,
    entry_point TEXT,
    inputs TEXT,                -- JSON
    source TEXT,                -- "script" or "external"
    origin TEXT,                -- free-text provenance for external files
    sha256 TEXT,
    PRIMARY KEY (run_id, filename)
);

CREATE TABLE IF NOT EXISTS derived_from (
    run_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    ref_archive TEXT,
    ref_session TEXT,
    ref_file TEXT,
    ref_asset TEXT,             -- AF-... when this edge points at an asset
    ref_sha256 TEXT,            -- the asset bytes seen at reference time
    ref_fidelity TEXT           -- "pinned" (blob stored) or "observed"
);

-- Assets are mutable by design, so unlike artifacts they carry no sealed
-- checksum to verify. What is indexed is their identity, current label
-- and policy -- enough to browse and filter without walking the tree.
CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    rel_path TEXT NOT NULL,
    name TEXT,                  -- current filename: a label, not identity
    created TEXT,
    scanned_at TEXT,
    size INTEGER,
    sha256 TEXT,
    policy TEXT,                -- as declared ("auto", "manual", ...)
    policy_resolved TEXT,       -- what "auto" currently resolves to
    origin TEXT,
    snapshots INTEGER,          -- retained (not pending_gc)
    snapshots_total INTEGER,
    sig TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS asset_derived_from (
    asset_id TEXT NOT NULL,
    ref_archive TEXT,
    ref_session TEXT,
    ref_file TEXT
);

CREATE TABLE IF NOT EXISTS year_seals (
    year TEXT PRIMARY KEY,
    digest TEXT NOT NULL,      -- digest the seal file claims
    seal_sig TEXT NOT NULL     -- stat signature of the seal file itself
);

CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created);
CREATE INDEX IF NOT EXISTS idx_sessions_year ON sessions(year);
CREATE INDEX IF NOT EXISTS idx_derived_from_run ON derived_from(run_id, filename);
-- The reverse edge: "what derives from this?" is the expensive direction,
-- since nothing records back-links. Without this it is a full table scan
-- per hop of a downstream traversal.
CREATE INDEX IF NOT EXISTS idx_derived_from_ref ON derived_from(ref_file, ref_session);
-- "which sessions used this asset?" is the asset-side downstream query,
-- and it is the reason an asset's history is worth keeping at all.
CREATE INDEX IF NOT EXISTS idx_derived_from_asset ON derived_from(ref_asset);
CREATE INDEX IF NOT EXISTS idx_assets_scanned ON assets(scanned_at);
"""


class IndexError_(Exception):
    """Something is wrong with the index that a rebuild won't fix."""


# --------------------------------------------------------------------------
# walking the archive
# --------------------------------------------------------------------------

def _year_dirs(archive_root: Path) -> List[Path]:
    """The data/<year>/ buckets, oldest first. Everything else at the
    archive root (code/, index.db, archive.yaml, .trash) is never walked."""
    data = Path(archive_root) / DATA_DIR
    if not data.is_dir():
        return []
    out = []
    with os.scandir(data) as it:
        for entry in it:
            if entry.is_dir() and entry.name.isdigit():
                out.append(Path(entry.path))
    return sorted(out)


def _session_dirs_in(year_dir: Path) -> List[Path]:
    out = []
    try:
        with os.scandir(year_dir) as it:
            for entry in it:
                if entry.is_dir() and (Path(entry.path) / SESSION_FILE).exists():
                    out.append(Path(entry.path))
    except OSError:
        return []
    return sorted(out)


def _iter_session_dirs(archive_root: Path) -> Iterator[Path]:
    archive_root = Path(archive_root)
    if not archive_root.exists():
        return
    for year_dir in _year_dirs(archive_root):
        for session_dir in _session_dirs_in(year_dir):
            yield session_dir


def session_signature(session_dir) -> str:
    """A cheap fingerprint of everything in this session that the index
    cares about: session.yaml and the sidecars, by name, size and mtime.

    One scandir, no file contents. Returns "" for a directory that has
    gone away, which reads naturally as "nothing indexed here".
    """
    parts: List[str] = []
    try:
        with os.scandir(session_dir) as it:
            for entry in it:
                name = entry.name
                if name != SESSION_FILE and not name.endswith(SIDECAR_SUFFIX):
                    continue        # data files and annotations.yaml don't matter
                try:
                    st = entry.stat()
                except OSError:
                    continue
                parts.append(f"{name}:{st.st_size}:{st.st_mtime_ns}")
    except OSError:
        return ""
    if not parts:
        return ""
    parts.sort()
    return hashlib.blake2b("\n".join(parts).encode(), digest_size=16).hexdigest()


def asset_signature(asset_dir) -> str:
    """A cheap fingerprint of one asset directory, covering ``asset.json``
    and *nothing else*.

    The asset's own bytes are deliberately excluded. Assets exist to be
    edited constantly, and every field the index holds about one comes
    from the record, not the file -- so signing the bytes would re-index
    an asset every time somebody nudged an SVG, for no gain. This is the
    same trade session_signature makes when it skips artifact data files,
    and it matters much more here.

    A rename or an edit still reaches the index: both go through
    assets.scan(), which rewrites asset.json (atomically, so the stat
    signature necessarily moves).
    """
    from nebula.assets import ASSET_FILE

    try:
        st = (Path(asset_dir) / ASSET_FILE).stat()
    except OSError:
        return ""
    return f"{ASSET_FILE}:{st.st_size}:{st.st_mtime_ns}"


def _file_signature(path: Path) -> str:
    """Same idea for a single file (used for the seal file itself)."""
    try:
        st = path.stat()
    except OSError:
        return ""
    return f"{st.st_size}:{st.st_mtime_ns}"


# --------------------------------------------------------------------------
# connections
# --------------------------------------------------------------------------

def index_path_for(archive_root, index_path: Optional[Path] = None) -> Path:
    return Path(index_path) if index_path else Path(archive_root) / INDEX_FILE


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    # WAL: reads can now trigger writes (ensure_fresh), so a reader and a
    # sweeper in different processes must not block each other into an error.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
    except sqlite3.DatabaseError:
        pass
    return conn


def _get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.DatabaseError:
        return None
    return row["value"] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def rebuild(archive: "str | Path", index_path: Optional[Path] = None) -> Path:
    """Walk the archive and rebuild the SQLite index from scratch.

    `archive` follows the same resolution rule as nebula.session(): a str
    is looked up as a registered archive name (KeyError if unknown), a
    Path is used literally. Returns the path to the index file.
    """
    archive_root, _ = resolve_archive(archive)
    archive_root = Path(archive_root)
    target = index_path_for(archive_root, index_path)

    # Build into a temp file then swap in, so a reader querying the old
    # index mid-rebuild never sees a half-written database.
    tmp_path = target.with_suffix(".db.tmp")
    for leftover in (tmp_path, Path(str(tmp_path) + "-wal"), Path(str(tmp_path) + "-shm")):
        if leftover.exists():
            leftover.unlink()

    conn = _connect(tmp_path)
    try:
        conn.executescript(SCHEMA)
        _set_meta(conn, "schema_version", SCHEMA_VERSION)
        for year_dir in _year_dirs(archive_root):
            for session_dir in _session_dirs_in(year_dir):
                _index_session(conn, archive_root, session_dir)
            _record_seal(conn, archive_root, year_dir.name)
            conn.commit()          # one commit per year: bounded WAL growth
        # Assets live outside data/<year>/ entirely, so they are swept once
        # after the year walk rather than inside it.
        _sweep_assets(conn, archive_root,
                      {"added": 0, "updated": 0, "removed": 0})
        conn.commit()
        _set_meta(conn, "built", _now())
        conn.commit()
    finally:
        # Checkpoint into the main file so the swap doesn't leave the data
        # stranded in a -wal file that we are about to orphan.
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:
            pass
        conn.close()

    for suffix in ("-wal", "-shm"):
        stray = Path(str(tmp_path) + suffix)
        if stray.exists():
            stray.unlink()
    tmp_path.replace(target)
    return target


def _rel(archive_root: Path, path: Path) -> str:
    """Path relative to the archive root, in POSIX form so an index built
    on one platform reads correctly on another."""
    try:
        return Path(path).relative_to(archive_root).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _index_session(conn: sqlite3.Connection, archive_root: Path, session_dir: Path) -> None:
    """(Re-)index one session. Idempotent: every table is cleared for this
    run_id first, so a session that lost a file doesn't keep a ghost row."""
    session_dir = Path(session_dir)
    meta = read_session_yaml(session_dir)
    run_id = meta.run_id
    sig = session_signature(session_dir)
    year = session_dir.parent.name

    conn.execute(
        "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            _rel(archive_root, session_dir),
            meta.created,
            meta.status,
            json.dumps(meta.tags),
            meta.description,
            meta.hold_until,
            json.dumps(meta.history),
            year,
            sig,
        ),
    )
    conn.execute("DELETE FROM related_runs WHERE run_id = ?", (run_id,))
    for r in meta.related_runs:
        conn.execute(
            "INSERT INTO related_runs VALUES (?, ?, ?, ?)",
            (run_id, r.get("archive"), r.get("session"), r.get("file")),
        )

    # Clear both artifact tables for this session rather than relying on
    # INSERT OR REPLACE: a file that was renamed or moved to .trash since
    # the last index must not survive as a row nothing will ever overwrite.
    conn.execute("DELETE FROM artifacts WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM derived_from WHERE run_id = ?", (run_id,))

    for sidecar_path in sorted(session_dir.glob(f"*{SIDECAR_SUFFIX}")):
        filename = sidecar_path.name[: -len(SIDECAR_SUFFIX)]
        try:
            with open(sidecar_path, "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue        # unreadable sidecar: check/reconcile reports it
        produced_by = data.get("produced_by", {}) or {}
        dirty = produced_by.get("dirty")
        conn.execute(
            "INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                filename,
                _rel(archive_root, session_dir / filename),
                data.get("created"),
                produced_by.get("repo"),
                produced_by.get("commit"),
                int(bool(dirty)) if dirty is not None else None,
                produced_by.get("entry_point"),
                json.dumps(data.get("inputs", {})),
                produced_by.get("source"),
                produced_by.get("origin"),
                data.get("sha256"),
            ),
        )
        for r in data.get("derived_from", []):
            conn.execute(
                "INSERT INTO derived_from VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, filename, r.get("archive"), r.get("session"),
                 r.get("file"), r.get("asset"), r.get("sha256"),
                 r.get("fidelity")),
            )


def _forget_session(conn: sqlite3.Connection, run_id: str) -> None:
    for table in ("sessions", "related_runs", "artifacts", "derived_from"):
        conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))


def _index_asset(conn: sqlite3.Connection, archive_root: Path,
                 asset_id: str, settings) -> None:
    """(Re-)index one asset from its record. Never reads the asset's own
    bytes: everything indexed lives in asset.json."""
    from nebula import assets as assets_mod

    try:
        meta = assets_mod.read_asset(archive_root, asset_id)
    except assets_mod.AssetError:
        return          # unreadable record: check reports it, index skips it

    retained = sum(1 for s in meta.snapshots if not s.pending_gc)
    conn.execute(
        "INSERT OR REPLACE INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            meta.id,
            _rel(archive_root, assets_mod.asset_dir(archive_root, meta.id)),
            meta.name,
            meta.created,
            meta.scanned_at,
            meta.size,
            meta.sha256,
            meta.policy,
            meta.effective_policy(settings),
            meta.origin,
            retained,
            len(meta.snapshots),
            asset_signature(assets_mod.asset_dir(archive_root, meta.id)),
        ),
    )
    conn.execute("DELETE FROM asset_derived_from WHERE asset_id = ?", (meta.id,))
    for r in meta.derived_from:
        conn.execute(
            "INSERT INTO asset_derived_from VALUES (?, ?, ?, ?)",
            (meta.id, r.get("archive"), r.get("session"), r.get("file")),
        )


def _forget_asset(conn: sqlite3.Connection, asset_id: str) -> None:
    for table in ("assets", "asset_derived_from"):
        conn.execute(f"DELETE FROM {table} WHERE asset_id = ?", (asset_id,))


def _sweep_assets(conn: sqlite3.Connection, archive_root: Path,
                  summary: Dict) -> None:
    """Bring the assets table into line with the tree, by signature.

    Assets are not year-sealed: unlike a session, an asset is never
    finished, so there is nothing to seal and every one is checked. That
    is affordable because the check is one stat per asset.
    """
    from nebula import assets as assets_mod
    from nebula import config as config_mod

    settings = config_mod.read_settings(archive_root, apply_env=False)
    known = {row["asset_id"]: row["sig"]
             for row in conn.execute("SELECT asset_id, sig FROM assets").fetchall()}
    seen = set()
    for asset_id in assets_mod.list_assets(archive_root):
        seen.add(asset_id)
        sig = asset_signature(assets_mod.asset_dir(archive_root, asset_id))
        if not sig:
            continue                       # no record: an orphan dir, check's job
        if known.get(asset_id) == sig:
            continue
        _index_asset(conn, archive_root, asset_id, settings)
        summary["added" if asset_id not in known else "updated"] += 1

    for asset_id in set(known) - seen:
        _forget_asset(conn, asset_id)
        summary["removed"] += 1


def update_session(archive: "str | Path", session_dir, *,
                   index_path: Optional[Path] = None) -> bool:
    """Re-index a single session in place, leaving the rest of the index
    untouched. Returns False if there is no index to update.

    This is the fast path used when a session closes: the sweep in
    ensure_fresh() would find the same change on the next read, so this is
    an optimisation, never the thing correctness depends on. It refuses to
    *create* an index, because a database holding one session would look
    authoritative to `nebula ls` while describing almost nothing.
    """
    archive_root, _ = resolve_archive(archive)
    archive_root = Path(archive_root)
    target = index_path_for(archive_root, index_path)
    if not target.is_file():
        return False
    try:
        conn = _connect(target)
    except sqlite3.DatabaseError:
        return False
    try:
        if _get_meta(conn, "schema_version") != str(SCHEMA_VERSION):
            return False      # let the next ensure_fresh() do the migration
        _index_session(conn, archive_root, Path(session_dir))
        conn.commit()
    except (sqlite3.DatabaseError, OSError, ValueError):
        return False
    finally:
        conn.close()
    return True


# --------------------------------------------------------------------------
# year seals
# --------------------------------------------------------------------------

def seal_path(archive_root, year: "str | int") -> Path:
    return Path(archive_root) / DATA_DIR / str(year) / SEAL_FILE


def year_digest(archive_root, year: "str | int") -> Tuple[str, int]:
    """A digest over every session signature in a year, plus the count.

    This is what a seal claims. It is derived from the same stat
    signatures the sweep uses, so sealing costs one scandir per session
    and verifying costs the same -- no file contents are read.
    """
    year_dir = Path(archive_root) / DATA_DIR / str(year)
    lines = []
    for session_dir in _session_dirs_in(year_dir):
        lines.append(f"{session_dir.name}:{session_signature(session_dir)}")
    digest = hashlib.blake2b("\n".join(lines).encode(), digest_size=16).hexdigest()
    return digest, len(lines)


def read_seal(archive_root, year: "str | int") -> Optional[Dict]:
    path = seal_path(archive_root, year)
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return None
    return raw if isinstance(raw, dict) else None


def seal_year(archive: "str | Path", year: "str | int", *, force: bool = False) -> Path:
    """Record that a year is finished, so freshness sweeps can skip it.

    A seal is a *claim*, not a lock: nothing stops a file under a sealed
    year from being edited, and if one is, the index will not notice --
    that is precisely the cost being traded for the speed. `nebula check`
    verifies seals, which is where such a change surfaces.

    Refuses the current year, and any year still holding an open session,
    unless forced.
    """
    archive_root, _ = resolve_archive(archive)
    archive_root = Path(archive_root)
    year = str(year)
    year_dir = archive_root / DATA_DIR / year
    if not year_dir.is_dir():
        raise IndexError_(f"no sessions for {year} in this archive")

    if not force:
        this_year = str(datetime.date.today().year)
        if year == this_year:
            raise IndexError_(
                f"{year} is the current year and will still gain sessions; "
                f"seal it next year, or pass force=True")
        open_runs = []
        for session_dir in _session_dirs_in(year_dir):
            try:
                if read_session_yaml(session_dir).status == "open":
                    open_runs.append(session_dir.name)
            except Exception:       # noqa: BLE001 -- unreadable is also not sealable
                open_runs.append(session_dir.name)
        if open_runs:
            raise IndexError_(
                f"{year} still has unfinished sessions ({', '.join(open_runs[:5])}"
                f"{'...' if len(open_runs) > 5 else ''}); close them or pass force=True")

    digest, count = year_digest(archive_root, year)
    payload = {
        "version": SEAL_VERSION,
        "year": year,
        "sessions": count,
        "digest": digest,
        "sealed": _now(),
    }
    path = seal_path(archive_root, year)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        yaml.safe_dump(payload, f, sort_keys=False)
    os.replace(tmp, path)

    # Drop any recorded seal for this year rather than recording the new
    # one. A seal earns its skip only after a sweep has actually brought
    # the year into the index under that exact seal -- otherwise sealing a
    # year you had just added sessions to would freeze the index at the
    # state before them.
    target = index_path_for(archive_root)
    if target.is_file():
        try:
            conn = _connect(target)
            try:
                conn.execute("DELETE FROM year_seals WHERE year = ?", (year,))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            pass
    return path


def unseal_year(archive: "str | Path", year: "str | int") -> bool:
    """Remove a year's seal, so it is swept normally again."""
    archive_root, _ = resolve_archive(archive)
    path = seal_path(archive_root, year)
    existed = path.is_file()
    if existed:
        path.unlink()
    target = index_path_for(archive_root)
    if target.is_file():
        try:
            conn = _connect(target)
            try:
                conn.execute("DELETE FROM year_seals WHERE year = ?", (str(year),))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            pass
    return existed


def verify_year_seal(archive: "str | Path", year: "str | int") -> Dict:
    """Recompute a sealed year and compare it with what the seal claims.

    Returns {sealed, ok, expected, actual, sessions, sealed_at, detail}.
    A sealed year that no longer matches means something under it changed
    after it was declared finished -- which may be entirely legitimate
    (a deliberate repair), but should never pass unnoticed.
    """
    archive_root, _ = resolve_archive(archive)
    year = str(year)
    seal = read_seal(archive_root, year)
    if seal is None:
        return {"sealed": False, "ok": True, "year": year, "detail": "not sealed"}
    digest, count = year_digest(archive_root, year)
    ok = digest == seal.get("digest")
    return {
        "sealed": True, "ok": ok, "year": year,
        "expected": seal.get("digest"), "actual": digest,
        "sessions": count, "sealed_sessions": seal.get("sessions"),
        "sealed_at": seal.get("sealed"),
        "detail": _seal_detail(year, ok, seal.get("sessions"), count),
    }


def _seal_detail(year: str, ok: bool, sealed_count, count: int) -> str:
    """Say *what* changed. A count that moved is the obvious case; an equal
    count with a different digest means a file inside a session was edited,
    which is the case a session tally would have quietly missed."""
    if ok:
        return "seal matches"
    if sealed_count != count:
        return (f"{year} changed since it was sealed: "
                f"{sealed_count} -> {count} session(s)")
    return (f"{year} changed since it was sealed: still {count} session(s), "
            f"but their contents no longer match the seal")


def _record_seal(conn: sqlite3.Connection, archive_root: Path, year: str) -> None:
    """Note in the index which seal (and which seal *file*) a year was last
    verified against. Both are needed: the digest says what was claimed,
    the file signature says whether the claim itself has been rewritten."""
    seal = read_seal(archive_root, year)
    if not seal or not seal.get("digest"):
        conn.execute("DELETE FROM year_seals WHERE year = ?", (year,))
        return
    conn.execute(
        "INSERT OR REPLACE INTO year_seals VALUES (?, ?, ?)",
        (year, str(seal["digest"]), _file_signature(seal_path(archive_root, year))),
    )


def sealed_years(archive: "str | Path") -> List[Dict]:
    """Every year that carries a seal, newest first, with its claim."""
    archive_root, _ = resolve_archive(archive)
    out = []
    for year_dir in _year_dirs(Path(archive_root)):
        seal = read_seal(archive_root, year_dir.name)
        if seal:
            out.append({"year": year_dir.name, "sessions": seal.get("sessions"),
                        "digest": seal.get("digest"), "sealed": seal.get("sealed")})
    return sorted(out, key=lambda s: s["year"], reverse=True)


# --------------------------------------------------------------------------
# freshness
# --------------------------------------------------------------------------

def ensure_fresh(archive: "str | Path", *, index_path: Optional[Path] = None,
                 allow_rebuild: bool = True) -> Dict:
    """Bring the index into line with what is actually on disk, cheaply.

    Statting each session costs microseconds and no file reads, so this is
    orders of magnitude cheaper than the walk a caller would otherwise do
    itself -- and it is what lets every other function here simply trust
    the index.

    Returns a summary: {"rebuilt", "updated", "added", "removed",
    "skipped_years", "checked_sessions"}.
    """
    archive_root, _ = resolve_archive(archive)
    archive_root = Path(archive_root)
    target = index_path_for(archive_root, index_path)
    summary = {"rebuilt": False, "updated": 0, "added": 0, "removed": 0,
               "skipped_years": [], "checked_sessions": 0}

    def full_rebuild(reason: str) -> Dict:
        if not allow_rebuild:
            summary["reason"] = reason
            return summary
        rebuild(archive_root, index_path=index_path)
        summary.update(rebuilt=True, reason=reason)
        return summary

    if not target.is_file():
        return full_rebuild("no index yet")

    try:
        conn = _connect(target)
    except sqlite3.DatabaseError:
        # The index is a disposable cache; a corrupt one is not an error to
        # propagate to someone who just wanted to list their sessions.
        _discard(target)
        return full_rebuild("index was unreadable")

    try:
        # sqlite3.connect() on a non-database succeeds; it only complains
        # when something actually reads. Ask a cheap question first so a
        # corrupt file is reported as corrupt rather than as "old schema".
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError:
        conn.close()
        _discard(target)
        return full_rebuild("index was unreadable")

    try:
        if _get_meta(conn, "schema_version") != str(SCHEMA_VERSION):
            conn.close()
            return full_rebuild("index schema is out of date")

        seen_years = set()
        for year_dir in _year_dirs(archive_root):
            year = year_dir.name
            seen_years.add(year)
            if _seal_lets_us_skip(conn, archive_root, year):
                summary["skipped_years"].append(year)
                continue
            _sweep_year(conn, archive_root, year_dir, summary)
            _record_seal(conn, archive_root, year)
            conn.commit()

        _sweep_assets(conn, archive_root, summary)
        conn.commit()

        # A whole year directory can disappear (moved away, or an archive
        # trimmed); its sessions must not linger in the index.
        for row in conn.execute("SELECT DISTINCT year FROM sessions").fetchall():
            if row["year"] not in seen_years:
                for s in conn.execute("SELECT run_id FROM sessions WHERE year = ?",
                                      (row["year"],)).fetchall():
                    _forget_session(conn, s["run_id"])
                    summary["removed"] += 1
        if summary["removed"]:
            conn.commit()
        if summary["updated"] or summary["added"] or summary["removed"]:
            _set_meta(conn, "built", _now())
            conn.commit()
    except sqlite3.DatabaseError:
        conn.close()
        _discard(target)
        return full_rebuild("index went bad mid-sweep")
    finally:
        try:
            conn.close()
        except sqlite3.DatabaseError:
            pass
    return summary


def _seal_lets_us_skip(conn: sqlite3.Connection, archive_root: Path, year: str) -> bool:
    """True when this year is sealed, the index already knows that exact
    seal, and the seal file has not been rewritten since. Anything else
    (unsealed, newly sealed, re-sealed, never indexed) falls through to a
    normal sweep -- so a seal can only ever skip work that a previous pass
    already did."""
    seal = read_seal(archive_root, year)
    if not seal or not seal.get("digest"):
        return False
    row = conn.execute("SELECT digest, seal_sig FROM year_seals WHERE year = ?",
                       (year,)).fetchone()
    if row is None:
        return False
    if row["digest"] != str(seal["digest"]):
        return False
    return row["seal_sig"] == _file_signature(seal_path(archive_root, year))


def _sweep_year(conn: sqlite3.Connection, archive_root: Path, year_dir: Path,
                summary: Dict) -> bool:
    """Compare one year on disk against the index. Returns whether
    anything changed. Memory stays bounded to a single year."""
    year = year_dir.name
    # Keyed by relative path, not run_id: the directory is what exists on
    # disk, and keying on it means a session whose yaml disagrees with its
    # folder name is still matched up instead of being re-indexed forever.
    indexed = {row["rel_path"]: (row["run_id"], row["sig"]) for row in conn.execute(
        "SELECT run_id, rel_path, sig FROM sessions WHERE year = ?", (year,)).fetchall()}
    changed = False
    on_disk = set()

    for session_dir in _session_dirs_in(year_dir):
        summary["checked_sessions"] += 1
        rel = _rel(archive_root, session_dir)
        on_disk.add(rel)
        known = indexed.get(rel)
        sig = session_signature(session_dir)
        if known and known[1] == sig and sig:
            continue                       # untouched: no file read at all
        try:
            _index_session(conn, archive_root, session_dir)
        except (OSError, ValueError):
            continue                       # half-written session; try again later
        summary["updated" if known else "added"] += 1
        changed = True

    for rel in set(indexed) - on_disk:
        _forget_session(conn, indexed[rel][0])
        summary["removed"] += 1
        changed = True
    return changed


def pending_changes(archive: "str | Path", index_path: Optional[Path] = None) -> Dict:
    """What a sweep *would* do, without doing it.

    Status displays need this: "is my index stale?" used to be answered by
    comparing session counts, which silently misses every edit that doesn't
    change how many sessions exist -- a reseal, a repaired sidecar, an
    imported file. Signatures answer it properly, and reading them costs
    the same stat-per-session the sweep costs, with no writes.
    """
    archive_root, _ = resolve_archive(archive)
    archive_root = Path(archive_root)
    target = index_path_for(archive_root, index_path)
    out = {"exists": target.is_file(), "stale": True, "added": 0, "updated": 0,
           "removed": 0, "skipped_years": [], "reason": None}
    if not out["exists"]:
        out["reason"] = "no index yet"
        return out
    try:
        conn = _connect(target)
    except sqlite3.DatabaseError:
        out["reason"] = "index is unreadable"
        return out
    try:
        if _get_meta(conn, "schema_version") != str(SCHEMA_VERSION):
            out["reason"] = "index schema is out of date"
            return out
        seen_years = set()
        for year_dir in _year_dirs(archive_root):
            year = year_dir.name
            seen_years.add(year)
            if _seal_lets_us_skip(conn, archive_root, year):
                out["skipped_years"].append(year)
                continue
            indexed = {row["rel_path"]: row["sig"] for row in conn.execute(
                "SELECT rel_path, sig FROM sessions WHERE year = ?", (year,)).fetchall()}
            on_disk = set()
            for session_dir in _session_dirs_in(year_dir):
                rel = _rel(archive_root, session_dir)
                on_disk.add(rel)
                sig = session_signature(session_dir)
                if rel not in indexed:
                    out["added"] += 1
                elif indexed[rel] != sig or not sig:
                    out["updated"] += 1
            out["removed"] += len(set(indexed) - on_disk)
        for row in conn.execute("SELECT year, count(*) AS n FROM sessions "
                                "GROUP BY year").fetchall():
            if row["year"] not in seen_years:
                out["removed"] += row["n"]
    except sqlite3.DatabaseError:
        out["reason"] = "index went bad while reading"
        return out
    finally:
        conn.close()
    out["stale"] = bool(out["added"] or out["updated"] or out["removed"])
    if out["stale"]:
        out["reason"] = "sessions have changed on disk since the last index"
    return out


def _discard(target: Path) -> None:
    for path in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm")):
        try:
            path.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def open_index(archive: "str | Path", index_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open the index without touching it. Raises FileNotFoundError if
    there isn't one. Prefer :func:`open_fresh` unless you specifically want
    to see the index exactly as it is on disk.

    `archive` follows the same resolution rule as nebula.session()."""
    archive_root, _ = resolve_archive(archive)
    target = index_path_for(archive_root, index_path)
    if not target.exists():
        raise FileNotFoundError(
            f"no index at {target}; call nebula.index.rebuild() first"
        )
    return _connect(target)


def open_fresh(archive: "str | Path", index_path: Optional[Path] = None) -> sqlite3.Connection:
    """Sweep for changes, then open. This is what query code should use:
    the sweep is cheap, and it means a caller never has to wonder whether
    somebody remembered to rebuild."""
    archive_root, _ = resolve_archive(archive)
    ensure_fresh(archive_root, index_path=index_path)
    return open_index(archive_root, index_path=index_path)


def session_path(archive_root, row) -> Path:
    """Absolute path of a session row. Paths are stored relative, so this
    is the one place that reattaches them to wherever the archive lives
    on *this* machine."""
    return Path(archive_root) / row["rel_path"]


def status(archive: "str | Path", index_path: Optional[Path] = None) -> Dict:
    """What the index knows, without changing it -- for status panels."""
    archive_root, _ = resolve_archive(archive)
    target = index_path_for(archive_root, index_path)
    out = {"exists": target.is_file(), "path": str(target), "size": None,
           "built": None, "sessions": None, "schema_version": None,
           "current_schema": SCHEMA_VERSION, "usable": False,
           "sealed_years": sealed_years(archive_root)}
    if not out["exists"]:
        return out
    out["size"] = target.stat().st_size
    try:
        conn = _connect(target)
    except sqlite3.DatabaseError:
        return out
    try:
        out["schema_version"] = _get_meta(conn, "schema_version")
        out["built"] = _get_meta(conn, "built")
        out["sessions"] = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
        out["usable"] = out["schema_version"] == str(SCHEMA_VERSION)
    except sqlite3.DatabaseError:
        pass
    finally:
        conn.close()
    return out


def flag_stale_open_sessions(conn: sqlite3.Connection, older_than_hours: float = 24.0):
    """Return sessions still marked 'open' whose created timestamp is
    older than the given threshold -- likely a crashed script that never
    hit __exit__. A cheap sanity check to run after rebuild()."""
    cutoff = datetime.datetime.now().astimezone() - datetime.timedelta(
        hours=older_than_hours
    )
    rows = conn.execute(
        "SELECT run_id, rel_path, created FROM sessions WHERE status = 'open'"
    ).fetchall()
    stale = []
    for row in rows:
        try:
            created = datetime.datetime.fromisoformat(row["created"])
        except (TypeError, ValueError):
            continue
        if created < cutoff:
            stale.append(row)
    return stale
