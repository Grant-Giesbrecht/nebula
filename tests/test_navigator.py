import datetime

import nebula
from nebula.navigator import model
from nebula.sidecar import read_session_yaml, write_session_yaml, SessionMeta


def _session_with(archive, files):
    s = nebula.new(archive, tags=["demo"], description="a session")
    for name, content in files.items():
        with s.artifact(name) as fn:
            fn.write_text(content)
    s.close()
    return s


# ---------------------------------------------------------------------
# model (no GUI)
# ---------------------------------------------------------------------

def test_list_items_pairs(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x", "proc.graf": "y"})
    items = {i.name: i for i in model.list_items(s.path)}
    assert set(items) == {"raw.csv", "proc.graf"}
    assert all(i.status == model.PAIRED for i in items.values())
    assert items["raw.csv"].has_artifact and items["raw.csv"].has_sidecar
    assert items["raw.csv"].source == "script"


def test_list_items_orphan(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    (s.path / "dropped.dat").write_text("hand-dropped")  # no sidecar
    items = {i.name: i for i in model.list_items(s.path)}
    assert items["dropped.dat"].status == model.ORPHAN
    assert items["dropped.dat"].has_artifact and not items["dropped.dat"].has_sidecar


def test_list_items_stray(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    (s.path / "raw.csv").unlink()  # leaves a stray sidecar
    item = {i.name: i for i in model.list_items(s.path)}["raw.csv"]
    assert item.status == model.STRAY
    assert item.has_sidecar and not item.has_artifact


def test_list_items_drift_only_when_verifying(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "original"})
    (s.path / "raw.csv").write_text("tampered")
    # without checksum verification, still looks paired (presence only)
    assert model.list_items(s.path)[0].status == model.PAIRED
    # with verification, drift is detected
    assert model.list_items(s.path, verify_checksums=True)[0].status == model.DRIFTED


def test_list_sessions_counts_problems(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    (s.path / "orphan.dat").write_text("y")
    sessions = model.list_sessions(archive)
    assert len(sessions) == 1
    assert sessions[0].run_id == s.id
    assert sessions[0].n_items == 2
    assert sessions[0].n_problems == 1


def test_list_items_excludes_trash(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    nebula.delete_file(archive, s.id, "raw.csv")  # moves to .trash/
    # nothing left but the trash dir -> no items surfaced
    assert model.list_items(s.path) == []


def test_resolve_prefers_path_for_unregistered(tmp_path):
    root, label = model.resolve(str(tmp_path / "archive"))
    assert root == tmp_path / "archive"


def test_sidecar_display_pretty_json(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    text = model.sidecar_display(s.path / "raw.csv.meta.json")
    assert '"produced_by"' in text
    assert '"source": "script"' in text


def test_sidecar_display_raw_when_unparseable(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    sc = s.path / "raw.csv.meta.json"
    sc.write_text("{not valid json")
    assert model.sidecar_display(sc) == "{not valid json"


def test_importable_sessions_excludes_frozen(tmp_path):
    archive = tmp_path / "archive"
    today = _session_with(archive, {"a.csv": "x"})       # closed today -> appendable
    # hand-place a session closed on a previous day -> frozen
    ym = archive / "2020" / "01"
    ym.mkdir(parents=True)
    d = ym / "S-9999"
    d.mkdir()
    write_session_yaml(d, SessionMeta(
        run_id="S-9999",
        created=datetime.datetime(2020, 1, 1).astimezone().isoformat(),
        status="closed", description="last year"))

    ids = {s.run_id for s in model.importable_sessions(archive)}
    assert today.id in ids
    assert "S-9999" not in ids
