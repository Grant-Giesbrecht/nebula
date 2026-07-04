"""
Nebula Navigator -- PySide6 view.

A Finder-like browser over an archive. The left column lists sessions (like
folders); the main area shows one icon per logical artefact: the operating
system's native file-type icon (via QFileIconProvider) with a small status
badge in the corner reflecting the artefact ↔ sidecar pairing:

    file icon + sidecar_good.png    -- paired, no issues
    file icon + sidecar_warn.png    -- an issue (e.g. sha256 drift)
    file icon + sidecar_error.png   -- the sidecar is missing (orphan)
    missing_artefact.png + warn     -- the sidecar exists but the data file is gone

This module imports PySide6; the model layer (navigator.model) does not, so
it stays importable/testable without a GUI.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from nebula import manual
from nebula.navigator import model, osutil
from nebula.navigator.dialogs import ImportDialog
from nebula.sidecar import SIDECAR_SUFFIX

# Model item roles.
_ROLE_ITEM = QtCore.Qt.UserRole            # the model.Item for this row's group
_ROLE_IS_SIDECAR = QtCore.Qt.UserRole + 1  # True on the nested .meta.json row

APP_NAME = "Nebula Navigator"

_ICON_SIZE = 80
_ASSET_DIR = Path(__file__).resolve().parent / "assets"

# Badge overlaid on the file icon, chosen by the pair status. STRAY also
# swaps the base image for missing_artefact.png (see _base_pixmap).
_BADGE_FOR_STATUS = {
    model.PAIRED: "sidecar_good.png",
    model.DRIFTED: "sidecar_warn.png",
    model.ORPHAN: "sidecar_error.png",   # sidecar missing
    model.STRAY: "sidecar_warn.png",     # sidecar present, artefact gone
}
_MISSING_ARTEFACT = "missing_artefact.png"

# Caches so a session full of same-type files doesn't re-query the OS or
# re-decode the badge PNGs.
_icon_provider = None
_ext_icon_cache: dict = {}
_asset_cache: dict = {}


def _provider():
    global _icon_provider
    if _icon_provider is None:
        # QFileIconProvider moved between QtWidgets and QtGui across Qt
        # versions; take whichever this PySide6 build exposes.
        cls = getattr(QtGui, "QFileIconProvider", None) \
            or getattr(QtWidgets, "QFileIconProvider")
        _icon_provider = cls()
    return _icon_provider


def _asset(name: str) -> QtGui.QPixmap:
    pm = _asset_cache.get(name)
    if pm is None:
        pm = QtGui.QPixmap(str(_ASSET_DIR / name))
        _asset_cache[name] = pm
    return pm


def _scaled(pm: QtGui.QPixmap, size: int) -> QtGui.QPixmap:
    return pm.scaled(size, size, QtCore.Qt.KeepAspectRatio,
                     QtCore.Qt.SmoothTransformation)


def _generic_file_icon():
    prov = _provider()
    icon_type = getattr(type(prov), "IconType", None)
    file_enum = (getattr(icon_type, "File", None) if icon_type is not None
                 else getattr(type(prov), "File", None))
    return prov.icon(file_enum) if file_enum is not None else QtGui.QIcon()


def _file_icon_pixmap(name_or_path, size: int) -> QtGui.QPixmap:
    """Native OS icon for a file *type*, cached by extension.

    We deliberately look the icon up by a synthetic name carrying only the
    extension (not the real file) so macOS returns the generic file-type
    icon rather than a QuickLook thumbnail/preview of this particular
    file's contents. Falls back to the plain document icon for types the OS
    has nothing for, and scales up to fill the canvas (OS icons often come
    back at 32px and would otherwise look tiny)."""
    ext = Path(name_or_path).suffix.lower()
    icon = _ext_icon_cache.get(ext)
    if icon is None:
        icon = _provider().icon(QtCore.QFileInfo("nebula-type-probe" + ext))
        if icon.isNull() or icon.pixmap(size, size).isNull():
            icon = _generic_file_icon()
        _ext_icon_cache[ext] = icon
    pm = icon.pixmap(size, size)
    return _scaled(pm, size) if not pm.isNull() else pm


def _base_pixmap(item: "model.Item", size: int) -> QtGui.QPixmap:
    if item.status == model.STRAY:
        # No data file to draw -- use the "missing artefact" graphic instead.
        return _scaled(_asset(_MISSING_ARTEFACT), size)
    # Use the real path when present so the OS can pick the exact icon; fall
    # back to the name (extension is enough for a type icon).
    return _file_icon_pixmap(item.artifact_path or item.name, size)


def compose_icon(item: "model.Item", size: int = _ICON_SIZE) -> QtGui.QIcon:
    """The OS file icon (or the missing-artefact graphic) with a corner
    status badge overlaid."""
    canvas = QtGui.QPixmap(size, size)
    canvas.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(canvas)
    p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
    try:
        base = _base_pixmap(item, size)
        if not base.isNull():
            p.drawPixmap((size - base.width()) // 2,
                         (size - base.height()) // 2, base)
        badge_name = _BADGE_FOR_STATUS.get(item.status)
        if badge_name:
            badge = _scaled(_asset(badge_name), round(size * 0.45))
            p.drawPixmap(size - badge.width(), size - badge.height(), badge)
    finally:
        p.end()
    return QtGui.QIcon(canvas)


_sidecar_icon_cache = None


def sidecar_icon() -> QtGui.QIcon:
    """A plain JSON file icon (no status badge) for the nested sidecar row."""
    global _sidecar_icon_cache
    if _sidecar_icon_cache is None:
        pm = _file_icon_pixmap("sidecar.json", _ICON_SIZE)
        _sidecar_icon_cache = QtGui.QIcon(pm) if not pm.isNull() else QtGui.QIcon()
    return _sidecar_icon_cache


class Navigator(QtWidgets.QMainWindow):
    def __init__(self, archive, archive_label: Optional[str] = None):
        super().__init__()
        # Resolve leniently (registered name or path) once, up front.
        self.archive_root, label = model.resolve(archive)
        self.setWindowTitle(f"{APP_NAME} — {archive_label or label}")
        self.resize(940, 620)
        self.setAcceptDrops(True)  # drop files anywhere on the window to import

        self.session_list = QtWidgets.QListWidget()
        self.session_list.setMinimumWidth(240)
        self.session_list.setMaximumWidth(340)
        self.session_list.currentItemChanged.connect(lambda *_: self._reload_items())
        self.session_list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(self._session_menu)

        # One model, two views: a Finder-like icon grid and a sortable
        # multi-column list. Both show the same status icon on the left.
        self.item_model = QtGui.QStandardItemModel(self)

        self.icon_view = QtWidgets.QListView()
        self.icon_view.setModel(self.item_model)
        self.icon_view.setViewMode(QtWidgets.QListView.IconMode)
        self.icon_view.setIconSize(QtCore.QSize(_ICON_SIZE, _ICON_SIZE))
        self.icon_view.setGridSize(QtCore.QSize(_ICON_SIZE + 60, _ICON_SIZE + 44))
        self.icon_view.setResizeMode(QtWidgets.QListView.Adjust)
        self.icon_view.setMovement(QtWidgets.QListView.Static)
        self.icon_view.setWordWrap(True)
        self.icon_view.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)

        self.list_view = QtWidgets.QTreeView()
        self.list_view.setModel(self.item_model)
        self.list_view.setRootIsDecorated(True)   # show the artefact→sidecar nesting
        self.list_view.setSortingEnabled(True)
        self.list_view.setUniformRowHeights(True)
        self.list_view.setAllColumnsShowFocus(True)
        self.list_view.setIconSize(QtCore.QSize(28, 28))
        self.list_view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.list_view.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        # Share a single selection across both views so toggling keeps context.
        self.list_view.setSelectionModel(self.icon_view.selectionModel())

        for view in (self.icon_view, self.list_view):
            view.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            view.customContextMenuRequested.connect(
                lambda pos, v=view: self._item_menu(v, pos))
            view.doubleClicked.connect(self._on_item_activated)
        self.icon_view.selectionModel().currentChanged.connect(self._on_current_changed)

        self.view_stack = QtWidgets.QStackedWidget()
        self.view_stack.addWidget(self.icon_view)   # index 0 = grid
        self.view_stack.addWidget(self.list_view)    # index 1 = list

        self.details = QtWidgets.QLabel("Select an item to see its provenance.")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.details.setStyleSheet("color:#444; padding:6px;")
        self.details.setMinimumHeight(64)
        self.details.setAlignment(QtCore.Qt.AlignTop)

        right = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.view_stack, 1)
        rl.addWidget(self.details, 0)

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(self.session_list)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        # Side panel for "Open sidecar": a dockable, closable pane showing the
        # sidecar JSON. Hidden until the user opens a sidecar.
        self.sidecar_panel = QtWidgets.QPlainTextEdit()
        self.sidecar_panel.setReadOnly(True)
        self.sidecar_panel.setFont(QtGui.QFontDatabase.systemFont(
            QtGui.QFontDatabase.FixedFont))
        self.sidecar_dock = QtWidgets.QDockWidget("Sidecar", self)
        self.sidecar_dock.setWidget(self.sidecar_panel)
        self.sidecar_dock.setAllowedAreas(QtCore.Qt.RightDockWidgetArea |
                                          QtCore.Qt.LeftDockWidgetArea)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.sidecar_dock)
        self.sidecar_dock.hide()

        self._build_toolbar()
        self.reload()

    def _build_toolbar(self) -> None:
        tb = self.addToolBar("main")
        tb.setMovable(False)
        refresh = QtGui.QAction("Refresh", self)
        refresh.triggered.connect(self.reload)
        tb.addAction(refresh)
        tb.addSeparator()

        self.list_toggle = QtGui.QAction("List view", self)
        self.list_toggle.setCheckable(True)
        self.list_toggle.setToolTip("Switch between a sortable list and the icon grid")
        self.list_toggle.toggled.connect(
            lambda on: self.view_stack.setCurrentIndex(1 if on else 0))
        tb.addAction(self.list_toggle)
        self.list_toggle.setChecked(True)  # list view is the default
        tb.addSeparator()

        self.verify_cb = QtWidgets.QCheckBox("Verify checksums")
        self.verify_cb.setToolTip("Re-hash each file to detect silent edits (slower)")
        self.verify_cb.stateChanged.connect(lambda *_: self._reload_items())
        tb.addWidget(self.verify_cb)

    # -- data loading -------------------------------------------------

    def reload(self) -> None:
        self.session_list.clear()
        folder = self.style().standardIcon(QtWidgets.QStyle.SP_DirIcon)
        sessions = model.list_sessions(self.archive_root)
        for s in sessions:
            line2 = s.status
            if s.held:
                line2 += "  HELD"
            if s.n_problems:
                line2 += f"  {s.n_problems} ⚠"
            item = QtWidgets.QListWidgetItem(
                folder, f"{s.run_id}   {s.description}\n{line2}")
            item.setData(QtCore.Qt.UserRole, s)
            self.session_list.addItem(item)
        if self.session_list.count():
            self.session_list.setCurrentRow(0)
        else:
            self.item_model.clear()
        self.statusBar().showMessage(f"{len(sessions)} session(s)")

    def _current_session(self) -> Optional["model.SessionInfo"]:
        it = self.session_list.currentItem()
        return it.data(QtCore.Qt.UserRole) if it else None

    @staticmethod
    def _row(icon, name, created, status, item, *, is_sidecar):
        """Build a [name, created, status] row of QStandardItems."""
        c_name = QtGui.QStandardItem(icon, name)
        c_name.setData(item, _ROLE_ITEM)
        c_name.setData(is_sidecar, _ROLE_IS_SIDECAR)
        c_created = QtGui.QStandardItem(created)
        c_status = QtGui.QStandardItem(status)
        cells = [c_name, c_created, c_status]
        for cell in cells:
            cell.setEditable(False)
        return cells

    def _reload_items(self) -> None:
        self.item_model.clear()
        self.item_model.setHorizontalHeaderLabels(["Name", "Created", "Status"])
        session = self._current_session()
        if session is None:
            return
        items = model.list_items(
            session.path, verify_checksums=self.verify_cb.isChecked())
        root = self.item_model.invisibleRootItem()
        for item in items:
            created = (item.timestamp or "")[:19].replace("T", " ")
            parent = self._row(compose_icon(item), item.name, created,
                               item.status_label, item, is_sidecar=False)
            parent[0].setToolTip(item.detail)
            root.appendRow(parent)
            # Show the sidecar file explicitly, nested under its artefact.
            if item.has_sidecar:
                child = self._row(sidecar_icon(), item.name + SIDECAR_SUFFIX,
                                  created, "metadata", item, is_sidecar=True)
                child[0].setToolTip(f"sidecar metadata for {item.name}")
                parent[0].appendRow(child)
        self.list_view.expandAll()
        self.list_view.resizeColumnToContents(0)
        if self.list_view.columnWidth(0) < 260:
            self.list_view.setColumnWidth(0, 260)
        self.list_view.resizeColumnToContents(1)
        if self.list_view.columnWidth(1) < 160:
            self.list_view.setColumnWidth(1, 160)
        problems = sum(1 for i in items if i.status != model.PAIRED)
        self.statusBar().showMessage(
            f"{session.run_id}: {len(items)} item(s), {problems} problem(s)")

    def _item_at(self, index) -> Optional["model.Item"]:
        cell = self.item_model.itemFromIndex(index.sibling(index.row(), 0))
        return cell.data(_ROLE_ITEM) if cell is not None else None

    def _is_sidecar_row(self, index) -> bool:
        cell = self.item_model.itemFromIndex(index.sibling(index.row(), 0))
        return bool(cell.data(_ROLE_IS_SIDECAR)) if cell is not None else False

    def _on_current_changed(self, current, _previous) -> None:
        if not current.isValid():
            self.details.setText("")
            return
        item = self._item_at(current)
        self.details.setText(item.detail if item else "")

    def _on_item_activated(self, index) -> None:
        item = self._item_at(index)
        if item is None:
            return
        # The nested sidecar row opens the sidecar panel; the artefact row
        # opens the data file (falling back to the sidecar if none exists).
        if self._is_sidecar_row(index):
            self._open_sidecar_panel(item)
        elif item.has_artifact:
            self._open_artifact(item)
        elif item.has_sidecar:
            self._open_sidecar_panel(item)

    # -- context menus ------------------------------------------------

    def _session_menu(self, pos) -> None:
        list_item = self.session_list.itemAt(pos)
        if list_item is None:
            return
        session = list_item.data(QtCore.Qt.UserRole)
        menu = QtWidgets.QMenu(self)
        act = menu.addAction(f"Open in {osutil.file_manager_name()}")
        act.triggered.connect(lambda: self._open_session_folder(session))
        menu.exec(self.session_list.viewport().mapToGlobal(pos))

    def _item_menu(self, view, pos) -> None:
        index = view.indexAt(pos)
        if not index.isValid():
            return
        item = self._item_at(index)
        if item is None:
            return
        menu = QtWidgets.QMenu(self)

        a_open = menu.addAction("Open artefact")
        a_open.setEnabled(item.has_artifact)
        a_open.triggered.connect(lambda: self._open_artifact(item))

        a_side = menu.addAction("Open sidecar")
        a_side.setEnabled(item.has_sidecar)
        a_side.triggered.connect(lambda: self._open_sidecar_panel(item))

        a_edit = menu.addAction("Open sidecar in editor")
        a_edit.setEnabled(item.has_sidecar)
        a_edit.triggered.connect(lambda: self._open_sidecar_editor(item))

        menu.exec(view.viewport().mapToGlobal(pos))

    # -- actions (also the unit-test entry points) --------------------

    def _open_session_folder(self, session) -> None:
        osutil.open_path(session.path)

    def _open_artifact(self, item) -> None:
        if item.artifact_path is not None:
            osutil.open_path(item.artifact_path)

    def _open_sidecar_editor(self, item) -> None:
        if item.sidecar_path is not None:
            osutil.open_path(item.sidecar_path)

    def _open_sidecar_panel(self, item) -> None:
        if item.sidecar_path is None:
            return
        self.sidecar_panel.setPlainText(model.sidecar_display(item.sidecar_path))
        self.sidecar_dock.setWindowTitle(f"Sidecar — {item.name}")
        self.sidecar_dock.show()
        self.sidecar_dock.raise_()

    # -- drag & drop import ------------------------------------------

    @staticmethod
    def _paths_from_mime(mime) -> "list[Path]":
        """The local, existing regular files in a drag's mime data."""
        paths = []
        if mime.hasUrls():
            for url in mime.urls():
                local = url.toLocalFile()
                if local and Path(local).is_file():
                    paths.append(Path(local))
        return paths

    def dragEnterEvent(self, event) -> None:
        if self._paths_from_mime(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if self._paths_from_mime(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = self._paths_from_mime(event.mimeData())
        if not paths:
            return
        event.acceptProposedAction()
        self._import_files(paths)

    def _import_files(self, paths) -> None:
        sessions = model.importable_sessions(self.archive_root)
        frozen = model.frozen_sessions(self.archive_root)
        current = self._current_session()
        preselect = None
        if current is not None and any(s.run_id == current.run_id for s in sessions):
            preselect = current.run_id
        dialog = ImportDialog(paths, sessions, frozen_sessions=frozen,
                              preselect_run_id=preselect, parent=self)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        self._perform_import(paths, dialog.spec())

    def _perform_import(self, paths, spec) -> None:
        """Run the actual import for a confirmed ImportSpec, then refresh and
        jump to the target session. Returns the target run_id (or None)."""
        try:
            if spec.mode == "new":
                session = manual.import_new(
                    self.archive_root, [str(p) for p in paths],
                    tags=spec.tags, description=spec.description,
                    origin=spec.origin_or_none)
                target = session.id
            else:
                for p in paths:
                    manual.import_file(
                        self.archive_root, spec.run_id, str(p),
                        origin=spec.origin_or_none,
                        allow_frozen=spec.allow_frozen)
                target = spec.run_id
        except (FileExistsError, FileNotFoundError, RuntimeError) as e:
            QtWidgets.QMessageBox.warning(self, "Import failed", str(e))
            return None
        self.reload()
        self._select_session(target)
        return target

    def _select_session(self, run_id) -> None:
        for row in range(self.session_list.count()):
            s = self.session_list.item(row).data(QtCore.Qt.UserRole)
            if s is not None and s.run_id == run_id:
                self.session_list.setCurrentRow(row)
                return


def launch(archive, archive_label: Optional[str] = None) -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    win = Navigator(archive, archive_label=archive_label)
    win.show()
    return app.exec()


def main() -> int:
    """Entry point for `python -m nebula.navigator` and the
    `nebula-navigator` gui-script."""
    args = sys.argv[1:]
    if not args:
        print("usage: nebula-navigator <archive-name-or-path>", file=sys.stderr)
        return 1
    return launch(args[0], archive_label=args[0])


if __name__ == "__main__":
    raise SystemExit(main())
