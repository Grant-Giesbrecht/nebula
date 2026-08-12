"""
`nebula rename`: renaming an artifact, and what happens to refs.

The design decision behind all of this (Grant, 2026-08-12): a rename must
not silently break a citation nobody can reach. The rename log is what buys
that -- an unrewritten ref still resolves through it. --no-history is the
one way to give that up, and it is opt-in.
"""
import pytest

import nebula
from nebula import manual, transfer
from nebula.check import check
from nebula.registry import get_registry
from nebula.sidecar import read_session_yaml, read_sidecar


@pytest.fixture
def arc(tmp_path):
    root = tmp_path / "arc"
    transfer.init_archive(root, kind="standard", name="arc", user="g@ncsu.edu")
    return root


def _pair(root):
    """a/raw.csv, and b/fit.png deriving from it."""
    a = nebula.new(root, description="raw")
    with a.artifact("raw.csv") as fn:
        fn.write_text("x")
    a.close()
    b = nebula.new(root, description="fit")
    with b.artifact("fit.png", derived_from=[f"{a.id}/raw.csv"]) as fn:
        fn.write_text("y")
    b.close()
    return a, b


def _derived(root, run_id, name):
    from nebula.session import _find_session_dir
    d = _find_session_dir(root, run_id)
    return read_sidecar(d / name).derived_from


# --- the move itself -----------------------------------------------------

def test_artifact_and_sidecar_move_together(arc):
    a, _ = _pair(arc)
    manual.rename_file(arc, a.id, "raw.csv", "sweep.csv")
    from nebula.session import _find_session_dir
    d = _find_session_dir(arc, a.id)
    assert (d / "sweep.csv").is_file()
    assert (d / "sweep.csv.meta.json").is_file()
    assert not (d / "raw.csv").exists()
    assert not (d / "raw.csv.meta.json").exists()


def test_rename_refuses_to_clobber(arc):
    a = nebula.new(arc, description="two files")
    for name in ("one.csv", "two.csv"):
        with a.artifact(name) as fn:
            fn.write_text(name)
    a.close()
    with pytest.raises(FileExistsError):
        manual.rename_file(arc, a.id, "one.csv", "two.csv")


@pytest.mark.parametrize("bad", ["", "sub/dir.csv", ".hidden", "x.meta.json"])
def test_rename_rejects_a_bad_name(arc, bad):
    a, _ = _pair(arc)
    with pytest.raises(ValueError):
        manual.rename_file(arc, a.id, "raw.csv", bad)


# --- refs: local ---------------------------------------------------------

def test_local_refs_are_rewritten_by_default(arc):
    a, b = _pair(arc)
    got = manual.rename_file(arc, a.id, "raw.csv", "sweep.csv")
    assert got["updated"] == 1
    assert _derived(arc, b.id, "fit.png")[0]["file"] == "sweep.csv"


def test_refs_none_leaves_them_alone(arc):
    a, b = _pair(arc)
    got = manual.rename_file(arc, a.id, "raw.csv", "sweep.csv", refs="none")
    assert got["updated"] == 0
    assert _derived(arc, b.id, "fit.png")[0]["file"] == "raw.csv"


def test_an_unrewritten_ref_still_resolves_through_the_log(arc):
    """The whole point: refs=none is safe because the session remembers."""
    a, b = _pair(arc)
    manual.rename_file(arc, a.id, "raw.csv", "sweep.csv", refs="none")
    from nebula.navigator import model
    tree = model.provenance_tree(arc, b.id, "fit.png", direction="up")
    node = tree["branches"][0]["upstream"][0]
    assert node["resolved"] is True

    from nebula.session import _find_session_dir
    assert manual.current_name(_find_session_dir(arc, a.id), "raw.csv") == "sweep.csv"


def test_check_calls_a_renamed_ref_info_not_dangling(arc):
    a, b = _pair(arc)
    manual.rename_file(arc, a.id, "raw.csv", "sweep.csv", refs="none")
    kinds = {i.kind: i for i in check(arc, verify_checksums=False)}
    assert "dangling_derived_from" not in kinds
    assert kinds["renamed_ref"].severity == "info"


def test_chained_renames_still_resolve(arc):
    a, _ = _pair(arc)
    manual.rename_file(arc, a.id, "raw.csv", "mid.csv", refs="none")
    manual.rename_file(arc, a.id, "mid.csv", "final.csv", refs="none")
    from nebula.session import _find_session_dir
    d = _find_session_dir(arc, a.id)
    assert manual.current_name(d, "raw.csv") == "final.csv"


# --- refs: none + no history ---------------------------------------------

def test_no_history_gives_up_resolvability(arc):
    """The immediate-typo case. It is the only irreversible choice here."""
    a, b = _pair(arc)
    manual.rename_file(arc, a.id, "raw.csv", "sweep.csv",
                       refs="none", record_history=False)
    from nebula.session import _find_session_dir
    assert manual.current_name(_find_session_dir(arc, a.id), "raw.csv") is None
    assert "dangling_derived_from" in {i.kind for i in check(arc, verify_checksums=False)}


def test_no_history_still_logs_that_it_happened(arc):
    """Not recording the *rename map* must not mean no audit trail."""
    a, _ = _pair(arc)
    manual.rename_file(arc, a.id, "raw.csv", "sweep.csv", record_history=False)
    from nebula.session import _find_session_dir
    meta = read_session_yaml(_find_session_dir(arc, a.id))
    assert meta.renames == []
    entry = next(h for h in meta.history if h["action"] == "renamed")
    assert entry["resolvable"] is False


# --- refs: all -----------------------------------------------------------

def test_refs_all_reaches_another_registered_archive(tmp_path):
    up = tmp_path / "up"
    transfer.init_archive(up, kind="standard", name="up", user="g@ncsu.edu")
    down = tmp_path / "down"
    transfer.init_archive(down, kind="standard", name="down", user="g@ncsu.edu")
    get_registry().register_archive(up)
    get_registry().register_archive(down)

    a = nebula.new(up, description="raw")
    with a.artifact("raw.csv") as fn:
        fn.write_text("x")
    a.close()
    c = nebula.new(down, description="fit")
    with c.artifact("fit.png", derived_from=[f"up|{a.id}/raw.csv"]) as fn:
        fn.write_text("y")
    c.close()

    local = manual.plan_rename(up, a.id, "raw.csv", "sweep.csv", refs="local")
    assert local["n_foreign"] == 0          # invisible in local mode

    got = manual.rename_file(up, a.id, "raw.csv", "sweep.csv", refs="all")
    assert got["n_foreign"] == 1
    assert got["updated"] == 1
    assert _derived(down, c.id, "fit.png")[0]["file"] == "sweep.csv"


def test_an_unregistered_archive_is_simply_invisible(tmp_path):
    """Honest limit: 'no references found' never means 'safe'."""
    up = tmp_path / "up"
    transfer.init_archive(up, kind="standard", name="up", user="g@ncsu.edu")
    down = tmp_path / "down"
    transfer.init_archive(down, kind="standard", name="down", user="g@ncsu.edu")
    get_registry().register_archive(up)       # down deliberately not registered

    a = nebula.new(up, description="raw")
    with a.artifact("raw.csv") as fn:
        fn.write_text("x")
    a.close()
    c = nebula.new(down, description="fit")
    with c.artifact("fit.png", derived_from=[f"up|{a.id}/raw.csv"]) as fn:
        fn.write_text("y")
    c.close()

    got = manual.rename_file(up, a.id, "raw.csv", "sweep.csv", refs="all")
    assert got["n_foreign"] == 0
    assert _derived(down, c.id, "fit.png")[0]["file"] == "raw.csv"   # now stale


# --- planning ------------------------------------------------------------

def test_plan_changes_nothing(arc):
    a, b = _pair(arc)
    plan = manual.plan_rename(arc, a.id, "raw.csv", "sweep.csv")
    assert plan["n_local"] == 1
    from nebula.session import _find_session_dir
    assert (_find_session_dir(arc, a.id) / "raw.csv").is_file()
    assert _derived(arc, b.id, "fit.png")[0]["file"] == "raw.csv"


def test_same_session_refs_are_found(arc):
    """A bare `raw.csv` ref names the same session and must be rewritten."""
    s = nebula.new(arc, description="both here")
    with s.artifact("raw.csv") as fn:
        fn.write_text("x")
    with s.artifact("fit.png", derived_from=["raw.csv"]) as fn:
        fn.write_text("y")
    s.close()
    got = manual.rename_file(arc, s.id, "raw.csv", "sweep.csv")
    assert got["updated"] == 1
    assert _derived(arc, s.id, "fit.png")[0]["file"] == "sweep.csv"
