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
    store = tmp_path / "store"
    s = nebula.new(store, tags=["RP23D"], description="test session")
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
    store = tmp_path / "store"
    s1 = nebula.new(store, description="first")
    s1.close()
    s2 = nebula.new(store, description="second")
    s2.close()
    assert s1.id == "S-0001"
    assert s2.id == "S-0002"


def test_context_manager_closes_on_success(tmp_path):
    store = tmp_path / "store"
    with nebula.session(store, description="ctx test") as s:
        run_id = s.id
    meta = read_session_yaml(store / str(_year_month(store, run_id)) / run_id) \
        if False else None  # placeholder, real lookup below
    # find it via glob since we don't want the test to know the path scheme
    found = list(store.rglob(f"{run_id}/session.yaml"))
    assert len(found) == 1
    meta = read_session_yaml(found[0].parent)
    assert meta.status == "closed"


def test_context_manager_marks_crashed_on_exception(tmp_path):
    store = tmp_path / "store"
    run_id = None
    with pytest.raises(ValueError):
        with nebula.session(store, description="will crash") as s:
            run_id = s.id
            raise ValueError("boom")
    found = list(store.rglob(f"{run_id}/session.yaml"))
    meta = read_session_yaml(found[0].parent)
    assert meta.status == "crashed"


def test_append_to_open_session_works(tmp_path):
    store = tmp_path / "store"
    s1 = nebula.new(store, description="first step")
    s1.close()
    with pytest.raises(RuntimeError):
        nebula.append_to(store, s1.id)  # closed -- should refuse


def test_append_to_still_open_session(tmp_path):
    store = tmp_path / "store"
    s1 = nebula.new(store, description="multi-step")
    s2 = nebula.append_to(store, s1.id)
    assert s2.id == s1.id
    s2.close()


def test_sidecar_written_with_provenance(tmp_path):
    repo_dir = tmp_path / "repo"
    script_path = _init_fake_repo(repo_dir)

    store = tmp_path / "store"

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

with nebula.session({str(store)!r}, description="prov test") as s:
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

    session_dir = next(store.rglob(f"{run_id}"))
    meta = read_sidecar(session_dir / "data.csv")
    assert meta.produced_by.repo == "repo"
    assert meta.produced_by.commit is not None
    # runner.py itself is untracked (created after the initial commit),
    # so git status --porcelain correctly reports the repo as dirty here.
    assert meta.produced_by.dirty is True
    assert meta.inputs == {"gain": 10}


def test_derived_from_round_trips(tmp_path):
    store = tmp_path / "store"
    with nebula.session(store, description="derive test") as s:
        (s.path / "raw.csv").write_text("x")
        s.write_meta_for("raw.csv")
        (s.path / "processed.graf").write_text("y")
        s.write_meta_for("processed.graf", derived_from=["raw.csv"])

    session_dir = next(store.rglob(f"{s.id}"))
    meta = read_sidecar(session_dir / "processed.graf")
    refs = meta.derived_from_refs()
    assert len(refs) == 1
    assert refs[0].file == "raw.csv"
    assert refs[0].session is None  # same-session ref stays implicit


def test_index_rebuild_and_query(tmp_path):
    store = tmp_path / "store"
    with nebula.session(store, tags=["RP23D"], description="idx test") as s:
        (s.path / "a.csv").write_text("x")
        s.write_meta_for("a.csv")
        (s.path / "b.graf").write_text("y")
        s.write_meta_for("b.graf", derived_from=["a.csv"])

    index.rebuild(store)
    conn = index.open_index(store)

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

    store = tmp_path / "store"
    year_month = store / "2020" / "01"
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

    index.rebuild(store)
    conn = index.open_index(store)
    stale = index.flag_stale_open_sessions(conn, older_than_hours=1)
    assert len(stale) == 1
    assert stale[0]["run_id"] == "S-0001"
    conn.close()


def _year_month(store, run_id):
    # helper retained for readability at call site; unused fallback
    return ""
