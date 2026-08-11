import datetime
import pathlib
import subprocess
import sys

import pytest

from nebula import assets, assetstore, codestore
from nebula.config import ArchiveSettings, read_settings, write_settings
from nebula.refs import Ref, format_ref, format_uri, parse_ref


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "arc"
    root.mkdir()
    write_settings(root, ArchiveSettings(name="arc", user="grant@ncsu.edu"))
    return root


def _src(tmp_path, name="figure.svg", data=b"<svg/>"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


# -- ids and layout -------------------------------------------------------

def test_id_roundtrip_and_bucket(archive):
    assert assets.format_asset_id(26, 17) == "AF-26-0017"
    assert assets.parse_asset_id("AF-26-0017") == (26, 17)
    # The path is derived from the id alone -- no lookup, no counter file.
    d = assets.asset_dir(archive, "AF-26-0017")
    assert d.relative_to(archive).parts == ("assets", "26", "00", "AF-26-0017")
    assert assets.asset_dir(archive, "AF-26-1234").parent.name == "12"


def test_malformed_id_raises(archive):
    for bad in ("", "AF-26", "S-26-0017", "AF-2026-0017", "figure.svg"):
        with pytest.raises(assets.AssetError):
            assets.asset_dir(archive, bad)


def test_ids_allocate_from_folder_listing(archive, tmp_path):
    now = datetime.datetime(2026, 3, 1)
    a = assets.import_asset(archive, _src(tmp_path, "a.svg"), now=now)
    b = assets.import_asset(archive, _src(tmp_path, "b.svg", b"<svg id=b/>"), now=now)
    assert (a.id, b.id) == ("AF-26-0001", "AF-26-0002")


# -- import ---------------------------------------------------------------

def test_import_copies_file_and_snapshots(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path))
    live = assets.live_file(archive, meta.id)
    assert live.name == "figure.svg"
    assert live.read_bytes() == b"<svg/>"
    # A first snapshot exists, so an early reference has something to pin.
    stored = assets.read_asset(archive, meta.id)
    assert len(stored.snapshots) == 1
    assert assetstore.blob_path(archive, stored.snapshots[0].sha256).is_file()


def test_default_policy_ladder(archive, tmp_path):
    s = ArchiveSettings(asset_periodic_above=100, asset_manual_above=1000)
    assert assets.default_policy_for_size(50, s) == "on_reference"
    assert assets.default_policy_for_size(500, s) == "periodic"
    assert assets.default_policy_for_size(5000, s) == "manual"


def test_auto_is_an_explicit_policy_that_follows_the_archive(archive, tmp_path):
    """An asset that never made a choice reads back as "auto" -- a policy
    the user can see -- and re-resolves against the archive every time."""
    meta = assets.import_asset(archive, _src(tmp_path))
    assert meta.policy == "auto"
    assert meta.effective_policy(ArchiveSettings()) == "on_reference"
    assert meta.effective_policy(ArchiveSettings(asset_policy="manual")) == "manual"


def test_auto_tracks_the_size_ladder_as_the_file_grows(archive, tmp_path):
    """The point of resolving at runtime: a file that grows past a
    threshold moves policy on its own."""
    meta = assets.import_asset(archive, _src(tmp_path))
    settings = ArchiveSettings(asset_periodic_above=100, asset_manual_above=1000)
    assert meta.effective_policy(settings) == "on_reference"

    assets.live_file(archive, meta.id).write_bytes(b"x" * 5000)
    assets.scan(archive, meta.id)
    assert assets.read_asset(archive, meta.id).effective_policy(settings) == "manual"


def test_archive_default_cannot_be_auto(archive):
    """The ladder's bottom rung *is* the archive setting, so "auto" there
    would recurse. It must fall back rather than be honoured."""
    write_settings(archive, ArchiveSettings(asset_policy="auto"))
    assert read_settings(archive).asset_policy == "on_reference"


def test_explicit_policy_is_recorded(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path), policy="manual")
    assert meta.policy == "manual"
    # No auto-snapshot on import under manual.
    assert assets.read_asset(archive, meta.id).snapshots == []


def test_import_records_derived_from(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path),
                               derived_from=["S-26-0152/raw.csv"])
    assert meta.derived_from == [
        {"archive": None, "session": "S-26-0152", "file": "raw.csv"}]


# -- rename detection -----------------------------------------------------

def test_rename_is_unambiguous(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path))
    live = assets.live_file(archive, meta.id)
    live.rename(live.with_name("figure_FINAL_v2.svg"))

    state = assets.scan(archive, meta.id)
    assert state["renamed"] == ("figure.svg", "figure_FINAL_v2.svg")
    after = assets.read_asset(archive, meta.id)
    assert after.name == "figure_FINAL_v2.svg"
    assert after.renames[-1]["from"] == "figure.svg"


def test_recycled_name_cannot_steal_an_identity(archive, tmp_path):
    """The failure this whole scheme exists to prevent: a new file given
    an old file's name must not inherit its provenance."""
    old = assets.import_asset(archive, _src(tmp_path))
    live = assets.live_file(archive, old.id)
    live.rename(live.with_name("renamed.svg"))
    assets.scan(archive, old.id)

    new = assets.import_asset(archive, _src(tmp_path, "figure.svg", b"<svg id=new/>"))
    assert new.id != old.id
    assert assets.read_asset(archive, old.id).snapshots[0].sha256 != new.sha256
    # Each identity still points at its own bytes.
    assert assets.live_file(archive, old.id).name == "renamed.svg"
    assert assets.live_file(archive, new.id).read_bytes() == b"<svg id=new/>"


def test_scan_detects_edits(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path))
    before = assets.read_asset(archive, meta.id).sha256
    live = assets.live_file(archive, meta.id)
    live.write_bytes(b"<svg edited/>")

    state = assets.scan(archive, meta.id)
    assert state["changed"] is True
    assert state["sha256"] != before


# -- snapshots ------------------------------------------------------------

def test_commit_is_idempotent_on_unchanged_bytes(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path))
    assert assets.commit(archive, meta.id) is None
    assert len(assets.history(archive, meta.id)) == 1


def test_commit_works_under_manual_policy(archive, tmp_path):
    """A policy governs what happens unasked; it must never block a
    deliberate save."""
    meta = assets.import_asset(archive, _src(tmp_path), policy="manual")
    snap = assets.commit(archive, meta.id, note="as submitted to APL")
    assert snap is not None
    assert snap.note == "as submitted to APL"
    assert assetstore.blob_path(archive, snap.sha256).is_file()


def test_big_file_downgrades_automatically_but_commits_when_forced(archive, tmp_path):
    write_settings(archive, ArchiveSettings(asset_manual_above=4))
    src = _src(tmp_path, "big.bin", b"0123456789")
    meta = assets.import_asset(archive, src, policy="on_reference")
    # Import's own snapshot is automatic, so the ceiling declines it...
    assert assets.read_asset(archive, meta.id).snapshots == []
    # ...but the reference is still recorded, just at lower fidelity.
    ref = assets.reference(archive, meta.id)
    assert ref["fidelity"] == "observed"
    assert ref["sha256"]
    # An explicit commit ignores the ceiling entirely.
    assert assets.commit(archive, meta.id) is not None


def test_periodic_rate_limits_references(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path), policy="periodic")
    live = assets.live_file(archive, meta.id)

    live.write_bytes(b"<svg v=2/>")
    now = datetime.datetime.now().astimezone()
    assert assets.reference(archive, meta.id, now=now)["fidelity"] == "observed"

    live.write_bytes(b"<svg v=3/>")
    later = now + datetime.timedelta(days=8)
    assert assets.reference(archive, meta.id, now=later)["fidelity"] == "pinned"


def test_reference_carries_id_and_readable_name(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path))
    ref = assets.reference(archive, meta.id)
    assert ref["asset"] == meta.id
    assert ref["file"] == "figure.svg"
    assert ref["fidelity"] == "pinned"


def _commit_versions(archive, asset_id, n):
    live = assets.live_file(archive, asset_id)
    for i in range(n):
        live.write_bytes(f"<svg v={i}/>".encode())
        assets.commit(archive, asset_id)


def test_snapshot_count_cap_marks_oldest_without_losing_the_record(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path))
    assets.set_policy(archive, meta.id, max_snapshots=2)
    _commit_versions(archive, meta.id, 3)

    hist = assets.history(archive, meta.id)
    # The record is cheap, so history stays complete and readable...
    assert len(hist) == 4
    assert [s.pending_gc for s in hist] == [True, True, False, False]
    # ...and no blob is deleted here: a session may have pinned one.
    assert all(assetstore.blob_path(archive, s.sha256).is_file() for s in hist)


def test_cap_action_drop_forgets_the_record(archive, tmp_path):
    write_settings(archive, ArchiveSettings(asset_cap_action="drop"))
    meta = assets.import_asset(archive, _src(tmp_path))
    assets.set_policy(archive, meta.id, max_snapshots=2)
    _commit_versions(archive, meta.id, 3)
    assert len(assets.history(archive, meta.id)) == 2


def test_evicted_snapshot_is_not_treated_as_recoverable(archive, tmp_path):
    """A marked record says a version existed, not that it can be got
    back -- so it must not satisfy a pin."""
    meta = assets.import_asset(archive, _src(tmp_path))
    assets.set_policy(archive, meta.id, max_snapshots=1)
    _commit_versions(archive, meta.id, 1)
    stored = assets.read_asset(archive, meta.id)
    assert stored.snapshots[0].pending_gc is True
    assert stored.latest_snapshot() is stored.snapshots[-1]


def test_byte_cap_marks_oldest(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path))
    assets.set_policy(archive, meta.id, max_snapshot_bytes=20)
    _commit_versions(archive, meta.id, 3)
    hist = assets.history(archive, meta.id)
    kept = [s for s in hist if not s.pending_gc]
    assert 0 < len(kept) < len(hist)
    assert sum(s.bytes for s in kept) <= 20


def test_clearing_an_override_returns_to_the_archive_default(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path))
    assets.set_policy(archive, meta.id, period_days=30)
    assert assets.read_asset(archive, meta.id).period_days == 30
    assets.set_policy(archive, meta.id, period_days=-1)
    stored = assets.read_asset(archive, meta.id)
    assert stored.period_days is None
    assert stored.effective_period_days(ArchiveSettings()) == 7


# -- refs -----------------------------------------------------------------

def test_asset_ref_spellings():
    ref = parse_ref("assets/AF-26-0017")
    assert ref == Ref(asset="AF-26-0017")
    assert ref.kind == "asset"
    assert format_ref(ref) == "assets/AF-26-0017"
    assert (format_uri(ref, user="grant@ncsu.edu", archive="postdoc")
            == "nebula://grant@ncsu.edu/postdoc/assets/AF-26-0017")


def test_bare_asset_id_parses():
    assert parse_ref("AF-26-0017") == Ref(asset="AF-26-0017")


def test_cross_archive_asset_ref():
    ref = parse_ref("postdoc|assets/AF-26-0017")
    assert ref == Ref(asset="AF-26-0017", archive="postdoc")
    assert format_ref(ref) == "postdoc|assets/AF-26-0017"


def test_asset_uri_roundtrip():
    ref = parse_ref("nebula://grant@ncsu.edu/postdoc/assets/AF-26-0017")
    assert ref == Ref(asset="AF-26-0017", archive="postdoc", user="grant@ncsu.edu")
    assert format_ref(ref) == "nebula://grant@ncsu.edu/postdoc/assets/AF-26-0017"


def test_asset_ref_survives_sidecar_roundtrip():
    from nebula.sidecar import _ref_from_dict, _ref_to_dict
    ref = Ref(asset="AF-26-0017", file="figure.svg")
    assert _ref_from_dict(_ref_to_dict(ref)) == ref


def test_asset_ref_is_not_filled_in_as_a_session():
    ref = parse_ref("assets/AF-26-0017").resolved(
        archive="postdoc", session="S-26-0152")
    assert ref.session is None
    assert ref.asset == "AF-26-0017"


# -- CLI ------------------------------------------------------------------

def _nebula(*args):
    return subprocess.run([sys.executable, "-m", "nebula.cli", *args],
                          capture_output=True, text=True)


def test_cli_import_reports_the_resolved_policy(archive, tmp_path):
    """The size ladder should be something the user learns, not something
    that silently happens to them."""
    r = _nebula("asset", "import", str(archive), str(_src(tmp_path)))
    assert r.returncode == 0, r.stderr
    assert "AF-26-" in r.stdout or "AF-" in r.stdout
    assert "auto -> on_reference" in r.stdout


def test_cli_explicit_policy_is_not_reported_as_auto(archive, tmp_path):
    r = _nebula("asset", "import", str(archive), str(_src(tmp_path)),
                "--policy", "manual")
    assert "[manual]" in r.stdout
    assert "auto" not in r.stdout


def test_cli_path_is_bare_so_it_composes(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path))
    r = _nebula("asset", "path", str(archive), meta.id)
    assert r.returncode == 0
    assert pathlib.Path(r.stdout.strip()).read_bytes() == b"<svg/>"


def test_cli_commit_prints_a_quotable_sha(archive, tmp_path):
    """The stated use is pasting the sha into notes, so it must not be
    truncated."""
    meta = assets.import_asset(archive, _src(tmp_path))
    assets.live_file(archive, meta.id).write_bytes(b"<svg v=2/>")
    r = _nebula("asset", "commit", str(archive), meta.id, "-m", "for APL")
    sha = r.stdout.strip().split("@")[-1].strip()
    assert len(sha) == 64
    assert assetstore.blob_path(archive, sha).is_file()


def test_cli_commit_is_quiet_when_nothing_changed(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path))
    r = _nebula("asset", "commit", str(archive), meta.id)
    assert r.returncode == 0
    assert "nothing committed" in r.stdout


def test_cli_accepts_a_bare_number_like_session_commands(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path))
    assert meta.id.endswith("0001")
    r = _nebula("asset", "show", str(archive), "1")
    assert r.returncode == 0, r.stderr
    assert meta.id in r.stdout


def test_cli_rejects_a_bad_asset_id(archive):
    r = _nebula("asset", "show", str(archive), "figure.svg")
    assert r.returncode != 0
    assert "not an asset id" in r.stderr


def test_cli_scan_reports_a_rename(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path))
    live = assets.live_file(archive, meta.id)
    live.rename(live.with_name("renamed.svg"))
    r = _nebula("asset", "scan", str(archive))
    assert "renamed figure.svg -> renamed.svg" in r.stdout


def test_cli_ls_filters_by_effective_policy(archive, tmp_path):
    assets.import_asset(archive, _src(tmp_path, "a.svg"))
    assets.import_asset(archive, _src(tmp_path, "b.svg", b"<svg id=b/>"),
                        policy="manual")
    r = _nebula("asset", "ls", str(archive), "--policy", "manual")
    assert "b.svg" in r.stdout
    assert "a.svg" not in r.stdout


# -- integration: sessions, check, index ----------------------------------

def _session_deriving_from(archive, asset_id, payload=b"plot"):
    import nebula
    with nebula.session(archive, description="uses an asset") as s:
        with s.artifact("plot.png", derived_from=[f"assets/{asset_id}"]) as p:
            p.write_bytes(payload)
        return s.id


def test_session_derived_from_asset_is_pinned_at_write_time(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path))
    assets.live_file(archive, meta.id).write_bytes(b"<svg v=2/>")

    run_id = _session_deriving_from(archive, meta.id)

    from nebula.sidecar import read_sidecar
    sc = read_sidecar(archive / "data" / str(datetime.date.today().year)
                      / run_id / "plot.png")
    edge = sc.derived_from[0]
    assert edge["asset"] == meta.id
    assert edge["fidelity"] == "pinned"
    # The pinned bytes are the ones the session actually saw...
    assert assetstore.blob_path(archive, edge["sha256"]).read_bytes() == b"<svg v=2/>"

    # ...and stay so after the asset moves on, which is the whole point.
    assets.live_file(archive, meta.id).write_bytes(b"<svg v=3/>")
    assets.commit(archive, meta.id)
    assert assetstore.blob_path(archive, edge["sha256"]).read_bytes() == b"<svg v=2/>"


def test_check_is_clean_for_a_healthy_asset_link(archive, tmp_path):
    from nebula import check as check_mod
    meta = assets.import_asset(archive, _src(tmp_path))
    _session_deriving_from(archive, meta.id)
    assert [i for i in check_mod.check(archive) if i.severity == "error"] == []


def test_check_never_reports_asset_drift(archive, tmp_path):
    """Editing an asset is the point of an asset -- it must not show up as
    an integrity problem the way a session artifact would."""
    from nebula import check as check_mod
    meta = assets.import_asset(archive, _src(tmp_path))
    _session_deriving_from(archive, meta.id)
    assets.live_file(archive, meta.id).write_bytes(b"<svg edited forever/>")

    issues = check_mod.check(archive)
    assert not [i for i in issues if i.kind == "checksum_mismatch"]
    assert not [i for i in issues if i.severity == "error"]


def test_check_reports_a_dangling_asset_ref(archive, tmp_path):
    import shutil
    from nebula import check as check_mod
    meta = assets.import_asset(archive, _src(tmp_path))
    _session_deriving_from(archive, meta.id)
    shutil.rmtree(assets.asset_dir(archive, meta.id))

    kinds = {i.kind for i in check_mod.check(archive)}
    assert "dangling_asset_ref" in kinds


def test_check_reports_a_missing_pinned_blob(archive, tmp_path):
    from nebula import check as check_mod
    meta = assets.import_asset(archive, _src(tmp_path))
    _session_deriving_from(archive, meta.id)
    sha = assets.read_asset(archive, meta.id).sha256
    assetstore.blob_path(archive, sha).unlink()

    issues = [i for i in check_mod.check(archive) if i.kind == "missing_asset_blob"]
    assert issues and any(i.severity == "error" for i in issues)


def test_check_does_not_chase_an_evicted_snapshot(archive, tmp_path):
    """An evicted record never claimed its bytes were recoverable."""
    from nebula import check as check_mod
    meta = assets.import_asset(archive, _src(tmp_path))
    assets.set_policy(archive, meta.id, max_snapshots=1)
    _commit_versions(archive, meta.id, 1)
    evicted = assets.history(archive, meta.id)[0]
    assetstore.blob_path(archive, evicted.sha256).unlink()

    assert not [i for i in check_mod.check(archive)
                if i.kind == "missing_asset_blob"]


def test_check_flags_an_unscanned_rename_as_info_only(archive, tmp_path):
    from nebula import check as check_mod
    meta = assets.import_asset(archive, _src(tmp_path))
    live = assets.live_file(archive, meta.id)
    live.rename(live.with_name("new.svg"))

    issues = [i for i in check_mod.check(archive)
              if i.kind == "unscanned_asset_rename"]
    assert len(issues) == 1
    assert issues[0].severity == "info"


def test_index_records_assets_and_the_asset_edge(archive, tmp_path):
    from nebula import index
    meta = assets.import_asset(archive, _src(tmp_path))
    run_id = _session_deriving_from(archive, meta.id)

    conn = index.open_fresh(archive)
    row = conn.execute("SELECT * FROM assets WHERE asset_id = ?",
                       (meta.id,)).fetchone()
    assert row["name"] == "figure.svg"
    assert row["policy"] == "auto"
    assert row["policy_resolved"] == "on_reference"

    edge = conn.execute(
        "SELECT * FROM derived_from WHERE ref_asset = ?", (meta.id,)).fetchone()
    assert edge["run_id"] == run_id
    assert edge["ref_fidelity"] == "pinned"
    conn.close()


def test_index_freshness_ignores_asset_bytes(archive, tmp_path):
    """Editing an asset must not re-index it: the index holds nothing that
    comes from the bytes, and assets are edited constantly."""
    from nebula import index
    meta = assets.import_asset(archive, _src(tmp_path))
    index.rebuild(archive)

    assets.live_file(archive, meta.id).write_bytes(b"<svg much bigger/>" * 100)
    summary = index.ensure_fresh(archive)
    assert summary["updated"] == 0
    assert summary["added"] == 0

    # A scan rewrites the record, and *that* is what the index follows.
    assets.scan(archive, meta.id)
    assert index.ensure_fresh(archive)["updated"] == 1


def test_index_forgets_a_removed_asset(archive, tmp_path):
    import shutil
    from nebula import index
    meta = assets.import_asset(archive, _src(tmp_path))
    index.rebuild(archive)
    shutil.rmtree(assets.asset_dir(archive, meta.id))

    index.ensure_fresh(archive)
    conn = index.open_index(archive)
    assert conn.execute("SELECT count(*) c FROM assets").fetchone()["c"] == 0
    conn.close()


# -- gc -------------------------------------------------------------------
# Assets share the blob store with captured code, so gc has to ask both.
# Before it did, gc deleted every asset blob in the archive.

def test_gc_keeps_a_retained_snapshot(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path))
    sha = assets.read_asset(archive, meta.id).snapshots[0].sha256
    res = assetstore.gc(archive, dry_run=True)
    assert sha not in res["blobs"]


def test_gc_keeps_a_blob_a_session_pinned(archive, tmp_path):
    """The failure that shipped: a session's provenance link is silently
    destroyed by a routine gc."""
    meta = assets.import_asset(archive, _src(tmp_path))
    _session_deriving_from(archive, meta.id)
    sha = assets.read_asset(archive, meta.id).snapshots[0].sha256

    assetstore.gc(archive, dry_run=False)
    assert assetstore.blob_path(archive, sha).is_file()
    from nebula import check as check_mod
    assert not [i for i in check_mod.check(archive) if i.severity == "error"]


def test_gc_collects_an_evicted_snapshot_nothing_else_claims(archive, tmp_path):
    """The other half: marks must actually become reclaimable, or the
    storage cap caps nothing."""
    meta = assets.import_asset(archive, _src(tmp_path))
    assets.set_policy(archive, meta.id, max_snapshots=1)
    _commit_versions(archive, meta.id, 1)
    evicted = assets.history(archive, meta.id)[0]
    assert evicted.pending_gc

    res = assetstore.gc(archive, dry_run=False)
    assert evicted.sha256 in res["blobs"]
    assert not assetstore.blob_path(archive, evicted.sha256).is_file()


def test_gc_spares_an_evicted_snapshot_a_session_still_pins(archive, tmp_path):
    """A session's pin outranks the asset's own cap -- we said so when we
    chose to mark rather than delete."""
    meta = assets.import_asset(archive, _src(tmp_path))
    _session_deriving_from(archive, meta.id)
    pinned = assets.read_asset(archive, meta.id).snapshots[0].sha256
    assets.set_policy(archive, meta.id, max_snapshots=1)
    _commit_versions(archive, meta.id, 1)

    assert assets.history(archive, meta.id)[0].pending_gc
    assetstore.gc(archive, dry_run=False)
    assert assetstore.blob_path(archive, pinned).is_file()


def test_code_gc_cannot_reach_asset_bytes(archive, tmp_path):
    """The structural point of two stores: the code sweep has no path to
    an asset blob, whatever it believes about reachability."""
    meta = assets.import_asset(archive, _src(tmp_path))
    sha = assets.read_asset(archive, meta.id).snapshots[0].sha256
    codestore.gc(archive, dry_run=False)
    assert assetstore.blob_path(archive, sha).is_file()


def test_code_gc_still_collects_dead_code_blobs(archive, tmp_path):
    orphan = codestore.store_blob(archive, b"unreferenced source")
    res = codestore.gc(archive, dry_run=True)
    assert orphan in res["blobs"]


def test_asset_gc_declines_the_sweep_when_a_record_is_unreadable(archive, tmp_path):
    """"Could not read it" must never be treated as "claims nothing" --
    that is what turns one corrupt file into deleted bytes."""
    meta = assets.import_asset(archive, _src(tmp_path))
    sha = assets.read_asset(archive, meta.id).snapshots[0].sha256
    assets.record_path(archive, meta.id).write_text("{ not json")

    res = assetstore.gc(archive, dry_run=False)
    assert res["skipped"]
    assert res["blobs"] == []
    assert assetstore.blob_path(archive, sha).is_file()


def test_gc_cli_warns_loudly_about_a_skipped_sweep(archive, tmp_path):
    meta = assets.import_asset(archive, _src(tmp_path))
    assets.record_path(archive, meta.id).write_text("{ not json")
    r = _nebula("gc", str(archive), "--delete")
    assert "asset blob collection skipped" in r.stderr


# --- archive-wide asset defaults (Navigator settings form) -----------------

def test_settings_reject_an_unreachable_ladder(archive):
    """periodic_above >= manual_above means no asset ever gets `periodic`.
    Accepting it would be a setting that silently does nothing."""
    from nebula.navigator import model
    root = archive
    with pytest.raises(model.AssetSettingsError) as e:
        model.set_asset_settings(root, {"periodic_above": 8 << 30,
                                        "manual_above": 1 << 30})
    assert "ladder" in str(e.value)


def test_settings_reject_auto_as_the_archive_default(archive):
    from nebula.navigator import model
    root = archive
    with pytest.raises(model.AssetSettingsError):
        model.set_asset_settings(root, {"policy": "auto"})


def test_settings_reject_nonsense_from_a_stale_front_end(archive):
    from nebula.navigator import model
    root = archive
    for bad in [{"policy": "whatever"}, {"cap_action": "explode"},
                {"max_snapshots": "lots"}, {"max_snapshots": -1},
                {"period_days": 0}]:
        with pytest.raises(model.AssetSettingsError):
            model.set_asset_settings(root, bad)


def test_settings_round_trip(archive):
    from nebula.navigator import model
    root = archive
    got = model.set_asset_settings(root, {
        "policy": "every_change", "periodic_above": 1 << 20,
        "manual_above": 4 << 20, "period_days": 3,
        "max_snapshots": 5, "cap_action": "drop"})
    assert got["policy"] == "every_change"
    assert got["periodic_above"] == 1 << 20
    assert got["max_snapshots"] == 5
    assert got["cap_action"] == "drop"
    assert model.asset_settings(root)["period_days"] == 3


def test_preview_reports_which_auto_assets_would_move(archive, tmp_path):
    """The blast radius that makes this form different from a normal one."""
    from nebula import assets
    from nebula.navigator import model
    root = archive
    model.set_asset_settings(root, {"periodic_above": 1 << 20,
                                    "manual_above": 8 << 20})

    small = tmp_path / "small.svg"
    small.write_bytes(b"x" * 1024)
    big = tmp_path / "big.bin"
    big.write_bytes(b"y" * (2 << 20))
    a_small = assets.import_asset(root, small, policy="auto")
    a_big = assets.import_asset(root, big, policy="auto")

    # Drop the periodic rung below the small file: it should move too.
    res = model.asset_settings_preview(root, {"periodic_above": 512})
    assert res["ok"] is True
    assert res["auto_assets"] == 2
    moved = {m["id"]: m for m in res["moved"]}
    assert a_small.id in moved
    assert moved[a_small.id]["to"] == "periodic"
    assert a_big.id not in moved       # already periodic, unchanged


def test_preview_ignores_assets_that_pinned_their_own_policy(archive, tmp_path):
    from nebula import assets
    from nebula.navigator import model
    root = archive
    f = tmp_path / "fixed.svg"
    f.write_bytes(b"x" * 1024)
    assets.import_asset(root, f, policy="manual")
    res = model.asset_settings_preview(root, {"periodic_above": 512})
    assert res["auto_assets"] == 0
    assert res["changed"] == 0


def test_preview_reports_a_bad_ladder_instead_of_raising(archive):
    """The form previews on every keystroke, so an invalid intermediate
    state must come back as data rather than blowing up the bridge."""
    from nebula.navigator import model
    root = archive
    res = model.asset_settings_preview(root, {"periodic_above": 1 << 40})
    assert res["ok"] is False
    assert "ladder" in res["error"]


def test_bridge_reports_a_rejected_setting_as_data(archive):
    from nebula.navigator import api
    root = archive
    res = api.OPS["set_asset_settings"]({"archive": str(root),
                                         "changes": {"policy": "auto"}})
    assert res["ok"] is False
    assert res["error"]


def test_bridge_returns_settings_on_success(archive):
    from nebula.navigator import api
    root = archive
    res = api.OPS["set_asset_settings"]({"archive": str(root),
                                         "changes": {"period_days": 14}})
    assert res["ok"] is True
    assert res["settings"]["period_days"] == 14
