import builtins
import io

import pytest

import nebula
from nebula.tags import collect_tags, input_tag


def _make_sessions(archive, tag_sets):
    """Create one closed session per tag list in tag_sets."""
    for tags in tag_sets:
        s = nebula.new(archive, tags=list(tags), description="t")
        s.close()


def test_collect_tags_counts_across_sessions(tmp_path):
    archive = tmp_path / "archive"
    _make_sessions(archive, [["warmup", "RP23D"], ["warmup"], ["noise"]])

    counts = collect_tags(archive)
    assert counts["warmup"] == 2
    assert counts["RP23D"] == 1
    assert counts["noise"] == 1
    assert "missing" not in counts


def test_collect_tags_empty_archive(tmp_path):
    # A not-yet-created archive root is fine -- no tags, no error.
    assert collect_tags(tmp_path / "nope") == {}


def _feed_input(monkeypatch, lines):
    """Make builtins.input() return successive lines, then raise EOFError
    (as a closed stdin would) so an under-fed test still terminates."""
    it = iter(lines)

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(builtins, "input", fake_input)


def test_input_tag_basic_entry(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    _feed_input(monkeypatch, ["warmup, RP23D", ""])  # add two, then finish
    assert input_tag(archive) == ["warmup", "RP23D"]


def test_input_tag_dedupes_and_preserves_order(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    _feed_input(monkeypatch, ["a", "b", "a", "/done"])
    assert input_tag(archive) == ["a", "b"]


def test_input_tag_remove_and_clear(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    _feed_input(monkeypatch, ["a, b, c", "/remove b", "/done"])
    assert input_tag(archive) == ["a", "c"]

    _feed_input(monkeypatch, ["a, b", "/clear", "z", ""])
    assert input_tag(archive) == ["z"]


def test_input_tag_list_shows_existing(tmp_path, monkeypatch, capsys):
    archive = tmp_path / "archive"
    _make_sessions(archive, [["warmup"], ["warmup"], ["noise"]])

    _feed_input(monkeypatch, ["/list", "/done"])
    result = input_tag(archive)
    out = capsys.readouterr().out
    assert result == []
    assert "warmup" in out
    assert "noise" in out
    assert "2 sessions" in out  # warmup used twice


def test_input_tag_search_filters(tmp_path, monkeypatch, capsys):
    archive = tmp_path / "archive"
    _make_sessions(archive, [["warmup"], ["warm_reset"], ["noise"]])

    _feed_input(monkeypatch, ["/search warm", "/done"])
    input_tag(archive)
    out = capsys.readouterr().out
    assert "warmup" in out
    assert "warm_reset" in out
    assert "noise" not in out


def test_input_tag_flags_new_tag(tmp_path, monkeypatch, capsys):
    archive = tmp_path / "archive"
    _make_sessions(archive, [["warmup"]])

    _feed_input(monkeypatch, ["warmup, brandnew", "/done"])
    result = input_tag(archive)
    out = capsys.readouterr().out
    assert result == ["warmup", "brandnew"]
    assert "brandnew" in out and "new tag" in out
    # An existing tag is added without the "new tag" nag.
    assert "warmup' (new tag)" not in out


class _FakeTTY(io.StringIO):
    def isatty(self):
        return True


def test_color_emitted_on_tty(monkeypatch):
    import io as _io  # local alias so the module-level import stays tidy
    from nebula import tags

    monkeypatch.delenv("NO_COLOR", raising=False)
    buf = _FakeTTY()
    tags._print_tag_table([("warmup", 2)], selected=["warmup"], query="warm", file=buf)
    out = buf.getvalue()
    assert "\033[" in out          # ANSI codes present on a tty
    # The matched query is split out into its own coloured run, so the two
    # halves of the tag appear either side of the highlight codes.
    assert "warm" in out and "up" in out
    assert "\033[33m" in out        # yellow highlight for the "warm" match


def test_no_color_env_disables_color(monkeypatch):
    from nebula import tags

    monkeypatch.setenv("NO_COLOR", "1")
    buf = _FakeTTY()
    tags._print_tag_table([("warmup", 2)], selected=["warmup"], query="warm", file=buf)
    assert "\033[" not in buf.getvalue()


def test_non_tty_is_plain(monkeypatch):
    from nebula import tags

    monkeypatch.delenv("NO_COLOR", raising=False)
    buf = io.StringIO()  # no isatty() -> treated as not a terminal
    tags._print_tag_table([("warmup", 2)], selected=[], file=buf)
    assert "\033[" not in buf.getvalue()


def test_input_tag_non_interactive_returns_initial(tmp_path, monkeypatch):
    archive = tmp_path / "archive"

    def eof_input(prompt=""):
        raise EOFError

    monkeypatch.setattr(builtins, "input", eof_input)
    assert input_tag(archive, initial=["preset"]) == ["preset"]
