"""Archive kinds, and moving data between them: export, merge, adopt."""

import shutil
from pathlib import Path

import pytest

import nebula
from nebula import transfer
from nebula.config import archive_identity, read_settings
from nebula.navigator import model
from nebula.session import ArchiveNotWritable
from nebula.sidecar import read_session_yaml, read_sidecar


@pytest.fixture(autouse=True)
def _identity(monkeypatch, tmp_path):
    monkeypatch.setenv("NEBULA_USER", "grant")
    monkeypatch.setenv("NEBULA_REGISTRY", str(tmp_path / "registry.yaml"))
    monkeypatch.setenv("NEBULA_HOME", str(tmp_path / "home"))
    import nebula.registry as registry_mod
    registry_mod._default_registry = None
    yield
    registry_mod._default_registry = None


def _write_run(archive, description="run", files=(("raw.tome", None),)):
    s = nebula.new(archive, description=description)
    for name, derived in files:
        with s.artifact(name, derived_from=list(derived) if derived else None) as fn:
            fn.write_text(f"{description}:{name}")
    s.close()
    return s


def _chain(archive):
    """raw.tome, then fit.png derived from it, in two sessions."""
    a = _write_run(archive, "raw run", [("raw.tome", None)])
    b = nebula.new(archive, description="fit")
    with b.artifact("fit.png", derived_from=[f"{a.id}/raw.tome"]) as fn:
        fn.write_text("plot")
    b.close()
    return a, b


# ---------------------------------------------------------------------
# Kinds and ids
# ---------------------------------------------------------------------

def test_an_intake_archive_mints_provisional_ids(tmp_path):
    intake = transfer.new_intake(tmp_path, label="rp1a")
    s = _write_run(intake, "sweep")
    assert s.id.startswith("I-")
    assert transfer.__name__          # (import used)
    from nebula.session import is_provisional
    assert is_provisional(s.id) is True


def test_intake_names_are_a_coordinate(tmp_path):
    intake = transfer.new_intake(tmp_path, label="scope2")
    assert intake.name.startswith("intake_")
    assert intake.name.endswith("_scope2")
    # recorded inside the archive too, so renaming the folder cannot lose it
    assert read_settings(intake, apply_env=False).name == intake.name


def test_a_standard_archive_still_mints_s_ids(tmp_path):
    archive = transfer.init_archive(tmp_path / "arc")
    assert _write_run(archive, "one").id.startswith("S-")


def test_an_unknown_kind_is_refused_rather_than_assumed(tmp_path):
    from nebula.config import ConfigError, config_path

    archive = transfer.init_archive(tmp_path / "arc")
    config_path(archive).write_text("kind: sortof\n")
    with pytest.raises(ConfigError):
        read_settings(archive, apply_env=False)


def test_a_fragment_cannot_be_written_to(tmp_path):
    archive = transfer.init_archive(tmp_path / "arc")
    _write_run(archive, "one")
    frag = tmp_path / "frag"
    transfer.export(archive, frag, sessions=["S-26-0001"])
    with pytest.raises(ArchiveNotWritable):
        nebula.new(frag, description="not mine to add to")


# ---------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------

def test_merge_renames_sessions_and_records_where_they_came_from(tmp_path):
    dest = transfer.init_archive(tmp_path / "postdoc")
    intake = transfer.new_intake(tmp_path)
    _write_run(intake, "sweep 1")
    _write_run(intake, "sweep 2")

    plan = transfer.plan_merge(intake, dest)
    assert plan.to_dict()["renames"] == {"I-26-0001": "S-26-0001",
                                         "I-26-0002": "S-26-0002"}
    transfer.merge(intake, dest)

    landed = sorted(p.name for p in (dest / "data").rglob("S-*") if p.is_dir())
    assert landed == ["S-26-0001", "S-26-0002"]
    meta = read_session_yaml(dest / "data" / "2026" / "S-26-0001")
    assert meta.run_id == "S-26-0001"
    note = [h["note"] for h in meta.history if h["action"] == "merged"][0]
    # the coordinate written in a notebook still resolves
    assert intake.name in note and "I-26-0001" in note


def test_merge_rewrites_cross_session_references(tmp_path):
    dest = transfer.init_archive(tmp_path / "postdoc")
    intake = transfer.new_intake(tmp_path)
    a, b = _chain(intake)
    transfer.merge(intake, dest)

    landed = dest / "data" / "2026" / "S-26-0002"
    refs = [r.get("session") for r in read_sidecar(landed / "fit.png").derived_from]
    assert refs == ["S-26-0001"]        # not I-26-0001
    tree = model.provenance_tree(dest, "S-26-0002", "fit.png", direction="up")
    up = tree["branches"][0]["upstream"]
    assert [(n["run_id"], n["exists"]) for n in up] == [("S-26-0001", True)]


def test_merge_collapses_refs_that_pointed_back_at_the_destination(tmp_path):
    """An intake on a lab machine may reference the main archive. Once
    merged, that is a local ref, not a cross-archive one."""
    dest = transfer.init_archive(tmp_path / "postdoc", name="postdoc")
    _write_run(dest, "calibration", [("cal.json", None)])
    intake = transfer.new_intake(tmp_path)
    s = nebula.new(intake, description="uses the cal")
    with s.artifact("sweep.tome", derived_from=["postdoc|S-26-0001/cal.json"]) as fn:
        fn.write_text("data")
    s.close()
    transfer.merge(intake, dest)

    landed = dest / "data" / "2026" / "S-26-0002"
    ref = read_sidecar(landed / "sweep.tome").derived_from[0]
    assert ref["archive"] is None and ref["session"] == "S-26-0001"


def test_merge_is_idempotent(tmp_path):
    dest = transfer.init_archive(tmp_path / "postdoc")
    intake = transfer.new_intake(tmp_path)
    _write_run(intake, "one")
    transfer.merge(intake, dest)

    again = transfer.plan_merge(intake, dest)
    assert again.sessions == []
    assert "already merged" in again.skipped[0]["note"]
    assert len(list((dest / "data").rglob("S-*"))) == 1


def test_a_merged_intake_locks_itself(tmp_path):
    """Scenario: write, merge, forget to prune, keep writing, merge again --
    the second merge would carry data the user thinks is already safe."""
    dest = transfer.init_archive(tmp_path / "postdoc")
    intake = transfer.new_intake(tmp_path)
    _write_run(intake, "one")
    transfer.merge(intake, dest)

    with pytest.raises(ArchiveNotWritable):
        nebula.new(intake, description="after the merge")

    transfer.unlock(intake)
    _write_run(intake, "deliberately more")     # allowed once unlocked
    assert transfer.plan_merge(intake, dest).sessions[0].run_id == "I-26-0002"


def test_merge_skips_a_session_that_is_still_open(tmp_path):
    dest = transfer.init_archive(tmp_path / "postdoc")
    intake = transfer.new_intake(tmp_path)
    s = nebula.new(intake, description="still writing")
    with s.artifact("partial.tome") as fn:
        fn.write_text("half")
    # deliberately not closed

    plan = transfer.plan_merge(intake, dest)
    assert plan.sessions == []
    assert "still open" in plan.skipped[0]["note"]


def test_merge_refuses_a_sealed_year(tmp_path):
    from nebula import index

    dest = transfer.init_archive(tmp_path / "postdoc")
    _write_run(dest, "existing")
    index.rebuild(dest)
    index.seal_year(dest, "2026", force=True)

    intake = transfer.new_intake(tmp_path)
    _write_run(intake, "new work")
    plan = transfer.plan_merge(intake, dest)
    assert any("sealed" in w for w in plan.warnings)
    with pytest.raises(transfer.TransferError):
        transfer.merge(intake, dest)


def test_merge_refuses_a_fragment_source(tmp_path):
    archive = transfer.init_archive(tmp_path / "postdoc")
    _write_run(archive, "one")
    frag = tmp_path / "frag"
    transfer.export(archive, frag, sessions=["S-26-0001"])
    other = transfer.init_archive(tmp_path / "mine")
    with pytest.raises(transfer.TransferError, match="fragment"):
        transfer.plan_merge(frag, other)


def test_prune_verifies_rather_than_trusting_the_lock(tmp_path):
    dest = transfer.init_archive(tmp_path / "postdoc")
    intake = transfer.new_intake(tmp_path)
    _write_run(intake, "one")
    transfer.merge(intake, dest)
    transfer.unlock(intake)
    _write_run(intake, "never merged")

    with pytest.raises(transfer.TransferError, match="not been merged"):
        transfer.prune(intake)
    assert intake.is_dir()

    transfer.merge(intake, dest)
    transfer.prune(intake)
    assert not intake.exists()


# ---------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------

def test_export_keeps_ids_so_a_citation_stays_valid(tmp_path):
    archive = transfer.init_archive(tmp_path / "postdoc", name="postdoc")
    _chain(archive)
    frag = tmp_path / "for-jane"
    transfer.export(archive, frag, sessions=["S-26-0002"])

    assert sorted(p.name for p in (frag / "data").rglob("S-*")) == [
        "S-26-0001", "S-26-0002"]
    ident = archive_identity(frag)
    assert ident["kind"] == "fragment"
    assert ident["name"] == "postdoc" and ident["user"] == "grant"


def test_export_pulls_in_what_the_selection_was_derived_from(tmp_path):
    """A figure without its raw data is a picture, not a reference."""
    archive = transfer.init_archive(tmp_path / "postdoc")
    _chain(archive)
    plan = transfer.plan_export(archive, tmp_path / "frag",
                                refs=["S-26-0002/fit.png"])
    got = {s.run_id: s.files for s in plan.sessions}
    assert got == {"S-26-0001": ["raw.tome"], "S-26-0002": ["fit.png"]}


def test_export_marks_a_partial_session(tmp_path):
    archive = transfer.init_archive(tmp_path / "postdoc")
    s = nebula.new(archive, description="many files")
    for name in ("a.csv", "b.csv", "c.csv"):
        with s.artifact(name) as fn:
            fn.write_text(name)
    s.close()

    plan = transfer.plan_export(archive, tmp_path / "frag", refs=["S-26-0001/a.csv"])
    sp = plan.sessions[0]
    assert sp.partial is True and sp.omitted == 2


def test_export_takes_the_code_store_with_it(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBULA_CAPTURE_CODE", "1")
    archive = transfer.init_archive(tmp_path / "postdoc")
    _write_run(archive, "with code")
    frag = tmp_path / "frag"
    plan = transfer.export(archive, frag, sessions=["S-26-0001"])
    if plan.manifests:              # capture can be a no-op outside a repo
        from nebula import codestore
        for digest in plan.manifests:
            assert codestore.read_manifest(frag, digest) is not None


def test_export_can_be_planned_without_writing_anything(tmp_path):
    archive = transfer.init_archive(tmp_path / "postdoc")
    _write_run(archive, "one")
    dest = tmp_path / "frag"
    plan = transfer.plan_export(archive, dest, sessions=["S-26-0001"])
    assert plan.to_dict()["n_files"] == 1 and plan.bytes > 0
    assert not dest.exists()


def test_export_refuses_an_intake_source(tmp_path):
    intake = transfer.new_intake(tmp_path)
    _write_run(intake, "one")
    with pytest.raises(transfer.TransferError, match="provisional"):
        transfer.plan_export(intake, tmp_path / "frag", sessions=["I-26-0001"])


def test_export_selects_from_a_collection(tmp_path):
    from nebula import collection

    archive = transfer.init_archive(tmp_path / "postdoc")
    _chain(archive)
    collection.create(archive, "paper-2026")
    collection.add(archive, "paper-2026", "S-26-0002/fit.png")

    plan = transfer.plan_export(archive, tmp_path / "frag", collection="paper-2026")
    assert {s.run_id for s in plan.sessions} == {"S-26-0001", "S-26-0002"}


# ---------------------------------------------------------------------
# Adopt
# ---------------------------------------------------------------------

def test_adopt_copies_into_new_local_ids_and_records_the_origin(tmp_path):
    mine = transfer.init_archive(tmp_path / "postdoc", name="postdoc")
    _write_run(mine, "grant's work")
    frag = tmp_path / "for-jane"
    transfer.export(mine, frag, sessions=["S-26-0001"])

    theirs = transfer.init_archive(tmp_path / "jane", name="jane-lab", user="jane")
    _write_run(theirs, "jane's own run")
    plan = transfer.adopt(frag, theirs)
    assert [(s.run_id, s.new_run_id) for s in plan.sessions] == [
        ("S-26-0001", "S-26-0002")]

    meta = read_session_yaml(theirs / "data" / "2026" / "S-26-0002")
    note = [h["note"] for h in meta.history if h["action"] == "adopted"][0]
    assert "nebula://grant/postdoc/S-26-0001" in note


def test_adopt_leaves_the_fragment_untouched(tmp_path):
    mine = transfer.init_archive(tmp_path / "postdoc")
    _write_run(mine, "one")
    frag = tmp_path / "frag"
    transfer.export(mine, frag, sessions=["S-26-0001"])
    before = sorted(p.name for p in (frag / "data").rglob("*") if p.is_file())

    theirs = transfer.init_archive(tmp_path / "jane")
    transfer.adopt(frag, theirs)
    assert sorted(p.name for p in (frag / "data").rglob("*") if p.is_file()) == before
    assert read_session_yaml(frag / "data" / "2026" / "S-26-0001").run_id == "S-26-0001"


def test_adopting_the_same_fragment_twice_is_caught(tmp_path):
    mine = transfer.init_archive(tmp_path / "postdoc")
    _write_run(mine, "one")
    frag = tmp_path / "frag"
    transfer.export(mine, frag, sessions=["S-26-0001"])
    theirs = transfer.init_archive(tmp_path / "jane")

    transfer.adopt(frag, theirs)
    second = transfer.plan_adopt(frag, theirs)
    assert second.sessions == []
    assert "already adopted" in second.skipped[0]["note"]


# ---------------------------------------------------------------------
# Receiving fragments
# ---------------------------------------------------------------------

def test_receive_files_a_fragment_by_owner_and_archive(tmp_path):
    from nebula.registry import fragment_dir

    mine = transfer.init_archive(tmp_path / "postdoc", name="postdoc")
    _write_run(mine, "one")
    frag = tmp_path / "delivery"
    transfer.export(mine, frag, sessions=["S-26-0001"])

    got = transfer.receive(frag)
    dest = Path(got["installed"][0]["dest"])
    assert dest == fragment_dir("grant", "postdoc")     # mirrors the URI
    assert (dest / "data" / "2026" / "S-26-0001").is_dir()


def test_receiving_twice_adds_nothing(tmp_path):
    mine = transfer.init_archive(tmp_path / "postdoc")
    _write_run(mine, "one")
    frag = tmp_path / "delivery"
    transfer.export(mine, frag, sessions=["S-26-0001"])

    transfer.receive(frag)
    again = transfer.receive(frag)
    assert again["added"] == 0 and again["skipped"] == 1
    assert again["conflicts"] == []


def test_receive_never_silently_replaces_differing_content(tmp_path):
    """Two deliveries of the same session that disagree: keep what is
    installed and say so. Someone may already have cited it."""
    mine = transfer.init_archive(tmp_path / "postdoc")
    _write_run(mine, "one")
    frag = tmp_path / "delivery"
    transfer.export(mine, frag, sessions=["S-26-0001"])
    transfer.receive(frag)

    kept = next((Path(transfer.plan_receive(frag)[0]["dest"]) / "data")
                .rglob("raw.tome"))
    original = kept.read_text()
    next((frag / "data").rglob("raw.tome")).write_text("a different version")

    got = transfer.receive(frag)
    assert got["conflicts"] and got["conflicts"][0]["files"] == ["raw.tome"]
    assert kept.read_text() == original

    transfer.receive(frag, overwrite_foreign=True)
    assert kept.read_text() == "a different version"


def test_a_nested_fragment_is_filed_under_its_own_owner(tmp_path):
    """John forwards an excerpt containing some of Jane's data. Jane's data
    belongs under Jane, not under John -- otherwise the same archive
    arriving via two colleagues lands in two places."""
    from nebula.registry import fragment_dir

    jane = transfer.init_archive(tmp_path / "jane-lab", name="lab", user="jane")
    _write_run(jane, "jane's raw", [("raw.tome", None)])
    delivered = tmp_path / "from-jane"
    transfer.export(jane, delivered, sessions=["S-26-0001"])
    transfer.receive(delivered)

    # John derives from Jane's file and exports the result
    john = transfer.init_archive(tmp_path / "john", name="john-arc")
    s = nebula.new(john, description="john's analysis")
    with s.artifact("result.csv", derived_from=["lab|S-26-0001/raw.tome"]) as fn:
        fn.write_text("analysed")
    s.close()

    onward = tmp_path / "from-john"
    plan = transfer.plan_export(john, onward, sessions=["S-26-0001"])
    assert any(sp.foreign for sp in plan.sessions), "Jane's data should be embedded"
    transfer.export(john, onward, plan=plan)

    got = transfer.receive(onward)
    filed = {i["name"]: Path(i["dest"]) for i in got["installed"]}
    assert filed["lab"] == fragment_dir("jane", "lab")
    assert filed["john-arc"] == fragment_dir("grant", "john-arc")


def test_export_can_leave_foreign_data_out_but_says_so(tmp_path):
    jane = transfer.init_archive(tmp_path / "jane-lab", name="lab", user="jane")
    _write_run(jane, "jane's raw", [("raw.tome", None)])
    transfer.receive(transfer.export(jane, tmp_path / "d", sessions=["S-26-0001"])
                     and tmp_path / "d")

    john = transfer.init_archive(tmp_path / "john", name="john-arc")
    s = nebula.new(john, description="analysis")
    with s.artifact("result.csv", derived_from=["lab|S-26-0001/raw.tome"]) as fn:
        fn.write_text("analysed")
    s.close()

    plan = transfer.plan_export(john, tmp_path / "out", sessions=["S-26-0001"],
                                include_foreign=False)
    assert not any(sp.foreign for sp in plan.sessions)
    assert any("another archive" in d["note"] for d in plan.dangling)


# ---------------------------------------------------------------------
# Registry and discovery
# ---------------------------------------------------------------------

def test_an_archive_is_registered_under_the_name_it_declares(tmp_path):
    from nebula.registry import get_registry

    archive = transfer.init_archive(tmp_path / "some-folder", name="postdoc")
    cfg = get_registry().register_archive(archive)
    assert cfg.nickname == "postdoc"        # not "some-folder"


def test_two_archives_with_the_same_name_can_coexist(tmp_path):
    from nebula.registry import get_registry

    mine = transfer.init_archive(tmp_path / "mine", name="postdoc")
    theirs = transfer.init_archive(tmp_path / "theirs", name="postdoc", user="jane")
    reg = get_registry()
    a = reg.register_archive(mine)
    b = reg.register_archive(theirs)
    assert a.nickname == "postdoc" and b.nickname == "jane-postdoc"
    assert reg.find("postdoc", "jane") is not None


def test_scanning_the_home_directory_finds_archives(tmp_path):
    from nebula.registry import get_registry, nebula_home

    home = nebula_home()
    (home).mkdir(parents=True, exist_ok=True)
    transfer.init_archive(home / "postdoc", name="postdoc")
    transfer.init_archive(home / "audio", name="audio")
    (home / "not-an-archive").mkdir()

    found = {cfg.nickname for cfg in get_registry().discover()}
    assert found == {"postdoc", "audio"}


def test_discovery_does_not_override_a_hand_registered_archive(tmp_path):
    from nebula.registry import get_registry, nebula_home

    elsewhere = transfer.init_archive(tmp_path / "on-a-nas", name="postdoc")
    reg = get_registry()
    reg.register("nas-postdoc", elsewhere)

    home = nebula_home()
    home.mkdir(parents=True, exist_ok=True)
    shutil.copytree(elsewhere, home / "postdoc")
    reg.discover()
    assert reg.get("nas-postdoc").root == elsewhere


# ---------------------------------------------------------------------
# What comes with a merge besides the files
# ---------------------------------------------------------------------

def test_merge_carries_collections_and_remaps_their_refs(tmp_path):
    """A collection is a list of references, so it is exactly what breaks
    when ids change: left alone every entry would name a dead id."""
    from nebula import collection

    dest = transfer.init_archive(tmp_path / "postdoc")
    intake = transfer.new_intake(tmp_path)
    _write_run(intake, "one")
    _write_run(intake, "two")
    collection.create(intake, "bench-sweeps")
    collection.add(intake, "bench-sweeps", "I-26-0001/raw.tome")
    collection.add(intake, "bench-sweeps", "I-26-0002/raw.tome")

    transfer.merge(intake, dest)
    got = collection.read(dest, "bench-sweeps")
    assert [e.ref for e in got.entries] == ["S-26-0001/raw.tome", "S-26-0002/raw.tome"]


def test_a_colliding_collection_name_is_not_merged_into(tmp_path):
    """Someone's curated set should not silently gain entries."""
    from nebula import collection

    dest = transfer.init_archive(tmp_path / "postdoc")
    _write_run(dest, "existing")
    collection.create(dest, "favourites")
    collection.add(dest, "favourites", "S-26-0001/raw.tome")

    intake = transfer.new_intake(tmp_path)
    _write_run(intake, "new")
    collection.create(intake, "favourites")
    collection.add(intake, "favourites", "I-26-0001/raw.tome")

    plan = transfer.plan_merge(intake, dest)
    assert plan.collections[0]["renamed"] is True
    transfer.merge(intake, dest)

    assert [e.ref for e in collection.read(dest, "favourites").entries] == [
        "S-26-0001/raw.tome"]                       # untouched
    incoming = [n for n in collection.list_names(dest) if n != "favourites"]
    assert len(incoming) == 1 and intake.name in incoming[0]
    assert [e.ref for e in collection.read(dest, incoming[0]).entries] == [
        "S-26-0002/raw.tome"]


def test_a_nested_collection_reference_is_remapped_too(tmp_path):
    from nebula import collection

    dest = transfer.init_archive(tmp_path / "postdoc")
    collection.create(dest, "parent")               # forces a rename of the incoming
    intake = transfer.new_intake(tmp_path)
    _write_run(intake, "one")
    collection.create(intake, "child")
    collection.add(intake, "child", "I-26-0001/raw.tome")
    collection.create(intake, "parent")
    collection.add(intake, "parent", "collections/child")

    transfer.merge(intake, dest)
    renamed = [n for n in collection.list_names(dest) if n.startswith("parent-")][0]
    assert [e.ref for e in collection.read(dest, renamed).entries] == ["collections/child"]


def test_merge_takes_the_code_store_and_skips_blobs_already_there(tmp_path, monkeypatch):
    """Content addressing does the deduplication: an identical blob is the
    same id, so it is skipped rather than compared or copied twice."""
    from nebula import codestore

    monkeypatch.setenv("NEBULA_CAPTURE_CODE", "1")
    dest = transfer.init_archive(tmp_path / "postdoc")
    intake = transfer.new_intake(tmp_path)
    _write_run(intake, "one")
    _write_run(intake, "two")           # same script -> same manifest

    plan = transfer.plan_merge(intake, dest)
    if not plan.manifests:
        pytest.skip("code capture produced nothing here (not inside a repo)")
    assert len(plan.manifests) == 1, "one script, one manifest -- not one per session"

    got = transfer._copy_code(plan, intake, dest)
    assert got["blobs_copied"] > 0 and got["missing"] == []
    again = transfer._copy_code(plan, intake, dest)
    assert again["blobs_copied"] == 0 and again["blobs_present"] > 0

    for digest in plan.manifests:
        assert codestore.read_manifest(dest, digest) is not None


def test_code_ids_need_no_rewriting(tmp_path, monkeypatch):
    """Unlike a session id, a code id means the same thing in every archive,
    so the sidecar's produced_by.code survives a merge untouched."""
    monkeypatch.setenv("NEBULA_CAPTURE_CODE", "1")
    dest = transfer.init_archive(tmp_path / "postdoc")
    intake = transfer.new_intake(tmp_path)
    _write_run(intake, "one")

    before = read_sidecar(Path(next((intake / "data").rglob("raw.tome")))).produced_by.code
    transfer.merge(intake, dest)
    after = read_sidecar(dest / "data" / "2026" / "S-26-0001" / "raw.tome").produced_by.code
    assert before == after


def test_missing_code_blobs_are_reported_not_hidden(tmp_path, monkeypatch):
    from nebula import codestore

    monkeypatch.setenv("NEBULA_CAPTURE_CODE", "1")
    dest = transfer.init_archive(tmp_path / "postdoc")
    intake = transfer.new_intake(tmp_path)
    _write_run(intake, "one")
    plan = transfer.plan_merge(intake, dest)
    if not plan.manifests:
        pytest.skip("code capture produced nothing here")

    manifest = codestore.read_manifest(intake, plan.manifests[0])
    for blob in set(manifest["files"].values()):
        codestore.blob_path(intake, blob).unlink()

    transfer.merge(intake, dest, plan=plan)
    assert any("missing from" in w for w in plan.warnings)


def test_fragments_inside_an_intake_are_reported_not_absorbed(tmp_path):
    """They belong to their authors, under NEBULA_HOME/fragments."""
    other = transfer.init_archive(tmp_path / "jane-lab", name="lab", user="jane")
    _write_run(other, "jane's")
    intake = transfer.new_intake(tmp_path)
    _write_run(intake, "mine")
    transfer.export(other, intake / "fragments" / "lab", sessions=["S-26-0001"])

    dest = transfer.init_archive(tmp_path / "postdoc")
    plan = transfer.plan_merge(intake, dest)
    assert any("nebula receive" in w for w in plan.warnings)
    transfer.merge(intake, dest)
    # the fragment's session did not sneak in as one of ours
    assert sorted(p.name for p in (dest / "data").rglob("S-*") if p.is_dir()) == ["S-26-0001"]


def test_adopt_carries_collections_too(tmp_path):
    from nebula import collection

    mine = transfer.init_archive(tmp_path / "postdoc", name="postdoc")
    _write_run(mine, "one")
    collection.create(mine, "shared-set")
    collection.add(mine, "shared-set", "S-26-0001/raw.tome")
    frag = tmp_path / "frag"
    transfer.export(mine, frag, sessions=["S-26-0001"])

    theirs = transfer.init_archive(tmp_path / "jane")
    _write_run(theirs, "their own")
    transfer.adopt(frag, theirs)
    got = collection.read(theirs, "shared-set")
    assert [e.ref for e in got.entries] == ["S-26-0002/raw.tome"]


def test_export_ships_the_grouping_but_not_dangling_entries(tmp_path):
    """"These twelve files are the paper" is information the recipient
    cannot reconstruct -- but an entry naming something left out would
    arrive broken."""
    from nebula import collection

    archive = transfer.init_archive(tmp_path / "postdoc")
    _write_run(archive, "one")
    _write_run(archive, "two")
    collection.create(archive, "paper-2026")
    collection.add(archive, "paper-2026", "S-26-0001/raw.tome")
    collection.add(archive, "paper-2026", "S-26-0002/raw.tome")
    collection.create(archive, "unrelated")
    collection.add(archive, "unrelated", "S-26-0002/raw.tome")

    frag = tmp_path / "frag"
    transfer.export(archive, frag, sessions=["S-26-0001"])

    got = collection.read(frag, "paper-2026")
    assert [e.ref for e in got.entries] == ["S-26-0001/raw.tome"]   # the other dropped
    assert collection.read(frag, "unrelated") is None              # nothing left to ship


def test_ids_are_untouched_in_an_exported_collection(tmp_path):
    from nebula import collection

    archive = transfer.init_archive(tmp_path / "postdoc")
    _write_run(archive, "one")
    collection.create(archive, "set")
    collection.add(archive, "set", "S-26-0001/raw.tome")
    frag = tmp_path / "frag"
    transfer.export(archive, frag, sessions=["S-26-0001"])
    assert [e.ref for e in collection.read(frag, "set").entries] == ["S-26-0001/raw.tome"]


def test_transfers_accept_a_literal_path_like_the_gui_sends(tmp_path):
    """Both front ends hand over a path *string*. The library resolver is
    deliberately strict about those (a typo must not create a folder), which
    is right for the write API and wrong here: every argument to a transfer
    names an archive that already exists."""
    archive = transfer.init_archive(tmp_path / "postdoc")
    _write_run(archive, "one")

    plan = transfer.plan_export(str(archive), str(tmp_path / "frag"),
                                sessions=["S-26-0001"])
    assert plan.to_dict()["n_sessions"] == 1

    intake = transfer.new_intake(tmp_path)
    _write_run(intake, "captured")
    assert transfer.plan_merge(str(intake), str(archive)).to_dict()["n_sessions"] == 1

    frag = tmp_path / "frag2"
    transfer.export(str(archive), str(frag), sessions=["S-26-0001"])
    other = transfer.init_archive(tmp_path / "jane")
    assert transfer.plan_adopt(str(frag), str(other)).to_dict()["n_sessions"] == 1


def test_a_registered_name_still_wins_over_a_path(tmp_path):
    from nebula.registry import get_registry

    archive = transfer.init_archive(tmp_path / "elsewhere", name="postdoc")
    _write_run(archive, "one")
    get_registry().register_archive(archive)
    plan = transfer.plan_export("postdoc", str(tmp_path / "frag"),
                                sessions=["S-26-0001"])
    assert Path(plan.source_root) == archive


# ---------------------------------------------------------------------
# Creating archives
# ---------------------------------------------------------------------

def test_init_lays_out_the_whole_skeleton(tmp_path):
    """A fresh archive should look like one before any data exists."""
    root = transfer.init_archive(tmp_path / "postdoc", name="postdoc")
    assert sorted(p.name for p in root.iterdir()) == ["archive.yaml", "code", "data"]


def test_init_records_the_rules_it_was_given(tmp_path):
    from nebula.config import ArchiveSettings

    root = transfer.init_archive(
        tmp_path / "arc", name="arc",
        settings=ArchiveSettings(on_overwrite="cancel", capture_code=False,
                                 auto_index=False, code_max_file_bytes=4096))
    got = read_settings(root, apply_env=False)
    assert got.on_overwrite == "cancel" and got.capture_code is False
    assert got.auto_index is False and got.code_max_file_bytes == 4096
    # ...without letting them override what the kind means
    assert got.kind == "standard" and got.name == "arc"


def test_init_refuses_to_land_on_something_else(tmp_path):
    root = tmp_path / "not-empty"
    root.mkdir()
    (root / "important.csv").write_text("data")
    with pytest.raises(transfer.TransferError, match="not empty"):
        transfer.init_archive(root)
    assert (root / "important.csv").is_file()


def test_an_intake_created_through_the_api_is_still_timestamped(tmp_path):
    from nebula.config import ArchiveSettings

    root = transfer.new_intake(tmp_path, label="scope2",
                               settings=ArchiveSettings(capture_code=False))
    assert root.name.startswith("intake_") and root.name.endswith("_scope2")
    got = read_settings(root, apply_env=False)
    assert got.kind == "intake" and got.capture_code is False
    assert sorted(p.name for p in root.iterdir()) == ["archive.yaml", "code", "data"]


def test_create_archive_op_registers_and_reports(tmp_path):
    from nebula.navigator import api

    res = api.dispatch("create_archive", {
        "root": str(tmp_path / "postdoc"), "name": "postdoc", "kind": "standard",
        "on_overwrite": "overwrite", "capture_code": False, "auto_index": True,
    })
    assert res["ok"] and res["registered"] == "postdoc"
    assert res["identity"]["kind"] == "standard"
    assert read_settings(res["root"], apply_env=False).on_overwrite == "overwrite"


def test_create_archive_op_reports_failure_rather_than_raising(tmp_path):
    from nebula.navigator import api

    transfer.init_archive(tmp_path / "arc", name="arc")
    res = api.dispatch("create_archive", {"root": str(tmp_path / "arc"), "name": "arc"})
    assert res["ok"] is False and "already an archive" in res["error"]


def test_identity_ops_round_trip(tmp_path, monkeypatch):
    from nebula.navigator import api

    monkeypatch.setenv("NEBULA_IDENTITY", str(tmp_path / "identity.yaml"))
    monkeypatch.delenv("NEBULA_USER", raising=False)
    assert api.dispatch("identity", {})["set"] is False

    bad = api.dispatch("set_identity", {"user": "not a name"})
    assert bad["ok"] is False and "spaces" in bad["error"]

    good = api.dispatch("set_identity", {"user": "grant"})
    assert good["ok"] and api.dispatch("identity", {})["user"] == "grant"


def test_receive_fragment_op_can_preview(tmp_path):
    from nebula.navigator import api

    archive = transfer.init_archive(tmp_path / "postdoc", name="postdoc")
    _write_run(archive, "one")
    frag = tmp_path / "frag"
    transfer.export(archive, frag, sessions=["S-26-0001"])

    preview = api.dispatch("receive_fragment", {"source": str(frag), "dry_run": True})
    assert preview["ok"] and preview["plan"][0]["name"] == "postdoc"
    assert preview["plan"][0]["exists"] is False

    done = api.dispatch("receive_fragment", {"source": str(frag)})
    assert done["ok"] and done["result"]["added"] == 1
    # ...and now it is there
    assert api.dispatch("receive_fragment",
                        {"source": str(frag), "dry_run": True})["plan"][0]["exists"] is True


def test_receive_fragment_op_refuses_a_non_fragment(tmp_path):
    from nebula.navigator import api

    archive = transfer.init_archive(tmp_path / "postdoc", name="postdoc")
    res = api.dispatch("receive_fragment", {"source": str(archive), "dry_run": True})
    assert res["ok"] is False and "not a fragment" in res["error"]
