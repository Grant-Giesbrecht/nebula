"""
Nebula Navigator -- Flet view.

A Finder-like browser over an archive. The left column lists sessions (like
folders); the main area shows one entry per logical artefact with a small
status badge reflecting the artefact <-> sidecar pairing:

    file icon + sidecar_good.png    -- paired, no issues
    file icon + sidecar_warn.png    -- an issue (e.g. sha256 drift)
    file icon + sidecar_error.png   -- the sidecar is missing (orphan)
    missing_artefact.png + warn     -- the sidecar exists but the data file is gone

Unlike the old PySide view there is no OS QFileIconProvider, so the base
graphic is a Material file-type icon chosen by extension rather than the
platform's native icon. This module imports Flet; the model layer
(navigator.model) does not, so it stays importable/testable without a GUI.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

import flet as ft

from nebula import manual
from nebula.navigator import model, osutil
from nebula.navigator.dialogs import ImportDialog
from nebula.sidecar import SIDECAR_SUFFIX

APP_NAME = "Nebula Navigator"

_ASSET_DIR = Path(__file__).resolve().parent / "assets"
_ICON_SIZE = 64

# Badge overlaid on the file icon, chosen by the pair status. STRAY also
# swaps the base image for missing_artefact.png (see _base_control).
_BADGE_FOR_STATUS = {
    model.PAIRED: "sidecar_good.png",
    model.DRIFTED: "sidecar_warn.png",
    model.ORPHAN: "sidecar_error.png",   # sidecar missing
    model.STRAY: "sidecar_warn.png",     # sidecar present, artefact gone
}
_MISSING_ARTEFACT = "missing_artefact.png"

# Material file-type icon by extension. Falls back to a plain document icon.
_EXT_ICON = {
    ".csv": ft.Icons.TABLE_CHART, ".tsv": ft.Icons.TABLE_CHART,
    ".json": ft.Icons.DATA_OBJECT, ".yaml": ft.Icons.DATA_OBJECT,
    ".yml": ft.Icons.DATA_OBJECT,
    ".txt": ft.Icons.DESCRIPTION, ".log": ft.Icons.DESCRIPTION,
    ".md": ft.Icons.DESCRIPTION,
    ".png": ft.Icons.IMAGE, ".jpg": ft.Icons.IMAGE, ".jpeg": ft.Icons.IMAGE,
    ".gif": ft.Icons.IMAGE, ".bmp": ft.Icons.IMAGE, ".svg": ft.Icons.IMAGE,
    ".pdf": ft.Icons.PICTURE_AS_PDF,
    ".py": ft.Icons.CODE, ".ipynb": ft.Icons.CODE,
    ".graf": ft.Icons.SHOW_CHART,
    ".h5": ft.Icons.STORAGE, ".hdf5": ft.Icons.STORAGE, ".npy": ft.Icons.STORAGE,
    ".npz": ft.Icons.STORAGE, ".mat": ft.Icons.STORAGE, ".dat": ft.Icons.STORAGE,
    ".zip": ft.Icons.FOLDER_ZIP, ".tar": ft.Icons.FOLDER_ZIP,
    ".gz": ft.Icons.FOLDER_ZIP,
}
_DEFAULT_FILE_ICON = ft.Icons.INSERT_DRIVE_FILE

# Tint used to mark the selected session / item across both views.
_SEL_BG = ft.Colors.with_opacity(0.14, ft.Colors.PRIMARY)


def _ext_icon(name: str) -> str:
    return _EXT_ICON.get(Path(name).suffix.lower(), _DEFAULT_FILE_ICON)


def compose_icon(item: "model.Item", size: int = _ICON_SIZE) -> ft.Control:
    """The file-type icon (or the missing-artefact graphic) with a corner
    status badge overlaid, as a Flet Stack."""
    if item.status == model.STRAY:
        base = ft.Image(src=_MISSING_ARTEFACT, width=size, height=size,
                        fit=ft.BoxFit.CONTAIN)
    else:
        base = ft.Icon(icon=_ext_icon(item.name), size=size,
                       color=ft.Colors.BLUE_GREY_400)
    layers: List[ft.Control] = [
        ft.Container(content=base, alignment=ft.Alignment.CENTER,
                     width=size, height=size),
    ]
    badge_name = _BADGE_FOR_STATUS.get(item.status)
    if badge_name:
        badge = round(size * 0.42)
        layers.append(ft.Container(
            content=ft.Image(src=badge_name, width=badge, height=badge),
            alignment=ft.Alignment.BOTTOM_RIGHT, width=size, height=size))
    return ft.Stack(controls=layers, width=size, height=size)


class Navigator:
    def __init__(self, page: ft.Page, archive, archive_label: Optional[str] = None):
        self.page = page
        # Resolve leniently (registered name or path) once, up front.
        self.archive_root, label = model.resolve(archive)

        page.title = f"{APP_NAME} — {archive_label or label}"
        page.window.width = 1000
        page.window.height = 660
        page.window.min_width = 720
        page.window.min_height = 460
        page.padding = 0
        # macOS-native (Cupertino) styling: an iOS-blue accent flows into the
        # Material bits (dividers, dropdown) so they match the Cupertino
        # controls, and iOS page transitions round out the feel.
        page.platform = ft.PagePlatform.MACOS
        page.theme = ft.Theme(color_scheme_seed=ft.Colors.BLUE)
        page.dark_theme = ft.Theme(color_scheme_seed=ft.Colors.BLUE)

        # --- shared state -------------------------------------------------
        self.sessions: List[model.SessionInfo] = []
        self.items: List[model.Item] = []
        self.current_session: Optional[model.SessionInfo] = None
        self.selected_item: Optional[model.Item] = None
        self.selected_is_sidecar = False
        self.list_view_mode = True   # list view is the default
        self.verify = False
        self.show_metadata = True    # show the nested sidecar rows in list view
        self._tiles: List[tuple] = []  # (item, is_sidecar, container) for grid/list

        # File picker used by the "Import files..." action.
        self.file_picker = ft.FilePicker()
        page.services.append(self.file_picker)

        self._build()
        self.reload()

    # -- layout -----------------------------------------------------------

    def _build(self) -> None:
        self.session_list = ft.ListView(expand=True, spacing=2, padding=6)
        session_panel = ft.Container(
            width=280, bgcolor=ft.Colors.with_opacity(0.04, ft.Colors.ON_SURFACE),
            content=ft.Column(spacing=0, controls=[
                ft.Container(padding=10, content=ft.Text("Sessions",
                             weight=ft.FontWeight.BOLD)),
                self.session_list,
            ]))

        # Toolbar (Cupertino controls: a sliding segmented control for the
        # view switch, iOS switches for the toggles, Cupertino buttons).
        self.view_toggle = ft.CupertinoSlidingSegmentedButton(
            selected_index=0 if self.list_view_mode else 1,
            padding=ft.Padding.symmetric(horizontal=8, vertical=4),
            controls=[ft.Text("List"), ft.Text("Grid")],
            on_change=self._toggle_view)
        self.verify_check = ft.CupertinoSwitch(
            value=False, on_change=self._toggle_verify)
        self.metadata_check = ft.CupertinoSwitch(
            value=True, on_change=self._toggle_metadata)
        toolbar = ft.Row(spacing=10, controls=[
            ft.CupertinoButton(icon=ft.Icons.REFRESH, tooltip="Refresh",
                               padding=8, on_click=lambda _: self.reload()),
            self.view_toggle,
            ft.VerticalDivider(),
            ft.Row([ft.Text("Verify", size=13), self.verify_check], spacing=4,
                   tooltip="Re-hash each file to detect silent edits (slower)"),
            ft.Row([ft.Text("Metadata", size=13), self.metadata_check], spacing=4,
                   tooltip="Show the nested sidecar (metadata) rows in the list"),
            ft.Container(expand=True),
            ft.CupertinoFilledButton(
                content=ft.Text("Import files…"), icon=ft.Icons.UPLOAD_FILE,
                on_click=self._pick_import),
        ])

        # Main item area (grid or list swapped into this container).
        self.item_area = ft.Container(expand=True, padding=8)

        # Details bar with per-item action buttons.
        self.details_text = ft.Text("Select an item to see its provenance.",
                                    selectable=True, size=12)
        _btn_pad = ft.Padding.symmetric(horizontal=10, vertical=6)
        self.open_artifact_btn = ft.CupertinoButton(
            content=ft.Text("Open artefact", size=13), icon=ft.Icons.OPEN_IN_NEW,
            padding=_btn_pad, disabled=True,
            on_click=lambda _: self._open_artifact(self.selected_item))
        self.open_sidecar_btn = ft.CupertinoButton(
            content=ft.Text("Open sidecar", size=13), icon=ft.Icons.INFO_OUTLINE,
            padding=_btn_pad, disabled=True,
            on_click=lambda _: self._open_sidecar_panel(self.selected_item))
        self.edit_sidecar_btn = ft.CupertinoButton(
            content=ft.Text("Open sidecar in editor", size=13), icon=ft.Icons.EDIT,
            padding=_btn_pad, disabled=True,
            on_click=lambda _: self._open_sidecar_editor(self.selected_item))
        details_bar = ft.Container(
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            bgcolor=ft.Colors.with_opacity(0.03, ft.Colors.ON_SURFACE),
            content=ft.Column(spacing=2, tight=True, controls=[
                ft.Row([self.open_artifact_btn, self.open_sidecar_btn,
                        self.edit_sidecar_btn], spacing=4),
                self.details_text,
            ]))

        main_panel = ft.Column(expand=True, spacing=0, controls=[
            ft.Container(padding=8, content=toolbar),
            ft.Divider(height=1),
            self.item_area,
            ft.Divider(height=1),
            details_bar,
        ])

        # Sidecar side panel, hidden until "Open sidecar" is used.
        self.sidecar_title = ft.Text("Sidecar", weight=ft.FontWeight.BOLD)
        self.sidecar_text = ft.Text("", selectable=True, size=12,
                                    font_family="monospace")
        self.sidecar_panel = ft.Container(
            width=340, visible=False,
            bgcolor=ft.Colors.with_opacity(0.04, ft.Colors.ON_SURFACE),
            content=ft.Column(expand=True, spacing=0, controls=[
                ft.Row([self.sidecar_title, ft.Container(expand=True),
                        ft.CupertinoButton(icon=ft.Icons.CLOSE, tooltip="Close",
                                           padding=6,
                                           on_click=self._close_sidecar_panel)],
                       ),
                ft.Divider(height=1),
                ft.Container(expand=True, padding=10,
                             content=ft.Column([self.sidecar_text],
                                               scroll=ft.ScrollMode.AUTO,
                                               expand=True)),
            ]))

        self.status_text = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT)

        page = self.page
        page.add(ft.Column(expand=True, spacing=0, controls=[
            ft.Row(expand=True, spacing=0, controls=[
                session_panel,
                ft.VerticalDivider(width=1),
                main_panel,
                ft.VerticalDivider(width=1),
                self.sidecar_panel,
            ]),
            ft.Container(padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                         content=self.status_text),
        ]))

    # -- data loading -----------------------------------------------------

    def reload(self) -> None:
        self.sessions = model.list_sessions(self.archive_root)
        self.session_list.controls = [
            self._session_tile(s) for s in self.sessions]
        if self.sessions:
            self._select_session(self.sessions[0])
        else:
            self.current_session = None
            self._reload_items()
        self.status_text.value = f"{len(self.sessions)} session(s)"
        self.page.update()

    def _session_tile(self, s: "model.SessionInfo") -> ft.Control:
        line2 = s.status
        if s.held:
            line2 += "  HELD"
        if s.n_problems:
            line2 += f"  {s.n_problems} ⚠"
        selected = self.current_session is not None and \
            s.run_id == self.current_session.run_id
        return ft.CupertinoListTile(
            leading=ft.Icon(ft.Icons.FOLDER, color=ft.Colors.AMBER),
            title=ft.Text(f"{s.run_id}   {s.description}".rstrip(),
                          size=13, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            subtitle=ft.Text(line2, size=11, color=ft.Colors.ON_SURFACE_VARIANT),
            trailing=ft.CupertinoButton(
                icon=ft.Icons.FOLDER_OPEN, icon_color=ft.Colors.PRIMARY,
                padding=4, tooltip=f"Open in {osutil.file_manager_name()}",
                on_click=lambda _, sess=s: osutil.open_path(sess.path)),
            bgcolor=_SEL_BG if selected else None,
            on_click=lambda _, sess=s: self._select_session(sess))

    def _select_session(self, session: "model.SessionInfo") -> None:
        self.current_session = session
        # Repaint sidebar highlights.
        self.session_list.controls = [
            self._session_tile(s) for s in self.sessions]
        self._reload_items()
        self.page.update()

    def _reload_items(self) -> None:
        self.selected_item = None
        self.selected_is_sidecar = False
        self._tiles = []
        self._update_details()
        if self.current_session is None:
            self.items = []
            self.item_area.content = None
            return
        self.items = model.list_items(
            self.current_session.path, verify_checksums=self.verify)
        self.item_area.content = (self._build_list() if self.list_view_mode
                                  else self._build_grid())
        problems = sum(1 for i in self.items if i.status != model.PAIRED)
        self.status_text.value = (
            f"{self.current_session.run_id}: {len(self.items)} item(s), "
            f"{problems} problem(s)")

    # -- grid view --------------------------------------------------------

    def _build_grid(self) -> ft.Control:
        grid = ft.GridView(expand=True, max_extent=150, spacing=10,
                           run_spacing=10, child_aspect_ratio=0.85)
        for item in self.items:
            grid.controls.append(self._grid_tile(item))
        return grid

    def _grid_tile(self, item: "model.Item") -> ft.Control:
        label = ft.Column(spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                          controls=[
            compose_icon(item),
            ft.Text(item.name, size=12, max_lines=2, text_align=ft.TextAlign.CENTER,
                    overflow=ft.TextOverflow.ELLIPSIS),
        ])
        container = ft.Container(
            padding=6, border_radius=8, tooltip=item.detail, ink=True,
            border=ft.Border.all(2, ft.Colors.TRANSPARENT),
            content=ft.GestureDetector(
                content=label,
                on_tap=lambda _, it=item: self._select(it, False),
                on_double_tap=lambda _, it=item: self._activate(it, False)))
        self._tiles.append((item, False, container))
        return container

    # -- list view --------------------------------------------------------

    def _build_list(self) -> ft.Control:
        rows: List[ft.Control] = []
        for item in self.items:
            created = (item.timestamp or "")[:19].replace("T", " ")
            rows.append(self._list_row(item, item.name, created,
                                       item.status_label, is_sidecar=False))
            if item.has_sidecar and self.show_metadata:
                rows.append(self._list_row(
                    item, item.name + SIDECAR_SUFFIX, created,
                    "metadata", is_sidecar=True))
        return ft.Column(rows, scroll=ft.ScrollMode.AUTO, expand=True, spacing=0)

    def _list_row(self, item, name, created, status, *, is_sidecar) -> ft.Control:
        # Nested sidecar rows are indented and carry a plain JSON icon; artefact
        # rows show the composed status badge. iOS list styling: title + gray
        # subtitle, with the status as "additional info" before the trailing.
        if is_sidecar:
            leading = ft.Container(
                padding=ft.Padding.only(left=24),
                content=ft.Icon(ft.Icons.DATA_OBJECT, size=22,
                                color=ft.Colors.BLUE_GREY_400))
        else:
            leading = compose_icon(item, 30)
        tile = ft.CupertinoListTile(
            leading=leading,
            title=ft.Text(name, size=13),
            subtitle=ft.Text(created, size=11, color=ft.Colors.ON_SURFACE_VARIANT),
            additional_info=ft.Text(status, size=12,
                                    color=ft.Colors.ON_SURFACE_VARIANT),
            on_click=lambda _, it=item, sc=is_sidecar: self._list_click(it, sc))
        self._tiles.append((item, is_sidecar, tile))
        return tile

    def _list_click(self, item, is_sidecar) -> None:
        """List rows select on click (opening a data file is left to the
        action buttons); clicking a metadata row also reveals its sidecar."""
        self._select(item, is_sidecar)
        if is_sidecar:
            self._open_sidecar_panel(item)

    # -- selection & activation ------------------------------------------

    def _select(self, item, is_sidecar) -> None:
        self.selected_item = item
        self.selected_is_sidecar = is_sidecar
        # Highlight the matching tile: grid tiles get a border, list rows a fill.
        for it, sc, ctrl in self._tiles:
            on = it is item and sc == is_sidecar
            if isinstance(ctrl, ft.CupertinoListTile):
                ctrl.bgcolor = _SEL_BG if on else None
            elif isinstance(ctrl, ft.Container):
                ctrl.border = ft.Border.all(
                    2, ft.Colors.PRIMARY if on else ft.Colors.TRANSPARENT)
        self._update_details()
        self.page.update()

    def _activate(self, item, is_sidecar) -> None:
        """Double-click: sidecar row opens the panel; artefact row opens the
        data file (falling back to the sidecar if none exists)."""
        self._select(item, is_sidecar)
        if is_sidecar:
            self._open_sidecar_panel(item)
        elif item.has_artifact:
            self._open_artifact(item)
        elif item.has_sidecar:
            self._open_sidecar_panel(item)

    def _update_details(self) -> None:
        item = self.selected_item
        self.details_text.value = item.detail if item else \
            "Select an item to see its provenance."
        self.open_artifact_btn.disabled = not (item and item.has_artifact)
        self.open_sidecar_btn.disabled = not (item and item.has_sidecar)
        self.edit_sidecar_btn.disabled = not (item and item.has_sidecar)

    # -- toolbar handlers -------------------------------------------------

    def _toggle_view(self, e) -> None:
        # Segment 0 = list, segment 1 = grid.
        self.list_view_mode = self.view_toggle.selected_index == 0
        self._reload_items()
        self.page.update()

    def _toggle_verify(self, e) -> None:
        self.verify = bool(self.verify_check.value)
        self._reload_items()
        self.page.update()

    def _toggle_metadata(self, e) -> None:
        self.show_metadata = bool(self.metadata_check.value)
        self._reload_items()
        self.page.update()

    # -- actions ----------------------------------------------------------

    def _open_artifact(self, item) -> None:
        if item is not None and item.artifact_path is not None:
            osutil.open_path(item.artifact_path)

    def _open_sidecar_editor(self, item) -> None:
        if item is not None and item.sidecar_path is not None:
            osutil.open_path(item.sidecar_path)

    def _open_sidecar_panel(self, item) -> None:
        if item is None or item.sidecar_path is None:
            return
        self.sidecar_text.value = model.sidecar_display(item.sidecar_path)
        self.sidecar_title.value = f"Sidecar — {item.name}"
        self.sidecar_panel.visible = True
        self.page.update()

    def _close_sidecar_panel(self, e) -> None:
        self.sidecar_panel.visible = False
        self.page.update()

    # -- drag & drop / file-picker import --------------------------------

    async def _pick_import(self, e) -> None:
        result = await self.file_picker.pick_files(
            dialog_title="Choose files to import", allow_multiple=True)
        paths = [Path(f.path) for f in (result or [])
                 if f.path and Path(f.path).is_file()]
        if paths:
            self._import_files(paths)

    def _import_files(self, paths) -> None:
        sessions = model.importable_sessions(self.archive_root)
        frozen = model.frozen_sessions(self.archive_root)
        preselect = None
        if self.current_session is not None and any(
                s.run_id == self.current_session.run_id for s in sessions):
            preselect = self.current_session.run_id
        ImportDialog(
            self.page, paths, sessions, frozen_sessions=frozen,
            preselect_run_id=preselect,
            on_submit=lambda spec: self._perform_import(paths, spec)).show()

    def _perform_import(self, paths, spec) -> Optional[str]:
        """Run the actual import for a confirmed ImportSpec, then refresh and
        jump to the target session."""
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
        except (FileExistsError, FileNotFoundError, RuntimeError) as exc:
            self._toast(f"Import failed: {exc}")
            return None
        self.reload()
        for s in self.sessions:
            if s.run_id == target:
                self._select_session(s)
                break
        return target

    def _toast(self, message: str) -> None:
        self.page.show_dialog(ft.SnackBar(content=ft.Text(message)))


def launch(archive, archive_label: Optional[str] = None) -> int:
    def main(page: ft.Page) -> None:
        Navigator(page, archive, archive_label=archive_label)

    ft.run(main, assets_dir=str(_ASSET_DIR))
    return 0


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
