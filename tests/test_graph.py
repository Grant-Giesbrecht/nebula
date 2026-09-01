import pytest

import nebula
from nebula import graph, index
from nebula.registry import Registry, get_registry


def _make_chain(archive):
    """S1/raw.csv -> S1/processed.graf -> S1/fit.png, all in one archive."""
    with nebula.session(archive, description="raw acquisition") as s1:
        (s1.path / "raw.csv").write_text("x")
        s1.write_meta_for("raw.csv")
    with nebula.session(archive, description="conversion") as s2:
        (s2.path / "processed.graf").write_text("y")
        s2.write_meta_for("processed.graf", derived_from=[f"{s1.id}/raw.csv"])
    with nebula.session(archive, description="fit") as s3:
        (s3.path / "fit.png").write_text("z")
        s3.write_meta_for("fit.png", derived_from=[f"{s2.id}/processed.graf"])
    index.rebuild(archive)
    return s1, s2, s3


def test_upstream_single_archive_chain(tmp_path):
    archive = tmp_path / "archive"
    s1, s2, s3 = _make_chain(archive)

    nodes = graph.upstream(archive, s3.id, "fit.png", archive_name="local")
    keys = [(n.archive, n.run_id, n.filename) for n in nodes]
    assert ("local", s2.id, "processed.graf") in keys
    assert ("local", s1.id, "raw.csv") in keys
    assert len(keys) == 2  # full chain, no duplicates


def test_downstream_single_archive_chain(tmp_path):
    archive = tmp_path / "archive"
    s1, s2, s3 = _make_chain(archive)

    nodes = graph.downstream(archive, s1.id, "raw.csv", archive_name="local")
    keys = [(n.archive, n.run_id, n.filename) for n in nodes]
    assert ("local", s2.id, "processed.graf") in keys
    assert ("local", s3.id, "fit.png") in keys
    assert len(keys) == 2


def test_upstream_of_root_artifact_is_empty(tmp_path):
    archive = tmp_path / "archive"
    with nebula.session(archive, description="no deps") as s:
        (s.path / "a.csv").write_text("x")
        s.write_meta_for("a.csv")
    index.rebuild(archive)

    nodes = graph.upstream(archive, s.id, "a.csv", archive_name="local")
    assert nodes == []


def test_downstream_of_leaf_artifact_is_empty(tmp_path):
    archive = tmp_path / "archive"
    with nebula.session(archive, description="leaf") as s:
        (s.path / "final.png").write_text("z")
        s.write_meta_for("final.png")
    index.rebuild(archive)

    nodes = graph.downstream(archive, s.id, "final.png", archive_name="local")
    assert nodes == []


def test_cross_archive_upstream(tmp_path):
    postdoc_archive = tmp_path / "postdoc"
    audio_archive = tmp_path / "audio"

    with nebula.session(postdoc_archive, description="postdoc raw", archive_name="postdoc") as sp:
        (sp.path / "diode.graf").write_text("x")
        sp.write_meta_for("diode.graf")
    index.rebuild(postdoc_archive)

    with nebula.session(audio_archive, description="reuse postdoc script", archive_name="audio") as sa:
        (sa.path / "reading.csv").write_text("y")
        sa.write_meta_for("reading.csv", derived_from=[f"postdoc|{sp.id}/diode.graf"])
    index.rebuild(audio_archive)

    reg = Registry(path=tmp_path / "registry.yaml")
    reg.register("postdoc", postdoc_archive)
    reg.register("audio", audio_archive)

    nodes = graph.upstream(
        audio_archive, sa.id, "reading.csv", archive_name="audio", registry=reg
    )
    assert len(nodes) == 1
    assert nodes[0].archive == "postdoc"
    assert nodes[0].run_id == sp.id
    assert nodes[0].filename == "diode.graf"
    assert not nodes[0].unresolved


def test_cross_archive_upstream_unresolved_when_not_registered(tmp_path):
    audio_archive = tmp_path / "audio"
    with nebula.session(audio_archive, description="dangling ref", archive_name="audio") as sa:
        (sa.path / "reading.csv").write_text("y")
        sa.write_meta_for("reading.csv", derived_from=["postdoc|S-9999/diode.graf"])
    index.rebuild(audio_archive)

    empty_registry = Registry(path=tmp_path / "empty_registry.yaml")
    nodes = graph.upstream(
        audio_archive, sa.id, "reading.csv", archive_name="audio", registry=empty_registry
    )
    assert len(nodes) == 1
    assert nodes[0].unresolved


def test_cross_archive_downstream_with_also_search(tmp_path):
    postdoc_archive = tmp_path / "postdoc"
    audio_archive = tmp_path / "audio"

    with nebula.session(postdoc_archive, description="postdoc raw", archive_name="postdoc") as sp:
        (sp.path / "diode.graf").write_text("x")
        sp.write_meta_for("diode.graf")
    index.rebuild(postdoc_archive)

    with nebula.session(audio_archive, description="reuse", archive_name="audio") as sa:
        (sa.path / "reading.csv").write_text("y")
        sa.write_meta_for("reading.csv", derived_from=[f"postdoc|{sp.id}/diode.graf"])
    index.rebuild(audio_archive)

    reg = Registry(path=tmp_path / "registry.yaml")
    reg.register("postdoc", postdoc_archive)
    reg.register("audio", audio_archive)

    nodes = graph.downstream(
        postdoc_archive,
        sp.id,
        "diode.graf",
        archive_name="postdoc",
        registry=reg,
        also_search_archives=["audio"],
    )
    assert len(nodes) == 1
    assert nodes[0].archive == "audio"
    assert nodes[0].run_id == sa.id
    assert nodes[0].filename == "reading.csv"


def test_downstream_without_also_search_misses_other_archive(tmp_path):
    postdoc_archive = tmp_path / "postdoc"
    audio_archive = tmp_path / "audio"

    with nebula.session(postdoc_archive, description="postdoc raw", archive_name="postdoc") as sp:
        (sp.path / "diode.graf").write_text("x")
        sp.write_meta_for("diode.graf")
    index.rebuild(postdoc_archive)

    with nebula.session(audio_archive, description="reuse", archive_name="audio") as sa:
        (sa.path / "reading.csv").write_text("y")
        sa.write_meta_for("reading.csv", derived_from=[f"postdoc|{sp.id}/diode.graf"])
    index.rebuild(audio_archive)

    nodes = graph.downstream(postdoc_archive, sp.id, "diode.graf", archive_name="postdoc")
    assert nodes == []  # correctly finds nothing -- we never told it to look in audio


def test_upstream_by_registered_name(tmp_path, monkeypatch):
    import nebula.registry as registry_mod
    from nebula.registry import Registry

    archive_root = tmp_path / "actual-data"
    registry_path = tmp_path / "registry.yaml"
    Registry(path=registry_path).register("postdoc", archive_root)
    monkeypatch.setenv("NEBULA_REGISTRY", str(registry_path))
    registry_mod._default_registry = None

    with nebula.session("postdoc", description="raw") as s1:
        (s1.path / "raw.csv").write_text("x")
        s1.write_meta_for("raw.csv")
    with nebula.session("postdoc", description="processed") as s2:
        (s2.path / "out.graf").write_text("y")
        s2.write_meta_for("out.graf", derived_from=[f"{s1.id}/raw.csv"])
    index.rebuild("postdoc")

    nodes = graph.upstream("postdoc", s2.id, "out.graf")
    assert len(nodes) == 1
    assert nodes[0].archive == "postdoc"
    assert nodes[0].run_id == s1.id
    assert nodes[0].filename == "raw.csv"

    registry_mod._default_registry = None


# ---------------------------------------------------------------------
# Owners and ids: an archive is (user, id), never a name alone
# ---------------------------------------------------------------------

def _owned(root, name, user):
    from nebula import transfer

    transfer.init_archive(root, kind="standard", name=name, user=user)
    return root


def _seg(root):
    """`label~id` for an archive, read from disk -- the id is random."""
    from nebula.config import read_settings

    got = read_settings(root, apply_env=False)
    return f"{got.name}~{got.id}" if got.id else got.name


def _id_of(root):
    from nebula.config import read_settings

    return read_settings(root, apply_env=False).id or None


def test_nodes_carry_the_archives_owner(tmp_path):
    root = _owned(tmp_path / "postdoc", "postdoc", "g@ncsu.edu")
    s1, s2, s3 = _make_chain(root)

    nodes = graph.upstream(root, s3.id, "fit.png", archive_name="postdoc")
    assert nodes and all(n.user == "g@ncsu.edu" for n in nodes)
    assert nodes[0].uri == (
        f"nebula://g@ncsu.edu/{_seg(root)}/{s2.id}/processed.graf")
    assert all(n.archive_id == _id_of(root) for n in nodes)


def test_an_archive_with_no_declared_owner_claims_none(tmp_path):
    """None, not the local identity: "mine" and "unknown" are different
    claims, and stamping this machine's name onto an archive that never
    said so is how a colleague's data starts looking like yours."""
    archive = tmp_path / "scratch"
    s1, s2, s3 = _make_chain(archive)
    nodes = graph.upstream(archive, s3.id, "fit.png", archive_name="local")
    assert nodes and all(n.user is None for n in nodes)
    assert nodes[0].uri is None


def test_two_owners_one_archive_name_are_not_the_same_node(tmp_path):
    """Both colleagues call their archive "shared"; a ref naming one must
    not resolve to the other."""
    mine = _owned(tmp_path / "mine", "shared", "me@here.edu")
    theirs = _owned(tmp_path / "theirs", "shared", "jane@lab.edu")
    local = _owned(tmp_path / "local", "local", "me@here.edu")
    get_registry().register_archive(mine)
    get_registry().register_archive(theirs)

    for root in (mine, theirs):
        with nebula.session(root, new_session=True, description="raw") as s:
            (s.path / "raw.csv").write_text("x")
            s.write_meta_for("raw.csv")
        index.rebuild(root)
    src = index.open_index(mine).execute(
        "SELECT run_id FROM sessions").fetchone()["run_id"]

    with nebula.session(local, new_session=True, description="fit") as f:
        (f.path / "fit.png").write_text("z")
        f.write_meta_for(
            "fit.png",
            derived_from=[f"nebula://jane@lab.edu/{_seg(theirs)}/{src}/raw.csv"])
    index.rebuild(local)

    nodes = graph.upstream(local, f.id, "fit.png", archive_name="local")
    assert len(nodes) == 1
    assert nodes[0].user == "jane@lab.edu"
    # ...and it resolved to *their* directory, not the same-named one here.
    assert str(theirs) in (nodes[0].path or "")
    assert str(mine) not in (nodes[0].path or "")


def test_an_unregistered_owners_archive_is_unresolved_not_wrong(tmp_path):
    """Better to say the chain continues and we cannot follow it than to
    answer with a same-named archive that happens to be here."""
    mine = _owned(tmp_path / "mine", "shared", "me@here.edu")
    local = _owned(tmp_path / "local", "local", "me@here.edu")
    get_registry().register_archive(mine)

    with nebula.session(local, new_session=True, description="fit") as f:
        (f.path / "fit.png").write_text("z")
        f.write_meta_for(
            "fit.png",
            derived_from=["nebula://jane@lab.edu/shared~999/S-26-0001/raw.csv"])
    index.rebuild(local)

    nodes = graph.upstream(local, f.id, "fit.png", archive_name="local")
    assert len(nodes) == 1
    assert nodes[0].unresolved is True
    assert nodes[0].user == "jane@lab.edu"


def test_downstream_does_not_claim_another_owners_child(tmp_path):
    """A dependent in *my* shared archive is not a dependent of *theirs*,
    even though the archive name and session id match."""
    mine = _owned(tmp_path / "mine", "shared", "me@here.edu")
    get_registry().register_archive(mine)

    with nebula.session(mine, new_session=True, description="raw") as raw:
        (raw.path / "raw.csv").write_text("x")
        raw.write_meta_for("raw.csv")
    with nebula.session(mine, new_session=True, description="fit") as fit:
        (fit.path / "fit.png").write_text("z")
        fit.write_meta_for(
            "fit.png",
            derived_from=[f"nebula://jane@lab.edu/shared~999/{raw.id}/raw.csv"])
    index.rebuild(mine)

    nodes = graph.downstream(mine, raw.id, "raw.csv", archive_name="shared")
    assert nodes == []


def test_a_ref_naming_my_own_owner_still_resolves(tmp_path):
    """Writing your own identity out in full is legal and common -- it is
    what `nebula uri` hands you -- so it must resolve exactly as the bare
    spelling does."""
    mine = _owned(tmp_path / "mine", "shared", "me@here.edu")
    get_registry().register_archive(mine)

    with nebula.session(mine, new_session=True, description="raw") as raw:
        (raw.path / "raw.csv").write_text("x")
        raw.write_meta_for("raw.csv")
    with nebula.session(mine, new_session=True, description="fit") as fit:
        (fit.path / "fit.png").write_text("z")
        fit.write_meta_for(
            "fit.png",
            derived_from=[f"nebula://me@here.edu/{_seg(mine)}/{raw.id}/raw.csv"])
    index.rebuild(mine)

    up = graph.upstream(mine, fit.id, "fit.png", archive_name="shared")
    assert [(n.run_id, n.filename) for n in up] == [(raw.id, "raw.csv")]
    assert up[0].unresolved is False

    down = graph.downstream(mine, raw.id, "raw.csv", archive_name="shared")
    assert [(n.run_id, n.filename) for n in down] == [(fit.id, "fit.png")]


def test_resolution_uses_the_declared_name_not_the_registry_nickname(tmp_path):
    """A nickname is local to one laptop. A ref written by the archive's
    author names what the archive calls itself, and that is what has to
    match."""
    other = _owned(tmp_path / "other", "measurements", "jane@lab.edu")
    local = _owned(tmp_path / "local", "local", "me@here.edu")
    # Filed here under a name nobody else has ever seen.
    get_registry().register_archive(other, key="janes-drive")

    with nebula.session(other, new_session=True, description="raw") as raw:
        (raw.path / "raw.csv").write_text("x")
        raw.write_meta_for("raw.csv")
    index.rebuild(other)

    with nebula.session(local, new_session=True, description="fit") as fit:
        (fit.path / "fit.png").write_text("z")
        fit.write_meta_for(
            "fit.png",
            derived_from=[f"nebula://jane@lab.edu/{_seg(other)}/{raw.id}/raw.csv"])
    index.rebuild(local)

    nodes = graph.upstream(local, fit.id, "fit.png", archive_name="local")
    assert len(nodes) == 1
    assert nodes[0].unresolved is False
    assert str(other) in (nodes[0].path or "")


# ---------------------------------------------------------------------
# Naming a node to a reader, and surviving a rename
# ---------------------------------------------------------------------

def test_describe_is_bare_at_home_and_a_uri_abroad(tmp_path):
    """The "shortest spelling that still says what it means" rule, applied
    to traversal output: a walk that never leaves home would otherwise
    print a fully-qualified owner on every line, burying the handful that
    genuinely cross a boundary."""
    mine = _owned(tmp_path / "mine", "postdoc", "me@here.edu")
    theirs = _owned(tmp_path / "theirs", "shared", "jane@lab.edu")
    get_registry().register_archive(theirs)

    src = nebula.new(theirs, description="raw", announce=False)
    with src.artifact("cal.json") as fn:
        fn.write_text("{}")
    src.close()

    local = nebula.new(mine, description="raw", announce=False)
    with local.artifact("raw.csv") as fn:
        fn.write_text("x")
    local.close()

    fit = nebula.new(mine, description="fit", announce=False)
    with fit.artifact("fit.png", derived_from=[
            f"{local.id}/raw.csv",
            f"nebula://jane@lab.edu/{_seg(theirs)}/{src.id}/cal.json"]) as fn:
        fn.write_text("z")
    fit.close()
    index.rebuild(mine)

    lines = {n.describe(relative_to="me@here.edu", archive_id=_id_of(mine))
             for n in graph.upstream(mine, fit.id, "fit.png",
                                     archive_name="postdoc")}
    # Inside this archive: a bare relative ref, exactly as you would type it.
    assert f"{local.id}/raw.csv" in lines
    # Out of it: a URI naming whose archive, and which one.
    assert f"nebula://jane@lab.edu/{_seg(theirs)}/{src.id}/cal.json" in lines
    # Everything printed is a ref in the current grammar, so it can be
    # pasted straight back into a derived_from.
    from nebula.refs import format_ref, parse_ref
    for line in lines:
        assert format_ref(parse_ref(line)) == line


def test_a_node_names_itself_relative_to_the_reader(tmp_path):
    """`describe` drops whatever the reader already has: their own owner,
    their own archive."""
    node = graph.ArtifactNode(archive="shared", run_id="S-26-0001",
                              filename="cal.json", user="jane@lab.edu",
                              archive_id="c73")

    # A reader somewhere else: everything spelled out.
    assert node.describe() == "nebula://jane@lab.edu/shared~c73/S-26-0001/cal.json"
    assert node.uri == node.describe()

    # Jane, looking at another of her archives: her name is redundant.
    assert node.describe(relative_to="jane@lab.edu") == \
        "nebula://shared~c73/S-26-0001/cal.json"

    # Jane, looking at this archive: so is the archive.
    assert node.describe(relative_to="jane@lab.edu", archive_id="c73") == \
        "S-26-0001/cal.json"
    # ...and the id is a number, so its spelling does not matter.
    assert node.describe(relative_to="jane@lab.edu", archive_id="0c73") == \
        "S-26-0001/cal.json"


def test_an_unresolved_node_says_so(tmp_path):
    node = graph.ArtifactNode(archive="shared", run_id="S-26-0001",
                              filename="cal.json", user="jane@lab.edu",
                              archive_id="c73", unresolved=True)
    assert node.describe().endswith(" (unresolved)")
    assert node.describe(relative_to="jane@lab.edu",
                         archive_id="c73").startswith("S-26-0001/cal.json")


def test_traversal_follows_a_ref_whose_archive_was_renamed(tmp_path):
    """The payoff, at the graph level: a `derived_from` written before a
    rename still finds the archive afterwards, because it named the id."""
    other = _owned(tmp_path / "other", "measurements", "jane@lab.edu")
    local = _owned(tmp_path / "local", "local", "me@here.edu")
    get_registry().register_archive(other)

    raw = nebula.new(other, description="raw", announce=False)
    with raw.artifact("raw.csv") as fn:
        fn.write_text("x")
    raw.close()
    index.rebuild(other)

    fit = nebula.new(local, description="fit", announce=False)
    with fit.artifact("fit.png", derived_from=[
            f"nebula://jane@lab.edu/{_seg(other)}/{raw.id}/raw.csv"]) as fn:
        fn.write_text("z")
    fit.close()
    index.rebuild(local)

    # Jane renames her archive. The ref on disk still says "measurements".
    from nebula.config import read_settings, write_settings
    settings = read_settings(other, apply_env=False)
    settings.name = "phd-data"
    write_settings(other, settings)
    get_registry().register_archive(other)

    nodes = graph.upstream(local, fit.id, "fit.png", archive_name="local")
    assert len(nodes) == 1
    assert nodes[0].unresolved is False
    assert str(other) in (nodes[0].path or "")


def test_two_labels_for_one_archive_are_one_node(tmp_path):
    """Node identity is on the id, so an artifact reached by an old label
    and a new one is one node, not two."""
    a = graph.ArtifactNode(archive="postdoc", run_id="S-26-0001",
                           filename="raw.csv", user="g@x.edu", archive_id="0fe")
    b = graph.ArtifactNode(archive="thesis", run_id="S-26-0001",
                           filename="raw.csv", user="g@x.edu", archive_id="0fe")
    assert a.key() == b.key()

    other = graph.ArtifactNode(archive="postdoc", run_id="S-26-0001",
                               filename="raw.csv", user="g@x.edu",
                               archive_id="1a2")
    assert a.key() != other.key()
