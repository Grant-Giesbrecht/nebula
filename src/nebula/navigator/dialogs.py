"""
Navigator dialogs. Currently: the import dialog shown when files are dropped
onto the window.

It forces a deliberate choice -- add the dropped files to an existing
(appendable) session, or start a new one -- and prompts for the origin note
that becomes the files' external provenance, plus tags/description for a new
session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from PySide6 import QtCore, QtWidgets


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


class ImportDialog(QtWidgets.QDialog):
    def __init__(self, files, sessions, *, frozen_sessions=None,
                 preselect_run_id=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import files")
        self.setMinimumWidth(440)
        self._files = [Path(f) for f in files]
        self._sessions = list(sessions)
        self._frozen = list(frozen_sessions or [])
        self._frozen_ids = {s.run_id for s in self._frozen}

        layout = QtWidgets.QVBoxLayout(self)

        layout.addWidget(QtWidgets.QLabel(f"Importing {len(self._files)} file(s):"))
        file_list = QtWidgets.QListWidget()
        file_list.addItems([f.name for f in self._files])
        file_list.setMaximumHeight(96)
        file_list.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        layout.addWidget(file_list)

        # --- target: existing session vs new -----------------------------
        self.existing_radio = QtWidgets.QRadioButton("Add to existing session")
        self.session_combo = QtWidgets.QComboBox()
        # Reveals frozen (previous-day-closed) sessions as targets; picking one
        # imports with a deliberate reopen.
        self.frozen_check = QtWidgets.QCheckBox(
            "Show sessions closed on a previous day (reopen to import)")
        self.frozen_check.setVisible(bool(self._frozen))
        self.new_radio = QtWidgets.QRadioButton("Create a new session")

        self.tags_edit = QtWidgets.QLineEdit()
        self.tags_edit.setPlaceholderText("tags (comma-separated, optional)")
        self.desc_edit = QtWidgets.QLineEdit()
        self.desc_edit.setPlaceholderText("session description (optional)")

        form = QtWidgets.QGridLayout()
        form.addWidget(self.existing_radio, 0, 0, 1, 2)
        form.addWidget(self.session_combo, 1, 1)
        form.addWidget(self.frozen_check, 2, 1)
        form.addWidget(self.new_radio, 3, 0, 1, 2)
        form.addWidget(QtWidgets.QLabel("Tags"), 4, 0)
        form.addWidget(self.tags_edit, 4, 1)
        form.addWidget(QtWidgets.QLabel("Description"), 5, 0)
        form.addWidget(self.desc_edit, 5, 1)
        layout.addLayout(form)

        # --- origin (applies to all imported files) ----------------------
        layout.addWidget(QtWidgets.QLabel("Origin (where did these come from?)"))
        self.origin_edit = QtWidgets.QLineEdit()
        self.origin_edit.setPlaceholderText("e.g. emailed by Jane, 2026-07-02")
        layout.addWidget(self.origin_edit)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.existing_radio.toggled.connect(self._sync_enabled)
        self.new_radio.toggled.connect(self._sync_enabled)
        self.frozen_check.toggled.connect(self._rebuild_combo)

        self._rebuild_combo()
        # Default selection: prefer the passed-in current session if it's an
        # importable target; else the first session; else force "new".
        if self.session_combo.count() > 0:
            if preselect_run_id is not None:
                idx = self.session_combo.findData(preselect_run_id)
                if idx >= 0:
                    self.session_combo.setCurrentIndex(idx)
            self.existing_radio.setChecked(True)
        else:
            self.new_radio.setChecked(True)
        self._sync_enabled()

    def _rebuild_combo(self) -> None:
        keep = self.session_combo.currentData()
        self.session_combo.clear()
        for s in self._sessions:
            self.session_combo.addItem(f"{s.run_id}   {s.description}".rstrip(),
                                       s.run_id)
        if self.frozen_check.isChecked():
            for s in self._frozen:
                self.session_combo.addItem(
                    f"{s.run_id}   {s.description}  (frozen)".rstrip(), s.run_id)
        if keep is not None:
            idx = self.session_combo.findData(keep)
            if idx >= 0:
                self.session_combo.setCurrentIndex(idx)
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        has_targets = self.session_combo.count() > 0
        self.existing_radio.setEnabled(has_targets)
        if not has_targets and self.existing_radio.isChecked():
            self.new_radio.setChecked(True)
        existing = self.existing_radio.isChecked()
        self.session_combo.setEnabled(existing)
        self.tags_edit.setEnabled(not existing)
        self.desc_edit.setEnabled(not existing)
        ok = self.buttons.button(QtWidgets.QDialogButtonBox.Ok)
        ok.setEnabled((not existing) or has_targets)

    def spec(self) -> ImportSpec:
        origin = self.origin_edit.text().strip()
        if self.existing_radio.isChecked():
            run_id = self.session_combo.currentData()
            return ImportSpec(
                mode="existing", run_id=run_id, origin=origin,
                allow_frozen=run_id in self._frozen_ids,
            )
        tags = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]
        return ImportSpec(
            mode="new", tags=tags,
            description=self.desc_edit.text().strip(), origin=origin,
        )
