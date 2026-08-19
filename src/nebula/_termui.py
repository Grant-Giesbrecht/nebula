"""
Shared terminal-UI helpers for nebula's interactive pickers.

Both the tag picker (tags.py) and the session picker (session_select.py)
want the same things: ANSI colour that switches itself off when output
isn't a real terminal, a search-match highlighter, and TAB completion
that actually works on macOS's libedit-backed readline. They live here so
the two pickers behave identically and the libedit workaround has exactly
one home.
"""

from __future__ import annotations

import os
import sys
from typing import Iterable, Tuple


# ---------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------

ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    # 256-colour: there is no ANSI-16 orange, and yellow is already used
    # for search highlights, so warnings get their own code rather than
    # reusing (and overloading the meaning of) an existing one.
    "orange": "\033[38;5;208m",
}

# readline needs non-printing sequences in a prompt wrapped in these
# markers, or it miscounts the prompt width and corrupts the cursor
# position on the line the user is editing.
RL_START = "\001"
RL_END = "\002"


def color_enabled(file) -> bool:
    """Colour only a real terminal, and honour the NO_COLOR convention
    (https://no-color.org) so piped/redirected output and CI stay clean.
    This also means test capture (not a tty) sees plain text."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    try:
        return bool(file.isatty())
    except Exception:
        return False


def paint(text: str, style: str, enabled: bool, *, guard: bool = False) -> str:
    """Wrap text in the ANSI codes named in `style` (space-separated) when
    `enabled`. `guard=True` additionally brackets the codes in readline's
    non-printing markers, for use inside an input() prompt."""
    if not enabled or not style:
        return text
    codes = "".join(ANSI[s] for s in style.split())
    reset = ANSI["reset"]
    if guard:
        return f"{RL_START}{codes}{RL_END}{text}{RL_START}{reset}{RL_END}"
    return f"{codes}{text}{reset}"


def highlight(
    text: str,
    query: str,
    base_style: str,
    enabled: bool,
    *,
    match_style: str = "yellow bold",
) -> str:
    """Render `text` in `base_style`, with the first case-insensitive
    occurrence of `query` picked out in `match_style` so search hits jump
    out. An empty query (or no match) just paints the whole thing."""
    if not query:
        return paint(text, base_style, enabled)
    idx = text.lower().find(query.lower())
    if idx < 0:
        return paint(text, base_style, enabled)
    before, match, after = text[:idx], text[idx : idx + len(query)], text[idx + len(query):]
    return (
        paint(before, base_style, enabled)
        + paint(match, match_style, enabled)
        + paint(after, base_style, enabled)
    )


# ---------------------------------------------------------------------
# CLI-command output: errors, warnings, and picking a word out of a line
# ---------------------------------------------------------------------
# The interactive pickers above colour whole lines; a plain `nebula`
# subcommand instead prints one message at a time and wants a quick way to
# mark it as a failure, a caution, or just call out one word (an archive's
# name, a run id) in an otherwise plain line. These three cover that
# without every command hand-rolling its own ANSI.

def err(msg: str, *, file=None) -> None:
    """Print an error to stderr in red. Colour is skipped automatically
    when stderr isn't a terminal or NO_COLOR is set (see color_enabled),
    so piped/redirected output and CI logs stay plain text."""
    f = file if file is not None else sys.stderr
    print(paint(str(msg), "bold red", color_enabled(f)), file=f)


def warn(msg: str, *, file=None) -> None:
    """Print a non-fatal caution to stderr in orange -- something worth
    noticing but that did not stop the command."""
    f = file if file is not None else sys.stderr
    print(paint(str(msg), "orange", color_enabled(f)), file=f)


def ok(msg: str, *, file=None) -> None:
    """Print a success confirmation to stdout in green."""
    f = file if file is not None else sys.stdout
    print(paint(str(msg), "green", color_enabled(f)), file=f)


def hl(text: str, *, file=None) -> str:
    """Bold+cyan for picking one word out of an otherwise plain line -- an
    archive's name, a run id -- the same role `highlight()` above plays
    for a search match, but for a fixed word rather than a query hit."""
    f = file if file is not None else sys.stdout
    return paint(str(text), "bold cyan", color_enabled(f))


# ---------------------------------------------------------------------
# Line editing / completion
# ---------------------------------------------------------------------

def is_interactive() -> bool:
    """True only when a human can actually answer -- both stdin and stdout
    are terminals. Batch/cron/piped runs come back False so callers can
    fall back to a non-interactive default instead of blocking on input()."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def install_completer(options: Iterable[str]) -> Tuple["object", bool]:
    """Wire up TAB completion against `options`, if readline is available.
    Returns (restore, have_readline): a zero-arg restore callback (a no-op
    when readline is missing) so callers can put the terminal's completer
    back the way they found it, and a bool for whether line editing is
    actually active."""
    try:
        import readline
    except ImportError:
        return (lambda: None), False  # Windows / no-readline build: skip

    opts = list(options)

    def complete(text, state):
        token = text.strip()
        matches = opts if not token else [o for o in opts if o.startswith(token)]
        return matches[state] if state < len(matches) else None

    prev_completer = readline.get_completer()
    prev_delims = readline.get_completer_delims()
    readline.set_completer(complete)
    # Split words only on whitespace and commas, so tokens containing '-'
    # or '_' (e.g. "warm-up", "S-0007") complete as a single unit rather
    # than breaking at the punctuation, which the default delimiter set
    # would do.
    readline.set_completer_delims(" \t\n,")
    # macOS ships a libedit-backed readline (this is what iTerm2, Terminal,
    # etc. all use); libedit ignores GNU readline's "tab: complete" and
    # needs its own bind syntax, so TAB does nothing unless we detect it
    # and bind the libedit way.
    if "libedit" in (getattr(readline, "__doc__", "") or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")

    def restore():
        readline.set_completer(prev_completer)
        readline.set_completer_delims(prev_delims)

    return restore, True
