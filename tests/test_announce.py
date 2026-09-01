"""
The save report: what a script prints when it writes an artifact.

On by default, because the two things a measurement script's author needs
afterwards -- where the data went, and what to call it in a paper -- were
otherwise discoverable only by going and looking. The tests here are mostly
about the ways it must *not* get in the way: it is suppressible three ways,
it never raises, and it never claims a URI it could not actually mint.
"""

import pytest

from importlib import import_module

import nebula
from nebula import transfer

# `from nebula import session` would import the session() *context manager*,
# which __init__ re-exports under that name -- not the module.
session_mod = import_module("nebula.session")


@pytest.fixture(autouse=True)
def announcing(monkeypatch):
    """Undo conftest's suite-wide silence. These are the tests that want it."""
    monkeypatch.delenv("NEBULA_ANNOUNCE", raising=False)
    # Said once per process, so a previous test's note would be swallowed.
    monkeypatch.setattr(session_mod, "_announced_no_owner", set())


def _archive(root, *, user="g@ncsu.edu", name="postdoc"):
    transfer.init_archive(root, kind="standard", name=name, user=user)
    return root


def _seg(root):
    """The archive segment a URI should carry: `label~id`, read from disk
    because the id is minted at random -- which is the point of it."""
    from nebula.config import read_settings

    got = read_settings(root, apply_env=False)
    return f"{got.name}~{got.id}" if got.id else got.name


def _save(root, filename="raw.csv", **kwargs):
    s = nebula.new(root, description="warmup sweep", tags=["ruby", "twpa"],
                   **kwargs)
    with s.artifact(filename, inputs={"gain": 10}) as fn:
        fn.write_text("x,y\n1,2\n")
    s.close()
    return s


def test_a_save_reports_the_uri_path_and_context(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc")
    s = _save(root)
    out = capsys.readouterr().out

    assert "saved raw.csv" in out
    assert f"nebula://g@ncsu.edu/{_seg(root)}/{s.id}/raw.csv" in out
    assert str(s.path / "raw.csv") in out
    assert "warmup sweep" in out
    assert "ruby, twpa" in out
    assert "gain=10" in out
    assert "sha256" in out


def test_derived_from_is_reported(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc")
    s = nebula.new(root, description="two files")
    with s.artifact("raw.csv") as fn:
        fn.write_text("x")
    with s.artifact("fit.png", derived_from=["raw.csv"]) as fn:
        fn.write_text("z")
    s.close()
    assert "derived from: raw.csv" in capsys.readouterr().out


def test_write_meta_for_reports_too(tmp_path, capsys):
    """The lower-level escape hatch saves an artifact just as much as
    `artifact()` does, and a report that appeared for only one of them would
    read as "that file was not recorded"."""
    root = _archive(tmp_path / "postdoc")
    s = nebula.new(root, description="manual")
    s.artifact_path("manual.txt").write_text("hi")
    s.write_meta_for("manual.txt")
    s.close()
    assert "saved manual.txt" in capsys.readouterr().out


def test_it_goes_to_stdout(tmp_path, capsys):
    """It is the script's own output about its own work, not a diagnostic."""
    root = _archive(tmp_path / "postdoc")
    _save(root)
    got = capsys.readouterr()
    assert "saved raw.csv" in got.out
    assert "saved raw.csv" not in got.err


# ---------------------------------------------------------------------
# turning it off
# ---------------------------------------------------------------------

def test_silenced_per_session(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc")
    _save(root, announce=False)
    assert capsys.readouterr().out == ""


def test_silenced_per_artifact(tmp_path, capsys):
    """The thousandth file of a sweep is noise; the one result the run is
    about is not."""
    root = _archive(tmp_path / "postdoc")
    s = nebula.new(root, description="a sweep")
    for i in range(3):
        with s.artifact(f"point-{i}.csv", announce=False) as fn:
            fn.write_text("x")
    with s.artifact("summary.csv") as fn:
        fn.write_text("x")
    s.close()

    out = capsys.readouterr().out
    assert "point-0.csv" not in out
    assert "saved summary.csv" in out


def test_a_per_artifact_true_overrides_a_quiet_session(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc")
    s = nebula.new(root, description="quiet", announce=False)
    with s.artifact("loud.csv", announce=True) as fn:
        fn.write_text("x")
    s.close()
    assert "saved loud.csv" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
def test_silenced_by_the_environment(tmp_path, capsys, monkeypatch, value):
    """The escape hatch for a script you cannot edit, so it overrides the
    code rather than the other way round."""
    monkeypatch.setenv("NEBULA_ANNOUNCE", value)
    root = _archive(tmp_path / "postdoc")
    _save(root, announce=True)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("value", ["1", "true", "yes", "", "   "])
def test_other_env_values_leave_it_on(tmp_path, capsys, monkeypatch, value):
    monkeypatch.setenv("NEBULA_ANNOUNCE", value)
    root = _archive(tmp_path / "postdoc")
    _save(root)
    assert "saved raw.csv" in capsys.readouterr().out


def test_session_context_manager_passes_the_flag_through(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc")
    with nebula.session(root, new_session=True, description="d",
                        announce=False) as s:
        with s.artifact("raw.csv") as fn:
            fn.write_text("x")
    assert capsys.readouterr().out == ""


def test_append_to_carries_the_flag(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc")
    first = _save(root, announce=False)
    capsys.readouterr()

    s = nebula.append_to(root, first.id, announce=False)
    with s.artifact("second.csv") as fn:
        fn.write_text("x")
    s.close()
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------
# honesty and safety
# ---------------------------------------------------------------------

def test_without_an_owner_it_shows_a_ref_and_says_why_once(tmp_path, capsys,
                                                           monkeypatch):
    """Never a URI with an invented owner in it -- that is the string
    someone would paste into a paper. And the explanation is said once per
    process, not once per artifact."""
    monkeypatch.delenv("NEBULA_USER", raising=False)
    monkeypatch.setenv("NEBULA_IDENTITY", str(tmp_path / "absent.yaml"))

    root = tmp_path / "scratch"
    s = nebula.new(root, description="no owner anywhere")
    for name in ("a.csv", "b.csv"):
        with s.artifact(name) as fn:
            fn.write_text("x")
    s.close()

    got = capsys.readouterr()
    assert "nebula://" not in got.out
    assert f"ref:        local|{s.id}/a.csv" in got.out
    assert got.err.count("no owner") == 1


def test_a_local_owner_still_gets_its_uri_and_one_caveat(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc", user="grant")
    s = nebula.new(root, description="local name")
    with s.artifact("raw.csv") as fn:
        fn.write_text("x")
    s.close()

    got = capsys.readouterr()
    assert f"uri:        nebula://grant/{_seg(root)}/{s.id}/raw.csv" in got.out
    assert "this machine and nowhere else" in got.err


def test_reporting_never_costs_the_measurement(tmp_path, capsys, monkeypatch):
    """The report runs immediately after data has been written to disk.
    Nothing about printing it is worth turning a completed save into a
    traceback."""
    def boom(*args, **kwargs):
        raise RuntimeError("archive.yaml is on fire")

    monkeypatch.setattr(session_mod, "_artifact_uri", boom)
    root = _archive(tmp_path / "postdoc")
    s = _save(root)
    assert (s.path / "raw.csv").read_text().startswith("x,y")
    from nebula.sidecar import sidecar_path_for
    assert sidecar_path_for(s.path / "raw.csv").is_file()
