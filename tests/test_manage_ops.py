"""The management operations the Navigator's archive panel drives."""

import time
from pathlib import Path

import pytest

import nebula
from nebula.navigator import api, model


def _archive(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="one", tags=["t"])
    with s.artifact("raw.csv") as fn:
        fn.write_text("x")
    with s.artifact("proc.graf", derived_from=["raw.csv"]) as fn:
        fn.write_text("y")
    s.close()
    return archive, s


# ---------------------------------------------------------------------
# archive_stats
# ---------------------------------------------------------------------

def test_archive_stats_counts(tmp_path):
    archive, s = _archive(tmp_path)
    st = api.dispatch("archive_stats", {"archive": str(archive)})
    assert st["n_sessions"] == 1 and st["n_items"] == 2 and st["n_problems"] == 0
    assert st["size"] > 0
    assert st["settings"]["on_overwrite"] == "duplicate"


def test_archive_stats_without_an_index(tmp_path):
    archive, _ = _archive(tmp_path)
    st = api.dispatch("archive_stats", {"archive": str(archive)})
    assert st["index"]["exists"] is False and st["index"]["built"] is None


def test_rebuild_then_stats_reports_the_build(tmp_path):
    archive, _ = _archive(tmp_path)
    api.dispatch("rebuild_index", {"archive": str(archive)})
    st = api.dispatch("archive_stats", {"archive": str(archive)})
    assert st["index"]["exists"] and st["index"]["built"]
    assert st["index"]["sessions"] == 1
    assert st["index"]["stale"] is False


def test_stats_flags_a_stale_index(tmp_path):
    """The index is a cache; a panel that shows its count must say when the
    archive has moved on."""
    archive, _ = _archive(tmp_path)
    api.dispatch("rebuild_index", {"archive": str(archive)})
    s2 = nebula.new(archive, description="two")
    s2.close()

    st = api.dispatch("archive_stats", {"archive": str(archive)})
    assert st["n_sessions"] == 2 and st["index"]["sessions"] == 1
    assert st["index"]["stale"] is True


def test_stats_flags_stale_open_sessions(tmp_path):
    from nebula.sidecar import SessionMeta, write_session_yaml

    archive, _ = _archive(tmp_path)
    old = archive / "data" / "2020"
    old.mkdir(parents=True)
    d = old / "S-20-0001"
    d.mkdir()
    write_session_yaml(d, SessionMeta(run_id="S-20-0001", status="open",
                                      created="2020-01-01T00:00:00+00:00"))

    st = api.dispatch("archive_stats", {"archive": str(archive)})
    assert [x["run_id"] for x in st["stale_open"]] == ["S-20-0001"]


def test_stats_reports_the_code_store(tmp_path):
    archive, _ = _archive(tmp_path)
    st = api.dispatch("archive_stats", {"archive": str(archive)})
    assert st["code"]["blobs"] >= 1 and st["code"]["manifests"] >= 1
    assert st["code"]["human"]


# ---------------------------------------------------------------------
# check / gc
# ---------------------------------------------------------------------

def test_check_op_is_clean_on_a_healthy_archive(tmp_path):
    archive, _ = _archive(tmp_path)
    res = api.dispatch("check", {"archive": str(archive)})
    assert res["issues"] == [] and res["n_errors"] == 0
    assert res["verified"] is False        # off unless asked


def test_check_op_reports_problems_with_fixes(tmp_path):
    archive, s = _archive(tmp_path)
    (s.path / "dropped.dat").write_text("no sidecar")
    res = api.dispatch("check", {"archive": str(archive)})
    kinds = [i["kind"] for i in res["issues"]]
    assert "orphan_artifact" in kinds or "orphan" in " ".join(kinds)
    assert res["n_errors"] + res["n_info"] == len(res["issues"])
    assert all("severity" in i for i in res["issues"])


def test_check_op_can_verify_checksums(tmp_path):
    archive, s = _archive(tmp_path)
    (s.path / "raw.csv").write_text("tampered")
    quiet = api.dispatch("check", {"archive": str(archive)})
    loud = api.dispatch("check", {"archive": str(archive), "verify": True})
    assert not [i for i in quiet["issues"] if i["kind"] == "checksum_mismatch"]
    assert [i for i in loud["issues"] if i["kind"] == "checksum_mismatch"]
    assert loud["verified"] is True


def test_gc_op_dry_run_then_delete(tmp_path):
    from nebula import codestore

    archive, _ = _archive(tmp_path)
    orphan = codestore.store_blob(archive, b"nobody points at me\n")

    dry = api.dispatch("gc", {"archive": str(archive)})
    assert dry["dry_run"] is True and orphan in dry["blobs"]
    assert codestore.blob_path(archive, orphan).is_file()   # still there
    assert dry["human"]

    wet = api.dispatch("gc", {"archive": str(archive), "delete": True})
    assert wet["dry_run"] is False and orphan in wet["blobs"]
    assert not codestore.blob_path(archive, orphan).is_file()


# ---------------------------------------------------------------------
# item + session actions
# ---------------------------------------------------------------------

def test_delete_file_op_moves_to_trash(tmp_path):
    archive, s = _archive(tmp_path)
    res = api.dispatch("delete_file", {"archive": str(archive), "run_id": s.id,
                                       "filename": "proc.graf"})
    assert ".trash" in res["trashed"]
    assert not (s.path / "proc.graf").exists()
    assert [i.name for i in model.list_items(s.path)] == ["raw.csv"]


def test_delete_file_op_refuses_a_depended_on_file(tmp_path):
    """proc.graf derives from raw.csv, so raw.csv is guarded."""
    archive, s = _archive(tmp_path)
    with pytest.raises(Exception):
        api.dispatch("delete_file", {"archive": str(archive), "run_id": s.id,
                                     "filename": "raw.csv"})
    assert (s.path / "raw.csv").exists()

    api.dispatch("delete_file", {"archive": str(archive), "run_id": s.id,
                                 "filename": "raw.csv", "force": True})
    assert not (s.path / "raw.csv").exists()


def test_reseal_op_updates_the_checksum(tmp_path):
    archive, s = _archive(tmp_path)
    (s.path / "raw.csv").write_text("edited on purpose")
    before = api.dispatch("check", {"archive": str(archive), "verify": True})
    assert [i for i in before["issues"] if i["kind"] == "checksum_mismatch"]

    api.dispatch("reseal", {"archive": str(archive), "run_id": s.id, "filename": "raw.csv"})
    after = api.dispatch("check", {"archive": str(archive), "verify": True})
    assert not [i for i in after["issues"] if i["kind"] == "checksum_mismatch"]


def test_adopt_op_writes_a_sidecar_for_an_orphan(tmp_path):
    archive, s = _archive(tmp_path)
    (s.path / "dropped.dat").write_text("by hand")
    api.dispatch("adopt_file", {"path": str(s.path / "dropped.dat"),
                                "origin": "adopted in Navigator"})

    item = [i for i in model.list_items(s.path) if i.name == "dropped.dat"][0]
    assert item.status == model.PAIRED
    assert item.source == "external" and item.origin == "adopted in Navigator"


def test_hold_and_release_ops(tmp_path):
    archive, s = _archive(tmp_path)
    api.dispatch("hold", {"archive": str(archive), "run_id": s.id})
    assert model.session_info(s.path)["held"] is True

    res = api.dispatch("release", {"archive": str(archive), "run_id": s.id})
    assert res["had_hold"] is True
    assert model.session_info(s.path)["held"] is False


def test_delete_session_op(tmp_path):
    archive, s = _archive(tmp_path)
    res = api.dispatch("delete_session", {"archive": str(archive), "run_id": s.id})
    assert ".trash" in res["trashed"]
    assert model.list_sessions(archive) == []
