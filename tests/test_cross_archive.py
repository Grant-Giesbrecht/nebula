"""
Provenance traversal across archive boundaries.

Decision (Grant, 2026-08-12): an archive we cannot reach is shown as an
*unresolved node*, not hidden. A truncated chain that looks complete is the
worse lie -- see relational-data-roadmap "Open questions".
"""
import pytest

import nebula
from nebula import transfer
from nebula.navigator import model
from nebula.registry import get_registry


@pytest.fixture
def two(tmp_path):
    # conftest gives each test a fresh registry; nothing to isolate here.
    upstream = tmp_path / "upstream"
    transfer.init_archive(upstream, kind="standard", name="upstream",
                          user="g@ncsu.edu")
    downstream = tmp_path / "downstream"
    transfer.init_archive(downstream, kind="standard", name="downstream",
                          user="g@ncsu.edu")
    return upstream, downstream


def _chain(up_root, down_root):
    """raw.csv in `upstream` <- mid.csv in `upstream` <- fit.png in `downstream`."""
    a = nebula.new(up_root, description="raw")
    with a.artifact("raw.csv") as fn:
        fn.write_text("x")
    a.close()
    b = nebula.new(up_root, description="mid")
    with b.artifact("mid.csv", derived_from=[f"{a.id}/raw.csv"]) as fn:
        fn.write_text("y")
    b.close()
    c = nebula.new(down_root, description="fit")
    with c.artifact("fit.png",
                    derived_from=[f"upstream|{b.id}/mid.csv"]) as fn:
        fn.write_text("z")
    c.close()
    return a, b, c


def _flatten(nodes, out=None):
    out = [] if out is None else out
    for n in nodes:
        out.append(n)
        _flatten(n.get("children") or [], out)
    return out


def test_traversal_continues_into_a_registered_archive(two):
    up_root, down_root = two
    get_registry().register_archive(up_root)
    a, b, c = _chain(up_root, down_root)

    tree = model.provenance_tree(down_root, c.id, "fit.png",
                                 direction="up", depth=5)
    nodes = _flatten(tree["branches"][0]["upstream"])
    refs = {(n["archive"], n["ref"]) for n in nodes}
    assert ("upstream", f"{b.id}/mid.csv") in refs
    # The point: the walk did not stop at the boundary.
    assert ("upstream", f"{a.id}/raw.csv") in refs
    assert "upstream" in tree["archives"]


def test_an_unregistered_archive_is_shown_not_hidden(two):
    up_root, down_root = two          # deliberately not registered
    a, b, c = _chain(up_root, down_root)

    tree = model.provenance_tree(down_root, c.id, "fit.png",
                                 direction="up", depth=5)
    nodes = _flatten(tree["branches"][0]["upstream"])
    assert len(nodes) == 1
    node = nodes[0]
    assert node["resolved"] is False
    assert node["archive"] == "upstream"
    assert "not registered" in node["note"]


def test_an_unreachable_node_says_the_chain_continues(two):
    """It must not read as the end of the story."""
    up_root, down_root = two
    a, b, c = _chain(up_root, down_root)
    tree = model.provenance_tree(down_root, c.id, "fit.png",
                                 direction="up", depth=5)
    assert _flatten(tree["branches"][0]["upstream"])[0]["truncated"] is True


def test_an_unmounted_archive_says_so(two, tmp_path):
    up_root, down_root = two
    get_registry().register_archive(up_root)
    a, b, c = _chain(up_root, down_root)
    import shutil
    shutil.rmtree(up_root)

    tree = model.provenance_tree(down_root, c.id, "fit.png",
                                 direction="up", depth=5)
    node = _flatten(tree["branches"][0]["upstream"])[0]
    assert node["resolved"] is False
    assert "not mounted" in node["note"]


def test_same_session_id_in_two_archives_is_not_merged(two):
    """Both archives mint S-26-0001. Keyed on (run_id, filename) alone the
    walk would treat them as one node and drop half the chain."""
    up_root, down_root = two
    get_registry().register_archive(up_root)
    a, b, c = _chain(up_root, down_root)
    assert a.id == c.id            # the collision this guards against

    tree = model.provenance_tree(down_root, c.id, "fit.png",
                                 direction="up", depth=5)
    nodes = _flatten(tree["branches"][0]["upstream"])
    assert any(n["archive"] == "upstream" and n["ref"] == f"{a.id}/raw.csv"
               for n in nodes)


def test_downstream_stays_within_the_archive(two):
    """Nothing records back-links, so another archive's dependents are not
    discoverable -- the tree must not imply otherwise."""
    up_root, down_root = two
    get_registry().register_archive(up_root)
    a, b, c = _chain(up_root, down_root)

    tree = model.provenance_tree(up_root, a.id, "raw.csv",
                                 direction="down", depth=5)
    nodes = _flatten(tree["branches"][0]["downstream"])
    assert nodes                                   # there is a chain to walk
    assert all(n["archive"] == tree["archive"] for n in nodes)


# ---------------------------------------------------------------------
# Owners in the tree
# ---------------------------------------------------------------------
# Same defect as in `graph`, in the GUI's own walk: it resolved a
# cross-archive edge by registry *nickname*, which is machine-local, and
# keyed nodes by archive name alone, which two people can share.

def test_tree_nodes_carry_the_owner(two):
    up_root, down_root = two
    get_registry().register_archive(up_root)
    a, b, c = _chain(up_root, down_root)

    tree = model.provenance_tree(down_root, c.id, "fit.png",
                                 direction="up", depth=5)
    nodes = _flatten(tree["branches"][0]["upstream"])
    assert nodes and all(n["user"] == "g@ncsu.edu" for n in nodes)
    assert tree["branches"][0]["item"]["user"] == "g@ncsu.edu"


def test_tree_follows_an_edge_into_someone_elses_archive(tmp_path):
    """The URI names jane's archive; only her copy should be opened, even
    though one of the same name is registered here."""
    mine = tmp_path / "mine"
    transfer.init_archive(mine, kind="standard", name="shared",
                          user="me@here.edu")
    theirs = tmp_path / "theirs"
    transfer.init_archive(theirs, kind="standard", name="shared",
                          user="jane@lab.edu")
    local = tmp_path / "local"
    transfer.init_archive(local, kind="standard", name="local",
                          user="me@here.edu")
    get_registry().register_archive(mine)
    get_registry().register_archive(theirs)

    src = nebula.new(theirs, description="raw", announce=False)
    with src.artifact("raw.csv") as fn:
        fn.write_text("x")
    src.close()

    fit = nebula.new(local, description="fit", announce=False)
    with fit.artifact(
            "fit.png",
            derived_from=[f"nebula://jane@lab.edu/shared/{src.id}/raw.csv"]) as fn:
        fn.write_text("z")
    fit.close()

    tree = model.provenance_tree(local, fit.id, "fit.png",
                                 direction="up", depth=5)
    node = _flatten(tree["branches"][0]["upstream"])[0]
    assert node["user"] == "jane@lab.edu"
    assert node["resolved"] is True
    assert node["root"] == str(theirs)


def test_a_foreign_archive_that_is_not_here_names_its_owner(tmp_path):
    """"Not registered" is more useful when it says whose."""
    local = tmp_path / "local"
    transfer.init_archive(local, kind="standard", name="local",
                          user="me@here.edu")
    fit = nebula.new(local, description="fit", announce=False)
    with fit.artifact(
            "fit.png",
            derived_from=["nebula://jane@lab.edu/shared/S-26-0001/raw.csv"]) as fn:
        fn.write_text("z")
    fit.close()

    tree = model.provenance_tree(local, fit.id, "fit.png",
                                 direction="up", depth=5)
    node = _flatten(tree["branches"][0]["upstream"])[0]
    assert node["resolved"] is False
    assert node["user"] == "jane@lab.edu"
    assert "jane@lab.edu" in node["note"] and "not registered" in node["note"]
