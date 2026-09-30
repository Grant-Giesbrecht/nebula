"""
The A!/S! shortcuts and `nebula default`.

A! stands for "the archive `nebula default` points at"; S! stands for
"the session `nebula.session(archive, reuse=True)` would use" -- the most
recent open/today/held one. Both are resolved centrally (see
cli.py's _resolve_archive_cli and _resolve_reuse_session_shortcut) so they
work everywhere an archive/run_id argument is accepted, not just in one
command.
"""

import pytest

import nebula
from nebula import transfer
from nebula.cli import DEFAULT_ARCHIVE_TOKEN, REUSE_SESSION_TOKEN, main
from nebula.registry import get_registry


def _archive(root, *, name, user="g@ncsu.edu"):
    transfer.init_archive(root, kind="standard", name=name, user=user)
    get_registry().register_archive(root)
    return root


def _session(root, *, filename="raw.csv"):
    s = nebula.new(root, description="a run", announce=False)
    with s.artifact(filename) as fn:
        fn.write_text("x")
    s.close()
    return s


# ---------------------------------------------------------------------
# nebula default
# ---------------------------------------------------------------------

def test_default_with_no_archive_reports_unset(capsys):
    main(["default"])
    err = capsys.readouterr().err
    assert "no default archive set" in err


def test_default_set_and_get(tmp_path, capsys):
    _archive(tmp_path / "postdoc", name="postdoc")
    main(["default", "postdoc"])
    out = capsys.readouterr().out
    assert "postdoc" in out

    main(["default"])
    out = capsys.readouterr().out
    assert out.strip() == "postdoc"


def test_default_unknown_archive_fails(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["default", "nope"])
    assert exc.value.code == 1
    assert "unknown archive" in capsys.readouterr().err.lower() or \
        "nope" in capsys.readouterr().err


# ---------------------------------------------------------------------
# A!
# ---------------------------------------------------------------------

def test_bang_archive_with_no_default_fails(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["ls", DEFAULT_ARCHIVE_TOKEN])
    assert exc.value.code == 1
    assert "no default archive set" in capsys.readouterr().err


def test_bang_archive_resolves_to_the_default(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc", name="postdoc")
    s = _session(root)
    main(["default", "postdoc"])
    capsys.readouterr()

    main(["show", DEFAULT_ARCHIVE_TOKEN, s.id])
    out = capsys.readouterr().out
    assert s.id in out
    assert "raw.csv" in out


def test_bang_archive_is_case_insensitive(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc", name="postdoc")
    s = _session(root)
    main(["default", "postdoc"])
    capsys.readouterr()

    main(["show", "a!", s.id])
    out = capsys.readouterr().out
    assert s.id in out


# ---------------------------------------------------------------------
# S!
# ---------------------------------------------------------------------

def test_bang_session_with_no_candidate_fails(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc", name="postdoc")
    with pytest.raises(SystemExit) as exc:
        main(["show", "postdoc", REUSE_SESSION_TOKEN])
    assert exc.value.code == 1
    assert "needs an open/today/held session" in capsys.readouterr().err


def test_bang_session_resolves_to_the_reuse_candidate(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc", name="postdoc")
    with nebula.session(root, reuse=True, announce=False) as s:
        with s.artifact("raw.csv") as fn:
            fn.write_text("x")
        run_id = s.id

    main(["show", "postdoc", REUSE_SESSION_TOKEN])
    out = capsys.readouterr().out
    assert run_id in out
    assert "raw.csv" in out


def test_bang_session_prefers_the_most_recent_candidate(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc", name="postdoc")
    with nebula.session(root, reuse=True, announce=False) as s1:
        with s1.artifact("a.csv") as fn:
            fn.write_text("x")

    with nebula.session(root, reuse=True, announce=False) as s2:
        with s2.artifact("b.csv") as fn:
            fn.write_text("x")
        run_id2 = s2.id

    main(["show", "postdoc", REUSE_SESSION_TOKEN])
    out = capsys.readouterr().out
    assert run_id2 in out


def test_bang_session_and_bang_archive_combine(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc", name="postdoc")
    with nebula.session(root, reuse=True, announce=False) as s:
        with s.artifact("raw.csv") as fn:
            fn.write_text("x")
        run_id = s.id
    main(["default", "postdoc"])
    capsys.readouterr()

    main(["show", DEFAULT_ARCHIVE_TOKEN, REUSE_SESSION_TOKEN])
    out = capsys.readouterr().out
    assert run_id in out
