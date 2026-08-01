"""Nebula URIs, identity, collections and saved searches."""

import subprocess
import sys
from pathlib import Path

import pytest

import nebula
from nebula import check as check_mod, collection as C, identity, views as V
from nebula.refs import Ref, format_ref, format_uri, parse_ref


def _archive(tmp_path, sessions=2):
    archive = tmp_path / "archive"
    made = []
    for i in range(sessions):
        s = nebula.new(archive, description=f"run {i}", tags=["demo"])
        with s.artifact(f"file{i}.tome") as fn:
            fn.write_text("x")
        s.close()
        made.append(s)
    return archive, made


# ---------------------------------------------------------------------
# URIs
# ---------------------------------------------------------------------

@pytest.mark.parametrize("text,kind,fields", [
    ("raw.csv", "file", {"file": "raw.csv"}),
    ("S-26-0152", "session", {"session": "S-26-0152"}),
    ("S-26-0152/raw.csv", "file", {"session": "S-26-0152", "file": "raw.csv"}),
    ("postdoc|S-26-0152/raw.csv", "file",
     {"archive": "postdoc", "session": "S-26-0152", "file": "raw.csv"}),
    ("collections/paper-2026", "collection", {"collection": "paper-2026"}),
    ("nebula://kai@lab/shared/S-26-0002/cal.json", "file",
     {"user": "kai@lab", "archive": "shared", "session": "S-26-0002",
      "file": "cal.json"}),
    ("nebula://kai@lab/shared/S-26-0002", "session",
     {"user": "kai@lab", "archive": "shared", "session": "S-26-0002"}),
    ("nebula://kai@lab/shared", "archive", {"user": "kai@lab", "archive": "shared"}),
    ("nebula://kai@lab/shared/collections/paper", "collection",
     {"user": "kai@lab", "archive": "shared", "collection": "paper"}),
])
def test_parse_ref_forms(text, kind, fields):
    ref = parse_ref(text)
    assert ref.kind == kind
    for key, value in fields.items():
        assert getattr(ref, key) == value, key


@pytest.mark.parametrize("bad", [
    "", "   ", "a|b|c", "nebula://", "nebula://onlyuser",
    "nebula://u/a/S-26-0001/too/many", "nebula://u/a/collections",
    "nebula://u/a/collections/x/y", "S-26-0001/a/b",
])
def test_parse_ref_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_ref(bad)


def test_uri_round_trips():
    text = "nebula://kai@lab/shared/S-26-0002/cal.json"
    assert format_ref(parse_ref(text)) == text


def test_compact_forms_stay_compact():
    """A ref that names no user keeps its short spelling -- URIs are for
    crossing owners, not for everyday same-archive links."""
    for text in ["raw.csv", "S-26-0152", "S-26-0152/raw.csv",
                 "postdoc|S-26-0152/raw.csv", "collections/paper"]:
        assert format_ref(parse_ref(text)) == text


def test_compact_ref_can_be_promoted_to_a_uri():
    ref = parse_ref("S-26-0152/raw.csv")
    assert (format_uri(ref, user="grant@ncsu.edu", archive="postdoc")
            == "nebula://grant@ncsu.edu/postdoc/S-26-0152/raw.csv")


def test_uri_needs_a_user_and_archive():
    with pytest.raises(ValueError):
        format_uri(parse_ref("raw.csv"))


def test_a_filename_with_dots_survives():
    """Why '/' and not '.' as the separator: filenames are full of dots."""
    ref = parse_ref("nebula://u/a/S-26-0001/run.2026-07-31.tar.gz")
    assert ref.file == "run.2026-07-31.tar.gz"


def test_names_may_not_contain_slashes():
    with pytest.raises(ValueError):
        parse_ref("nebula://u/a/S-26-0001/sub/dir.csv")


# ---------------------------------------------------------------------
# URIs are accepted everywhere a ref is
# ---------------------------------------------------------------------

def test_derived_from_accepts_a_full_uri(tmp_path):
    from nebula.sidecar import read_sidecar

    archive = tmp_path / "archive"
    s = nebula.new(archive, description="d")
    uri = "nebula://kai@lab/shared/S-26-0002/cal.json"
    with s.artifact("out.tome", derived_from=[uri]) as fn:
        fn.write_text("x")
    s.close()

    refs = read_sidecar(s.path / "out.tome").derived_from_refs()
    assert [format_ref(r) for r in refs] == [uri]
    assert refs[0].user == "kai@lab" and refs[0].archive == "shared"


def test_related_runs_accepts_a_full_uri(tmp_path):
    from nebula.sidecar import read_session_yaml

    archive = tmp_path / "archive"
    s = nebula.new(archive, description="d")
    s.add_related_run("nebula://kai@lab/shared/S-26-0002")
    s.close()

    refs = read_session_yaml(s.path).related_run_refs()
    assert refs[0].user == "kai@lab" and refs[0].session == "S-26-0002"


# ---------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------

def test_identity_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBULA_IDENTITY", str(tmp_path / "identity.yaml"))
    monkeypatch.delenv("NEBULA_USER", raising=False)
    assert identity.get_user() is None
    identity.set_user("grant@ncsu.edu")
    assert identity.get_user() == "grant@ncsu.edu"


def test_identity_env_overrides_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBULA_IDENTITY", str(tmp_path / "identity.yaml"))
    identity.set_user("from-file")
    monkeypatch.setenv("NEBULA_USER", "from-env")
    assert identity.get_user() == "from-env"


@pytest.mark.parametrize("bad", ["", "   ", "has space", "has/slash", "has|pipe"])
def test_identity_rejects_unusable_names(bad):
    with pytest.raises(identity.IdentityError):
        identity.clean_user(bad)


def test_require_user_explains_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBULA_IDENTITY", str(tmp_path / "none.yaml"))
    monkeypatch.delenv("NEBULA_USER", raising=False)
    with pytest.raises(identity.IdentityError) as e:
        identity.require_user()
    assert "nebula whoami --set" in str(e.value)


def test_registry_finds_an_archive_by_owner(tmp_path, monkeypatch):
    import nebula.registry as reg

    path = tmp_path / "archives.yaml"
    monkeypatch.setenv("NEBULA_REGISTRY", str(path))
    monkeypatch.setattr(reg, "_default_registry", None)
    registry = reg.get_registry()
    registry.register("shared", tmp_path / "shared", user="kai@lab")

    assert registry.find("shared", "kai@lab") is not None
    assert registry.find("shared", "someone-else") is None
    assert registry.find("shared") is not None          # name only: any owner


# ---------------------------------------------------------------------
# collections
# ---------------------------------------------------------------------

def test_collection_is_one_file_each(tmp_path):
    archive, _ = _archive(tmp_path)
    C.create(archive, "paper-2026", title="Figures")
    C.create(archive, "campaign")
    assert sorted(p.name for p in (archive / "collections").glob("*.yaml")) == [
        "campaign.yaml", "paper-2026.yaml"]
    assert C.list_names(archive) == ["campaign", "paper-2026"]


def test_collection_holds_files_sessions_and_collections(tmp_path):
    archive, sessions = _archive(tmp_path)
    C.create(archive, "inner")
    C.create(archive, "outer")
    C.add(archive, "inner", f"{sessions[0].id}/file0.tome", note="the good run")
    C.add(archive, "outer", "collections/inner")
    C.add(archive, "outer", sessions[1].id)
    C.add(archive, "outer", "nebula://kai@lab/shared/S-26-0002/cal.json")

    kinds = [e.kind for e in C.read(archive, "outer").entries]
    assert kinds == ["collection", "session", "file"]


def test_collection_nests_and_resolves(tmp_path):
    archive, sessions = _archive(tmp_path)
    C.create(archive, "inner")
    C.create(archive, "outer")
    C.add(archive, "inner", f"{sessions[0].id}/file0.tome")
    C.add(archive, "outer", "collections/inner")

    tree = C.tree(archive, "outer")
    assert tree["entries"][0]["kind"] == "collection"
    child = tree["entries"][0]["child"]
    assert child["name"] == "inner"
    assert child["entries"][0]["exists"] is True


def test_collection_membership_never_touches_the_member(tmp_path):
    archive, sessions = _archive(tmp_path)
    sidecar = sessions[0].path / "file0.tome.meta.json"
    before = sidecar.read_bytes()
    session_yaml = (sessions[0].path / "session.yaml").read_bytes()

    C.create(archive, "c")
    C.add(archive, "c", f"{sessions[0].id}/file0.tome")

    assert sidecar.read_bytes() == before
    assert (sessions[0].path / "session.yaml").read_bytes() == session_yaml


def test_collection_refuses_to_contain_itself(tmp_path):
    archive, _ = _archive(tmp_path)
    C.create(archive, "c")
    with pytest.raises(C.CollectionError):
        C.add(archive, "c", "collections/c")


def test_collection_refuses_an_indirect_cycle(tmp_path):
    archive, _ = _archive(tmp_path)
    for name in ("a", "b", "c"):
        C.create(archive, name)
    C.add(archive, "a", "collections/b")
    C.add(archive, "b", "collections/c")
    with pytest.raises(C.CollectionError) as e:
        C.add(archive, "c", "collections/a")
    assert "cycle" in str(e.value)


def test_tree_survives_a_cycle_made_by_hand(tmp_path):
    """add() guards against cycles, but the files are hand-editable."""
    archive, _ = _archive(tmp_path)
    C.create(archive, "a")
    C.create(archive, "b")
    C.add(archive, "a", "collections/b")
    C.path_for(archive, "b").write_text(
        "version: 1\nname: b\nentries:\n  - ref: collections/a\n")

    tree = C.tree(archive, "a")
    child = tree["entries"][0]["child"]           # b
    assert child["entries"][0]["child"]["cycle"] is True


def test_collection_rejects_duplicates_and_junk(tmp_path):
    archive, sessions = _archive(tmp_path)
    C.create(archive, "c")
    C.add(archive, "c", f"{sessions[0].id}/file0.tome")
    with pytest.raises(C.CollectionError):
        C.add(archive, "c", f"{sessions[0].id}/file0.tome")
    with pytest.raises(ValueError):
        C.add(archive, "c", "a|b|c")


def test_collection_remove_and_delete(tmp_path):
    archive, sessions = _archive(tmp_path)
    C.create(archive, "c")
    C.add(archive, "c", f"{sessions[0].id}/file0.tome")
    C.remove(archive, "c", f"{sessions[0].id}/file0.tome")
    assert C.read(archive, "c").entries == []
    with pytest.raises(C.CollectionError):
        C.remove(archive, "c", "S-26-0001/nope.csv")

    assert C.delete(archive, "c") is True
    assert C.list_names(archive) == []
    # deleting a collection must not delete what it pointed at
    assert (sessions[0].path / "file0.tome").is_file()


def test_collection_reports_missing_and_unreachable_entries(tmp_path):
    archive, sessions = _archive(tmp_path)
    C.create(archive, "c")
    C.add(archive, "c", f"{sessions[0].id}/gone.csv")
    C.add(archive, "c", "nebula://kai@lab/elsewhere/S-26-0001/x.csv")

    entries = C.tree(archive, "c")["entries"]
    assert entries[0]["exists"] is False and entries[0]["resolved"] is True
    assert entries[1]["resolved"] is False and entries[1]["foreign"] is True
    assert "not registered" in entries[1]["note_error"]


def test_containing_is_the_reverse_lookup(tmp_path):
    archive, sessions = _archive(tmp_path)
    ref = f"{sessions[0].id}/file0.tome"
    C.create(archive, "a")
    C.create(archive, "b")
    C.add(archive, "a", ref)
    C.add(archive, "b", ref)
    assert C.containing(archive, ref) == ["a", "b"]
    assert C.containing(archive, f"{sessions[1].id}/file1.tome") == []


@pytest.mark.parametrize("bad", ["", "  ", "has space", "has/slash", "-leading"])
def test_collection_names_are_validated(bad):
    with pytest.raises(C.CollectionError):
        C.clean_name(bad)


def test_check_reports_dangling_collection_entries(tmp_path):
    archive, sessions = _archive(tmp_path)
    C.create(archive, "c")
    C.add(archive, "c", f"{sessions[0].id}/gone.csv")

    issues = [i for i in check_mod.check(archive)
              if i.kind == "dangling_collection_entry"]
    assert len(issues) == 1
    assert issues[0].severity == "info"      # a pointer list, not corruption


# ---------------------------------------------------------------------
# saved searches
# ---------------------------------------------------------------------

def test_view_round_trip(tmp_path):
    archive, _ = _archive(tmp_path)
    V.save(archive, "drifty", query="demo", title="Demo files",
           fields=["filename", "tags"], date_from="2026-01-01")
    got = V.read(archive, "drifty")
    assert got.query == "demo" and got.fields == ["filename", "tags"]
    assert got.date_from == "2026-01-01" and got.title == "Demo files"
    assert (archive / "saved-searches" / "drifty.yaml").is_file()


def test_view_runs_the_search(tmp_path):
    archive, sessions = _archive(tmp_path)
    V.save(archive, "all-demo", query="demo")
    res = V.run(archive, "all-demo")
    assert {h["item"].name for h in res["items"]} == {"file0.tome", "file1.tome"}
    assert res["view"]["name"] == "all-demo"


def test_view_respects_its_stored_fields(tmp_path):
    archive, sessions = _archive(tmp_path)
    V.save(archive, "names-only", query="demo", fields=["filename"])
    assert V.run(archive, "names-only")["items"] == []     # 'demo' is a tag

    V.save(archive, "tags-too", query="demo", fields=["filename", "tags"])
    assert V.run(archive, "tags-too")["items"]


def test_view_overwrite_keeps_created(tmp_path):
    archive, _ = _archive(tmp_path)
    first = V.save(archive, "v", query="a")
    second = V.save(archive, "v", query="b")
    assert second.query == "b" and second.created == first.created


def test_view_delete_and_missing(tmp_path):
    archive, _ = _archive(tmp_path)
    V.save(archive, "v", query="a")
    assert V.delete(archive, "v") is True
    assert V.list_names(archive) == []
    with pytest.raises(V.ViewError):
        V.run(archive, "v")


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def _nebula(*args):
    return subprocess.run([sys.executable, "-m", "nebula.cli", *args],
                          capture_output=True, text=True)


def test_cli_collection_flow(tmp_path):
    archive, sessions = _archive(tmp_path)
    assert _nebula("collection", str(archive), "new", "paper",
                   "--title", "Paper figures").returncode == 0
    assert _nebula("collection", str(archive), "new", "campaign").returncode == 0
    _nebula("collection", str(archive), "add", "campaign",
            f"{sessions[0].id}/file0.tome", "--note", "the good run")
    _nebula("collection", str(archive), "add", "paper", "collections/campaign")

    shown = _nebula("collection", str(archive), "show", "paper")
    assert shown.returncode == 0, shown.stderr
    assert "[paper]" in shown.stdout and "[campaign]" in shown.stdout
    assert "the good run" in shown.stdout

    listed = _nebula("collection", str(archive), "list").stdout
    assert "campaign" in listed and "paper" in listed


def test_cli_collection_cycle_is_refused(tmp_path):
    archive, _ = _archive(tmp_path)
    _nebula("collection", str(archive), "new", "a")
    _nebula("collection", str(archive), "new", "b")
    _nebula("collection", str(archive), "add", "a", "collections/b")
    res = _nebula("collection", str(archive), "add", "b", "collections/a")
    assert res.returncode != 0 and "cycle" in res.stderr


def test_cli_view_flow(tmp_path):
    archive, _ = _archive(tmp_path)
    assert _nebula("view", str(archive), "save", "demo-files",
                   "--query", "demo").returncode == 0
    assert "demo-files" in _nebula("view", str(archive), "list").stdout
    run = _nebula("view", str(archive), "run", "demo-files")
    assert "2 match(es)" in run.stdout, run.stdout


def test_cli_whoami(tmp_path, monkeypatch):
    env_path = tmp_path / "identity.yaml"
    res = subprocess.run([sys.executable, "-m", "nebula.cli", "whoami",
                          "--set", "grant@ncsu.edu"],
                         capture_output=True, text=True,
                         env={**dict(__import__("os").environ),
                              "NEBULA_IDENTITY": str(env_path),
                              "NEBULA_USER": ""})
    assert res.returncode == 0, res.stderr
    assert "grant@ncsu.edu" in res.stdout
    assert env_path.is_file()


def test_collection_move_between_collections(tmp_path):
    """What a drag-and-drop does: the entry (and its note) changes parent,
    and nothing it points at is touched."""
    archive, sessions = _archive(tmp_path)
    ref = f"{sessions[0].id}/file0.tome"
    C.create(archive, "inbox")
    C.create(archive, "papers")
    C.add(archive, "inbox", ref, note="keep this note")

    C.move(archive, "inbox", "papers", ref)
    assert [e.ref for e in C.read(archive, "inbox").entries] == []
    moved = C.read(archive, "papers").entries[0]
    assert moved.ref == ref and moved.note == "keep this note"
    assert (sessions[0].path / "file0.tome").is_file()


def test_collection_move_a_nested_folder(tmp_path):
    archive, _ = _archive(tmp_path)
    for name in ("a", "b", "sub"):
        C.create(archive, name)
    C.add(archive, "a", "collections/sub")

    C.move(archive, "a", "b", "collections/sub")
    assert [e.ref for e in C.read(archive, "a").entries] == []
    assert [e.ref for e in C.read(archive, "b").entries] == ["collections/sub"]


def test_collection_move_refuses_a_cycle_without_losing_the_entry(tmp_path):
    """The add is attempted first, so a refused move leaves the source
    intact rather than dropping the entry on the floor."""
    archive, _ = _archive(tmp_path)
    C.create(archive, "outer")
    C.create(archive, "inner")
    C.add(archive, "outer", "collections/inner")

    with pytest.raises(C.CollectionError):
        C.move(archive, "outer", "inner", "collections/inner")
    assert [e.ref for e in C.read(archive, "outer").entries] == ["collections/inner"]


def test_collection_move_to_itself_is_a_noop(tmp_path):
    archive, sessions = _archive(tmp_path)
    ref = f"{sessions[0].id}/file0.tome"
    C.create(archive, "c")
    C.add(archive, "c", ref)
    C.move(archive, "c", "c", ref)
    assert [e.ref for e in C.read(archive, "c").entries] == [ref]
