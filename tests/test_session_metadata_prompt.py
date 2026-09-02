"""
Asking for a session's tags and description *after* the choice is made.

The bug: nebula.session(archive, tags=[...], description="...") made the
caller collect both up front, before the picker had run -- and then threw
them away if the user picked an existing session, silently. Half the time
the question cost typing and had no effect, and nothing said so.

The fix has two halves, and both are tested here: ask afterwards and only
when a new session is actually being made, and when they *were* supplied
and are being discarded, say so.
"""

import pytest

import nebula
from nebula import session_select, transfer
from nebula.session_select import ask_new_session_metadata


def _archive(root, *, user="g@ncsu.edu", name="postdoc"):
    transfer.init_archive(root, kind="standard", name=name, user=user)
    return root


@pytest.fixture()
def interactive(monkeypatch):
    """Pretend there is a human at the keyboard. The prompts are the thing
    under test, and they are all suppressed without this."""
    monkeypatch.setattr(session_select, "is_interactive", lambda: True)
    return monkeypatch


def _answers(monkeypatch, tags, description):
    """Stub the two prompts. input_tag is patched rather than driven
    through stdin because it is its own tested UI; what matters here is
    whether it gets called at all, and with what."""
    calls = {}

    def fake_input_tag(archive, *, prompt="tags", initial=None):
        calls["tag_prompt"] = {"prompt": prompt, "initial": initial}
        return list(tags)

    def fake_input(_prompt=""):
        calls["desc_prompt"] = _prompt
        return description

    monkeypatch.setattr(session_select, "input_tag", fake_input_tag)
    monkeypatch.setattr("builtins.input", fake_input)
    return calls


# ---------------------------------------------------------------------
# asked at the right moment
# ---------------------------------------------------------------------

def test_a_new_session_is_asked_for_tags_and_a_description(tmp_path, interactive):
    calls = _answers(interactive, ["drift"], "warm-up sweep")
    root = _archive(tmp_path / "arc")

    got_tags, got_desc = ask_new_session_metadata(root)

    assert got_tags == ["drift"]
    assert got_desc == "warm-up sweep"
    assert "tag_prompt" in calls and "desc_prompt" in calls


def test_supplied_values_are_not_asked_for_again(tmp_path, interactive):
    """The old spelling still works and stays quiet: someone who already
    knows the answers should not be interrogated about them."""
    calls = _answers(interactive, ["ignored"], "ignored")
    root = _archive(tmp_path / "arc")

    got_tags, got_desc = ask_new_session_metadata(
        root, tags=["given"], description="given")

    assert (got_tags, got_desc) == (["given"], "given")
    assert calls == {}


def test_only_the_missing_half_is_asked_for(tmp_path, interactive):
    calls = _answers(interactive, ["asked"], "asked")
    root = _archive(tmp_path / "arc")

    got_tags, got_desc = ask_new_session_metadata(root, description="given")

    assert (got_tags, got_desc) == (["asked"], "given")
    assert "tag_prompt" in calls and "desc_prompt" not in calls


def test_ask_true_asks_anyway_and_offers_what_was_supplied(tmp_path, interactive):
    calls = _answers(interactive, ["edited"], "edited")
    root = _archive(tmp_path / "arc")

    got_tags, got_desc = ask_new_session_metadata(
        root, tags=["draft"], description="draft", ask=True)

    assert (got_tags, got_desc) == (["edited"], "edited")
    # Pre-filled, not started from scratch -- otherwise ask=True would
    # mean retyping what you already passed in.
    assert calls["tag_prompt"]["initial"] == ["draft"]


def test_ask_false_never_prompts(tmp_path, interactive):
    calls = _answers(interactive, ["never"], "never")
    root = _archive(tmp_path / "arc")

    assert ask_new_session_metadata(root, ask=False) == (None, "")
    assert calls == {}


def test_a_batch_run_is_never_prompted(tmp_path, monkeypatch):
    """An unattended script must behave exactly as it did before: no
    prompt, no block, whatever it passed in."""
    monkeypatch.setattr(session_select, "is_interactive", lambda: False)
    calls = _answers(monkeypatch, ["never"], "never")
    root = _archive(tmp_path / "arc")

    assert ask_new_session_metadata(root, ask=True) == (None, "")
    assert calls == {}


def test_an_empty_description_answer_leaves_a_supplied_one_alone(tmp_path, interactive):
    """Enter on the prompt means "no change", not "erase what I passed"."""
    calls = _answers(interactive, ["t"], "")
    root = _archive(tmp_path / "arc")

    _, got_desc = ask_new_session_metadata(
        root, description="from the script", ask=True)

    assert got_desc == "from the script"
    assert "desc_prompt" in calls


def test_stdin_closing_mid_prompt_does_not_take_the_session_with_it(
        tmp_path, interactive):
    """Data is on the line by the time this is asked. Losing the terminal
    must cost the description, not the session."""
    interactive.setattr(session_select, "input_tag",
                        lambda *a, **k: ["from-tags"])
    def closed(_prompt=""):
        raise EOFError
    interactive.setattr("builtins.input", closed)
    root = _archive(tmp_path / "arc")

    assert ask_new_session_metadata(root) == (["from-tags"], "")


# ---------------------------------------------------------------------
# through the picker, end to end
# ---------------------------------------------------------------------

def test_choosing_new_in_the_picker_asks_and_stores_the_answers(
        tmp_path, interactive):
    interactive.setattr(session_select, "input_tag",
                        lambda *a, **k: ["picked-tag"])
    replies = iter(["/new", "picked description"])
    interactive.setattr("builtins.input", lambda _p="": next(replies))
    root = _archive(tmp_path / "arc")

    s = session_select.select_session(root)
    s.close()

    assert s.meta.tags == ["picked-tag"]
    assert s.meta.description == "picked description"


def test_choosing_an_existing_session_asks_nothing(tmp_path, interactive):
    root = _archive(tmp_path / "arc")
    first = nebula.new(root, tags=["original"], description="the first")
    first.close()

    def refuse(*a, **k):
        raise AssertionError("must not prompt when appending")

    interactive.setattr(session_select, "input_tag", refuse)
    replies = iter([first.id])
    interactive.setattr("builtins.input", lambda _p="": next(replies))

    s = session_select.select_session(root)
    s.close()

    assert s.id == first.id
    assert s.meta.tags == ["original"]


def test_discarded_tags_are_reported_rather_than_vanishing(
        tmp_path, interactive, capsys):
    """The original complaint. Passing tags= and then appending is not an
    error, but it must not look like it worked."""
    root = _archive(tmp_path / "arc")
    first = nebula.new(root, tags=["original"])
    first.close()

    replies = iter([first.id])
    interactive.setattr("builtins.input", lambda _p="": next(replies))

    s = session_select.select_session(
        root, tags=["from-the-script"], description="from the script")
    s.close()

    out = capsys.readouterr().out
    assert "from-the-script" in out
    assert "not applied" in out
    assert s.meta.tags == ["original"]


def test_appending_by_run_id_reports_the_same_thing(tmp_path, capsys):
    """nebula.session(run_id=...) reaches the same discard by a different
    door, so it carries the same warning."""
    root = _archive(tmp_path / "arc")
    first = nebula.new(root, tags=["original"])
    first.close()

    with nebula.session(root, run_id=first.id, tags=["dropped"]) as s:
        assert s.meta.tags == ["original"]

    assert "not applied" in capsys.readouterr().err


def test_session_wide_artifact_tags_reach_the_picker(tmp_path, interactive):
    """artifact_tags is the argument that is *not* discarded by appending,
    so it has to survive the route through the picker."""
    root = _archive(tmp_path / "arc")
    first = nebula.new(root)
    first.close()

    replies = iter([first.id])
    interactive.setattr("builtins.input", lambda _p="": next(replies))

    s = session_select.select_session(root, artifact_tags=["file-level"])
    with s.artifact("raw.csv") as fn:
        fn.write_text("x")
    s.close()

    from nebula import annotations
    assert annotations.get(s.path, "raw.csv")["tags"] == ["file-level"]
