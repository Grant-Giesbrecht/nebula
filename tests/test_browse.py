"""
`nebula browse` -- the interactive cd/ls-style shell.

Driven the same way test_session_select.py drives its picker: monkeypatch
is_interactive() to True and builtins.input() to replay scripted lines,
then assert on what landed on stdout.
"""

import builtins
import contextlib
import io

import pytest

import nebula
from nebula import annotations, assets, browse, collection, transfer
from nebula.registry import get_registry


def _force_interactive(monkeypatch):
    monkeypatch.setattr(browse, "is_interactive", lambda: True)


def _feed_input(monkeypatch, lines):
    it = iter(lines)

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(builtins, "input", fake_input)


def _run(archive, monkeypatch, lines, *, start_archive=None, start_run_id=None):
    """Drive run_browse with `lines` (an "exit" is appended so the loop
    always terminates cleanly) and return everything it printed, stdout
    and stderr interleaved (errors/warnings go to stderr, same convention
    as the rest of the CLI -- see _termui.err/warn)."""
    _force_interactive(monkeypatch)
    _feed_input(monkeypatch, list(lines) + ["exit"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        browse.run_browse(start_archive=start_archive, start_run_id=start_run_id)
    return buf.getvalue()


@pytest.fixture
def populated_archive(tmp_path):
    root = tmp_path / "archive"
    s = nebula.new(root, description="a run", announce=False)
    with s.artifact("raw.csv", tags=["RP23D"]) as fn:
        fn.write_text("x")
    s.close()

    collection.create(root, "paper-2026", title="Paper 2026")
    collection.add(root, "paper-2026", f"{s.id}/raw.csv")

    calib = tmp_path / "calib.json"
    calib.write_text("{}")
    meta = assets.import_asset(root, str(calib), name="calib.json")

    return root, s.id, meta.id


@pytest.fixture
def registered_archive(tmp_path):
    """A registered (nameable-by-URI) archive with two sessions, so refs
    and full nebula:// URIs pointing at the *other* session are
    resolvable -- something populated_archive can't test since it has
    only one and is never registered."""
    root = tmp_path / "archive"
    transfer.init_archive(root, kind="standard", name="postdoc", user="tester@example.edu")
    get_registry().register_archive(root)

    with nebula.session(root, new_session=True, announce=False) as s1:
        with s1.artifact("a.csv", tags=["x"]) as fn:
            fn.write_text("1")
        run1 = s1.id

    with nebula.session(root, new_session=True, announce=False) as s2:
        with s2.artifact("s21_sweep.HDF5", tags=["RP23D"]) as fn:
            fn.write_text("hdf5")
        annotations.set_annotation(s2.path, "s21_sweep.HDF5", comment="key sweep")
        run2 = s2.id

    from nebula import uris

    uri = uris.describe(root, session=run2, file="s21_sweep.HDF5").uri
    return root, run1, run2, uri


# ---------------------------------------------------------------------
# non-interactive refusal
# ---------------------------------------------------------------------

def test_refuses_when_not_interactive(monkeypatch, tmp_path):
    monkeypatch.setattr(browse, "is_interactive", lambda: False)
    with pytest.raises(SystemExit) as exc:
        browse.run_browse()
    assert exc.value.code == 1


# ---------------------------------------------------------------------
# navigation
# ---------------------------------------------------------------------

def test_cd_into_archive_and_session_lists_artifacts(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", f"cd {run_id}", "ls"])
    assert "raw.csv" in out


def test_bare_enter_repeats_the_listing(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", ""])
    assert run_id in out


def test_multi_segment_cd_and_dotdot_chain(populated_archive, monkeypatch):
    root, run_id, asset_id = populated_archive
    out = _run(root, monkeypatch, [
        f"cd {root}",
        "cd collections/paper-2026",
        "pwd",
        "cd ../../assets",
        "pwd",
        f"cd {asset_id}",
        "pwd",
    ])
    assert "collections/paper-2026" in out
    lines = [l for l in out.splitlines() if l.startswith("/")]
    assert any(l.endswith("/assets") for l in lines)
    assert any(l.endswith(f"/assets/{asset_id}") for l in lines)


def test_bad_cd_leaves_cursor_unchanged(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [
        f"cd {root}", "pwd", "cd nonexistent/deeper", "pwd",
    ])
    pwds = [l for l in out.splitlines() if l.startswith("/") and l != "/"]
    assert len(pwds) == 2
    assert pwds[0] == pwds[1]
    assert "no such" in out


def test_cd_to_root_and_back(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", f"cd {run_id}", "cd /", "pwd"])
    assert out.strip().splitlines()[-1] == "/"


def test_cd_unknown_archive_reports_known_ones(tmp_path, monkeypatch):
    out = _run(tmp_path, monkeypatch, ["cd nope"])
    assert "no such archive" in out


# ---------------------------------------------------------------------
# show / info / uri
# ---------------------------------------------------------------------

def test_show_flags_inside_a_session(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", f"cd {run_id}", "show -u -t -l"])
    assert "uri:" in out
    assert "tags: RP23D" in out
    assert "sha256:" in out


def test_info_is_show_dash_l(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", f"cd {run_id}", "info raw.csv"])
    assert "sha256:" in out
    assert "uri:" in out
    assert "tags:" in out


def test_uri_command_prints_a_nebula_uri(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", f"cd {run_id}", "uri raw.csv"])
    assert "nebula://" in out
    assert "raw.csv" in out


def test_show_collection_prints_its_tree(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [
        f"cd {root}", "cd collections", "show paper-2026",
    ])
    assert "paper-2026" in out
    assert f"{run_id}/raw.csv" in out


def test_show_asset_prints_its_detail(populated_archive, monkeypatch):
    root, run_id, asset_id = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", "cd assets", f"show {asset_id}"])
    assert asset_id in out
    assert "calib.json" in out


# ---------------------------------------------------------------------
# annotate
# ---------------------------------------------------------------------

def test_annotate_add_tags(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [
        f"cd {root}", f"cd {run_id}", "annotate raw.csv --add-tags extra",
    ])
    assert "extra" in out

    from nebula import annotations
    from nebula.session import _find_session_dir

    session_dir = _find_session_dir(root, run_id)
    assert "extra" in annotations.get(session_dir, "raw.csv")["tags"]


def test_annotate_outside_a_session_is_refused(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", "annotate raw.csv --add-tags x"])
    assert "annotate only works on a session" in out


# ---------------------------------------------------------------------
# open / reveal
# ---------------------------------------------------------------------

def test_open_and_reveal_are_mocked_not_launched(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    calls = []
    from nebula.navigator import osutil

    monkeypatch.setattr(osutil, "open_path", lambda p: calls.append(("open", p)) or True)
    monkeypatch.setattr(osutil, "reveal_path", lambda p: calls.append(("reveal", p)) or True)

    _run(root, monkeypatch, [f"cd {root}", f"cd {run_id}", "open raw.csv", "reveal raw.csv"])

    kinds = [c[0] for c in calls]
    assert kinds == ["open", "reveal"]
    assert all(str(p).endswith("raw.csv") for _, p in calls)


# ---------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------

def test_unknown_command(populated_archive, monkeypatch):
    out = _run(populated_archive[0], monkeypatch, ["bogus"])
    assert "unknown command" in out


def test_help_lists_commands(populated_archive, monkeypatch):
    out = _run(populated_archive[0], monkeypatch, ["help"])
    assert "cd <name>" in out
    assert "annotate <name>" in out


def test_start_archive_and_run_id_land_directly_in_the_session(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, ["pwd"], start_archive=str(root), start_run_id=run_id)
    assert out.strip().splitlines()[-1].endswith(f"/{run_id}")


# ---------------------------------------------------------------------
# numbered listings, referenced by number
# ---------------------------------------------------------------------

def test_ls_numbers_entries(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", "ls"])
    assert "1. collections/" in out
    assert "2. assets/" in out
    assert f"3. {run_id}" in out


def test_cd_by_number(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", "ls", "cd 3", "pwd"])
    assert out.strip().splitlines()[-1].endswith(f"/{run_id}")


def test_show_and_info_by_number_inside_a_session(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [
        f"cd {root}", f"cd {run_id}", "ls", "show 1 -u", "info 1",
    ])
    assert "uri:" in out
    assert "sha256:" in out


def test_annotate_by_number(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    _run(root, monkeypatch, [
        f"cd {root}", f"cd {run_id}", "ls", "annotate 1 --add-tags numbered",
    ])
    from nebula import annotations
    from nebula.session import _find_session_dir

    session_dir = _find_session_dir(root, run_id)
    assert "numbered" in annotations.get(session_dir, "raw.csv")["tags"]


def test_number_out_of_range_is_treated_as_a_literal_name(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", "ls", "cd 999"])
    assert "no such session '999'" in out


def test_number_refers_to_the_listing_that_was_actually_shown(populated_archive, monkeypatch):
    """cd changes location without re-listing, so a stale number from a
    previous `ls` must not silently resolve against the new location."""
    root, run_id, asset_id = populated_archive
    out = _run(root, monkeypatch, [
        f"cd {root}", "ls",       # 3 -> run_id, in the archive listing
        "cd assets", "ls",        # re-lists: now 1 -> asset_id
        "cd 1", "pwd",
    ])
    assert out.strip().splitlines()[-1].endswith(f"/assets/{asset_id}")


# ---------------------------------------------------------------------
# A!/S! shortcuts inside the shell
# ---------------------------------------------------------------------

def test_cd_bang_archive(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    from nebula.registry import get_registry

    reg = get_registry()
    reg.register("postdoc", root)
    reg.set_default("postdoc")

    out = _run(root, monkeypatch, ["cd A!", "pwd"])
    assert out.strip().splitlines()[-1] == "/postdoc"


def test_cd_bang_session(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", "cd S!", "pwd"])
    assert out.strip().splitlines()[-1].endswith(f"/{run_id}")


# ---------------------------------------------------------------------
# refs and full URIs as input (cd, show, info, open, reveal, uri, copy)
# ---------------------------------------------------------------------

def test_cd_session_file_shorthand_lands_on_the_session(registered_archive, monkeypatch):
    """The reported bug: `cd S-26-0002/file.HDF5` (the verbatim spelling
    a search hit or derived_from line prints) used to error instead of
    landing on the session that holds the file."""
    root, run1, run2, uri = registered_archive
    out = _run(root, monkeypatch, [
        "cd postdoc", f"cd {run2}/s21_sweep.HDF5", "pwd",
    ])
    assert out.strip().splitlines()[-1].endswith(f"/postdoc/{run2}")


def test_cd_full_uri_lands_on_the_session(registered_archive, monkeypatch):
    root, run1, run2, uri = registered_archive
    out = _run(root, monkeypatch, ["cd " + uri, "pwd"])
    assert out.strip().splitlines()[-1].endswith(f"/postdoc/{run2}")


def test_cd_ref_to_a_nonexistent_session_reports_an_error_and_stays_put(
        registered_archive, monkeypatch):
    root, run1, run2, uri = registered_archive
    out = _run(root, monkeypatch, [
        "cd postdoc", f"cd {run1}", "pwd", "cd S-26-9999/nope.csv", "pwd",
    ])
    pwds = [l for l in out.splitlines() if l.startswith("/") and l != "/"]
    assert len(pwds) == 2 and pwds[0] == pwds[1]


def test_info_accepts_session_file_shorthand_from_a_different_session(
        registered_archive, monkeypatch):
    """Same bug, via `info` instead of `cd`: this is the exact command
    (copy-pasted output of `search`) that was reported failing."""
    root, run1, run2, uri = registered_archive
    out = _run(root, monkeypatch, [
        "cd postdoc", f"cd {run1}", f"info {run2}/s21_sweep.HDF5",
    ])
    assert "sha256:" in out
    assert "tags: RP23D" in out
    assert "comment: key sweep" in out


def test_show_accepts_a_full_uri(registered_archive, monkeypatch):
    root, run1, run2, uri = registered_archive
    out = _run(root, monkeypatch, ["cd postdoc", f"show {uri} -u -t"])
    assert "uri:" in out
    assert "tags: RP23D" in out


def test_uri_command_accepts_session_file_shorthand(registered_archive, monkeypatch):
    root, run1, run2, uri = registered_archive
    out = _run(root, monkeypatch, ["cd postdoc", f"uri {run2}/s21_sweep.HDF5"])
    assert uri in out


def test_open_and_reveal_accept_a_full_uri(registered_archive, monkeypatch):
    root, run1, run2, uri = registered_archive
    from nebula.navigator import osutil

    calls = []
    monkeypatch.setattr(osutil, "open_path", lambda p: calls.append(("open", p)) or True)
    monkeypatch.setattr(osutil, "reveal_path", lambda p: calls.append(("reveal", p)) or True)

    _run(root, monkeypatch, ["cd postdoc", f"open {uri}", f"reveal {uri}"])
    assert [c[0] for c in calls] == ["open", "reveal"]
    assert all(str(p).endswith("s21_sweep.HDF5") for _, p in calls)


# ---------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------

def test_copy_uri(registered_archive, monkeypatch):
    root, run1, run2, uri = registered_archive
    from nebula.navigator import osutil

    copied = []
    monkeypatch.setattr(osutil, "copy_to_clipboard", lambda t: copied.append(t) or True)

    out = _run(root, monkeypatch, ["cd postdoc", f"cd {run2}", "copy s21_sweep.HDF5"])
    assert copied == [uri]
    assert "copied" in out


def test_copy_path(registered_archive, monkeypatch):
    root, run1, run2, uri = registered_archive
    from nebula.navigator import osutil

    copied = []
    monkeypatch.setattr(osutil, "copy_to_clipboard", lambda t: copied.append(t) or True)

    _run(root, monkeypatch, ["cd postdoc", f"cd {run2}", "copy s21_sweep.HDF5 --path"])
    assert len(copied) == 1
    assert copied[0].endswith("s21_sweep.HDF5")
    assert not copied[0].startswith("nebula://")


def test_copy_falls_back_to_printing_when_clipboard_unreachable(
        registered_archive, monkeypatch):
    root, run1, run2, uri = registered_archive
    from nebula.navigator import osutil

    monkeypatch.setattr(osutil, "copy_to_clipboard", lambda t: False)

    out = _run(root, monkeypatch, ["cd postdoc", f"cd {run2}", "copy s21_sweep.HDF5"])
    assert "could not reach the system clipboard" in out
    assert uri in out


def test_copy_by_number(registered_archive, monkeypatch):
    root, run1, run2, uri = registered_archive
    from nebula.navigator import osutil

    copied = []
    monkeypatch.setattr(osutil, "copy_to_clipboard", lambda t: copied.append(t) or True)

    _run(root, monkeypatch, ["cd postdoc", f"cd {run2}", "ls", "copy 1"])
    assert copied == [uri]


# ---------------------------------------------------------------------
# show -c/--comment
# ---------------------------------------------------------------------

def test_show_comment_flag(registered_archive, monkeypatch):
    root, run1, run2, uri = registered_archive
    out = _run(root, monkeypatch, ["cd postdoc", f"cd {run2}", "show s21_sweep.HDF5 -c"])
    assert "comment: key sweep" in out
    assert "tags:" not in out


# ---------------------------------------------------------------------
# search --tag/--comment and the grep-like `| filter`
# ---------------------------------------------------------------------

def test_search_tag_and_comment_flags(registered_archive, monkeypatch):
    root, run1, run2, uri = registered_archive
    out = _run(root, monkeypatch, ["cd postdoc", "search sweep --tag --comment"])
    assert "tags: RP23D" in out
    assert "comment: key sweep" in out


def test_search_filter_keeps_matching_records(registered_archive, monkeypatch):
    root, run1, run2, uri = registered_archive
    out = _run(root, monkeypatch, [
        "cd postdoc", "search sweep --tag --comment | key",
    ])
    assert "s21_sweep.HDF5" in out


def test_search_filter_drops_non_matching_records(registered_archive, monkeypatch):
    root, run1, run2, uri = registered_archive
    out = _run(root, monkeypatch, [
        "cd postdoc", "search sweep --tag --comment | nonexistentxyz",
    ])
    assert "nothing matches the filter" in out
    assert "s21_sweep.HDF5" not in out


# ---------------------------------------------------------------------
# a bare session id resolves from anywhere in the archive, not just the
# sessions listing (so `cd`/show/info/etc. never need `cd ..` first)
# ---------------------------------------------------------------------

def test_cd_bare_session_id_from_inside_a_different_session(registered_archive, monkeypatch):
    root, run1, run2, uri = registered_archive
    out = _run(root, monkeypatch, ["cd postdoc", f"cd {run1}", f"cd {run2}", "pwd"])
    assert out.strip().splitlines()[-1].endswith(f"/postdoc/{run2}")


def test_cd_bare_session_id_from_inside_assets(registered_archive, monkeypatch):
    root, run1, run2, uri = registered_archive
    out = _run(root, monkeypatch, ["cd postdoc", "cd assets", f"cd {run2}", "pwd"])
    assert out.strip().splitlines()[-1].endswith(f"/postdoc/{run2}")


def test_info_bare_session_ref_from_inside_a_different_session(registered_archive, monkeypatch):
    root, run1, run2, uri = registered_archive
    out = _run(root, monkeypatch, [
        "cd postdoc", f"cd {run1}", f"info {run2}/s21_sweep.HDF5",
    ])
    assert "sha256:" in out


# ---------------------------------------------------------------------
# tab completion (_ref_completions) -- exercised directly, the same way
# readline would call it, rather than driving real terminal keystrokes
# ---------------------------------------------------------------------

def test_complete_bare_word_offers_session_ids_and_shortcuts(registered_archive):
    root, run1, run2, uri = registered_archive
    state = browse._State()
    browse._do_cd(state, "postdoc")
    got = set(browse._ref_completions(state, ""))
    assert {run1, run2, "collections", "assets", "A!", "S!"} <= got


def test_complete_session_slash_partial_filename(registered_archive):
    root, run1, run2, uri = registered_archive
    state = browse._State()
    browse._do_cd(state, "postdoc")
    got = browse._ref_completions(state, f"{run2}/s21")
    assert got == [f"{run2}/s21_sweep.HDF5"]


def test_complete_works_from_a_different_session(registered_archive):
    """The reported gap: completing `show S-26-0002/pump_` (or here,
    s21_sweep) while sitting inside a *different* session, not the one
    named in the partial ref."""
    root, run1, run2, uri = registered_archive
    state = browse._State()
    browse._do_cd(state, "postdoc")
    browse._do_cd(state, run1)
    got = browse._ref_completions(state, f"{run2}/s21")
    assert got == [f"{run2}/s21_sweep.HDF5"]


def test_complete_bang_archive_slash_session_slash_partial(registered_archive):
    root, run1, run2, uri = registered_archive
    get_registry().register("postdoc", root)
    get_registry().set_default("postdoc")

    state = browse._State()  # still at the global root
    got = browse._ref_completions(state, f"A!/{run2}/s21")
    assert got == [f"A!/{run2}/s21_sweep.HDF5"]


def test_complete_unresolvable_prefix_returns_nothing(registered_archive):
    root, run1, run2, uri = registered_archive
    state = browse._State()
    browse._do_cd(state, "postdoc")
    assert browse._ref_completions(state, "S-26-9999/x") == []


def test_complete_does_not_offer_names_for_a_flag(registered_archive):
    """A word starting with "-" is a flag, not a name -- _install_completer
    special-cases this itself; here we just confirm _ref_completions
    isn't what's consulted for one (it would return odd results for "-t"
    if it were, since nothing starts with a literal dash)."""
    root, run1, run2, uri = registered_archive
    state = browse._State()
    browse._do_cd(state, "postdoc")
    assert browse._ref_completions(state, "-t") == []
