"""
Thin OS-integration layer for the Navigator: opening files and folders in
the platform's default handlers.

Everything goes through Qt's QDesktopServices, which is the cross-platform
way to "open this the way a double-click would" -- a folder opens in the
file manager, a data file opens in its default app, a .json sidecar opens in
whatever the OS has registered for JSON. Isolated here so the view can call
it by name and tests can monkeypatch it (rather than launching real apps).
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6 import QtCore, QtGui


def file_manager_name() -> str:
    """The user-facing name of the platform file manager, for menu labels."""
    if sys.platform == "darwin":
        return "Finder"
    if sys.platform.startswith("win"):
        return "File Explorer"
    return "File Manager"


def open_path(path) -> bool:
    """Open a path with the OS default: a folder in the file manager, a file
    in its registered default application. Returns whether Qt accepted it."""
    url = QtCore.QUrl.fromLocalFile(str(Path(path)))
    return QtGui.QDesktopServices.openUrl(url)
