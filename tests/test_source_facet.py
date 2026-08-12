"""
"Show me every hand-imported file" -- source as a search facet.

The distinction that matters: a sidecar written before produced_by.source
existed reads as "script" because that is the default, not because anyone
recorded it. See relational-data-roadmap item 6.
"""
import json

import pytest

import nebula
from nebula import manual, transfer
from nebula.navigator import model


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "arc"
    transfer.init_archive(root, kind="standard", name="arc", user="g@ncsu.edu")
    return root


def _script_file(archive, name="fit.png"):
    s = nebula.new(archive, description="run")
    with s.artifact(name) as fn:
        fn.write_text("x")
    s.close()
    return s


def _strip_source(session_dir, name):
    """Make a sidecar look like one written before the field existed."""
    p = session_dir / f"{name}.meta.json"
    raw = json.loads(p.read_text())
    raw["produced_by"].pop("source", None)
    p.write_text(json.dumps(raw))


def test_a_script_written_file_records_its_source(archive):
    s = _script_file(archive)
    it = next(i for i in model.list_items(s.path) if i.name == "fit.png")
    assert it.source == "script"
    assert it.source_recorded is True
    assert model.item_source_facet(it) == "script"


def test_an_old_sidecar_is_unrecorded_not_script(archive):
    s = _script_file(archive)
    _strip_source(s.path, "fit.png")
    it = next(i for i in model.list_items(s.path) if i.name == "fit.png")
    assert it.source == "script"          # the default still reads through
    assert it.source_recorded is False    # but it was never stated
    assert model.item_source_facet(it) == "unrecorded"


def test_a_hand_imported_file_is_external(archive, tmp_path):
    src = tmp_path / "outside.csv"
    src.write_text("a,b\n")
    s = nebula.new(archive, description="import target")
    s.close()
    manual.import_file(archive, s.id, src)
    it = next(i for i in model.list_items(s.path) if i.name == "outside.csv")
    assert model.item_source_facet(it) == "external"


def test_filtering_to_external_answers_the_audit_question(archive, tmp_path):
    s = _script_file(archive)
    src = tmp_path / "outside.csv"
    src.write_text("a\n")
    manual.import_file(archive, s.id, src)

    res = model.search_items(archive, "", sources=["external"])
    assert [i["item"].name for i in res["items"]] == ["outside.csv"]


def test_the_filter_narrows_without_a_query(archive):
    """Unlike a search term, a facet is a filter -- it must work alone."""
    _script_file(archive)
    assert model.search_items(archive, "")["items"] == []
    assert model.search_items(archive, "", sources=["script"])["items"]


def test_unrecorded_is_selectable_on_its_own(archive):
    a = _script_file(archive, "new.png")
    b = _script_file(archive, "old.png")
    _strip_source(b.path, "old.png")
    res = model.search_items(archive, "", sources=["unrecorded"])
    assert [i["item"].name for i in res["items"]] == ["old.png"]


def test_selecting_every_facet_is_no_restriction(archive):
    _script_file(archive)
    allf = model.search_items(archive, "fit", sources=list(model.SOURCE_FACETS))
    plain = model.search_items(archive, "fit")
    assert len(allf["items"]) == len(plain["items"]) == 1


def test_selecting_no_facet_matches_nothing(archive):
    """An explicit empty selection asks for nothing. It must not quietly
    read as 'no restriction' just because a query is also present."""
    _script_file(archive)
    assert model.search_items(archive, "fit", sources=[])["items"] == []
    assert model.search_items(archive, "", sources=[])["items"] == []


def test_none_means_no_filter_at_all(archive):
    _script_file(archive)
    assert len(model.search_items(archive, "fit", sources=None)["items"]) == 1


def test_the_bridge_exposes_the_facet(archive):
    from nebula.navigator import api
    s = _script_file(archive)
    got = api.OPS["list_items"]({"session_path": str(s.path)})
    row = next(r for r in got if r["name"] == "fit.png")
    assert row["source_facet"] == "script"
    assert row["source_recorded"] is True
