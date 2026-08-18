"""User annotations: mutable tags/comments that never touch sidecars."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import nebula
from nebula import annotations, check as check_mod
from nebula.annotations import TagError
from nebula.navigator import model


def _session(archive, files=("a.csv",), tags=("created-tag",)):
    s = nebula.new(archive, description="a session", tags=list(tags))
    for name in files:
        with s.artifact(name) as fn:
            fn.write_text("x")
    s.close()
    return s


# ---------------------------------------------------------------------
# tag rules
# ---------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("  spaced  ", "spaced"),           # outer whitespace stripped
    ("two   words", "two words"),       # internal runs collapsed
    ("paper:2026", "paper:2026"),       # colons allowed
    ("rp23-warmup", "rp23-warmup"),     # hyphens allowed
    ("shows_drift", "shows_drift"),     # underscores allowed
    ("RP23D", "RP23D"),                 # case preserved
])
def test_clean_tag_accepts(raw, expected):
    assert annotations.clean_tag(raw) == expected


@pytest.mark.parametrize("bad", ["", "   ", "has,comma", "has\nnewline", "has\ttab"])
def test_clean_tag_rejects(bad):
    with pytest.raises(TagError):
        annotations.clean_tag(bad)


def test_clean_tags_dedupes_but_keeps_order():
    assert annotations.clean_tags(["b", "a", "b ", " a"]) == ["b", "a"]


def test_split_tags_parses_the_comma_form():
    assert annotations.split_tags("one, two:three , ,four") == ["one", "two:three", "four"]


# ---------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------

def test_annotations_live_beside_the_session(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)
    annotations.set_annotation(s.path, "a.csv", tags=["shows-drift"], comment="note")
    assert (s.path / "annotations.yaml").is_file()


def test_sidecar_is_never_modified(tmp_path):
    """The whole point: annotating must not touch machine-written files."""
    archive = tmp_path / "archive"
    s = _session(archive)
    sidecar = s.path / "a.csv.meta.json"
    before = sidecar.read_bytes()
    before_yaml = (s.path / "session.yaml").read_bytes()

    annotations.set_annotation(s.path, "a.csv", tags=["t"], comment="c")
    annotations.set_annotation(s.path, None, tags=["s"], comment="sc")

    assert sidecar.read_bytes() == before
    assert (s.path / "session.yaml").read_bytes() == before_yaml


def test_creation_tags_and_user_tags_stay_separate(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive, tags=["created-tag"])
    annotations.set_annotation(s.path, None, tags=["user-tag"])

    info = model.session_info(s.path)
    assert info["tags"] == ["created-tag"]        # sacred, from session.yaml
    assert info["user_tags"] == ["user-tag"]      # mutable, from annotations
    assert "user-tag" not in info["tags"]


def test_no_file_until_there_is_something_to_store(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)
    assert not (s.path / "annotations.yaml").exists()
    assert annotations.get(s.path) == {"tags": [], "comment": ""}


def test_clearing_everything_removes_the_file(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)
    annotations.set_annotation(s.path, "a.csv", tags=["t"], comment="c")
    annotations.set_annotation(s.path, "a.csv", tags=[], comment="")
    assert not (s.path / "annotations.yaml").exists()


def test_comment_survives_multiple_lines(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)
    text = "First line.\n\nSecond paragraph with detail.\n  indented note"
    annotations.set_annotation(s.path, "a.csv", comment=text)
    assert annotations.get(s.path, "a.csv")["comment"] == text.strip()


def test_editing_one_target_leaves_the_other_alone(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive, files=("a.csv", "b.csv"))
    annotations.set_annotation(s.path, "a.csv", tags=["ta"], comment="ca")
    annotations.set_annotation(s.path, "b.csv", tags=["tb"])
    annotations.set_annotation(s.path, None, comment="session note")

    assert annotations.get(s.path, "a.csv") == {"tags": ["ta"], "comment": "ca"}
    assert annotations.get(s.path, "b.csv") == {"tags": ["tb"], "comment": ""}
    assert annotations.get(s.path)["comment"] == "session note"


def test_setting_only_tags_keeps_the_comment(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)
    annotations.set_annotation(s.path, "a.csv", tags=["t"], comment="keep me")
    annotations.set_annotation(s.path, "a.csv", tags=["t2"])
    got = annotations.get(s.path, "a.csv")
    assert got == {"tags": ["t2"], "comment": "keep me"}


def test_add_and_remove_tags(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)
    annotations.add_tags(s.path, "a.csv", ["one", "two"])
    annotations.add_tags(s.path, "a.csv", ["two", "three"])      # dedupes
    assert annotations.get(s.path, "a.csv")["tags"] == ["one", "two", "three"]
    annotations.remove_tags(s.path, "a.csv", ["two"])
    assert annotations.get(s.path, "a.csv")["tags"] == ["one", "three"]


def test_append_comment_adds_a_line_without_losing_the_rest(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)
    annotations.append_comment(s.path, "a.csv", "first line")
    annotations.append_comment(s.path, "a.csv", "second line")
    assert annotations.get(s.path, "a.csv")["comment"] == "first line\nsecond line"


def test_append_comment_onto_empty_comment(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)
    annotations.append_comment(s.path, "a.csv", "  padded  ")
    assert annotations.get(s.path, "a.csv")["comment"] == "padded"


def test_append_comment_blank_text_is_a_no_op(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)
    annotations.set_annotation(s.path, "a.csv", comment="keep")
    annotations.append_comment(s.path, "a.csv", "   ")
    assert annotations.get(s.path, "a.csv")["comment"] == "keep"


def test_bad_tag_is_rejected_without_writing(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)
    annotations.set_annotation(s.path, "a.csv", tags=["good"])
    with pytest.raises(TagError):
        annotations.set_annotation(s.path, "a.csv", tags=["also,bad"])
    assert annotations.get(s.path, "a.csv")["tags"] == ["good"]


def test_malformed_file_is_ignored_not_fatal(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)
    (s.path / "annotations.yaml").write_text("{{{ not yaml")
    assert annotations.get(s.path, "a.csv") == {"tags": [], "comment": ""}
    assert model.list_items(s.path)[0].user_tags == []


def test_hand_edited_junk_tags_are_dropped(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)
    (s.path / "annotations.yaml").write_text(
        "version: 1\nartifacts:\n  a.csv:\n    tags: ['ok', '', 'has,comma']\n")
    assert annotations.get(s.path, "a.csv")["tags"] == ["ok"]


# ---------------------------------------------------------------------
# not an artifact
# ---------------------------------------------------------------------

def test_annotations_file_is_not_treated_as_an_artifact(tmp_path):
    """It sits in the session folder, so every artifact scan must skip it
    -- otherwise it shows up as an orphan and close() stubs a sidecar."""
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="x")
    with s.artifact("a.csv") as fn:
        fn.write_text("1")
    annotations.set_annotation(s.path, None, comment="written while open")
    s.close()

    assert [i.name for i in model.list_items(s.path)] == ["a.csv"]
    assert not (s.path / "annotations.yaml.meta.json").exists()
    assert check_mod.check(archive) == []


# ---------------------------------------------------------------------
# search
# ---------------------------------------------------------------------

def test_search_finds_artifacts_by_user_tag(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive, files=("a.csv", "b.csv"))
    annotations.set_annotation(s.path, "a.csv", tags=["shows-drift"])

    hits = [h["item"].name for h in model.search_items(archive, "shows-drift")["items"]]
    assert hits == ["a.csv"]


def test_search_finds_artifacts_by_comment(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive, files=("a.csv", "b.csv"))
    annotations.set_annotation(s.path, "b.csv", comment="the run that showed the phenomenon")

    hits = [h["item"].name for h in model.search_items(archive, "phenomenon")["items"]]
    assert hits == ["b.csv"]


def test_search_can_exclude_annotations(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)
    annotations.set_annotation(s.path, "a.csv", tags=["findme"], comment="alsofindme")

    assert model.search_items(archive, "findme", fields=["filename"])["items"] == []
    assert model.search_items(archive, "alsofindme", fields=["user_tags"])["items"] == []
    assert model.search_items(archive, "findme", fields=["user_tags"])["items"]


def test_session_user_tags_reach_the_session_list(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)
    annotations.set_annotation(s.path, None, tags=["thesis-ch3"])
    assert model.list_sessions(archive)[0].user_tags == ["thesis-ch3"]


# ---------------------------------------------------------------------
# check
# ---------------------------------------------------------------------

def test_check_reports_annotations_for_a_missing_file(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)
    annotations.set_annotation(s.path, "gone.csv", comment="notes for a file that left")

    issues = [i for i in check_mod.check(archive) if i.kind == "annotation_without_file"]
    assert len(issues) == 1
    assert issues[0].severity == "info"        # not corruption
    assert issues[0].file == "gone.csv"


# ---------------------------------------------------------------------
# bulk editing (GUI multi-select)
# ---------------------------------------------------------------------

def test_bulk_annotate_adds_and_removes_tags_across_targets(tmp_path):
    from nebula.navigator import api

    archive = tmp_path / "archive"
    s = _session(archive, files=("a.csv", "b.csv"))
    annotations.add_tags(s.path, "a.csv", ["keep-me", "drop-me"])

    res = api.dispatch("bulk_annotate", {
        "targets": [{"session_path": str(s.path), "filename": "a.csv"},
                    {"session_path": str(s.path), "filename": "b.csv"}],
        "add_tags": "twpa-v6, shared",
        "remove_tags": "drop-me",
    })
    by_file = {r["filename"]: r for r in res["results"]}
    assert by_file["a.csv"]["ok"] and by_file["a.csv"]["tags"] == ["keep-me", "twpa-v6", "shared"]
    assert by_file["b.csv"]["ok"] and by_file["b.csv"]["tags"] == ["twpa-v6", "shared"]


def test_bulk_annotate_appends_a_comment_line_preserving_each_files_own(tmp_path):
    from nebula.navigator import api

    archive = tmp_path / "archive"
    s = _session(archive, files=("a.csv", "b.csv"))
    annotations.set_annotation(s.path, "a.csv", comment="a's own note")

    res = api.dispatch("bulk_annotate", {
        "targets": [{"session_path": str(s.path), "filename": "a.csv"},
                    {"session_path": str(s.path), "filename": "b.csv"}],
        "append_comment": "shows drift",
    })
    by_file = {r["filename"]: r for r in res["results"]}
    assert by_file["a.csv"]["comment"] == "a's own note\nshows drift"
    assert by_file["b.csv"]["comment"] == "shows drift"


def test_bulk_annotate_bad_shared_tag_reports_error_without_touching_targets(tmp_path):
    from nebula.navigator import api

    archive = tmp_path / "archive"
    s = _session(archive, files=("a.csv",))
    annotations.set_annotation(s.path, "a.csv", tags=["existing"])

    res = api.dispatch("bulk_annotate", {
        "targets": [{"session_path": str(s.path), "filename": "a.csv"}],
        "add_tags": "bad\ttab",   # invalid: tabs aren't allowed inside a tag
    })
    assert res["results"] == [] and "error" in res
    assert annotations.get(s.path, "a.csv")["tags"] == ["existing"]


def test_bulk_annotate_one_bad_target_does_not_stop_the_rest(tmp_path):
    from nebula.navigator import api

    archive = tmp_path / "archive"
    s = _session(archive, files=("a.csv", "b.csv"))
    # A regular file standing in for "this target's session is gone" --
    # writing an annotation there fails with a real OSError, deterministically
    # and without touching anything outside tmp_path.
    not_a_session = tmp_path / "not_a_session.txt"
    not_a_session.write_text("x")

    res = api.dispatch("bulk_annotate", {
        "targets": [{"session_path": str(s.path), "filename": "a.csv"},
                    {"session_path": str(not_a_session), "filename": "b.csv"},
                    {"session_path": str(s.path), "filename": "c.csv"}],
        "add_tags": "twpa-v6",
    })
    by_file = {r["filename"]: r for r in res["results"]}
    assert by_file["a.csv"]["ok"] and by_file["a.csv"]["tags"] == ["twpa-v6"]
    assert by_file["c.csv"]["ok"] and by_file["c.csv"]["tags"] == ["twpa-v6"]
    assert not by_file["b.csv"]["ok"] and by_file["b.csv"]["error"]
    assert annotations.get(s.path, "a.csv")["tags"] == ["twpa-v6"]


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def _nebula(*args):
    return subprocess.run([sys.executable, "-m", "nebula.cli", *args],
                          capture_output=True, text=True)


def test_cli_annotate_round_trip(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)

    add = _nebula("annotate", str(archive), s.id, "a.csv",
                  "--add-tags", "shows-drift, paper:2026", "--comment", "seen here")
    assert add.returncode == 0, add.stderr
    assert "shows-drift, paper:2026" in add.stdout
    assert "seen here" in add.stdout

    show = _nebula("annotate", str(archive), s.id[-4:], "a.csv")   # bare-number id
    assert show.returncode == 0, show.stderr
    assert "shows-drift" in show.stdout
    assert "(updated)" not in show.stdout        # reading changes nothing


def test_cli_annotate_session_level_and_removal(tmp_path):
    archive = tmp_path / "archive"
    s = _session(archive)
    _nebula("annotate", str(archive), s.id, "--set-tags", "a,b")
    out = _nebula("annotate", str(archive), s.id, "--rm-tags", "a").stdout
    assert "user tags: b" in out
    assert annotations.get(s.path)["tags"] == ["b"]
