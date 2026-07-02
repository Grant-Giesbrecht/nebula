import json
import subprocess
from pathlib import Path

import pytest

import nebula
from nebula import index
from nebula.sidecar import read_sidecar, read_session_yaml


def _init_fake_repo(repo_dir: Path) -> Path:
    """Create a minimal git repo with one committed script, for
    provenance-capture tests."""
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    script = repo_dir / "acquire.py"
    script.write_text("# fake acquisition script\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)
    return script


def test_new_session_creates_folder_and_yaml(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, tags=["RP23D"], description="test session")
    assert s.id.startswith("S-")
    assert s.path.exists()
    assert (s.path / "session.yaml").exists()

    meta = read_session_yaml(s.path)
    assert meta.run_id == s.id
    assert meta.status == "open"
    assert meta.tags == ["RP23D"]
    s.close()

    meta2 = read_session_yaml(s.path)
    assert meta2.status == "closed"


def test_session_ids_increment(tmp_path):
    archive = tmp_path / "archive"
    s1 = nebula.new(archive, description="first")
    s1.close()
    s2 = nebula.new(archive, description="second")
    s2.close()
    assert s1.id == "S-0001"
    assert s2.id == "S-0002"


def test_session_by_registered_name(tmp_path):
    from nebula.registry import Registry, resolve_archive

    archive_root = tmp_path / "actual-data"
    reg = Registry(path=tmp_path / "registry.yaml")
    reg.register("postdoc", archive_root)

    root, name = resolve_archive("postdoc", registry=reg)
    assert root == archive_root
    assert name == "postdoc"


def test_new_delegates_to_registry_via_env_override(tmp_path, monkeypatch):
    import nebula.registry as registry_mod

    archive_root = tmp_path / "actual-data"
    registry_path = tmp_path / "registry.yaml"
    from nebula.registry import Registry

    Registry(path=registry_path).register("postdoc", archive_root)

    monkeypatch.setenv("NEBULA_REGISTRY", str(registry_path))
    registry_mod._default_registry = None  # force reload with the new env var

    s = nebula.new("postdoc", description="via registered name")
    assert s.archive == "postdoc"
    assert s.path.is_relative_to(archive_root)
    s.close()

    registry_mod._default_registry = None  # don't leak state to other tests


def test_unknown_registered_name_raises(tmp_path):
    with pytest.raises(KeyError):
        nebula.new("definitely-not-a-registered-archive", description="oops")


def test_path_argument_defaults_archive_label_to_local(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="path based")
    assert s.archive == "local"
    s.close()


def test_archive_name_override(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="custom label", archive_name="scratch")
    assert s.archive == "scratch"
    s.close()


def test_context_manager_closes_on_success(tmp_path):
    archive = tmp_path / "archive"
    with nebula.session(archive, description="ctx test") as s:
        run_id = s.id
    meta = read_session_yaml(archive / str(_year_month(archive, run_id)) / run_id) \
        if False else None  # placeholder, real lookup below
    # find it via glob since we don't want the test to know the path scheme
    found = list(archive.rglob(f"{run_id}/session.yaml"))
    assert len(found) == 1
    meta = read_session_yaml(found[0].parent)
    assert meta.status == "closed"


def test_context_manager_marks_crashed_on_exception(tmp_path):
    archive = tmp_path / "archive"
    run_id = None
    with pytest.raises(ValueError):
        with nebula.session(archive, description="will crash") as s:
            run_id = s.id
            raise ValueError("boom")
    found = list(archive.rglob(f"{run_id}/session.yaml"))
    meta = read_session_yaml(found[0].parent)
    assert meta.status == "crashed"


def test_append_to_open_session_works(tmp_path):
    archive = tmp_path / "archive"
    s1 = nebula.new(archive, description="first step")
    s1.close()
    with pytest.raises(RuntimeError):
        nebula.append_to(archive, s1.id)  # closed -- should refuse


def test_append_to_still_open_session(tmp_path):
    archive = tmp_path / "archive"
    s1 = nebula.new(archive, description="multi-step")
    s2 = nebula.append_to(archive, s1.id)
    assert s2.id == s1.id
    s2.close()


def test_sidecar_written_with_provenance(tmp_path):
    repo_dir = tmp_path / "repo"
    script_path = _init_fake_repo(repo_dir)

    archive = tmp_path / "archive"

    # Simulate the script itself calling into nebula by executing a
    # subprocess that imports nebula and writes a sidecar -- this way
    # capture_provenance's stack inspection sees a real caller file
    # inside the fake repo.
    runner = repo_dir / "run.py"
    runner.write_text(
        f"""
import sys
sys.path.insert(0, {str((Path(__file__).parents[1] / "src")).__repr__()})
import nebula
from pathlib import Path

with nebula.session(Path({str(archive)!r}), description="prov test") as s:
    (s.path / "data.csv").write_text("1,2,3\\n")
    s.write_meta_for("data.csv", inputs={{"gain": 10}})
    print(s.id)
"""
    )
    result = subprocess.run(
        ["python3", str(runner)], cwd=repo_dir, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    run_id = result.stdout.strip().splitlines()[-1]

    session_dir = next(archive.rglob(f"{run_id}"))
    meta = read_sidecar(session_dir / "data.csv")
    assert meta.produced_by.repo == "repo"
    assert meta.produced_by.commit is not None
    # runner.py itself is untracked (created after the initial commit),
    # so git status --porcelain correctly reports the repo as dirty here.
    assert meta.produced_by.dirty is True
    assert meta.inputs == {"gain": 10}


def test_derived_from_round_trips(tmp_path):
    archive = tmp_path / "archive"
    with nebula.session(archive, description="derive test") as s:
        (s.path / "raw.csv").write_text("x")
        s.write_meta_for("raw.csv")
        (s.path / "processed.graf").write_text("y")
        s.write_meta_for("processed.graf", derived_from=["raw.csv"])

    session_dir = next(archive.rglob(f"{s.id}"))
    meta = read_sidecar(session_dir / "processed.graf")
    refs = meta.derived_from_refs()
    assert len(refs) == 1
    assert refs[0].file == "raw.csv"
    assert refs[0].session is None  # same-session ref stays implicit


def test_index_rebuild_and_query(tmp_path):
    archive = tmp_path / "archive"
    with nebula.session(archive, tags=["RP23D"], description="idx test") as s:
        (s.path / "a.csv").write_text("x")
        s.write_meta_for("a.csv")
        (s.path / "b.graf").write_text("y")
        s.write_meta_for("b.graf", derived_from=["a.csv"])

    index.rebuild(archive)
    conn = index.open_index(archive)

    sessions = conn.execute("SELECT * FROM sessions").fetchall()
    assert len(sessions) == 1
    assert sessions[0]["run_id"] == s.id
    assert json.loads(sessions[0]["tags"]) == ["RP23D"]

    artifacts = conn.execute(
        "SELECT * FROM artifacts WHERE run_id = ?", (s.id,)
    ).fetchall()
    assert {a["filename"] for a in artifacts} == {"a.csv", "b.graf"}

    derived = conn.execute(
        "SELECT * FROM derived_from WHERE run_id = ? AND filename = ?",
        (s.id, "b.graf"),
    ).fetchall()
    assert len(derived) == 1
    assert derived[0]["ref_file"] == "a.csv"
    conn.close()


def test_flag_stale_open_sessions(tmp_path):
    import datetime
    from nebula.sidecar import write_session_yaml, SessionMeta

    archive = tmp_path / "archive"
    year_month = archive / "2020" / "01"
    year_month.mkdir(parents=True)
    session_dir = year_month / "S-0001"
    session_dir.mkdir()
    old_meta = SessionMeta(
        run_id="S-0001",
        created=datetime.datetime(2020, 1, 1).astimezone().isoformat(),
        status="open",
        tags=[],
        description="ancient orphaned session",
    )
    write_session_yaml(session_dir, old_meta)

    index.rebuild(archive)
    conn = index.open_index(archive)
    stale = index.flag_stale_open_sessions(conn, older_than_hours=1)
    assert len(stale) == 1
    assert stale[0]["run_id"] == "S-0001"
    conn.close()


def _year_month(archive, run_id):
    # helper retained for readability at call site; unused fallback
    return ""


def test_artifact_context_manager_writes_sidecar(tmp_path):
    archive = tmp_path / "archive"
    with nebula.session(archive, description="artifact ctx") as s:
        with s.artifact("raw.csv", inputs={"gain": 10}) as fn:
            fn.write_text("1,2,3\n")

    session_dir = next(archive.rglob(f"{s.id}"))
    meta = read_sidecar(session_dir / "raw.csv")
    assert meta.inputs == {"gain": 10}
    # No orphan stub marker -- this went through the front door.
    assert meta.extra.get("auto_stub") is None


def test_artifact_derived_from_via_context_manager(tmp_path):
    archive = tmp_path / "archive"
    with nebula.session(archive, description="derive ctx") as s:
        with s.artifact("raw.csv") as fn:
            fn.write_text("x")
        with s.artifact("processed.graf", derived_from=["raw.csv"]) as fn:
            fn.write_text("y")

    session_dir = next(archive.rglob(f"{s.id}"))
    refs = read_sidecar(session_dir / "processed.graf").derived_from_refs()
    assert len(refs) == 1
    assert refs[0].file == "raw.csv"


def test_artifact_raises_if_nothing_written(tmp_path):
    archive = tmp_path / "archive"
    with nebula.session(archive, description="empty artifact") as s:
        with pytest.raises(FileNotFoundError):
            with s.artifact("never_written.csv"):
                pass  # forgot to actually write the file
        s.close()  # avoid the crash marker from the propagating error


def test_artifact_exception_writes_no_sidecar(tmp_path):
    from nebula.sidecar import sidecar_path_for

    archive = tmp_path / "archive"
    with pytest.raises(ValueError):
        with nebula.session(archive, description="mid-write crash") as s:
            with s.artifact("partial.csv") as fn:
                fn.write_text("half")
                raise ValueError("boom")

    session_dir = next(archive.rglob(f"{s.id}"))
    assert not sidecar_path_for(session_dir / "partial.csv").exists()


def test_close_stubs_orphan_artifacts(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="orphan stub", on_missing_meta="stub")
    # Write a file the low-level way and "forget" to document it.
    (s.path / "forgotten.csv").write_text("x")
    s.close()

    session_dir = next(archive.rglob(f"{s.id}"))
    meta = read_sidecar(session_dir / "forgotten.csv")
    assert meta.extra.get("auto_stub") is True


def test_default_policy_stubs_and_warns(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="default policy")
    assert s.on_missing_meta == "stub+warn"
    (s.path / "forgotten.csv").write_text("x")
    with pytest.warns(nebula.MissingMetadataWarning):
        s.close()

    # ...and the stub is still written, so nothing is left un-tracked.
    session_dir = next(archive.rglob(f"{s.id}"))
    meta = read_sidecar(session_dir / "forgotten.csv")
    assert meta.extra.get("auto_stub") is True


def test_warn_only_policy_leaves_orphan(tmp_path):
    from nebula.sidecar import sidecar_path_for

    archive = tmp_path / "archive"
    s = nebula.new(archive, description="warn only", on_missing_meta="warn")
    (s.path / "forgotten.csv").write_text("x")
    with pytest.warns(nebula.MissingMetadataWarning):
        s.close()

    # "warn" surfaces the orphan but does NOT stub it.
    session_dir = next(archive.rglob(f"{s.id}"))
    assert not sidecar_path_for(session_dir / "forgotten.csv").exists()


def test_close_raise_policy_rejects_orphans(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="strict", on_missing_meta="raise")
    (s.path / "forgotten.csv").write_text("x")
    with pytest.raises(nebula.MissingMetadataError):
        s.close()


def test_close_warn_policy_emits_warning(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="warny", on_missing_meta="warn")
    (s.path / "forgotten.csv").write_text("x")
    with pytest.warns(nebula.MissingMetadataWarning):
        s.close()


def test_crashed_session_orphans_not_stubbed(tmp_path):
    from nebula.sidecar import sidecar_path_for

    archive = tmp_path / "archive"
    run_id = None
    with pytest.raises(ValueError):
        with nebula.session(archive, description="crash keeps orphans") as s:
            run_id = s.id
            (s.path / "half.csv").write_text("x")
            raise ValueError("boom")

    session_dir = next(archive.rglob(f"{run_id}"))
    # A crashed session is honest about its holes -- no stub written.
    assert not sidecar_path_for(session_dir / "half.csv").exists()


def test_documented_artifact_not_stubbed_at_close(tmp_path):
    archive = tmp_path / "archive"
    with nebula.session(archive, description="mixed") as s:
        with s.artifact("good.csv", inputs={"n": 1}) as fn:
            fn.write_text("x")

    session_dir = next(archive.rglob(f"{s.id}"))
    meta = read_sidecar(session_dir / "good.csv")
    assert meta.inputs == {"n": 1}
    assert meta.extra.get("auto_stub") is None


def test_invalid_missing_meta_policy_rejected(tmp_path):
    archive = tmp_path / "archive"
    with pytest.raises(ValueError):
        nebula.new(archive, description="bad policy", on_missing_meta="nonsense")


def test_index_rebuild_and_query_by_registered_name(tmp_path, monkeypatch):
    import nebula.registry as registry_mod
    from nebula.registry import Registry
    from nebula import index

    archive_root = tmp_path / "actual-data"
    registry_path = tmp_path / "registry.yaml"
    Registry(path=registry_path).register("postdoc", archive_root)
    monkeypatch.setenv("NEBULA_REGISTRY", str(registry_path))
    registry_mod._default_registry = None

    with nebula.session("postdoc", description="via name") as s:
        (s.path / "a.csv").write_text("x")
        s.write_meta_for("a.csv")

    index.rebuild("postdoc")
    conn = index.open_index("postdoc")
    rows = conn.execute("SELECT run_id FROM sessions").fetchall()
    assert len(rows) == 1
    assert rows[0]["run_id"] == s.id
    conn.close()

    registry_mod._default_registry = None
