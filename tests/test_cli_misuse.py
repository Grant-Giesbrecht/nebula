"""CLI misuse: pointing a command at an archive name/path that doesn't
resolve to a real archive.

Regression coverage for a real bug: `nebula ls <unregistered-name>` used to
crash with a raw sqlite3.OperationalError several frames deep in index.py,
because _resolve_archive_cli() silently treated the unknown name as a
literal (nonexistent) relative path and every command downstream assumed
`root` was already a real archive directory. The fix makes
_resolve_archive_cli() itself the checkpoint: an unresolvable archive now
exits(1) with a clear message, for every command that goes through it.
"""

import pytest

import nebula
from nebula.cli import _resolve_archive_cli, main
from nebula.registry import get_registry


def _register(root, nickname):
    from nebula import transfer

    transfer.init_archive(root, name=nickname)
    get_registry().register_archive(root)
    return root


# ---------------------------------------------------------------------
# _resolve_archive_cli() directly -- the single choke point every
# archive-taking subcommand goes through.
# ---------------------------------------------------------------------

def test_resolve_registered_name(tmp_path):
    root = _register(tmp_path / "postdoc", "postdoc")
    resolved, name = _resolve_archive_cli("postdoc")
    assert resolved == root
    assert name == "postdoc"


def test_resolve_literal_path_to_real_archive(tmp_path):
    from nebula import transfer

    root = tmp_path / "scratch"
    transfer.init_archive(root, name="scratch")
    resolved, name = _resolve_archive_cli(str(root))
    assert resolved == root


def test_resolve_unregistered_and_nonexistent_name_exits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _resolve_archive_cli("does-not-exist-anywhere")
    assert exc.value.code == 1


def test_resolve_existing_dir_without_archive_yaml_is_allowed(tmp_path):
    # Ad hoc archives created purely through nebula.new()/session() (no
    # 'nebula init') never get an archive.yaml, and are a supported,
    # tested way to use nebula -- see session.new()'s own warn-don't-raise
    # handling of the same case. The CLI resolver must not be stricter
    # than the API it's calling into.
    plain_dir = tmp_path / "just_a_folder"
    plain_dir.mkdir()
    resolved, name = _resolve_archive_cli(str(plain_dir))
    assert resolved == plain_dir
    assert name == "local"


# ---------------------------------------------------------------------
# Through main(): the actual bug report was `nebula ls postdoc` where
# "postdoc" was never registered. Every command that takes a bare
# `archive` positional should fail the same clean way, not with whatever
# exception the first filesystem/db operation downstream happens to raise.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("subcommand", ["ls", "rebuild", "check", "index"])
def test_unregistered_archive_name_exits_cleanly_not_a_raw_exception(
    subcommand, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main([subcommand, "postdoc"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "postdoc" in err
    assert "Traceback" not in err


@pytest.mark.parametrize("subcommand", ["ls", "rebuild", "check", "index"])
def test_existing_non_archive_directory_does_not_crash(subcommand, tmp_path):
    # An empty, existing, un-init'd directory is the ad hoc/API-only
    # archive case (see test_resolve_existing_dir_without_archive_yaml_is_
    # allowed) -- it should behave like an empty archive, not error.
    plain_dir = tmp_path / "not_an_archive"
    plain_dir.mkdir()
    main([subcommand, str(plain_dir)])  # should not raise


def test_ls_on_real_registered_archive_still_works(tmp_path, capsys):
    _register(tmp_path / "postdoc", "postdoc")
    main(["ls", "postdoc"])  # should not raise
    out = capsys.readouterr().out
    assert "no sessions" in out.lower() or out.strip() == ""


def test_error_message_lists_known_archives(tmp_path, monkeypatch, capsys):
    _register(tmp_path / "postdoc", "postdoc")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["ls", "typo-of-postdoc"])
    err = capsys.readouterr().err
    assert "postdoc" in err  # the real nickname is surfaced as a hint


# ---------------------------------------------------------------------
# The name on screen is a name you can type
# ---------------------------------------------------------------------

def _declared(tmp_path, folder, *, name, key=None, user=None):
    from nebula import transfer

    root = tmp_path / folder
    transfer.init_archive(root, name=name, user=user)
    get_registry().register_archive(root, key=key)
    return root


def test_every_command_accepts_the_name_archives_prints(tmp_path, capsys):
    """`nebula archives` shows the declared name; `nebula ls` used to
    refuse it and suggest the registry key instead."""
    root = _declared(tmp_path, "folder", name="declared", key="a-nickname")

    resolved, label = _resolve_archive_cli("declared")
    assert resolved == root

    main(["ls", "declared"])            # no SystemExit
    capsys.readouterr()
    main(["ls", "a-nickname"])          # and the key still works
    capsys.readouterr()


def test_an_archive_resolves_by_its_id_too(tmp_path):
    from nebula.config import read_settings

    root = _declared(tmp_path, "folder", name="declared", key="a-nickname")
    ident = read_settings(root, apply_env=False).id
    assert _resolve_archive_cli(ident)[0] == root


def test_an_unknown_name_lists_names_that_would_work(tmp_path, capsys):
    _declared(tmp_path, "folder", name="declared", key="a-nickname")
    with pytest.raises(SystemExit):
        _resolve_archive_cli("nonsense")
    err_text = capsys.readouterr().err
    assert "declared" in err_text and "a-nickname" in err_text


def test_an_ambiguous_name_is_refused_rather_than_treated_as_a_path(tmp_path,
                                                                    capsys):
    """Two archives, one declared name. Neither picking one nor falling
    through to "...and it is not a directory either" is a useful answer."""
    _declared(tmp_path, "a", name="shared", user="me@here.edu", key="mine")
    _declared(tmp_path, "b", name="shared", user="jane@lab.edu", key="theirs")
    with pytest.raises(SystemExit):
        _resolve_archive_cli("shared")
    err_text = capsys.readouterr().err
    assert "different archives" in err_text
    assert "mine" in err_text and "theirs" in err_text


def test_remove_accepts_the_declared_name(tmp_path, capsys):
    _declared(tmp_path, "folder", name="declared", key="a-nickname")
    main(["register", "--remove", "declared"])
    assert "removed" in capsys.readouterr().out
    assert get_registry().all() == {}


def test_remove_forgets_every_alias(tmp_path, capsys):
    """Otherwise --remove looks like it did nothing: the archive is still
    listed, under another of its names."""
    root = _declared(tmp_path, "folder", name="declared", key="first")
    get_registry().register_archive(root, key="second")

    main(["register", "--remove", "declared"])
    out = capsys.readouterr().out
    assert "first" in out and "second" in out
    assert get_registry().all() == {}


def test_archives_tells_two_same_named_archives_apart(tmp_path, capsys):
    """Two rows that both read "shared" leave no way to say which one a
    command should act on."""
    _declared(tmp_path, "a", name="shared", user="me@here.edu", key="mine")
    _declared(tmp_path, "b", name="shared", user="jane@lab.edu", key="theirs")
    main(["archives"])
    out = capsys.readouterr().out
    assert "shared (me@here.edu)" in out
    assert "shared (jane@lab.edu)" in out
    # ...and the path is still readable, not run into by the longer name.
    for line in out.splitlines():
        if "shared (" in line:
            assert ") /" in line or ")  " in line


def test_removing_one_of_two_same_named_archives_says_so(tmp_path, capsys):
    _declared(tmp_path, "a", name="shared", user="me@here.edu", key="mine")
    _declared(tmp_path, "b", name="shared", user="jane@lab.edu", key="theirs")
    main(["register", "--remove", "mine"])
    got = capsys.readouterr()
    assert "removed" in got.out
    assert "still declare the name 'shared'" in got.err
    assert list(get_registry().all()) == ["theirs"]
