import datetime
import os

import pytest

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


# ---------------------------------------------------------------------
# view smoke test (skipped if PySide6 isn't installed)
# ---------------------------------------------------------------------

def test_view_constructs_headless(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from nebula.navigator import app

    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    (s.path / "orphan.dat").write_text("y")

    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = app.Navigator(archive)
    # sidebar populated, items populated for the (only) session
    assert win.session_list.count() == 1
    assert win.item_model.rowCount() == 2
    assert win.item_model.columnCount() == 3  # Name, Created, Status
    # the icon composer (file icon + status badge) runs for each status
    for st in (model.PAIRED, model.ORPHAN, model.STRAY, model.DRIFTED):
        icon = app.compose_icon(model.Item("f.csv", st, st != model.STRAY,
                                           st != model.ORPHAN))
        assert not icon.isNull()
    win.close()


def test_compose_icon_uses_missing_artefact_for_stray(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from nebula.navigator import app

    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # The four badge/graphic assets must exist where the app looks for them.
    for name in ("sidecar_good.png", "sidecar_warn.png", "sidecar_error.png",
                 "missing_artefact.png"):
        assert (app._ASSET_DIR / name).exists(), name
    # A stray item composes without error and yields a non-null icon.
    stray = model.Item("gone.csv", model.STRAY, has_artifact=False, has_sidecar=True)
    assert not app.compose_icon(stray).isNull()


def test_context_actions_open_paths(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from nebula.navigator import app, osutil

    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})

    opened = []
    monkeypatch.setattr(osutil, "open_path", lambda p: opened.append(str(p)))

    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = app.Navigator(archive)
    session = win.session_list.item(0).data(1 << 8)  # Qt.UserRole == 0x0100
    item = win.item_model.item(0, 0).data(1 << 8)

    win._open_session_folder(session)
    assert opened[-1] == str(session.path)

    win._open_artifact(item)
    assert opened[-1] == str(item.artifact_path)

    win._open_sidecar_editor(item)
    assert opened[-1] == str(item.sidecar_path)
    win.close()


def test_list_view_toggle_and_columns(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from nebula.navigator import app

    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    (s.path / "orphan.dat").write_text("y")

    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = app.Navigator(archive)

    # list view is the default now
    assert win.view_stack.currentIndex() == 1
    win.list_toggle.setChecked(False)
    assert win.view_stack.currentIndex() == 0  # switched to the icon grid

    # both views share the one model, with the three sortable columns
    assert win.list_view.model() is win.item_model is win.icon_view.model()
    headers = [win.item_model.horizontalHeaderItem(c).text() for c in range(3)]
    assert headers == ["Name", "Created", "Status"]
    # the status column shows human labels
    statuses = {win.item_model.item(r, 2).text() for r in range(win.item_model.rowCount())}
    assert "no metadata" in statuses  # the orphan
    win.close()


def test_sidecar_shown_as_nested_child(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from nebula.navigator import app

    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    (s.path / "orphan.dat").write_text("y")  # no sidecar

    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = app.Navigator(archive)

    parents = {win.item_model.item(r, 0).text(): win.item_model.item(r, 0)
               for r in range(win.item_model.rowCount())}
    # the paired artefact has its sidecar nested underneath, shown explicitly
    raw = parents["raw.csv"]
    assert raw.rowCount() == 1
    assert raw.child(0, 0).text() == "raw.csv.meta.json"
    assert raw.child(0, 0).data(1 << 8) is not None  # carries the Item (UserRole)
    # the orphan has no metadata child
    assert parents["orphan.dat"].rowCount() == 0
    win.close()


def test_open_sidecar_panel_shows_dock(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from nebula.navigator import app

    archive = tmp_path / "archive"
    _session_with(archive, {"raw.csv": "x"})

    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = app.Navigator(archive)
    item = win.item_model.item(0, 0).data(1 << 8)

    assert win.sidecar_dock.isHidden()
    win._open_sidecar_panel(item)
    assert not win.sidecar_dock.isHidden()
    assert '"produced_by"' in win.sidecar_panel.toPlainText()
    win.close()


# ---------------------------------------------------------------------
# drag & drop import
# ---------------------------------------------------------------------

def test_import_dialog_spec_existing_and_new(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from nebula.navigator.dialogs import ImportDialog

    archive = tmp_path / "archive"
    s = _session_with(archive, {"a.csv": "x"})
    sessions = model.importable_sessions(archive)
    src = tmp_path / "incoming.csv"
    src.write_text("data")

    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    dlg = ImportDialog([src], sessions, preselect_run_id=s.id)
    # defaults to the existing session we pre-selected
    spec = dlg.spec()
    assert spec.mode == "existing" and spec.run_id == s.id

    dlg.new_radio.setChecked(True)
    dlg.tags_edit.setText("warmup, RP23D")
    dlg.desc_edit.setText("from a coworker")
    dlg.origin_edit.setText("emailed by Jane")
    spec = dlg.spec()
    assert spec.mode == "new"
    assert spec.tags == ["warmup", "RP23D"]
    assert spec.description == "from a coworker"
    assert spec.origin_or_none == "emailed by Jane"


def test_import_dialog_forces_new_when_no_sessions(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from nebula.navigator.dialogs import ImportDialog

    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dlg = ImportDialog([tmp_path / "x.csv"], sessions=[])
    assert not dlg.existing_radio.isEnabled()
    assert dlg.new_radio.isChecked()
    assert dlg.spec().mode == "new"


def test_perform_import_new_session(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from nebula.navigator import app
    from nebula.navigator.dialogs import ImportSpec

    archive = tmp_path / "archive"
    _session_with(archive, {"seed.csv": "x"})  # so an archive exists
    src = tmp_path / "coworker.csv"
    src.write_text("shared data\n")

    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = app.Navigator(archive)
    spec = ImportSpec(mode="new", tags=["shared"], description="drop",
                      origin="emailed 2026-07-02")
    target = win._perform_import([src], spec)

    from nebula.session import _find_session_dir
    from nebula.sidecar import read_sidecar
    dest = _find_session_dir(archive, target) / "coworker.csv"
    assert dest.exists()
    assert read_sidecar(dest).produced_by.origin == "emailed 2026-07-02"
    win.close()


def _place_frozen(archive, run_id="S-9999"):
    ym = archive / "2020" / "01"
    ym.mkdir(parents=True, exist_ok=True)
    d = ym / run_id
    d.mkdir()
    write_session_yaml(d, SessionMeta(
        run_id=run_id,
        created=datetime.datetime(2020, 1, 1).astimezone().isoformat(),
        status="closed", description="last year"))
    return run_id


def test_import_dialog_frozen_toggle(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from nebula.navigator.dialogs import ImportDialog

    archive = tmp_path / "archive"
    _session_with(archive, {"a.csv": "x"})
    _place_frozen(archive)

    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dlg = ImportDialog([tmp_path / "f.csv"],
                       model.importable_sessions(archive),
                       frozen_sessions=model.frozen_sessions(archive))
    assert not dlg.frozen_check.isHidden()  # shown because frozen sessions exist
    # frozen session hidden until the box is checked
    assert dlg.session_combo.findData("S-9999") < 0
    dlg.frozen_check.setChecked(True)
    idx = dlg.session_combo.findData("S-9999")
    assert idx >= 0
    dlg.session_combo.setCurrentIndex(idx)
    spec = dlg.spec()
    assert spec.mode == "existing" and spec.run_id == "S-9999"
    assert spec.allow_frozen is True


def test_perform_import_into_frozen_session(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from nebula.navigator import app
    from nebula.navigator.dialogs import ImportSpec

    archive = tmp_path / "archive"
    _session_with(archive, {"seed.csv": "x"})   # so the archive/index exists
    frozen_id = _place_frozen(archive)
    src = tmp_path / "late.csv"
    src.write_text("added to an old session\n")

    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = app.Navigator(archive)

    # Without allow_frozen this would raise; the spec carries it through.
    spec = ImportSpec(mode="existing", run_id=frozen_id, origin="reopened",
                      allow_frozen=True)
    win._perform_import([src], spec)

    from nebula.session import _find_session_dir
    assert (_find_session_dir(archive, frozen_id) / "late.csv").exists()
    win.close()


def test_perform_import_existing_session(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from nebula.navigator import app
    from nebula.navigator.dialogs import ImportSpec

    archive = tmp_path / "archive"
    s = _session_with(archive, {"seed.csv": "x"})
    src = tmp_path / "added.csv"
    src.write_text("more\n")

    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = app.Navigator(archive)
    spec = ImportSpec(mode="existing", run_id=s.id, origin="by hand")
    win._perform_import([src], spec)

    from nebula.session import _find_session_dir
    assert (_find_session_dir(archive, s.id) / "added.csv").exists()
    win.close()


def test_drop_triggers_import(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtWidgets
    from nebula.navigator import app
    from nebula.navigator.dialogs import ImportSpec

    archive = tmp_path / "archive"
    _session_with(archive, {"seed.csv": "x"})
    src = tmp_path / "dropped.csv"
    src.write_text("z\n")

    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = app.Navigator(archive)

    # Stub the dialog so no modal window blocks the test; auto-accept as "new".
    class _FakeDialog:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QtWidgets.QDialog.Accepted

        def spec(self):
            return ImportSpec(mode="new", description="dropped", origin="drag")

    monkeypatch.setattr(app, "ImportDialog", _FakeDialog)

    # _paths_from_mime extracts local files from a drag's urls
    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(str(src))])
    assert win._paths_from_mime(mime) == [src]

    imported = []
    monkeypatch.setattr(win, "_perform_import",
                        lambda paths, spec: imported.append((paths, spec)))
    win._import_files(win._paths_from_mime(mime))
    assert imported and imported[0][0] == [src]
    assert imported[0][1].mode == "new"
    win.close()
