"""
Navigator dialogs. Currently: the import dialog shown when files are chosen
for import.

It forces a deliberate choice -- add the files to an existing (appendable)
session, or start a new one -- and prompts for the origin note that becomes
the files' external provenance, plus tags/description for a new session.

``ImportSpec`` is toolkit-independent (plain dataclass); ``ImportDialog``
builds the Flet ``AlertDialog`` around it and reports the chosen spec through
an ``on_submit`` callback when the user confirms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

import flet as ft


@dataclass
class ImportSpec:
    mode: str                       # "existing" | "new"
    run_id: Optional[str] = None    # set when mode == "existing"
    tags: List[str] = field(default_factory=list)
    description: str = ""
    origin: str = ""
    allow_frozen: bool = False      # reopen a previous-day-closed target

    @property
    def origin_or_none(self) -> Optional[str]:
        return self.origin or None


class ImportDialog:
    """Builds the import ``AlertDialog``. Call :meth:`show` to display it; on
    confirmation it invokes ``on_submit(spec)`` with the assembled
    :class:`ImportSpec`."""

    def __init__(self, page: ft.Page, files, sessions, *,
                 frozen_sessions=None, preselect_run_id=None,
                 on_submit: Optional[Callable[[ImportSpec], None]] = None):
        self.page = page
        self.on_submit = on_submit
        self._shown = False
        self._files = [Path(f) for f in files]
        self._sessions = list(sessions)
        self._frozen = list(frozen_sessions or [])
        self._frozen_ids = {s.run_id for s in self._frozen}

        # --- target: existing session vs new -----------------------------
        # The dropdown + frozen checkbox sit directly under the "existing"
        # radio (indented) so the control they govern is next to its option;
        # the "new" radio follows.
        self.session_dropdown = ft.Dropdown(label="Session", options=[])
        # Reveals frozen (previous-day-closed) sessions as targets; picking one
        # imports with a deliberate reopen.
        self.frozen_check = ft.Checkbox(
            label="Include previously-closed sessions",
            tooltip="Sessions closed on an earlier day; importing reopens them",
            visible=bool(self._frozen),
            on_change=lambda _: self._rebuild_options(),
        )
        self.mode = ft.RadioGroup(
            value="existing" if self._sessions else "new",
            on_change=lambda _: self._sync_enabled(),
            content=ft.Column(tight=True, spacing=4, controls=[
                ft.Radio(value="existing", label="Add to existing session",
                         disabled=not self._sessions),
                ft.Container(padding=ft.Padding.only(left=32),
                             content=ft.Column(tight=True, spacing=4, controls=[
                                 self.session_dropdown,
                                 self.frozen_check,
                             ])),
                ft.Radio(value="new", label="Create a new session"),
            ]),
        )

        self.tags_edit = ft.TextField(label="Tags",
                                      hint_text="comma-separated, optional")
        self.desc_edit = ft.TextField(label="Description",
                                      hint_text="session description, optional")
        self.origin_edit = ft.TextField(
            label="Origin", hint_text="where did these come from? e.g. emailed by Jane")

        self._rebuild_options()
        if preselect_run_id is not None and any(
                o.key == preselect_run_id for o in self.session_dropdown.options):
            self.session_dropdown.value = preselect_run_id

        file_names = ft.Column(
            tight=True, spacing=1, scroll=ft.ScrollMode.AUTO, height=60,
            controls=[ft.Text(f.name, size=12) for f in self._files])

        # Bounded, scrollable content so a tall body scrolls internally rather
        # than growing the dialog until it overlaps the Cancel/Import row.
        self.dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Import {len(self._files)} file(s)"),
            content=ft.Container(width=460, height=440, content=ft.Column(
                tight=True, spacing=12, scroll=ft.ScrollMode.AUTO,
                controls=[
                    file_names,
                    ft.Divider(),
                    self.mode,
                    self.tags_edit,
                    self.desc_edit,
                    self.origin_edit,
                ])),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: self._close()),
                ft.FilledButton("Import", on_click=lambda _: self._confirm()),
            ],
        )
        self._sync_enabled()

    def show(self) -> None:
        self._shown = True
        self.page.show_dialog(self.dialog)

    def _close(self) -> None:
        self._shown = False
        self.page.pop_dialog()

    def _rebuild_options(self) -> None:
        keep = self.session_dropdown.value
        options = [ft.DropdownOption(key=s.run_id,
                                     text=f"{s.run_id}   {s.description}".rstrip())
                   for s in self._sessions]
        if self.frozen_check.value:
            options += [
                ft.DropdownOption(
                    key=s.run_id,
                    text=f"{s.run_id}   {s.description}  (frozen)".rstrip())
                for s in self._frozen]
        self.session_dropdown.options = options
        keys = {o.key for o in options}
        self.session_dropdown.value = keep if keep in keys else (
            options[0].key if options else None)
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        existing = self.mode.value == "existing"
        self.session_dropdown.disabled = not existing
        self.tags_edit.disabled = existing
        self.desc_edit.disabled = existing
        if self._shown:
            self.dialog.update()

    def spec(self) -> ImportSpec:
        origin = (self.origin_edit.value or "").strip()
        if self.mode.value == "existing":
            run_id = self.session_dropdown.value
            return ImportSpec(mode="existing", run_id=run_id, origin=origin,
                              allow_frozen=run_id in self._frozen_ids)
        tags = [t.strip() for t in (self.tags_edit.value or "").split(",")
                if t.strip()]
        return ImportSpec(mode="new", tags=tags,
                          description=(self.desc_edit.value or "").strip(),
                          origin=origin)

    def _confirm(self) -> None:
        spec = self.spec()
        self._close()
        if self.on_submit is not None:
            self.on_submit(spec)
