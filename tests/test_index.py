"""The index as a self-maintaining cache: freshness sweeps, portability,
year seals, and the promise that a broken index is never fatal."""

import sqlite3
import shutil
import time
from pathlib import Path

import pytest

import nebula
from nebula import graph, index


def _session(archive, description="run", **kw):
    s = nebula.new(archive, description=description, **kw)
    with s.artifact("raw.csv") as fn:
        fn.write_text("1,2\n")
    s.close()
    return s


def _touch_sidecar(session_path, name="raw.csv"):
    """Rewrite a sidecar with identical content, so only its mtime moves --
    the change a session-count comparison would miss."""
    time.sleep(0.01)
    sc = Path(session_path) / f"{name}.meta.json"
    sc.write_text(sc.read_text())


# ---------------------------------------------------------------------
# signatures
# ---------------------------------------------------------------------

def test_signature_ignores_things_the_index_does_not_store(tmp_path):
    """Annotations and artifact bytes don't reach the index, so touching
    them must not cost a re-index -- otherwise every tag edit re-reads a
    session's sidecars for nothing."""
    archive = tmp_path / "arc"
    s = _session(archive)
    before = index.session_signature(s.path)

    time.sleep(0.01)
    (Path(s.path) / "raw.csv").write_text("9,9,9,9\n")        # artifact body
    from nebula import annotations
    annotations.add_tags(s.path, "raw.csv", ["interesting"])   # annotations.yaml

    assert index.session_signature(s.path) == before


def test_signature_moves_when_a_sidecar_does(tmp_path):
    archive = tmp_path / "arc"
    s = _session(archive)
    before = index.session_signature(s.path)
    _touch_sidecar(s.path)
    assert index.session_signature(s.path) != before


# ---------------------------------------------------------------------
# freshness
# ---------------------------------------------------------------------

def test_ensure_fresh_builds_when_there_is_no_index(tmp_path):
    archive = tmp_path / "arc"
    _session(archive)
    (archive / "index.db").unlink(missing_ok=True)

    got = index.ensure_fresh(archive)
    assert got["rebuilt"] is True
    assert index.status(archive)["sessions"] == 1


def test_ensure_fresh_skips_sessions_that_have_not_changed(tmp_path):
    archive = tmp_path / "arc"
    _session(archive)
    _session(archive, "two")
    index.rebuild(archive)

    got = index.ensure_fresh(archive)
    assert got["checked_sessions"] == 2      # stat'ed
    assert got["updated"] == got["added"] == got["removed"] == 0   # but not re-read


def test_ensure_fresh_picks_up_an_edit_nobody_announced(tmp_path):
    archive = tmp_path / "arc"
    s = _session(archive)
    index.rebuild(archive)
    _touch_sidecar(s.path)

    assert index.pending_changes(archive)["stale"] is True
    got = index.ensure_fresh(archive)
    assert got["updated"] == 1 and got["rebuilt"] is False
    assert index.pending_changes(archive)["stale"] is False


def test_ensure_fresh_notices_a_session_that_arrived_from_elsewhere(tmp_path):
    """A synced archive gains whole session directories without any local
    process ever writing them."""
    archive = tmp_path / "arc"
    _session(archive)
    index.rebuild(archive)

    donor = tmp_path / "donor"
    other = _session(donor, "from another machine")
    year = Path(other.path).parent.name
    arrived = archive / "data" / year / "S-26-9001"
    shutil.copytree(other.path, arrived)
    # The other machine allocated its own id; give it the one its folder
    # says, as a real sync of distinct sessions would.
    yaml_path = arrived / "session.yaml"
    yaml_path.write_text(yaml_path.read_text().replace(other.id, "S-26-9001"))

    got = index.ensure_fresh(archive)
    assert got["added"] == 1
    assert index.status(archive)["sessions"] == 2


def test_ensure_fresh_forgets_a_session_that_is_gone(tmp_path):
    archive = tmp_path / "arc"
    s = _session(archive)
    _session(archive, "two")
    index.rebuild(archive)

    shutil.rmtree(s.path)
    got = index.ensure_fresh(archive)
    assert got["removed"] == 1
    conn = index.open_index(archive)
    try:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        # every table, not just the session row
        assert conn.execute("SELECT count(*) FROM artifacts WHERE run_id = ?",
                            (s.id,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_reindexing_drops_rows_for_files_that_left(tmp_path):
    """The bug a per-session update would otherwise introduce: rebuild()
    starts from an empty database and never notices, but updating in place
    would leave a ghost row for a deleted file."""
    archive = tmp_path / "arc"
    s = nebula.new(archive, description="one")
    with s.artifact("raw.csv") as fn:
        fn.write_text("x")
    with s.artifact("extra.csv") as fn:
        fn.write_text("y")
    s.close()
    index.rebuild(archive)

    (Path(s.path) / "extra.csv").unlink()
    (Path(s.path) / "extra.csv.meta.json").unlink()
    index.ensure_fresh(archive)

    conn = index.open_index(archive)
    try:
        names = {r["filename"] for r in conn.execute(
            "SELECT filename FROM artifacts WHERE run_id = ?", (s.id,)).fetchall()}
    finally:
        conn.close()
    assert names == {"raw.csv"}


def test_a_schema_from_the_future_is_rebuilt_not_queried(tmp_path):
    archive = tmp_path / "arc"
    _session(archive)
    index.rebuild(archive)
    conn = sqlite3.connect(archive / "index.db")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', '999')")
    conn.commit()
    conn.close()

    got = index.ensure_fresh(archive)
    assert got["rebuilt"] is True
    assert index.status(archive)["usable"] is True


def test_a_corrupt_index_is_a_non_event(tmp_path):
    archive = tmp_path / "arc"
    _session(archive)
    index.rebuild(archive)
    (archive / "index.db").write_bytes(b"not a database at all")

    got = index.ensure_fresh(archive)
    assert got["rebuilt"] is True and got["reason"] == "index was unreadable"
    assert index.status(archive)["sessions"] == 1


# ---------------------------------------------------------------------
# closing a session (the fast path)
# ---------------------------------------------------------------------

def test_closing_a_session_updates_an_existing_index(tmp_path):
    archive = tmp_path / "arc"
    _session(archive)
    index.rebuild(archive)

    _session(archive, "two")
    assert index.status(archive)["sessions"] == 2
    assert index.pending_changes(archive)["stale"] is False


def test_closing_a_session_does_not_create_an_index(tmp_path):
    """One session in a database that `nebula ls` treats as the whole
    archive is worse than no database at all."""
    archive = tmp_path / "arc"
    _session(archive)
    assert not (archive / "index.db").exists()


def test_a_crashed_session_still_reaches_the_index(tmp_path):
    archive = tmp_path / "arc"
    _session(archive)
    index.rebuild(archive)

    with pytest.raises(RuntimeError):
        with nebula.session(archive, description="doomed") as s:
            with s.artifact("partial.csv") as fn:
                fn.write_text("half")
            raise RuntimeError("instrument fell over")

    conn = index.open_index(archive)
    try:
        rows = conn.execute("SELECT status FROM sessions ORDER BY created").fetchall()
    finally:
        conn.close()
    assert [r["status"] for r in rows] == ["closed", "crashed"]


def test_indexing_failure_never_costs_the_user_their_data(tmp_path, monkeypatch):
    archive = tmp_path / "arc"
    _session(archive)
    index.rebuild(archive)

    def explode(*a, **kw):
        raise sqlite3.OperationalError("disk fell off")

    monkeypatch.setattr(index, "update_session", explode)
    s = _session(archive, "two")          # must not raise
    assert (Path(s.path) / "raw.csv").is_file()


def test_auto_index_can_be_turned_off(tmp_path):
    from nebula.config import ArchiveSettings, write_settings

    archive = tmp_path / "arc"
    _session(archive)
    index.rebuild(archive)
    write_settings(archive, ArchiveSettings(auto_index=False))

    _session(archive, "two")
    assert index.status(archive)["sessions"] == 1        # the writer stayed out of it
    assert index.pending_changes(archive)["stale"] is True
    index.ensure_fresh(archive)                          # the reader still catches up
    assert index.status(archive)["sessions"] == 2


# ---------------------------------------------------------------------
# portability
# ---------------------------------------------------------------------

def test_an_archive_can_be_moved_without_invalidating_its_index(tmp_path):
    archive = tmp_path / "arc"
    s = _session(archive)
    index.rebuild(archive)

    moved = tmp_path / "somewhere-else" / "arc"
    moved.parent.mkdir()
    shutil.move(str(archive), str(moved))

    assert index.pending_changes(moved)["stale"] is False
    conn = index.open_index(moved)
    try:
        row = conn.execute("SELECT rel_path FROM sessions WHERE run_id = ?",
                           (s.id,)).fetchone()
    finally:
        conn.close()
    assert not Path(row["rel_path"]).is_absolute()
    assert index.session_path(moved, row).is_dir()


def test_paths_resolve_to_wherever_the_archive_now_lives(tmp_path):
    archive = tmp_path / "arc"
    s = nebula.new(archive, description="one")
    with s.artifact("raw.csv") as fn:
        fn.write_text("x")
    with s.artifact("proc.csv", derived_from=["raw.csv"]) as fn:
        fn.write_text("y")
    s.close()
    index.rebuild(archive)

    moved = tmp_path / "elsewhere"
    shutil.move(str(archive), str(moved))

    up = graph.upstream(moved, s.id, "proc.csv")
    assert [n.filename for n in up] == ["raw.csv"]
    assert up[0].path.startswith(str(moved))
    assert Path(up[0].path).is_file()


# ---------------------------------------------------------------------
# year seals
# ---------------------------------------------------------------------

def _past_year_session(archive, year="2020", run_id="S-20-0001"):
    """A finished session in a past year, built by hand -- new() always
    writes into the current year."""
    from nebula.sidecar import SessionMeta, write_session_yaml

    d = archive / "data" / year / run_id
    d.mkdir(parents=True)
    write_session_yaml(d, SessionMeta(
        run_id=run_id, created=f"{year}-03-04T10:00:00-06:00",
        status="closed", tags=[], description="old work"))
    return d


def test_sealing_refuses_the_current_year(tmp_path):
    archive = tmp_path / "arc"
    _session(archive)
    import datetime
    with pytest.raises(index.IndexError_):
        index.seal_year(archive, datetime.date.today().year)


def test_sealing_refuses_a_year_with_unfinished_work(tmp_path):
    archive = tmp_path / "arc"
    d = _past_year_session(archive)
    from nebula.sidecar import read_session_yaml, write_session_yaml
    meta = read_session_yaml(d)
    meta.status = "open"
    write_session_yaml(d, meta)

    with pytest.raises(index.IndexError_):
        index.seal_year(archive, "2020")


def test_a_sealed_year_is_skipped_by_the_sweep(tmp_path):
    archive = tmp_path / "arc"
    _past_year_session(archive)
    _session(archive)
    index.rebuild(archive)

    index.seal_year(archive, "2020")
    # Sealing invalidates rather than grants the skip: one sweep verifies
    # the year against the new seal, and only then is it skipped.
    first = index.ensure_fresh(archive)
    assert first["skipped_years"] == []
    got = index.ensure_fresh(archive)
    assert got["skipped_years"] == ["2020"]
    # the current year is still swept, so ordinary work is never skipped
    assert got["checked_sessions"] == 1


def test_a_seal_is_a_claim_and_check_is_what_audits_it(tmp_path):
    from nebula import check as check_mod

    archive = tmp_path / "arc"
    d = _past_year_session(archive)
    index.rebuild(archive)
    index.seal_year(archive, "2020")
    index.ensure_fresh(archive)              # verifies the year under its seal

    time.sleep(0.01)
    (d / "session.yaml").write_text((d / "session.yaml").read_text() + "\n")

    # the sweep deliberately does not look...
    assert index.ensure_fresh(archive)["skipped_years"] == ["2020"]
    # ...so check is what surfaces it, and says both ways out
    kinds = [i for i in check_mod.check(archive, verify_checksums=False)
             if i.kind == "year_seal_mismatch"]
    assert len(kinds) == 1
    assert "unseal" in kinds[0].fix and "--force" in kinds[0].fix

    assert index.verify_year_seal(archive, "2020")["ok"] is False
    index.unseal_year(archive, "2020")
    assert index.ensure_fresh(archive)["skipped_years"] == []


def test_a_reseal_is_noticed_even_though_the_year_is_sealed(tmp_path):
    """Skipping is conditional on the seal the index already verified, so
    re-sealing can never leave the index pinned to the old state."""
    archive = tmp_path / "arc"
    d = _past_year_session(archive)
    index.rebuild(archive)
    index.seal_year(archive, "2020")
    index.ensure_fresh(archive)
    assert index.ensure_fresh(archive)["skipped_years"] == ["2020"]

    time.sleep(0.01)
    _past_year_session(archive, "2020", "S-20-0002")
    index.seal_year(archive, "2020", force=True)

    got = index.ensure_fresh(archive)
    assert got["skipped_years"] == [] and got["added"] == 1
    assert index.status(archive)["sessions"] == 2
    assert index.ensure_fresh(archive)["skipped_years"] == ["2020"]


def test_seal_survives_being_listed(tmp_path):
    archive = tmp_path / "arc"
    _past_year_session(archive)
    index.seal_year(archive, "2020")
    listed = index.sealed_years(archive)
    assert [s["year"] for s in listed] == ["2020"]
    assert listed[0]["sessions"] == 1 and listed[0]["digest"]


def test_an_index_from_the_previous_schema_is_replaced(tmp_path):
    """The version already on Grant's disk has no meta table at all, so
    the version probe has to survive the column simply not being there."""
    archive = tmp_path / "arc"
    _session(archive)
    old = archive / "index.db"
    conn = sqlite3.connect(old)
    conn.executescript("""
        CREATE TABLE sessions (run_id TEXT PRIMARY KEY, path TEXT, created TEXT,
                               status TEXT, tags TEXT, description TEXT,
                               hold_until TEXT, history TEXT);
        INSERT INTO sessions VALUES ('S-26-0001', '/old/absolute/path',
                                     '2026-01-01', 'closed', '[]', 'old', NULL, '[]');
    """)
    conn.commit()
    conn.close()

    got = index.ensure_fresh(archive)
    assert got["rebuilt"] is True
    st = index.status(archive)
    assert st["usable"] is True and st["sessions"] == 1


def test_open_fresh_builds_an_index_on_demand(tmp_path):
    archive = tmp_path / "arc"
    _session(archive)
    assert not (archive / "index.db").exists()

    conn = index.open_fresh(archive)
    try:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    finally:
        conn.close()
