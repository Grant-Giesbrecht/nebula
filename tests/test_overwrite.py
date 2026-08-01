"""Overwrite protection: duplicate (default), overwrite, or cancel."""

import json
from pathlib import Path

import pytest

import nebula
from nebula import manual
from nebula.config import ArchiveSettings, read_settings, write_settings
from nebula.navigator import model
from nebula.session import duplicate_name, resolve_write_target
from nebula.sidecar import read_sidecar


def _archive(tmp_path, policy=None):
    archive = tmp_path / "archive"
    archive.mkdir()
    if policy:
        write_settings(archive, ArchiveSettings(on_overwrite=policy))
    return archive


def _force_same_timestamp(session_dir, when="2026-01-01T00:00:00+00:00"):
    """Give every sidecar an identical `created`.

    Timestamps have second resolution, so ties are common in real use --
    but whether a test produces one depends on where the clock happens to
    fall, which makes assertions about tie-breaking flaky. Force it.
    """
    for sc in Path(session_dir).glob("*.meta.json"):
        data = json.loads(sc.read_text())
        data["created"] = when
        sc.write_text(json.dumps(data))


def _write(session, name, text):
    with session.artifact(name) as fn:
        fn.write_text(text)


# ---------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------

@pytest.mark.parametrize("name,n,expected", [
    ("raw.csv", 1, "raw-001.csv"),
    ("raw.csv", 12, "raw-012.csv"),
    ("noext", 1, "noext-001"),
    ("data.tar.gz", 1, "data.tar-001.gz"),
    ("run.2026-07-31.csv", 2, "run.2026-07-31-002.csv"),
])
def test_duplicate_name(name, n, expected):
    assert duplicate_name(name, n) == expected


# ---------------------------------------------------------------------
# the default: duplicate
# ---------------------------------------------------------------------

def test_repeat_writes_land_beside_each_other(tmp_path):
    archive = _archive(tmp_path)
    s = nebula.new(archive, description="d")
    for i in range(3):
        _write(s, "raw.csv", f"attempt {i}")
    s.close()

    names = sorted(p.name for p in s.path.glob("raw*.csv"))
    assert names == ["raw-001.csv", "raw-002.csv", "raw.csv"]
    assert (s.path / "raw.csv").read_text() == "attempt 0"       # never clobbered
    assert (s.path / "raw-002.csv").read_text() == "attempt 2"


def test_rename_is_recorded_on_the_sidecar(tmp_path):
    archive = _archive(tmp_path)
    s = nebula.new(archive, description="d")
    _write(s, "raw.csv", "first")
    _write(s, "raw.csv", "second")
    s.close()

    first = read_sidecar(s.path / "raw.csv")
    second = read_sidecar(s.path / "raw-001.csv")
    assert first.original_name is None and first.duplicate_index is None
    assert second.original_name == "raw.csv"
    assert second.duplicate_index == 1


def test_each_duplicate_keeps_its_own_provenance(tmp_path):
    archive = _archive(tmp_path)
    s = nebula.new(archive, description="d")
    with s.artifact("raw.csv", inputs={"gain": 1}) as fn:
        fn.write_text("a")
    with s.artifact("raw.csv", inputs={"gain": 2}) as fn:
        fn.write_text("b")
    s.close()

    assert read_sidecar(s.path / "raw.csv").inputs == {"gain": 1}
    assert read_sidecar(s.path / "raw-001.csv").inputs == {"gain": 2}


def test_duplicates_are_not_a_check_problem(tmp_path):
    from nebula import check as check_mod

    archive = _archive(tmp_path)
    s = nebula.new(archive, description="d")
    _write(s, "raw.csv", "a")
    _write(s, "raw.csv", "b")
    s.close()
    assert check_mod.check(archive) == []


# ---------------------------------------------------------------------
# the other two policies
# ---------------------------------------------------------------------

def test_overwrite_policy_replaces(tmp_path):
    archive = _archive(tmp_path, "overwrite")
    s = nebula.new(archive, description="d")
    _write(s, "raw.csv", "first")
    _write(s, "raw.csv", "second")
    s.close()

    assert sorted(p.name for p in s.path.glob("raw*.csv")) == ["raw.csv"]
    assert (s.path / "raw.csv").read_text() == "second"
    assert read_sidecar(s.path / "raw.csv").original_name is None


def test_cancel_policy_refuses(tmp_path):
    archive = _archive(tmp_path, "cancel")
    s = nebula.new(archive, description="d")
    _write(s, "raw.csv", "first")

    with pytest.raises(FileExistsError) as e:
        _write(s, "raw.csv", "second")
    assert "on_overwrite" in str(e.value)
    assert (s.path / "raw.csv").read_text() == "first"
    s.close()


def test_unknown_policy_falls_back_to_duplicate(tmp_path):
    archive = _archive(tmp_path)
    (archive / "archive.yaml").write_text("on_overwrite: destroy-everything\n")
    assert read_settings(archive).on_overwrite == "duplicate"


def test_policy_only_applies_to_collisions(tmp_path):
    archive = _archive(tmp_path, "cancel")
    s = nebula.new(archive, description="d")
    _write(s, "a.csv", "a")
    _write(s, "b.csv", "b")          # different name, no collision
    s.close()
    assert (s.path / "b.csv").is_file()


# ---------------------------------------------------------------------
# imports honour it too
# ---------------------------------------------------------------------

def test_import_duplicates_instead_of_refusing(tmp_path):
    archive = _archive(tmp_path)
    s = nebula.new(archive, description="d")
    _write(s, "raw.csv", "original")
    s.close()

    src = tmp_path / "raw.csv"
    src.write_text("from a coworker")
    dest = manual.import_file(archive, s.id, src, origin="emailed", allow_frozen=True)

    assert dest.name == "raw-001.csv"
    meta = read_sidecar(dest)
    assert meta.original_name == "raw.csv" and meta.duplicate_index == 1
    assert meta.produced_by.source == "external"      # still an import
    assert (s.path / "raw.csv").read_text() == "original"


def test_import_cancel_policy_still_raises(tmp_path):
    archive = _archive(tmp_path, "cancel")
    s = nebula.new(archive, description="d")
    _write(s, "raw.csv", "original")
    s.close()

    src = tmp_path / "raw.csv"
    src.write_text("x")
    with pytest.raises(FileExistsError):
        manual.import_file(archive, s.id, src, allow_frozen=True)


# ---------------------------------------------------------------------
# how the GUI groups them
# ---------------------------------------------------------------------

def test_items_group_duplicates_in_write_order(tmp_path):
    archive = _archive(tmp_path)
    s = nebula.new(archive, description="d")
    for i in range(3):
        _write(s, "raw.csv", str(i))
    _write(s, "other.csv", "x")
    s.close()

    items = model.list_items(s.path)
    raws = [it for it in items if it.display_name == "raw.csv"]
    # Every write is kept and numbered; the list itself is newest-first, so
    # the group reads downwards from the latest write.
    assert {it.name for it in raws} == {"raw.csv", "raw-001.csv", "raw-002.csv"}
    assert sorted((it.position, it.name) for it in raws) == [
        (1, "raw.csv"), (2, "raw-001.csv"), (3, "raw-002.csv")]
    assert {it.total for it in raws} == {3}
    assert all(it.is_duplicate for it in raws)

    other = [it for it in items if it.name == "other.csv"][0]
    assert other.is_duplicate is False and other.position == 1 and other.total == 1
    assert other.display_name == "other.csv"


def test_group_members_stay_adjacent(tmp_path):
    """Sorting by filename alone splits a group: '-' sorts before '.'."""
    archive = _archive(tmp_path)
    s = nebula.new(archive, description="d")
    _write(s, "raw.csv", "a")
    _write(s, "raw.csv", "b")
    _write(s, "rax.csv", "c")        # sorts between raw-001.csv and raw.csv
    s.close()
    _force_same_timestamp(s.path)

    names = [it.name for it in model.list_items(s.path)]
    assert names == ["raw.csv", "raw-001.csv", "rax.csv"]


def test_display_name_is_what_was_asked_for(tmp_path):
    archive = _archive(tmp_path)
    s = nebula.new(archive, description="d")
    _write(s, "raw.csv", "a")
    _write(s, "raw.csv", "b")
    s.close()

    dup = [it for it in model.list_items(s.path) if it.name == "raw-001.csv"][0]
    assert dup.display_name == "raw.csv"        # what the GUI renders
    assert dup.name == "raw-001.csv"            # what is on disk


# ---------------------------------------------------------------------
# the resolver itself
# ---------------------------------------------------------------------

def test_resolve_write_target_leaves_a_free_name_alone(tmp_path):
    path, original, index = resolve_write_target(tmp_path, "free.csv", "duplicate")
    assert path == tmp_path / "free.csv" and original is None and index is None


def test_resolve_write_target_fills_gaps_in_order(tmp_path):
    (tmp_path / "raw.csv").write_text("a")
    (tmp_path / "raw-001.csv").write_text("b")
    path, original, index = resolve_write_target(tmp_path, "raw.csv", "duplicate")
    assert path.name == "raw-002.csv" and original == "raw.csv" and index == 2


# ---------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------

def test_items_sort_newest_first_not_by_name(tmp_path):
    import time

    archive = _archive(tmp_path)
    s = nebula.new(archive, description="d")
    for name in ("zebra.csv", "apple.csv", "middle.csv"):
        _write(s, name, "x")
        time.sleep(1.1)          # timestamps have second resolution
    s.close()

    assert [it.name for it in model.list_items(s.path)] == [
        "middle.csv", "apple.csv", "zebra.csv"]


def test_duplicates_stay_in_write_order_when_timestamps_tie(tmp_path):
    """Files written within the same second must not fall back to a
    filename sort, which would put raw-001.csv before raw.csv."""
    archive = _archive(tmp_path)
    s = nebula.new(archive, description="d")
    _write(s, "raw.csv", "a")
    _write(s, "raw.csv", "b")
    _write(s, "rax.csv", "c")
    s.close()
    _force_same_timestamp(s.path)

    names = [it.name for it in model.list_items(s.path)]
    assert names.index("raw.csv") < names.index("raw-001.csv")   # 1 of 2 before 2 of 2


def test_undated_items_sort_last(tmp_path):
    archive = _archive(tmp_path)
    s = nebula.new(archive, description="d")
    _write(s, "dated.csv", "x")
    s.close()
    # a stray sidecar with no timestamp and no file to take an mtime from
    (s.path / "mystery.csv.meta.json").write_text('{"created": null, "produced_by": {}}')

    names = [it.name for it in model.list_items(s.path)]
    assert names[-1] == "mystery.csv"
