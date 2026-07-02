"""
Interactive session picker (PyQt5).

This module is intentionally NOT imported by nebula/__init__.py -- PyQt5
is a heavy, optional dependency, and non-interactive scripts (cron jobs,
batch reprocessing) should never be forced to have a display available.
Import it explicitly when you want the GUI:

    from nebula.picker import pick_session

Design intent (per-process caching, today+open filter, explicit
new-session path) is implemented here; wire it up to a real Qt event loop
before use -- this is scaffolding, not a finished widget.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import List, Optional

from nebula.index import open_index
from nebula.session import Session, append_to, new

try:
    from PyQt5 import QtWidgets

    HAVE_QT = True
except ImportError:
    HAVE_QT = False


# Per-process cache: once a session is chosen for a given store, reuse it
# silently within the same process (e.g. repeated cells in a notebook)
# instead of popping the dialog again.
_process_session_cache: dict = {}


def _candidate_sessions(store_root: Path, tags: Optional[List[str]] = None):
    """Sessions eligible to append to: created today, OR still open
    regardless of date. Closed sessions from prior days are excluded --
    by policy they're immutable; reference them via related_runs instead."""
    conn = open_index(store_root)
    today = datetime.date.today().isoformat()
    rows = conn.execute(
        "SELECT run_id, created, status, tags, description FROM sessions "
        "WHERE status = 'open' OR substr(created, 1, 10) = ?",
        (today,),
    ).fetchall()
    conn.close()

    import json

    results = []
    for row in rows:
        row_tags = json.loads(row["tags"])
        if tags and not (set(tags) & set(row_tags)):
            continue
        results.append(row)
    return results


class SessionPickerDialog:
    """Thin wrapper around a QDialog. Kept separate from the Qt widget
    tree construction so the selection logic is testable without a
    display."""

    def __init__(self, store_root: Path, tags: Optional[List[str]] = None):
        if not HAVE_QT:
            raise RuntimeError(
                "PyQt5 is not installed. Install it, or bypass the picker "
                "by calling nebula.new(...) / nebula.append_to(...) with "
                "an explicit run_id."
            )
        self.store_root = Path(store_root)
        self.tags = tags
        self.candidates = _candidate_sessions(store_root, tags)
        self.result_run_id: Optional[str] = None
        self.create_new = False
        self.new_tags: List[str] = tags or []
        self.new_description = ""

    def exec_(self) -> Optional[str]:
        """Returns a run_id to append to, or None if the user chose to
        create a new session (self.create_new will be True in that case)
        or cancelled entirely (both create_new and result_run_id falsy)."""
        dialog = QtWidgets.QDialog()
        dialog.setWindowTitle("Nebula — choose a session")
        layout = QtWidgets.QVBoxLayout(dialog)

        label = QtWidgets.QLabel(
            "Append to an existing session, or start a new one:"
        )
        layout.addWidget(label)

        list_widget = QtWidgets.QListWidget()
        for row in self.candidates:
            item_text = f"{row['run_id']}  [{row['status']}]  {row['description']}"
            item = QtWidgets.QListWidgetItem(item_text)
            item.setData(1, row["run_id"])
            list_widget.addItem(item)
        layout.addWidget(list_widget)

        button_row = QtWidgets.QHBoxLayout()
        new_button = QtWidgets.QPushButton("New Session...")
        append_button = QtWidgets.QPushButton("Append")
        cancel_button = QtWidgets.QPushButton("Cancel")
        button_row.addWidget(new_button)
        button_row.addWidget(append_button)
        button_row.addWidget(cancel_button)
        layout.addLayout(button_row)

        def on_new():
            self.create_new = True
            dialog.accept()

        def on_append():
            item = list_widget.currentItem()
            if item is not None:
                self.result_run_id = item.data(1)
                dialog.accept()

        def on_cancel():
            dialog.reject()

        new_button.clicked.connect(on_new)
        append_button.clicked.connect(on_append)
        cancel_button.clicked.connect(on_cancel)

        dialog.exec_()
        return self.result_run_id


def pick_session(
    store_root: Path,
    *,
    tags: Optional[List[str]] = None,
    description: str = "",
    non_interactive_run_id: Optional[str] = None,
    store: Optional[str] = None,
) -> Session:
    """The main entry point scripts call.

    - If non_interactive_run_id is given, skips the GUI entirely and
      appends to that session (for scheduled/batch jobs).
    - If a session was already chosen earlier in this process for this
      store_root, reuse it silently (Jupyter-cell-rerun case).
    - Otherwise, pop the picker dialog.
    """
    cache_key = str(Path(store_root))

    if non_interactive_run_id:
        return append_to(store_root, non_interactive_run_id, store=store)

    if cache_key in _process_session_cache:
        cached_id = _process_session_cache[cache_key]
        return append_to(store_root, cached_id, store=store)

    if not HAVE_QT:
        # No display available and no explicit run_id -- fail loudly
        # rather than silently creating sessions nobody chose.
        raise RuntimeError(
            "No session specified and PyQt5 is unavailable. Pass "
            "non_interactive_run_id=... explicitly for non-interactive use."
        )

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    picker = SessionPickerDialog(store_root, tags=tags)
    chosen = picker.exec_()

    if picker.create_new:
        s = new(store_root, tags=tags, description=description, store=store)
    elif chosen:
        s = append_to(store_root, chosen, store=store)
    else:
        raise RuntimeError("session selection cancelled")

    _process_session_cache[cache_key] = s.id
    return s


def clear_session_cache() -> None:
    """Force the next pick_session() call to show the dialog again."""
    _process_session_cache.clear()
