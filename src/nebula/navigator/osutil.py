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


def reveal_path(path) -> bool:
    """Show a file *in* the file manager with it selected, rather than
    opening it. open_path on a file launches its application, which is not
    what "reveal" means; on a folder the two coincide."""
    target = Path(path)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", f"/select,{target}"])
        else:
            # No portable "select" on Linux; open the containing folder.
            subprocess.Popen(["xdg-open", str(target.parent if target.is_file() else target)])
    except OSError:
        return False
    return True


def open_url(url: str) -> bool:
    """Open a URL in the default browser. Separate from open_path because
    that one normalises through Path(), which mangles a URL ("https://x"
    becomes "https:/x")."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", url])
        elif sys.platform.startswith("win"):
            os.startfile(url)  # type: ignore[attr-defined]  # Windows-only
        else:
            subprocess.Popen(["xdg-open", url])
    except OSError:
        return False
    return True


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


#: Clipboard commands to try, in order, on a non-macOS/Windows platform --
#: `wl-copy` for Wayland, `xclip`/`xsel` for X11. All three read the text
#: from stdin, so one call shape covers them.
_LINUX_CLIPBOARD_COMMANDS = (
    ["wl-copy"],
    ["xclip", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--input"],
)


def copy_to_clipboard(text: str) -> bool:
    """Copy `text` to the system clipboard. Best-effort: returns False --
    never raises -- when nothing on this platform can reach it, most
    commonly a headless/SSH Linux session with no clipboard utility
    installed, so a caller (the CLI's `copy` command) can fall back to
    just printing the text for the user to select by hand."""
    if sys.platform == "darwin":
        candidates = (["pbcopy"],)
    elif sys.platform.startswith("win"):
        candidates = (["clip"],)
    else:
        candidates = _LINUX_CLIPBOARD_COMMANDS

    data = text.encode("utf-8")
    for cmd in candidates:
        try:
            proc = subprocess.run(cmd, input=data, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        except (OSError, FileNotFoundError):
            continue
        if proc.returncode == 0:
            return True
    return False
