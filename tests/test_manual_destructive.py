import pytest

import nebula
from nebula import check as check_mod
from nebula import manual
from nebula.session import _find_session_dir
from nebula.sidecar import read_sidecar, read_session_yaml, sha256_file


def _session_with_files(archive, files):
    """Create a closed session containing the named files (each documented)."""
    s = nebula.new(archive, description="host")
    for name, content in files.items():
        with s.artifact(name) as fn:
            fn.write_text(content)
    s.close()
    return s.id


def _src(tmp_path, name="new.csv", content="new bytes\n"):
    p = tmp_path / name
    p.write_text(content)
    return p


# ---------------------------------------------------------------------
# delete_file
# ---------------------------------------------------------------------

def test_delete_file_soft_deletes_to_trash(tmp_path):
    archive = tmp_path / "archive"
    run_id = _session_with_files(archive, {"raw.csv": "x"})
    session_dir = _find_session_dir(archive, run_id)

    trashed = manual.delete_file(archive, run_id, "raw.csv", reason="bad cal")

    assert not (session_dir / "raw.csv").exists()
    assert trashed.exists()
    assert trashed.parent == session_dir / ".trash"
    # sidecar went to trash too
    assert list((session_dir / ".trash").glob("*raw.csv.meta.json"))
    hist = read_session_yaml(session_dir).history[-1]
    assert hist["action"] == "delete"
    assert hist["file"] == "raw.csv"
    assert hist["note"] == "bad cal"


def test_delete_file_refuses_when_depended_on(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="host")
    with s.artifact("raw.csv") as fn:
        fn.write_text("x")
    with s.artifact("proc.graf", derived_from=["raw.csv"]) as fn:
        fn.write_text("y")
    s.close()

    with pytest.raises(RuntimeError):
        manual.delete_file(archive, s.id, "raw.csv")
    # still there
    assert (_find_session_dir(archive, s.id) / "raw.csv").exists()


def test_delete_file_force_breaks_link(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="host")
    with s.artifact("raw.csv") as fn:
        fn.write_text("x")
    with s.artifact("proc.graf", derived_from=["raw.csv"]) as fn:
        fn.write_text("y")
    s.close()

    manual.delete_file(archive, s.id, "raw.csv", force=True)
    session_dir = _find_session_dir(archive, s.id)
    assert not (session_dir / "raw.csv").exists()
    # the broken link is recorded
    assert read_session_yaml(session_dir).history[-1]["broke_links"] == [f"{s.id}/proc.graf"]


# ---------------------------------------------------------------------
# replace_file
# ---------------------------------------------------------------------

def test_replace_file_swaps_bytes_and_trashes_old(tmp_path):
    archive = tmp_path / "archive"
    run_id = _session_with_files(archive, {"data.csv": "old\n"})
    session_dir = _find_session_dir(archive, run_id)
    old_sha = read_sidecar(session_dir / "data.csv").sha256

    new = _src(tmp_path, content="brand new\n")
    manual.replace_file(archive, run_id, "data.csv", new, reason="rescan")

    assert (session_dir / "data.csv").read_text() == "brand new\n"
    meta = read_sidecar(session_dir / "data.csv")
    assert meta.sha256 == sha256_file(session_dir / "data.csv")
    assert meta.sha256 != old_sha
    assert meta.produced_by.source == "external"
    assert meta.extra["replaced_sha256"] == old_sha
    # old version preserved in trash
    assert list((session_dir / ".trash").glob("*data.csv"))
    hist = read_session_yaml(session_dir).history[-1]
    assert hist["action"] == "replace"
    assert hist["old_sha256"] == old_sha


def test_replace_preserves_derived_from(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="host")
    with s.artifact("raw.csv") as fn:
        fn.write_text("x")
    with s.artifact("proc.graf", derived_from=["raw.csv"]) as fn:
        fn.write_text("old\n")
    s.close()

    manual.replace_file(archive, s.id, "proc.graf", _src(tmp_path, content="reprocessed\n"))
    refs = read_sidecar(_find_session_dir(archive, s.id) / "proc.graf").derived_from_refs()
    assert [r.file for r in refs] == ["raw.csv"]


# ---------------------------------------------------------------------
# delete_session
# ---------------------------------------------------------------------

def test_delete_session_moves_folder_to_archive_trash(tmp_path):
    archive = tmp_path / "archive"
    run_id = _session_with_files(archive, {"a.csv": "x"})
    session_dir = _find_session_dir(archive, run_id)

    dest = manual.delete_session(archive, run_id, reason="mistake")
    assert not session_dir.exists()
    assert dest.exists()
    assert dest.parent == archive / ".trash"
    # the tombstone rode along in the moved folder's session.yaml
    assert read_session_yaml(dest).history[-1]["action"] == "delete-session"


def test_delete_session_refuses_when_referenced(tmp_path):
    archive = tmp_path / "archive"
    # S-0001 provides raw.csv
    s1 = nebula.new(archive, description="source")
    with s1.artifact("raw.csv") as fn:
        fn.write_text("x")
    s1.close()
    # S-0002 derives from S-0001/raw.csv
    s2 = nebula.new(archive, description="consumer")
    with s2.artifact("out.graf", derived_from=[f"{s1.id}/raw.csv"]) as fn:
        fn.write_text("y")
    s2.close()

    with pytest.raises(RuntimeError):
        manual.delete_session(archive, s1.id)
    manual.delete_session(archive, s1.id, force=True)  # override works
    with pytest.raises(FileNotFoundError):
        _find_session_dir(archive, s1.id)


# ---------------------------------------------------------------------
# check (fsck)
# ---------------------------------------------------------------------

def test_check_clean_archive(tmp_path):
    archive = tmp_path / "archive"
    _session_with_files(archive, {"a.csv": "x"})
    assert check_mod.check(archive) == []


def test_check_finds_orphan(tmp_path):
    archive = tmp_path / "archive"
    run_id = _session_with_files(archive, {"a.csv": "x"})
    (_find_session_dir(archive, run_id) / "dropped.dat").write_text("y")  # by hand
    kinds = {i.kind for i in check_mod.check(archive)}
    assert "orphan" in kinds


def test_check_finds_checksum_mismatch(tmp_path):
    archive = tmp_path / "archive"
    run_id = _session_with_files(archive, {"a.csv": "x"})
    # tamper with the file after its sidecar (with sha256) was written
    (_find_session_dir(archive, run_id) / "a.csv").write_text("TAMPERED")
    issues = check_mod.check(archive)
    assert any(i.kind == "checksum_mismatch" and i.file == "a.csv" for i in issues)


def test_check_finds_missing_artifact(tmp_path):
    archive = tmp_path / "archive"
    run_id = _session_with_files(archive, {"a.csv": "x"})
    (_find_session_dir(archive, run_id) / "a.csv").unlink()  # remove file, keep sidecar
    issues = check_mod.check(archive)
    assert any(i.kind == "missing_artifact" for i in issues)


def test_check_finds_dangling_derived_from(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="host")
    with s.artifact("raw.csv") as fn:
        fn.write_text("x")
    with s.artifact("proc.graf", derived_from=["raw.csv"]) as fn:
        fn.write_text("y")
    s.close()
    # delete the upstream out from under the derived file (force past guard)
    manual.delete_file(archive, s.id, "raw.csv", force=True)
    issues = check_mod.check(archive)
    assert any(i.kind == "dangling_derived_from" for i in issues)


def test_check_finds_missing_session_yaml(tmp_path):
    archive = tmp_path / "archive"
    run_id = _session_with_files(archive, {"a.csv": "x"})
    (_find_session_dir(archive, run_id) / "session.yaml").unlink()
    issues = check_mod.check(archive)
    assert any(i.kind == "missing_session_yaml" for i in issues)


def test_check_finds_unreadable_sidecar(tmp_path):
    archive = tmp_path / "archive"
    run_id = _session_with_files(archive, {"a.csv": "x"})
    (_find_session_dir(archive, run_id) / "a.csv.meta.json").write_text("{not json")
    issues = check_mod.check(archive)
    assert any(i.kind == "unreadable_sidecar" for i in issues)


def test_check_finds_id_mismatch(tmp_path):
    archive = tmp_path / "archive"
    run_id = _session_with_files(archive, {"a.csv": "x"})
    session_dir = _find_session_dir(archive, run_id)
    meta = read_session_yaml(session_dir)
    meta.run_id = "S-9999"  # doesn't match the folder
    from nebula.sidecar import write_session_yaml
    write_session_yaml(session_dir, meta)
    issues = check_mod.check(archive)
    assert any(i.kind == "id_mismatch" for i in issues)


def test_check_finds_invalid_status(tmp_path):
    archive = tmp_path / "archive"
    run_id = _session_with_files(archive, {"a.csv": "x"})
    session_dir = _find_session_dir(archive, run_id)
    meta = read_session_yaml(session_dir)
    meta.status = "banana"
    from nebula.sidecar import write_session_yaml
    write_session_yaml(session_dir, meta)
    issues = check_mod.check(archive)
    assert any(i.kind == "invalid_status" for i in issues)


def test_check_finds_self_reference(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="host")
    with s.artifact("a.csv") as fn:
        fn.write_text("x")
    s.close()
    # hand-edit the sidecar to reference itself
    sc = _find_session_dir(archive, s.id) / "a.csv.meta.json"
    import json
    data = json.loads(sc.read_text())
    data["derived_from"] = [{"archive": None, "session": None, "file": "a.csv"}]
    sc.write_text(json.dumps(data))
    issues = check_mod.check(archive)
    assert any(i.kind == "self_reference" for i in issues)


def test_check_cross_archive_ref_is_info_not_error(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="host")
    with s.artifact("a.csv", derived_from=["other|S-0001/raw.csv"]) as fn:
        fn.write_text("x")
    s.close()
    issues = check_mod.check(archive)
    xrefs = [i for i in issues if i.kind == "unresolved_cross_archive_ref"]
    assert xrefs and xrefs[0].severity == "info"


def test_check_issues_carry_fix_hints(tmp_path):
    archive = tmp_path / "archive"
    run_id = _session_with_files(archive, {"a.csv": "x"})
    (_find_session_dir(archive, run_id) / "dropped.dat").write_text("y")
    orphan = next(i for i in check_mod.check(archive) if i.kind == "orphan")
    assert orphan.fix and "nebula reconcile" in orphan.fix


# ---------------------------------------------------------------------
# reseal (the fix for checksum_mismatch)
# ---------------------------------------------------------------------

def test_reseal_fixes_checksum_mismatch(tmp_path):
    archive = tmp_path / "archive"
    run_id = _session_with_files(archive, {"a.csv": "x"})
    session_dir = _find_session_dir(archive, run_id)
    (session_dir / "a.csv").write_text("intentionally edited")

    assert any(i.kind == "checksum_mismatch" for i in check_mod.check(archive))
    new_sha = manual.reseal(archive, run_id, "a.csv")
    assert new_sha == sha256_file(session_dir / "a.csv")
    assert check_mod.check(archive) == []  # mismatch resolved
    assert read_session_yaml(session_dir).history[-1]["action"] == "reseal"


def test_rm_clears_stray_sidecar(tmp_path):
    archive = tmp_path / "archive"
    run_id = _session_with_files(archive, {"a.csv": "x"})
    session_dir = _find_session_dir(archive, run_id)
    (session_dir / "a.csv").unlink()  # leaves a stray sidecar

    assert any(i.kind == "missing_artifact" for i in check_mod.check(archive))
    manual.delete_file(archive, run_id, "a.csv")  # rm should clear the stray sidecar
    assert not (session_dir / "a.csv.meta.json").exists()
    assert check_mod.check(archive) == []


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def test_cli_rm_replace_check(tmp_path, monkeypatch):
    from nebula import cli

    archive = tmp_path / "archive"
    run_id = _session_with_files(archive, {"data.csv": "old\n"})

    cli.main(["replace", str(archive), run_id, "data.csv",
              str(_src(tmp_path, content="new\n")), "--reason", "rescan"])
    assert (_find_session_dir(archive, run_id) / "data.csv").read_text() == "new\n"

    cli.main(["rm", str(archive), run_id, "data.csv", "--reason", "done"])
    assert not (_find_session_dir(archive, run_id) / "data.csv").exists()

    # clean archive -> check exits 0
    cli.main(["check", str(archive)])


def test_cli_check_nonzero_on_problem(tmp_path):
    from nebula import cli

    archive = tmp_path / "archive"
    run_id = _session_with_files(archive, {"a.csv": "x"})
    (_find_session_dir(archive, run_id) / "orphan.dat").write_text("z")
    with pytest.raises(SystemExit):
        cli.main(["check", str(archive)])
