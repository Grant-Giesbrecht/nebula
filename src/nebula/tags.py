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
    """Count how often each tag is used across an archive.

    A not-yet-existing archive root just yields an empty Counter (so a
    brand-new archive still works -- the user simply starts with no tags
    to choose from), and a single unreadable session.yaml is skipped
    rather than sinking the whole listing.

    Args:
        archive: The archive to scan, following the same resolution rule
            as nebula.session(): a str is looked up as a registered
            archive name, a Path is used literally.

    Returns:
        A Counter mapping every tag used anywhere in the archive to the
        number of sessions carrying it.
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
  (tags marked "always" are added by the script and can't be removed)
  /done  /d  (or Enter on an empty line)   finish and return
  /help  /h  /?        show this help
TAB completes existing tag names (if your terminal supports it)."""


def _split_tags(text: str) -> List[str]:
    """Split a comma-separated line into individual tags.

    Args:
        text: A raw input line, e.g. "warmup, RP23D".

    Returns:
        The trimmed, non-empty tags in the order they were written.
    """
    return [t.strip() for t in text.split(",") if t.strip()]


def _highlight(tag: str, query: str, color: bool) -> str:
    """Render a tag name for display, picking out a search match.

    Args:
        tag: The tag name to render.
        query: The substring to highlight; "" highlights nothing.
        color: Whether ANSI colour is enabled for the target stream.

    Returns:
        The tag in cyan, with the matched `query` substring (if any)
        picked out in bold yellow so search hits jump out.
    """
    return _termui_highlight(tag, query, "cyan", color)


def _print_tag_table(pairs, selected, *, query: str = "", file=None) -> None:
    """Print an aligned table of tags and their session counts.

    Already-selected tags are marked with a `*`, and in a real terminal
    the names are coloured with the search query highlighted. Padding is
    computed from the raw tag length -- the coloured string carries
    invisible escape codes that would throw off :<width> alignment.

    Args:
        pairs: Iterable of (tag, count) to display.
        selected: Tags to mark as already chosen.
        query: Substring to highlight in each name; "" highlights nothing.
        file: Stream to print to. Resolved to sys.stdout at call time, not
            import time, so a replaced stream (pytest's capsys,
            redirect_stdout, ...) is honoured.
    """
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
    """Order tags for display.

    Args:
        counter: Tag -> session-count mapping, as from collect_tags().

    Returns:
        A list of (tag, count) sorted most-used first, ties broken
        alphabetically -- the tags a user is most likely to want are the
        ones already used a lot.
    """
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


# Slash commands offered for TAB completion inside input_tag.
_TAG_COMMANDS = ["/list", "/search", "/remove", "/clear", "/done", "/help"]


def _require_tag_list(value, param: str) -> List[str]:
    """Normalise a caller-supplied tag list, rejecting a bare string.

    A bare string is almost always a caller who typed
    param="ruby, twpa" expecting the same comma-separated parsing the
    interactive prompt itself uses -- but dict.fromkeys() below would
    iterate it character by character instead, silently turning that into
    single-letter "tags". Loud beats silent here: the fix is trivial once
    it's visible.

    Args:
        value: The list of tags to check, or None for "no tags".
        param: The caller's parameter name, used in the error message.

    Returns:
        The tags de-duplicated with their order intact ([] for None).

    Raises:
        TypeError: If `value` is a string rather than a list of strings.
    """
    if isinstance(value, str):
        raise TypeError(
            f"{param} must be a list of tag strings, not a single string "
            f"({value!r}). Split it first, e.g. "
            f"{param}={_split_tags(value)!r}."
        )
    # dict.fromkeys keeps insertion order while dropping duplicates.
    return list(dict.fromkeys(value or []))


def input_tag(
    archive: "str | object",
    *,
    prompt: str = "tags",
    initial: Optional[List[str]] = None,
    fixed: Optional[List[str]] = None,
) -> List[str]:
    """Interactively collect a list of tags for a session.

    Shows the user what tags already exist in `archive` so they reuse them
    instead of inventing near-duplicates. A friendlier stand-in for:

        tags = [t.strip() for t in input("Tags: ").split(",") if t.strip()]

    Type tags (comma-separated) to add them; use /list and /search to
    browse existing tags; TAB-complete tag names; press Enter on an empty
    line (or /done) to finish.

    Args:
        archive: The archive whose existing tags are offered for reuse,
            resolved as in collect_tags().
        prompt: Label shown at the start of the input line.
        initial: Tags to pre-fill the selection with. They start out
            chosen but the user is free to /remove or /clear them.
        fixed: Tags that are always part of the result no matter what the
            user types -- the opposite of `initial`. They are disclosed up
            front, shown in the prompt as "always: ...", and refused by
            /remove and /clear. Use it when the calling script is
            classifying the run itself (a sweep name, an instrument id)
            and the answer isn't the user's to change.

    Returns:
        `fixed` first, then the tags the user selected in the order they
        were added, de-duplicated. Non-interactive callers (no TTY /
        piped-closed stdin) get back `fixed` + `initial` rather than an
        error, so the same script runs unattended.

    Raises:
        TypeError: If `initial` or `fixed` is a string rather than a list
            of strings.
    """
    fixed_tags = _require_tag_list(fixed, "fixed")
    # A tag that's fixed is already accounted for; keeping it out of
    # `selected` means /remove and /clear can't quietly drop it.
    selected: List[str] = [t for t in _require_tag_list(initial, "initial")
                           if t not in fixed_tags]

    existing = collect_tags(archive)

    color = _color_enabled(sys.stdout)
    print(
        _paint(
            f"Enter tags. {len(existing)} tag(s) already in this archive. "
            f"Type /help for commands, /list to browse.",
            "dim",
            color,
        )
    )
    if fixed_tags:
        names = ", ".join(_paint(t, "cyan", color) for t in fixed_tags)
        print(
            _paint("Always added by this script: ", "dim", color)
            + names
            + _paint(" -- these can't be removed here.", "dim", color)
        )

    restore_completer, have_readline = _install_completer(sorted(existing) + _TAG_COMMANDS)
    try:
        while True:
            try:
                line = input(
                    _format_prompt(prompt, fixed_tags, selected, color, have_readline)
                ).strip()
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
                    _print_tag_table(_sorted_tags(existing), fixed_tags + selected)
                elif cmd in ("/search", "/s"):
                    q = arg.lower()
                    hits = [(t, c) for t, c in _sorted_tags(existing) if q in t.lower()]
                    _print_tag_table(hits, fixed_tags + selected, query=arg)
                elif cmd in ("/remove", "/rm"):
                    for t in _split_tags(arg):
                        if t in selected:
                            selected.remove(t)
                        elif t in fixed_tags:
                            print(_paint(
                                f"  ({t!r} is always added -- can't remove it)",
                                "yellow", color))
                        else:
                            print(_paint(f"  (not selected: {t!r})", "yellow", color))
                elif cmd == "/clear":
                    selected.clear()
                    if fixed_tags:
                        print(_paint(
                            "  (always-added tags kept: "
                            + ", ".join(repr(t) for t in fixed_tags) + ")",
                            "yellow", color))
                else:
                    print(_paint(f"  unknown command {cmd!r} -- try /help", "red", color))
                continue

            # A plain line: add its tags. Flag ones that don't already
            # exist in the archive so a typo is visible before it's saved,
            # but still allow it (new tags are legitimate).
            for tag in _split_tags(line):
                if tag in selected:
                    continue
                if tag in fixed_tags:
                    print(_paint(f"  ({tag!r} is already always added)", "dim", color))
                    continue
                if tag not in existing:
                    print(_paint(f"  + {tag!r} (new tag)", "green", color))
                selected.append(tag)
    finally:
        restore_completer()

    return fixed_tags + selected


def _format_prompt(prompt: str, fixed: List[str], selected: List[str],
                   color: bool, guard: bool) -> str:
    """Build the input() prompt showing the current selection.

    Args:
        prompt: Label shown at the start of the line.
        fixed: Always-added tags, shown ahead of the selection as
            `always: sweep7 |` so the user can see at a glance what
            they're getting on top of their own choices.
        selected: The tags chosen so far; "empty" is shown when there are
            none yet.
        color: Whether ANSI colour is enabled for the target stream.
        guard: Whether to wrap colour codes for readline, so the cursor
            stays aligned.

    Returns:
        The prompt string, e.g. `tags [always: sweep7 | warmup, RP23D]> `.
    """
    if selected:
        inner = ", ".join(_paint(t, "green", color, guard=guard) for t in selected)
    else:
        inner = _paint("empty", "dim", color, guard=guard)
    if fixed:
        always = ", ".join(_paint(t, "cyan", color, guard=guard) for t in fixed)
        inner = (_paint("always: ", "dim", color, guard=guard) + always
                 + _paint(" | ", "dim", color, guard=guard) + inner)
    lb = _paint("[", "dim", color, guard=guard)
    rb = _paint("]", "dim", color, guard=guard)
    return f"{prompt} {lb}{inner}{rb}> "
