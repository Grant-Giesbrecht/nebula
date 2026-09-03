"""
`nebula uri`, and URIs as input to the commands that take an archive.

The split between the two streams is the contract worth pinning down: the
URI alone goes to stdout so `URI=$(nebula uri ...)` works, and everything
a human wants to see -- the path, the caveats -- goes to stderr. Same rule
as `nebula whoami`, and a test is the only thing that keeps it true.
"""

import json

import pytest

import nebula
from nebula import transfer
from nebula.cli import main
from nebula.registry import get_registry


def _archive(root, *, name, user="g@ncsu.edu", register=True):
    transfer.init_archive(root, kind="standard", name=name, user=user)
    if register:
        get_registry().register_archive(root)
    return root


def _seg(root):
    """The archive segment a URI should carry: `label~id`, read from disk
    because the id is minted at random -- which is the point of it."""
    from nebula.config import read_settings

    got = read_settings(root, apply_env=False)
    return f"{got.name}~{got.id}" if got.id else got.name


def _session(root, filename="raw.csv"):
    s = nebula.new(root, description="a run", tags=["twpa"], announce=False)
    with s.artifact(filename) as fn:
        fn.write_text("x")
    s.close()
    return s


# ---------------------------------------------------------------------
# minting
# ---------------------------------------------------------------------

def test_uri_of_an_archive(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc", name="postdoc")
    main(["uri", "postdoc"])
    out = capsys.readouterr()
    assert out.out.strip() == f"nebula://g@ncsu.edu/{_seg(root)}"
    # The id is there, and it is what makes the URI survive a rename.
    assert out.out.strip().startswith("nebula://g@ncsu.edu/postdoc~")
    assert "kind:   archive" in out.err


def test_uri_of_a_session_and_a_file(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc", name="postdoc")
    s = _session(root)

    main(["uri", "postdoc", s.id])
    assert capsys.readouterr().out.strip() == \
        f"nebula://g@ncsu.edu/{_seg(root)}/{s.id}"

    main(["uri", "postdoc", s.id, "raw.csv"])
    assert capsys.readouterr().out.strip() == \
        f"nebula://g@ncsu.edu/{_seg(root)}/{s.id}/raw.csv"


def test_only_the_uri_reaches_stdout(tmp_path, capsys):
    """So it can be captured. Everything else -- path, kind, owner -- is on
    stderr, where a shell substitution will not pick it up."""
    root = _archive(tmp_path / "postdoc", name="postdoc")
    s = _session(root)
    main(["uri", "postdoc", s.id, "raw.csv"])
    out = capsys.readouterr()
    assert out.out.count("\n") == 1
    assert out.out.startswith("nebula://")
    assert str(root) in out.err            # the path did get shown, just not there


def test_a_bare_number_resolves_to_this_years_session(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc", name="postdoc")
    s = _session(root)
    main(["uri", "postdoc", s.id.rsplit("-", 1)[-1]])
    assert capsys.readouterr().out.strip().endswith(f"/{s.id}")


def test_uri_of_a_collection(tmp_path, capsys):
    from nebula import collection

    root = _archive(tmp_path / "postdoc", name="postdoc")
    collection.create(root, "paper-2026")
    main(["uri", "postdoc", "--collection", "paper-2026"])
    assert capsys.readouterr().out.strip() == \
        f"nebula://g@ncsu.edu/{_seg(root)}/collections/paper-2026"


def test_uri_of_an_asset(tmp_path, capsys):
    from nebula import assets

    root = _archive(tmp_path / "postdoc", name="postdoc")
    src = tmp_path / "cal.json"
    src.write_text("{}")
    meta = assets.import_asset(root, src)
    main(["uri", "postdoc", "--asset", meta.id])
    assert capsys.readouterr().out.strip() == \
        f"nebula://g@ncsu.edu/{_seg(root)}/assets/{meta.id}"


def test_uri_json(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc", name="postdoc")
    s = _session(root)
    main(["uri", "postdoc", s.id, "raw.csv", "--json"])
    got = json.loads(capsys.readouterr().out)
    assert got["kind"] == "file"
    assert got["user"] == "g@ncsu.edu"
    assert got["exists"] is True
    assert got["unique"] is True
    assert got["warnings"] == []


def test_an_unqualified_owner_is_warned_about(tmp_path, capsys):
    """The URI is still printed -- it is the best available name -- but the
    command says plainly that it is not unique."""
    root = _archive(tmp_path / "postdoc", name="postdoc", user="grant")
    main(["uri", "postdoc"])
    out = capsys.readouterr()
    assert out.out.strip() == f"nebula://grant/{_seg(root)}"
    assert "warning" in out.err
    assert "this machine and nowhere else" in out.err


def test_no_owner_at_all_fails_loudly(tmp_path, capsys, monkeypatch):
    from nebula.config import read_settings, write_settings

    monkeypatch.delenv("NEBULA_USER", raising=False)
    monkeypatch.setenv("NEBULA_IDENTITY", str(tmp_path / "absent.yaml"))
    root = _archive(tmp_path / "postdoc", name="postdoc", user="")
    settings = read_settings(root, apply_env=False)
    settings.user = ""
    write_settings(root, settings)

    with pytest.raises(SystemExit) as exc:
        main(["uri", "postdoc"])
    assert exc.value.code == 1
    assert "no owner" in capsys.readouterr().err


# ---------------------------------------------------------------------
# resolving
# ---------------------------------------------------------------------

def test_uri_resolves_a_uri_to_a_local_path(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc", name="postdoc")
    s = _session(root)
    uri = f"nebula://g@ncsu.edu/{_seg(root)}/{s.id}/raw.csv"

    main(["uri", uri])
    out = capsys.readouterr()
    assert out.out.strip() == uri          # unchanged: it round-trips
    assert str(root) in out.err            # ...and this is where it landed


def test_resolving_an_unknown_uri_exits_with_advice(tmp_path, capsys):
    _archive(tmp_path / "postdoc", name="postdoc")
    with pytest.raises(SystemExit) as exc:
        main(["uri", "nebula://nobody@nowhere.edu/postdoc"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "nebula archives" in err and "nebula register" in err


def test_show_accepts_a_uri_that_names_its_session(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc", name="postdoc")
    s = _session(root)
    main(["show", f"nebula://g@ncsu.edu/postdoc/{s.id}"])
    out = capsys.readouterr().out
    assert s.id in out and "raw.csv" in out


def test_show_by_uri_still_takes_an_explicit_run_id(tmp_path, capsys):
    """An explicit id wins, so the command does what was typed rather than
    silently preferring one half of a contradiction."""
    root = _archive(tmp_path / "postdoc", name="postdoc")
    first = _session(root)
    second = _session(root, filename="other.csv")
    main(["show", f"nebula://g@ncsu.edu/postdoc/{first.id}", second.id])
    assert "other.csv" in capsys.readouterr().out


def test_show_with_neither_a_run_id_nor_one_in_the_uri(tmp_path, capsys):
    _archive(tmp_path / "postdoc", name="postdoc")
    with pytest.raises(SystemExit) as exc:
        main(["show", "nebula://g@ncsu.edu/postdoc"])
    assert exc.value.code == 1
    assert "no session given" in capsys.readouterr().err


def test_ls_accepts_a_uri(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc", name="postdoc")
    s = _session(root)
    main(["ls", "nebula://g@ncsu.edu/postdoc"])
    assert s.id in capsys.readouterr().out


def test_a_uri_picks_the_right_owners_archive(tmp_path, capsys):
    """Both are called "shared" and both are registered here; only the owner
    tells them apart."""
    mine = _archive(tmp_path / "mine", name="shared", user="me@here.edu")
    theirs = _archive(tmp_path / "theirs", name="shared", user="jane@lab.edu")
    ours = _session(mine, filename="mine.csv")
    hers = _session(theirs, filename="hers.csv")

    main(["ls", "nebula://jane@lab.edu/shared"])
    out = capsys.readouterr().out
    assert hers.id in out
    main(["show", f"nebula://jane@lab.edu/shared/{hers.id}"])
    assert "hers.csv" in capsys.readouterr().out


# ---------------------------------------------------------------------
# display
# ---------------------------------------------------------------------

def test_show_prints_a_refs_owner(tmp_path, capsys):
    """A provenance dump has to distinguish "somebody else's postdoc" from
    "mine", which it cannot do if the owner is dropped on the way in or on
    the way out."""
    root = _archive(tmp_path / "postdoc", name="postdoc")
    s = nebula.new(root, description="cross-user", announce=False)
    with s.artifact("fit.png", derived_from=[
            "nebula://jane@lab.edu/shared~1a2/S-26-0002/cal.json"]) as fn:
        fn.write_text("z")
    s.close()

    main(["show", "postdoc", s.id])
    out = capsys.readouterr().out
    # A real ref in the current grammar, id and all: copyable straight back
    # into a derived_from.
    assert "nebula://jane@lab.edu/shared~1a2/S-26-0002/cal.json" in out


def test_an_explicit_run_id_overrides_the_one_in_the_uri(tmp_path, capsys):
    """A URI plus arguments does what was typed, not a mixture: the file
    from the URI is dropped along with the session it belonged to."""
    root = _archive(tmp_path / "postdoc", name="postdoc")
    first = _session(root)
    second = _session(root, filename="other.csv")

    main(["uri", f"nebula://g@ncsu.edu/{_seg(root)}/{first.id}/raw.csv",
          second.id])
    assert capsys.readouterr().out.strip() == \
        f"nebula://g@ncsu.edu/{_seg(root)}/{second.id}"


def test_show_prints_an_implicit_ref_readably(tmp_path, capsys):
    """The parts a ref deliberately left implicit are named rather than
    elided -- what was unsaid is what a person reading a provenance dump
    needs to know."""
    root = _archive(tmp_path / "postdoc", name="postdoc")
    a = nebula.new(root, description="raw", announce=False)
    with a.artifact("raw.csv") as fn:
        fn.write_text("x")
    a.close()
    b = nebula.new(root, description="fit", announce=False)
    with b.artifact("fit.png", derived_from=["nebula://jane@lab.edu/shared~1a2"]) as fn:
        fn.write_text("z")
    b.close()

    main(["show", "postdoc", b.id])
    out = capsys.readouterr().out
    assert "(same session)" in out and "(whole session)" in out


def test_renaming_an_archive_keeps_its_uris_resolving(tmp_path, capsys):
    """End to end, and the reason ids exist at all."""
    root = _archive(tmp_path / "postdoc", name="postdoc")
    s = _session(root)
    main(["uri", "postdoc", s.id, "raw.csv"])
    cited = capsys.readouterr().out.strip()

    main(["config", "postdoc", "--name", "thesis"])
    capsys.readouterr()
    get_registry().register_archive(root)

    main(["uri", cited])
    out = capsys.readouterr()
    # Resolves, and comes back wearing the new label -- a slug redirect.
    assert out.out.strip().startswith("nebula://g@ncsu.edu/thesis~")
    assert str(root) in out.err


def test_a_name_that_could_never_appear_in_a_uri_is_refused(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc", name="postdoc")
    with pytest.raises(SystemExit):
        main(["config", "postdoc", "--name", "post~doc"])
    assert "~" in capsys.readouterr().err


# ---------------------------------------------------------------------
# the Navigator's URL handler
# ---------------------------------------------------------------------

def test_resolve_uri_finds_a_file(tmp_path):
    from nebula.navigator import model

    root = _archive(tmp_path / "postdoc", name="postdoc")
    s = _session(root)
    got = model.resolve_uri(f"nebula://g@ncsu.edu/{_seg(root)}/{s.id}/raw.csv")
    assert got["ok"] and got["error"] is None
    assert got["kind"] == "file"
    assert got["run_id"] == s.id and got["filename"] == "raw.csv"
    assert got["archive"] == "postdoc"            # the registered name, for the switcher
    assert got["archive_root"] == str(root)
    assert got["exists"] and got["path"].endswith("raw.csv")


def test_resolve_uri_of_a_session_and_an_archive(tmp_path):
    from nebula.navigator import model

    root = _archive(tmp_path / "postdoc", name="postdoc")
    s = _session(root)
    session = model.resolve_uri(f"nebula://g@ncsu.edu/{_seg(root)}/{s.id}")
    assert session["kind"] == "session" and session["filename"] is None
    assert session["run_id"] == s.id and session["exists"]

    whole = model.resolve_uri(f"nebula://g@ncsu.edu/{_seg(root)}")
    assert whole["kind"] == "archive" and whole["run_id"] is None
    assert whole["path"] == str(root)


def test_resolve_uri_reports_an_unknown_archive_rather_than_raising(tmp_path):
    """The ordinary case for a link from a colleague. The handler needs a
    sentence to show, not an exception to swallow."""
    from nebula.navigator import model

    got = model.resolve_uri("nebula://someone@else.edu/theirs/S-26-0001/raw.csv")
    assert got["ok"] is False
    assert "registered on this machine" in got["error"]
    assert got["archive_root"] is None


def test_resolve_uri_separates_missing_target_from_missing_archive(tmp_path):
    """A URI naming a file that has since gone still resolves: the archive
    is here, the file is not, and those are different problems."""
    from nebula.navigator import model

    root = _archive(tmp_path / "postdoc", name="postdoc")
    s = _session(root)
    got = model.resolve_uri(f"nebula://g@ncsu.edu/{_seg(root)}/{s.id}/gone.csv")
    assert got["ok"] and got["exists"] is False
    assert got["filename"] == "gone.csv"


def test_resolve_uri_rejects_something_that_is_not_a_uri(tmp_path):
    from nebula.navigator import model

    got = model.resolve_uri("https://example.com/whatever")
    assert got["ok"] is False and got["error"]


def test_resolve_uri_accepts_the_compact_form(tmp_path):
    """The same handler serves a pasted `postdoc|S-.../file` ref, which is
    what people actually put in a derived_from."""
    from nebula.navigator import model

    root = _archive(tmp_path / "postdoc", name="postdoc")
    s = _session(root)
    got = model.resolve_uri(f"postdoc|{s.id}/raw.csv")
    assert got["ok"] and got["run_id"] == s.id and got["filename"] == "raw.csv"
