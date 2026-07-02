"""
Tag discovery and interactive tag entry for measurement scripts.

The pain this solves: when a script asks "what tags do you want?", the
user has no way to see which tags already exist, so they fat-finger
"warmup" as "warm-up" as "warm_up" and the archive fragments. input_tag()
is a drop-in replacement for input() that lets the user browse and search
the archive's existing tags (/list, /search) and TAB-complete them, so
they reuse an existing tag instead of inventing a near-duplicate.

Tags are read straight from the session.yaml files (the source of truth),
not the SQLite index -- so a tag you created five minutes ago shows up
without anyone having to rebuild the index first.
"""

from __future__ import annotations

import sys
from collections import Counter
from typing import List, Optional

from nebula._termui import (
    color_enabled as _color_enabled,
    highlight as _termui_highlight,
    install_completer as _install_completer,
    paint as _paint,
)
from nebula.index import _iter_session_dirs
from nebula.registry import resolve_archive
from nebula.sidecar import read_session_yaml


def collect_tags(archive: "str | object") -> Counter:
    """Return a Counter mapping every tag used anywhere in the archive to
    the number of sessions carrying it.

    `archive` follows the same resolution rule as nebula.session(): a str
    is looked up as a registered archive name, a Path is used literally.
    A not-yet-existing archive root just yields an empty Counter (so a
    brand-new archive still works -- the user simply starts with no tags
    to choose from).
    """
    archive_root, _ = resolve_archive(archive)
    counter: Counter = Counter()
    for session_dir in _iter_session_dirs(archive_root):
        try:
            meta = read_session_yaml(session_dir)
        except Exception:
            # A single unreadable/half-written session.yaml shouldn't sink
            # the whole tag listing -- skip it and carry on.
            continue
        for tag in meta.tags:
            counter[tag] += 1
    return counter


# ---------------------------------------------------------------------
# Interactive entry
# ---------------------------------------------------------------------

_HELP = """\
commands:
  <tag>[, <tag> ...]   add one or more tags (comma-separated)
  /list  /l            list all tags already in the archive
  /search <text>  /s   list existing tags containing <text>
  /remove <tag>  /rm   drop a tag from your current selection
  /clear               clear the whole current selection
  /done  /d  (or Enter on an empty line)   finish and return
  /help  /h  /?        show this help
TAB completes existing tag names (if your terminal supports it)."""


def _split_tags(text: str) -> List[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


def _highlight(tag: str, query: str, color: bool) -> str:
    """Render a tag name in cyan, with the matched `query` substring (if
    any) picked out in bold yellow so search hits jump out."""
    return _termui_highlight(tag, query, "cyan", color)


def _print_tag_table(pairs, selected, *, query: str = "", file=None) -> None:
    """pairs: iterable of (tag, count). Marks already-selected tags and, in
    a real terminal, colours the names (highlighting the search query)."""
    # Resolve sys.stdout at call time, not import time, so a replaced
    # stream (pytest's capsys, redirect_stdout, ...) is honoured.
    if file is None:
        file = sys.stdout
    color = _color_enabled(file)
    if not pairs:
        print(_paint("  (no matching tags)", "dim", color), file=file)
        return
    width = max(len(t) for t, _ in pairs)
    for tag, count in pairs:
        mark = _paint("*", "green bold", color) if tag in selected else " "
        # Pad from the raw tag length -- the coloured string carries
        # invisible escape codes that would throw off :<width> alignment.
        pad = " " * (width - len(tag))
        name = _highlight(tag, query, color)
        noun = "session" if count == 1 else "sessions"
        count_str = _paint(f"({count} {noun})", "dim", color)
        print(f"  {mark} {name}{pad}  {count_str}", file=file)


def _sorted_tags(counter: Counter):
    # Most-used first, ties broken alphabetically -- the tags a user is
    # most likely to want are the ones already used a lot.
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


# Slash commands offered for TAB completion inside input_tag.
_TAG_COMMANDS = ["/list", "/search", "/remove", "/clear", "/done", "/help"]


def input_tag(
    archive: "str | object",
    *,
    prompt: str = "tags",
    initial: Optional[List[str]] = None,
) -> List[str]:
    """Interactively collect a list of tags, showing the user what tags
    already exist in `archive` so they reuse them instead of inventing
    near-duplicates. A friendlier stand-in for:

        tags = [t.strip() for t in input("Tags: ").split(",") if t.strip()]

    Type tags (comma-separated) to add them; use /list and /search to
    browse existing tags; TAB-complete tag names; press Enter on an empty
    line (or /done) to finish. Returns the selected tags in the order they
    were added, de-duplicated.

    Non-interactive callers (no TTY / piped-closed stdin) get back
    `initial` unchanged rather than an error, so the same script runs
    unattended.
    """
    existing = collect_tags(archive)
    # dict.fromkeys keeps insertion order while dropping duplicates.
    selected: List[str] = list(dict.fromkeys(initial or []))

    color = _color_enabled(sys.stdout)
    print(
        _paint(
            f"Enter tags. {len(existing)} tag(s) already in this archive. "
            f"Type /help for commands, /list to browse.",
            "dim",
            color,
        )
    )

    restore_completer, have_readline = _install_completer(sorted(existing) + _TAG_COMMANDS)
    try:
        while True:
            try:
                line = input(_format_prompt(prompt, selected, color, have_readline)).strip()
            except EOFError:
                # Piped/closed stdin (e.g. an unattended run): take what we
                # have and stop rather than blowing up.
                print()
                break

            if not line or line in ("/done", "/d"):
                break

            if line.startswith("/"):
                parts = line.split(maxsplit=1)
                cmd = parts[0]
                arg = parts[1].strip() if len(parts) > 1 else ""

                if cmd in ("/help", "/h", "/?"):
                    print(_paint(_HELP, "dim", color))
                elif cmd in ("/list", "/l"):
                    _print_tag_table(_sorted_tags(existing), selected)
                elif cmd in ("/search", "/s"):
                    q = arg.lower()
                    hits = [(t, c) for t, c in _sorted_tags(existing) if q in t.lower()]
                    _print_tag_table(hits, selected, query=arg)
                elif cmd in ("/remove", "/rm"):
                    for t in _split_tags(arg):
                        if t in selected:
                            selected.remove(t)
                        else:
                            print(_paint(f"  (not selected: {t!r})", "yellow", color))
                elif cmd == "/clear":
                    selected.clear()
                else:
                    print(_paint(f"  unknown command {cmd!r} -- try /help", "red", color))
                continue

            # A plain line: add its tags. Flag ones that don't already
            # exist in the archive so a typo is visible before it's saved,
            # but still allow it (new tags are legitimate).
            for tag in _split_tags(line):
                if tag in selected:
                    continue
                if tag not in existing:
                    print(_paint(f"  + {tag!r} (new tag)", "green", color))
                selected.append(tag)
    finally:
        restore_completer()

    return selected


def _format_prompt(prompt: str, selected: List[str], color: bool, guard: bool) -> str:
    """Build the input() prompt showing the current selection, e.g.
    `tags [warmup, RP23D]> `, with the selected tags coloured. `guard`
    wraps colour codes for readline so the cursor stays aligned."""
    if selected:
        inner = ", ".join(_paint(t, "green", color, guard=guard) for t in selected)
    else:
        inner = _paint("empty", "dim", color, guard=guard)
    lb = _paint("[", "dim", color, guard=guard)
    rb = _paint("]", "dim", color, guard=guard)
    return f"{prompt} {lb}{inner}{rb}> "
