"""
Interactive CLI session picker.

When a measurement script opens a session without saying which one, this
module asks: append to a session you already have going, or start a new
one? It lists the sessions worth appending to -- everything created today,
plus anything still marked OPEN from an earlier day -- and lets the user
filter them with the same /list, /search slash-command style as the tag
picker (tags.py).

The model: a session's promise is the *date it started*, not that only one
script ever wrote to it. So /open (or just typing the id) appends to any
same-day session -- even one a previous script already closed -- letting
you gather many related measurements under one folder. The only thing kept
frozen is a session CLOSED ON A PREVIOUS DAY; writing to one of those needs
a deliberate /reopen <id> --force, so you can't silently rewrite last
week's record by reflex.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from typing import List, Optional

from nebula._termui import (
    color_enabled,
    highlight,
    install_completer,
    is_interactive,
    paint,
)
from nebula.index import _iter_session_dirs
from nebula.registry import resolve_archive
from nebula.session import Session, append_to, new, reopen
from nebula.session import _DEFAULT_MISSING_META, _hold_active
from nebula.sidecar import SessionMeta, read_session_yaml
from nebula.tags import collect_tags, input_tag, _print_tag_table, _sorted_tags

_STATUS_STYLE = {"open": "green bold", "closed": "dim", "crashed": "red"}

_COMMANDS = ["/list", "/search", "/tags", "/open", "/reopen", "/new", "/help", "/cancel"]

_HELP = """\
commands:
  <run-id>             append to that session (any session from today, or
                       one still OPEN from an earlier day)
  /open <run-id>  /o   same as typing the id
  /new [description]   start a fresh session (optionally set its description)
  /list  /l [flags]    list the selectable sessions. flags:
                         -t / --tags     also show each session's tags
                         -a / --all      list every session, not just
                                         today's/open ones
                         -d / --days N   list all sessions from the last N days
  /search <text>  /s   filter selectable sessions by id, tags, or description
  /tags [text]         list all tags in the archive (optionally filtered)
  /reopen <id> --force write into a session CLOSED on a previous day (needs
                       --force, or you'll be asked to re-type the id)
  /cancel  /q          abort without choosing
  /help  /h  /?        show this help
TAB completes session ids and commands (if your terminal supports it)."""


def _candidate_sessions(archive_root: Path) -> List[SessionMeta]:
    """Sessions eligible to append to: created today, still open regardless
    of date, OR held (see session.hold) so cross-midnight work stays
    reachable. Read straight from session.yaml (the source of truth) so a
    session you opened moments ago shows up without an index rebuild.
    Newest first."""
    today = datetime.date.today().isoformat()
    out: List[SessionMeta] = []
    for session_dir in _iter_session_dirs(archive_root):
        try:
            meta = read_session_yaml(session_dir)
        except Exception:
            continue
        if (meta.status == "open"
                or (meta.created or "")[:10] == today
                or _hold_active(meta)):
            out.append(meta)
    out.sort(key=lambda m: m.created or "", reverse=True)
    return out


def _all_sessions(archive_root: Path) -> List[SessionMeta]:
    """Every session in the archive, newest first -- the superset /list -a
    and /list -d draw from."""
    out: List[SessionMeta] = []
    for session_dir in _iter_session_dirs(archive_root):
        try:
            out.append(read_session_yaml(session_dir))
        except Exception:
            continue
    out.sort(key=lambda m: m.created or "", reverse=True)
    return out


def _within_days(metas: List[SessionMeta], days: int) -> List[SessionMeta]:
    """Keep sessions created within the last `days` days (0 = today only)."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    return [m for m in metas if (m.created or "")[:10] >= cutoff]


def _matches(meta: SessionMeta, query: str) -> bool:
    q = query.lower()
    return (
        q in meta.run_id.lower()
        or q in (meta.description or "").lower()
        or any(q in t.lower() for t in meta.tags)
    )


def _int_or_none(text: str) -> Optional[int]:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _parse_list_flags(tokens: List[str]):
    """Parse the flags after /list. Returns ((show_tags, show_all, days),
    None) on success or (None, error_message) on a bad flag."""
    show_tags = show_all = False
    days: Optional[int] = None
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-t", "--tags"):
            show_tags = True
        elif t in ("-a", "--all"):
            show_all = True
        elif t in ("-d", "--days"):
            i += 1
            if i >= len(tokens):
                return None, "  /list --days needs a number, e.g. /list -d 7"
            days = _int_or_none(tokens[i])
            if days is None:
                return None, f"  /list --days: {tokens[i]!r} is not a number"
        elif t.startswith("--days="):
            days = _int_or_none(t.split("=", 1)[1])
            if days is None:
                return None, f"  /list --days: {t.split('=', 1)[1]!r} is not a number"
        else:
            return None, f"  unknown /list flag {t!r} (try -t, -a, -d N)"
        i += 1
    return (show_tags, show_all, days), None


def _print_session_table(
    metas: List[SessionMeta], *, query: str = "", show_tags: bool = False, file=None
) -> None:
    if file is None:
        file = sys.stdout
    color = color_enabled(file)
    if not metas:
        print(paint("  (no matching sessions)", "dim", color), file=file)
        return
    idw = max(len(m.run_id) for m in metas)
    for m in metas:
        rid = highlight(m.run_id, query, "cyan bold", color)
        idpad = " " * (idw - len(m.run_id))
        status = paint(f"{m.status:<7}", _STATUS_STYLE.get(m.status, ""), color)
        when = paint((m.created or "")[:16], "dim", color)
        held = paint(" HELD", "yellow bold", color) if _hold_active(m) else ""
        desc = highlight(m.description or "", query, "", color)
        print(f"  {rid}{idpad}  {status}  {when}{held}  {desc}", file=file)
        if show_tags:
            if m.tags:
                shown = ", ".join(highlight(t, query, "cyan", color) for t in m.tags)
            else:
                shown = paint("(none)", "dim", color)
            print(f"        tags: {shown}", file=file)


def _prompt(color: bool, guard: bool) -> str:
    arrow = paint("session>", "bold", color, guard=guard)
    return f"{arrow} "


def ask_new_session_metadata(
    archive: "str | Path",
    *,
    tags: Optional[List[str]] = None,
    description: Optional[str] = None,
    ask: Optional[bool] = None,
) -> "tuple[Optional[List[str]], str]":
    """Collect a new session's tags and description, at the moment we know
    a new session is actually being made.

    This is the whole point of the argument's existence. Asking *before*
    the picker runs -- which is what a script passing tags= to
    nebula.session() does -- means answering a question that only matters
    in one of the two outcomes: pick an existing session and the answers
    are thrown away, silently, having already cost the user the typing.
    Asked here, the question is only ever put when it has an effect.

    `ask` is tri-state, matching nebula.session():
      None  -- ask for whichever of the two was not supplied (the default);
      True  -- ask for both, pre-filling anything supplied;
      False -- never ask, take what was given.

    Non-interactive runs never ask, so an unattended script behaves as it
    always did.
    """
    if ask is False or not is_interactive():
        return tags, description or ""

    color = color_enabled(sys.stdout)
    print(paint("New session -- describe it (Enter to skip either).",
                "dim", color))

    if ask or tags is None:
        tags = input_tag(archive, prompt="session tags", initial=tags)
    if ask or not description:
        try:
            answer = input("session description> ").strip()
        except EOFError:
            print()
            answer = ""
        description = answer or description or ""
    return tags, description or ""


def _note_ignored_metadata(run_id: str, tags, description, color: bool) -> None:
    """Say so when tags/description were supplied but the user picked an
    existing session, which already has its own.

    Silence here is the bug this function exists for: the values simply
    vanished, and nothing said the session was not tagged the way the
    script's author believed it was.
    """
    supplied = []
    if tags:
        supplied.append(f"tags {', '.join(tags)}")
    if description:
        supplied.append("a description")
    if not supplied:
        return
    print(paint(
        f"  note: {run_id} already has its own tags and description, so "
        f"the {' and '.join(supplied)} passed to nebula.session() were not "
        f"applied. Use sess.annotate(tags=[...]) to add them anyway.",
        "yellow", color))


def select_session(
    archive: "str | Path",
    *,
    tags: Optional[List[str]] = None,
    description: str = "",
    archive_name: Optional[str] = None,
    on_missing_meta: str = _DEFAULT_MISSING_META,
    announce: bool = True,
    ask: Optional[bool] = None,
    artifact_tags: Optional[List[str]] = None,
) -> Session:
    """Interactively choose a session to write into, returning an open
    Session. Used by nebula.session() when no run_id is given and the
    caller didn't ask for an automatic new session.

    Tags and a description are asked for *after* the choice, and only when
    the choice was to start a new session -- see
    `ask_new_session_metadata`. Pass ask=False to keep the old behaviour
    of using whatever the caller supplied without prompting.

    In a non-interactive context (batch/cron/piped) there's no one to ask,
    so this quietly creates a new session -- the same thing the old default
    did -- rather than blocking on input() forever.
    """
    archive_root, _ = resolve_archive(archive)

    def _new(desc: str) -> Session:
        chosen_tags, chosen_desc = ask_new_session_metadata(
            archive, tags=tags, description=desc, ask=ask)
        return new(
            archive,
            tags=chosen_tags,
            description=chosen_desc,
            archive_name=archive_name,
            on_missing_meta=on_missing_meta,
            announce=announce,
            artifact_tags=artifact_tags,
        )

    if not is_interactive():
        return _new(description)

    candidates = _candidate_sessions(archive_root)
    color = color_enabled(sys.stdout)
    print(
        paint(
            f"Select a session ({len(candidates)} open/today). Type a "
            f"run-id to append, /new for a fresh one, /help for commands.",
            "dim",
            color,
        )
    )
    _print_session_table(candidates, file=sys.stdout)

    ids = [m.run_id for m in candidates]
    restore, have_rl = install_completer(ids + _COMMANDS)
    try:
        while True:
            try:
                line = input(_prompt(color, have_rl)).strip()
            except EOFError:
                # stdin closed mid-prompt: don't guess -- abort loudly.
                print()
                raise RuntimeError("session selection aborted (end of input)")

            if not line:
                continue

            if not line.startswith("/"):
                # A bare token is a shortcut for /open <id>.
                s = _try_open(archive, line.split()[0], color,
                              announce=announce, archive_name=archive_name,
                              on_missing_meta=on_missing_meta,
                              artifact_tags=artifact_tags,
                              supplied_tags=tags,
                              supplied_description=description)
                if s is not None:
                    return s
                continue

            parts = line.split()
            cmd = parts[0]
            rest = parts[1:]

            if cmd in ("/help", "/h", "/?"):
                print(paint(_HELP, "dim", color))
            elif cmd in ("/list", "/l"):
                opts, err = _parse_list_flags(rest)
                if err is not None:
                    print(paint(err, "yellow", color))
                    continue
                show_tags, show_all, days = opts
                if show_all or days is not None:
                    metas = _all_sessions(archive_root)
                    if days is not None:
                        metas = _within_days(metas, days)
                else:
                    metas = candidates
                _print_session_table(metas, show_tags=show_tags, file=sys.stdout)
            elif cmd in ("/search", "/s"):
                q = line.split(maxsplit=1)[1].strip() if len(parts) > 1 else ""
                hits = [m for m in candidates if _matches(m, q)]
                # Show tags too, since a hit may match on a tag we'd
                # otherwise not display.
                _print_session_table(hits, query=q, show_tags=True, file=sys.stdout)
            elif cmd in ("/tags", "/tag"):
                q = line.split(maxsplit=1)[1].strip() if len(parts) > 1 else ""
                counts = collect_tags(archive)
                pairs = _sorted_tags(counts)
                if q:
                    pairs = [(t, c) for t, c in pairs if q.lower() in t.lower()]
                if not pairs:
                    print(paint("  (no matching tags)", "dim", color))
                else:
                    _print_tag_table(pairs, selected=(), query=q, file=sys.stdout)
            elif cmd in ("/new", "/n"):
                desc = line.split(maxsplit=1)[1].strip() if len(parts) > 1 else description
                return _new(desc)
            elif cmd in ("/open", "/o"):
                if not rest:
                    print(paint("  usage: /open <run-id>", "yellow", color))
                    continue
                s = _try_open(archive, rest[0], color, announce=announce,
                              archive_name=archive_name,
                              on_missing_meta=on_missing_meta,
                              artifact_tags=artifact_tags,
                              supplied_tags=tags,
                              supplied_description=description)
                if s is not None:
                    return s
            elif cmd == "/reopen":
                s = _try_reopen(archive, rest, color, announce=announce,
                                archive_name=archive_name,
                                on_missing_meta=on_missing_meta,
                                artifact_tags=artifact_tags,
                                supplied_tags=tags,
                                supplied_description=description)
                if s is not None:
                    return s
            elif cmd in ("/cancel", "/q"):
                raise RuntimeError("session selection cancelled")
            else:
                print(paint(f"  unknown command {cmd!r} -- try /help", "red", color))
    finally:
        restore()


def _try_open(archive, run_id, color, *, archive_name, on_missing_meta,
              announce: bool = True, artifact_tags=None,
              supplied_tags=None, supplied_description="") -> Optional[Session]:
    """Append to a same-day-or-open session, or explain why we can't."""
    try:
        s = append_to(archive, run_id, announce=announce,
                      archive_name=archive_name,
                      on_missing_meta=on_missing_meta,
                      artifact_tags=artifact_tags)
        _note_ignored_metadata(s.id, supplied_tags, supplied_description, color)
        return s
    except FileNotFoundError:
        print(paint(f"  no session {run_id!r} in this archive", "red", color))
    except RuntimeError:
        # append_to only refuses sessions closed on a previous day.
        print(paint(
            f"  session {run_id!r} was closed on an earlier day -- use "
            f"/reopen {run_id} --force to write into it anyway", "yellow", color))
    return None


def _try_reopen(archive, tokens, color, *, archive_name, on_missing_meta,
                announce: bool = True, artifact_tags=None,
                supplied_tags=None, supplied_description="") -> Optional[Session]:
    """Force-reopen a closed session, gated behind --force or a typed
    confirmation so it can't happen by reflex."""
    forced = any(t in ("--force", "-f", "!") for t in tokens)
    ids = [t for t in tokens if not t.startswith("-") and t != "!"]
    if not ids:
        print(paint("  usage: /reopen <run-id> [--force]", "yellow", color))
        return None
    run_id = ids[0]

    if not forced:
        print(paint(
            f"  {run_id} was closed on a previous day; writing to it "
            f"overrides that freeze.", "yellow", color))
        try:
            confirm = input(f"  re-type {run_id} to confirm (anything else cancels): ").strip()
        except EOFError:
            print()
            confirm = ""
        if confirm != run_id:
            print(paint("  reopen cancelled", "dim", color))
            return None

    try:
        s = reopen(archive, run_id, announce=announce,
                   archive_name=archive_name,
                   on_missing_meta=on_missing_meta,
                   artifact_tags=artifact_tags)
        _note_ignored_metadata(s.id, supplied_tags, supplied_description, color)
        return s
    except FileNotFoundError:
        print(paint(f"  no session {run_id!r} in this archive", "red", color))
        return None
