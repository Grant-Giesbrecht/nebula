"""
Thin OS-integration layer for the Navigator: opening files and folders in
the platform's default handlers.

The Flet view runs as a desktop (Flutter) client with no Qt around, so this
uses only the standard library: the platform "open" command (``open`` on
macOS, ``start`` on Windows, ``xdg-open`` on Linux) does the "open this the
way a double-click would" job -- a folder opens in the file manager, a data
file opens in its default app, a .json sidecar in whatever handles JSON.
Isolated here so the view can call it by name and tests can monkeypatch it
(rather than launching real apps).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def file_manager_name() -> str:
    """The user-facing name of the platform file manager, for menu labels."""
    if sys.platform == "darwin":
        return "Finder"
    if sys.platform.startswith("win"):
        return "File Explorer"
    return "File Manager"


def open_path(path) -> bool:
    """Open a path with the OS default: a folder in the file manager, a file
    in its registered default application. Returns whether the launch was
    dispatched without raising."""
    target = str(Path(path))
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", target])
        elif sys.platform.startswith("win"):
            os.startfile(target)  # type: ignore[attr-defined]  # Windows-only
        else:
            subprocess.Popen(["xdg-open", target])
    except OSError:
        return False
    return True
