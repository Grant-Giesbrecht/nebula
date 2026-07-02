"""
Disposable SQLite index over an archive's sessions and artifacts.

This is a cache, not a database of record: it is always rebuilt from
scratch by walking session.yaml + *.meta.json files, never written to
directly by measurement scripts. If it gets corrupted or out of sync,
delete it and call rebuild() again.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterator, Optional

from nebula.registry import resolve_archive
from nebula.sidecar import SESSION_FILE, SIDECAR_SUFFIX, read_session_yaml

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    run_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    created TEXT NOT NULL,
    status TEXT NOT NULL,
    tags TEXT NOT NULL,        -- JSON list
    description TEXT NOT NULL,
    hold_until TEXT            -- NULL, "forever", or an ISO expiry timestamp
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
    path TEXT NOT NULL,
    created TEXT,
    repo TEXT,
    commit_hash TEXT,
    dirty INTEGER,
    entry_point TEXT,
    inputs TEXT,                -- JSON
    PRIMARY KEY (run_id, filename)
);

CREATE TABLE IF NOT EXISTS derived_from (
    run_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    ref_archive TEXT,
    ref_session TEXT,
    ref_file TEXT
);

CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_derived_from_run ON derived_from(run_id, filename);
"""


def _iter_session_dirs(archive_root: Path) -> Iterator[Path]:
    archive_root = Path(archive_root)
    if not archive_root.exists():
        return
    for year_dir in sorted(archive_root.iterdir()):
        if not year_dir.is_dir():
            continue
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir():
                continue
            for session_dir in sorted(month_dir.iterdir()):
                if (session_dir / SESSION_FILE).exists():
                    yield session_dir


def rebuild(archive: "str | Path", index_path: Optional[Path] = None) -> Path:
    """Walk the archive and rebuild the SQLite index from scratch.

    `archive` follows the same resolution rule as nebula.session(): a str
    is looked up as a registered archive name (KeyError if unknown), a
    Path is used literally. Returns the path to the index file.
    """
    archive_root, _ = resolve_archive(archive)
    index_path = Path(index_path) if index_path else archive_root / "index.db"

    # Build into a temp file then swap in, so a reader querying the old
    # index mid-rebuild never sees a half-written database.
    tmp_path = index_path.with_suffix(".db.tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    conn = sqlite3.connect(tmp_path)
    try:
        conn.executescript(SCHEMA)
        for session_dir in _iter_session_dirs(archive_root):
            _index_session(conn, session_dir)
        conn.commit()
    finally:
        conn.close()

    tmp_path.replace(index_path)
    return index_path


def _index_session(conn: sqlite3.Connection, session_dir: Path) -> None:
    meta = read_session_yaml(session_dir)
    conn.execute(
        "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            meta.run_id,
            str(session_dir),
            meta.created,
            meta.status,
            json.dumps(meta.tags),
            meta.description,
            meta.hold_until,
        ),
    )
    conn.execute("DELETE FROM related_runs WHERE run_id = ?", (meta.run_id,))
    for r in meta.related_runs:
        conn.execute(
            "INSERT INTO related_runs VALUES (?, ?, ?, ?)",
            (meta.run_id, r.get("archive"), r.get("session"), r.get("file")),
        )

    for sidecar_path in sorted(session_dir.glob(f"*{SIDECAR_SUFFIX}")):
        filename = sidecar_path.name[: -len(SIDECAR_SUFFIX)]
        with open(sidecar_path, "r") as f:
            data = json.load(f)
        produced_by = data.get("produced_by", {})
        conn.execute(
            "INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                meta.run_id,
                filename,
                str(session_dir / filename),
                data.get("created"),
                produced_by.get("repo"),
                produced_by.get("commit"),
                int(bool(produced_by.get("dirty"))) if produced_by.get("dirty") is not None else None,
                produced_by.get("entry_point"),
                json.dumps(data.get("inputs", {})),
            ),
        )
        conn.execute(
            "DELETE FROM derived_from WHERE run_id = ? AND filename = ?",
            (meta.run_id, filename),
        )
        for r in data.get("derived_from", []):
            conn.execute(
                "INSERT INTO derived_from VALUES (?, ?, ?, ?, ?)",
                (meta.run_id, filename, r.get("archive"), r.get("session"), r.get("file")),
            )


def open_index(archive: "str | Path", index_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open the index read-only-ish (no schema assumptions beyond what
    rebuild() creates). Does NOT rebuild automatically -- call rebuild()
    explicitly (e.g. on a schedule or on demand) to refresh it.

    `archive` follows the same resolution rule as nebula.session()."""
    archive_root, _ = resolve_archive(archive)
    index_path = Path(index_path) if index_path else archive_root / "index.db"
    if not index_path.exists():
        raise FileNotFoundError(
            f"no index at {index_path}; call nebula.index.rebuild() first"
        )
    conn = sqlite3.connect(index_path)
    conn.row_factory = sqlite3.Row
    return conn


def flag_stale_open_sessions(conn: sqlite3.Connection, older_than_hours: float = 24.0):
    """Return sessions still marked 'open' whose created timestamp is
    older than the given threshold -- likely a crashed script that never
    hit __exit__. A cheap sanity check to run after rebuild()."""
    import datetime

    cutoff = datetime.datetime.now().astimezone() - datetime.timedelta(
        hours=older_than_hours
    )
    rows = conn.execute(
        "SELECT run_id, path, created FROM sessions WHERE status = 'open'"
    ).fetchall()
    stale = []
    for row in rows:
        created = datetime.datetime.fromisoformat(row["created"])
        if created < cutoff:
            stale.append(row)
    return stale
