"""
Tagging a file at the moment you write it.

The gap this closes: nebula had per-file tags all along (annotations.yaml,
what `nebula annotate` edits and `nebula search tag:` reads), but nothing
in the Python API could set them. A measurement script could name the
*session's* tags and nothing else, so the tags that actually describe a
file had to be added afterwards, by hand, from the CLI -- which meant in
practice they were not added at all.

The other half is where they must *not* go: a sidecar records what
happened and is never rewritten. A tag is something you change your mind
about. Keeping them apart is why `artifact(tags=...)` writes annotations
rather than a sidecar field.
"""

import pytest

import nebula
from nebula import annotations, transfer


def _archive(root, *, user="g@ncsu.edu", name="postdoc"):
    transfer.init_archive(root, kind="standard", name=name, user=user)
    return root


@pytest.fixture()
def sess(tmp_path):
    root = _archive(tmp_path / "arc")
    s = nebula.new(root, tags=["run-level"], description="d")
    yield s
    s.close()


# ---------------------------------------------------------------------
# the tags land, and land in the right file
# ---------------------------------------------------------------------

def test_artifact_tags_are_written_at_creation(sess):
    with sess.artifact("raw.csv", tags=["shows-drift", "twpa"]) as fn:
        fn.write_text("x")
    assert annotations.get(sess.path, "raw.csv")["tags"] == ["shows-drift", "twpa"]


def test_tags_go_in_annotations_not_the_sidecar(sess):
    """The separation the whole design rests on: a sidecar is a sealed
    record of what happened, so a mutable label must not become one of its
    fields -- otherwise editing a tag later means rewriting provenance."""
    with sess.artifact("raw.csv", tags=["mutable"]) as fn:
        fn.write_text("x")
    meta = nebula.read_sidecar(sess.artifact_path("raw.csv"))
    assert "tags" not in meta.to_dict()
    assert "mutable" not in str(meta.to_dict())


def test_a_comment_can_be_set_at_creation(sess):
    with sess.artifact("raw.csv", comment="the run that showed it") as fn:
        fn.write_text("x")
    assert annotations.get(sess.path, "raw.csv")["comment"] == "the run that showed it"


def test_file_tags_are_separate_from_session_tags(sess):
    with sess.artifact("raw.csv", tags=["file-level"]) as fn:
        fn.write_text("x")
    assert sess.meta.tags == ["run-level"]
    assert annotations.get(sess.path, "raw.csv")["tags"] == ["file-level"]
    assert annotations.get(sess.path)["tags"] == []      # session annotations


def test_untagged_artifacts_write_no_annotations_file(sess):
    """A file with nothing to say about it must not leave an empty
    annotations.yaml in every session nebula has ever written."""
    with sess.artifact("raw.csv") as fn:
        fn.write_text("x")
    assert not annotations.annotations_path(sess.path).exists()


# ---------------------------------------------------------------------
# failing early
# ---------------------------------------------------------------------

def test_a_bad_tag_raises_before_the_measurement_runs(sess):
    """At the artifact() call, not at block exit: a typo'd tag must cost a
    traceback while there is still nothing to lose, not after an hour of
    sweeping has been written to disk."""
    with pytest.raises(annotations.TagError):
        sess.artifact("raw.csv", tags=["has,a,comma"])
    assert not sess.artifact_path("raw.csv").exists()


def test_a_bare_string_of_tags_is_refused(sess):
    """"a, b" would otherwise be iterated character by character into
    single-letter tags -- the same guard new() and input_tag() carry."""
    with pytest.raises(TypeError) as e:
        sess.artifact("raw.csv", tags="drift, twpa")
    assert "list of strings" in str(e.value)


# ---------------------------------------------------------------------
# session-wide artifact tags
# ---------------------------------------------------------------------

def test_artifact_tags_apply_to_every_file(tmp_path):
    """The "tags collected once at the top of the script" case: they
    describe the data, so they belong on the data."""
    root = _archive(tmp_path / "arc")
    s = nebula.new(root, artifact_tags=["sweep-a"])
    for name in ("one.csv", "two.csv"):
        with s.artifact(name) as fn:
            fn.write_text("x")
    s.close()
    for name in ("one.csv", "two.csv"):
        assert annotations.get(s.path, name)["tags"] == ["sweep-a"]


def test_per_artifact_tags_add_to_the_session_wide_ones(tmp_path):
    root = _archive(tmp_path / "arc")
    s = nebula.new(root, artifact_tags=["sweep-a"])
    with s.artifact("one.csv", tags=["outlier"]) as fn:
        fn.write_text("x")
    s.close()
    assert annotations.get(s.path, "one.csv")["tags"] == ["sweep-a", "outlier"]


def test_a_tag_named_twice_is_stored_once(tmp_path):
    root = _archive(tmp_path / "arc")
    s = nebula.new(root, artifact_tags=["sweep-a"])
    with s.artifact("one.csv", tags=["sweep-a"]) as fn:
        fn.write_text("x")
    s.close()
    assert annotations.get(s.path, "one.csv")["tags"] == ["sweep-a"]


def test_artifact_tags_are_validated_when_the_session_opens(tmp_path):
    root = _archive(tmp_path / "arc")
    with pytest.raises(annotations.TagError):
        nebula.new(root, artifact_tags=["bad,tag"])


def test_artifact_tags_survive_appending_to_an_existing_session(tmp_path):
    """The asymmetry that motivates this argument: session tags only
    describe a session being created, so appending discards them --
    artifact tags describe the files this script writes, so they apply
    either way."""
    root = _archive(tmp_path / "arc")
    first = nebula.new(root, tags=["original"])
    first.close()

    again = nebula.append_to(root, first.id, artifact_tags=["second-script"])
    with again.artifact("late.csv") as fn:
        fn.write_text("x")
    again.close()

    assert again.meta.tags == ["original"]
    assert annotations.get(again.path, "late.csv")["tags"] == ["second-script"]


# ---------------------------------------------------------------------
# the awkward edges
# ---------------------------------------------------------------------

def test_tags_follow_a_file_renamed_by_overwrite_protection(tmp_path):
    """Tags are stored under the name on disk. If protection wrote
    raw-001.csv, tagging "raw.csv" would describe the *previous* run's
    file -- the same trap _redirect_ref exists for on the lineage side."""
    root = _archive(tmp_path / "arc")
    from nebula.config import read_settings, write_settings
    settings = read_settings(root, apply_env=False)
    settings.on_overwrite = "rename"
    write_settings(root, settings)

    s = nebula.new(root)
    with s.artifact("raw.csv", tags=["first"]) as fn:
        fn.write_text("1")
    with s.artifact("raw.csv", tags=["second"]) as fn:
        fn.write_text("2")
    s.close()

    names = sorted(p.name for p in s.path.glob("raw*.csv"))
    assert names == ["raw-001.csv", "raw.csv"]
    # The first write kept the name asked for; the second was renamed.
    assert annotations.get(s.path, "raw.csv")["tags"] == ["first"]
    assert annotations.get(s.path, "raw-001.csv")["tags"] == ["second"]


def test_nothing_is_annotated_when_the_write_fails(sess):
    """Same rule as the sidecar: a block that raised produced no artifact,
    so it must leave no trace claiming one exists."""
    with pytest.raises(ValueError):
        with sess.artifact("raw.csv", tags=["never"]):
            raise ValueError("boom")
    assert annotations.get(sess.path, "raw.csv")["tags"] == []


def test_write_meta_for_takes_tags_too(sess):
    """The lower-level escape hatch has to reach the same field, or
    switching to it silently loses the tags."""
    sess.artifact_path("raw.csv").write_text("x")
    sess.write_meta_for("raw.csv", tags=["hatch"])
    assert annotations.get(sess.path, "raw.csv")["tags"] == ["hatch"]


def test_annotate_reaches_the_session_itself(sess):
    sess.annotate(tags=["about-the-run"], comment="warmed up first")
    got = annotations.get(sess.path)
    assert got["tags"] == ["about-the-run"]
    assert got["comment"] == "warmed up first"


def test_annotate_accumulates_tags_rather_than_replacing(sess):
    sess.annotate("raw.csv", tags=["one"])
    sess.annotate("raw.csv", tags=["two"])
    assert annotations.get(sess.path, "raw.csv")["tags"] == ["one", "two"]


def test_annotate_with_nothing_to_say_is_a_no_op(sess):
    sess.annotate("raw.csv")
    assert not annotations.annotations_path(sess.path).exists()


def test_tags_are_searchable_the_same_way_annotate_makes_them(tmp_path):
    """The point of using annotations rather than a new field: these show
    up in the search everyone already uses."""
    from nebula.navigator.model import search_items

    root = _archive(tmp_path / "arc")
    s = nebula.new(root)
    with s.artifact("hit.csv", tags=["twpa-v6"]) as fn:
        fn.write_text("x")
    with s.artifact("miss.csv") as fn:
        fn.write_text("x")
    s.close()

    found = search_items(root, "user_tag:twpa-v6")
    hits = [row["item"].name for row in found["items"]]
    assert hits == ["hit.csv"]
