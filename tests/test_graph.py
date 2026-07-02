import pytest

import nebula
from nebula import graph, index
from nebula.registry import Registry


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
