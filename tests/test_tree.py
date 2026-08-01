"""The relations view's data: a nested, depth-capped provenance tree."""

from pathlib import Path

import pytest

import nebula
from nebula import index
from nebula.navigator import api, model


def _chain(archive):
    """raw -> proc -> fig, across two sessions."""
    s1 = nebula.new(archive, description="raw run")
    with s1.artifact("raw.csv") as fn:
        fn.write_text("1")
    s1.close()
    s2 = nebula.new(archive, description="processing")
    with s2.artifact("proc.csv", derived_from=[f"{s1.id}/raw.csv"]) as fn:
        fn.write_text("2")
    with s2.artifact("fig.png", derived_from=["proc.csv"]) as fn:
        fn.write_text("3")
    s2.close()
    return s1, s2


def _refs(nodes):
    return [n["ref"] for n in nodes]


def test_downstream_is_transitive(tmp_path):
    archive = tmp_path / "arc"
    s1, s2 = _chain(archive)
    tree = model.provenance_tree(archive, s1.id, "raw.csv")
    down = tree["branches"][0]["downstream"]
    assert _refs(down) == [f"{s2.id}/proc.csv"]
    assert _refs(down[0]["children"]) == [f"{s2.id}/fig.png"]


def test_upstream_is_transitive(tmp_path):
    archive = tmp_path / "arc"
    s1, s2 = _chain(archive)
    tree = model.provenance_tree(archive, s2.id, "fig.png")
    up = tree["branches"][0]["upstream"]
    assert _refs(up) == [f"{s2.id}/proc.csv"]
    assert _refs(up[0]["children"]) == [f"{s1.id}/raw.csv"]


def test_depth_caps_and_says_so(tmp_path):
    archive = tmp_path / "arc"
    s1, s2 = _chain(archive)
    tree = model.provenance_tree(archive, s1.id, "raw.csv", depth=1)
    down = tree["branches"][0]["downstream"]
    assert _refs(down) == [f"{s2.id}/proc.csv"]
    assert down[0]["children"] == []
    # the view needs to know there is more, or "depth 1" looks like "the end"
    assert down[0]["truncated"] is True


def test_a_leaf_is_not_marked_truncated(tmp_path):
    """Only a node whose children were actually withheld says so."""
    archive = tmp_path / "arc"
    s1, s2 = _chain(archive)
    tree = model.provenance_tree(archive, s1.id, "raw.csv", depth=5)
    fig = tree["branches"][0]["downstream"][0]["children"][0]
    assert fig["truncated"] is False


def test_a_repeat_is_marked_rather_than_expanded_twice(tmp_path):
    """An indented tree cannot draw a diamond; it should at least admit to
    one instead of silently duplicating a whole subtree."""
    archive = tmp_path / "arc"
    s = nebula.new(archive, description="diamond")
    with s.artifact("raw.csv") as fn:
        fn.write_text("r")
    with s.artifact("a.csv", derived_from=["raw.csv"]) as fn:
        fn.write_text("a")
    with s.artifact("b.csv", derived_from=["raw.csv"]) as fn:
        fn.write_text("b")
    with s.artifact("merged.csv", derived_from=["a.csv", "b.csv"]) as fn:
        fn.write_text("m")
    s.close()

    tree = model.provenance_tree(archive, s.id, "merged.csv", direction="up", depth=5)
    up = tree["branches"][0]["upstream"]
    assert sorted(_refs(up)) == [f"{s.id}/a.csv", f"{s.id}/b.csv"]
    raws = [n for branch in up for n in branch["children"]]
    assert [n["ref"] for n in raws] == [f"{s.id}/raw.csv"] * 2
    # It appears under both parents (it really is reachable both ways), but
    # only one of them expands it; the other says so.
    assert sorted(n["seen"] for n in raws) == [False, True]


def test_a_cycle_terminates(tmp_path):
    """Two files declaring each other must not hang the view."""
    from nebula.sidecar import read_sidecar, write_sidecar

    archive = tmp_path / "arc"
    s = nebula.new(archive, description="cycle")
    with s.artifact("a.csv") as fn:
        fn.write_text("a")
    with s.artifact("b.csv", derived_from=["a.csv"]) as fn:
        fn.write_text("b")
    s.close()
    # hand-edit a.csv's sidecar to point back at b.csv
    meta = read_sidecar(Path(s.path) / "a.csv")
    meta.derived_from = [{"file": "b.csv"}]
    write_sidecar(Path(s.path) / "a.csv", meta)
    index.rebuild(archive)

    tree = model.provenance_tree(archive, s.id, "b.csv", direction="up", depth=50)
    node = tree["branches"][0]["upstream"][0]
    hops = 0
    while node.get("children"):
        node = node["children"][0]
        hops += 1
        assert hops < 10, "cycle was not cut"
    assert node["seen"] is True


def test_a_whole_session_becomes_one_branch_per_artefact(tmp_path):
    archive = tmp_path / "arc"
    s1, s2 = _chain(archive)
    tree = model.provenance_tree(archive, s2.id)
    assert sorted(b["item"]["filename"] for b in tree["branches"]) == ["fig.png", "proc.csv"]
    assert tree["session"]["description"] == "processing"


def test_missing_and_unreachable_are_reported_differently(tmp_path):
    archive = tmp_path / "arc"
    s = nebula.new(archive, description="refs out")
    with s.artifact("out.csv", derived_from=["S-26-9999/gone.csv",
                                             "nowhere|S-26-0001/far.csv"]) as fn:
        fn.write_text("x")
    s.close()

    up = model.provenance_tree(archive, s.id, "out.csv", direction="up")["branches"][0]["upstream"]
    by_file = {n["filename"]: n for n in up}
    assert by_file["gone.csv"]["resolved"] is True      # this archive, just absent
    assert by_file["gone.csv"]["exists"] is False
    assert by_file["far.csv"]["resolved"] is False      # can't even look
    assert "not registered" in by_file["far.csv"]["note"]


def test_the_tree_says_whether_it_used_the_index(tmp_path):
    archive = tmp_path / "arc"
    s1, _ = _chain(archive)
    assert model.provenance_tree(archive, s1.id, "raw.csv")["source"] == "index"


def test_it_still_answers_with_no_usable_index(tmp_path, monkeypatch):
    """Index-first, scan-as-fallback: a broken index costs speed, not truth."""
    archive = tmp_path / "arc"
    s1, s2 = _chain(archive)

    def no_index(*a, **kw):
        raise RuntimeError("no index for you")

    monkeypatch.setattr("nebula.index.open_fresh", no_index)
    tree = model.provenance_tree(archive, s1.id, "raw.csv")
    assert tree["source"] == "scan"
    down = tree["branches"][0]["downstream"]
    assert _refs(down) == [f"{s2.id}/proc.csv"]
    assert _refs(down[0]["children"]) == [f"{s2.id}/fig.png"]


def test_the_op_is_reachable_from_the_bridge(tmp_path):
    archive = tmp_path / "arc"
    s1, _ = _chain(archive)
    got = api.dispatch("provenance_tree", {"archive": str(archive), "run_id": s1.id,
                                           "filename": "raw.csv", "depth": 2})
    assert got["depth"] == 2 and got["branches"][0]["item"]["filename"] == "raw.csv"
