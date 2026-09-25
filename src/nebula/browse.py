"""
Interactive `cd`/`ls`-style shell for browsing a nebula archive without the
Navigator GUI: `nebula browse [archive [run_id]]`.

Mirrors session_select.py's REPL pattern (install_completer + an input()
loop + bare-word dispatch), but the vocabulary is directory verbs (cd, ls,
pwd) plus nebula's own show/info/uri/annotate/search -- reusing the real
CLI's command names rather than inventing an `ls` where the rest of nebula
says `show`.

Location model -- a path of segments over the sibling-namespace model
`Ref` already defines (session vs collection vs asset are mutually
exclusive there; browse just walks it):

    /                              registered archives
    /<archive>                     sessions in this archive (flat by
                                    run_id, matching how refs address them
                                    -- not the on-disk year/month layout)
    /<archive>/<run_id>            one session: its artifacts
    /<archive>/collections         collection names
    /<archive>/collections/<name>  one collection's entries
    /<archive>/assets              asset ids
    /<archive>/assets/<id>         one asset's detail

Nothing here is new business logic: every command is a thin wrapper over
the library functions the rest of the CLI already uses (index, collection,
assets, uris, annotations, navigator.model, navigator.osutil), plus a
couple of private cli.py printers reused directly so listings look
identical to their `nebula show`/`nebula collection show` equivalents.
"""

from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from nebula import index
from nebula._termui import color_enabled, err, install_completer, is_interactive, paint, warn
from nebula.registry import get_registry

_COMMANDS = ["cd", "ls", "show", "info", "open", "reveal", "uri", "search",
             "tags", "annotate", "pwd", "help", "exit", "quit"]

_HELP = """\
commands:
  cd <name>  cd ..  cd /       move around (archive -> session, or
                                collections/assets -> one item)
  ls  [-u] [-t] [-l]           list what's here (bare Enter repeats it)
  show [name] [-u] [-t] [-l]   detail on here, or a named child, without cd
  info <name> [-l]             same as `show <name> -l`
  open <name>                  open with the OS default app
  reveal <name>                reveal in the file manager
  uri [name]                   print the nebula:// URI
  search <query>                search artifacts in the current archive
  tags [query]                  tag frequency table for the current archive
  annotate <name> --add-tags a,b | --rm-tags a,b | --set-tags a,b | --comment "..."
  pwd                           print the current location
  help  ?                       show this help
  exit  quit  (or Ctrl-D)        leave
-u/--uri, -t/--tag, -l/--long on `ls`/`show` mean the same thing they do
on `nebula show`. TAB completes names and commands where supported."""


@dataclass
class _State:
    archive_text: Optional[str] = None     # as typed / registry nickname
    archive_root: Optional[Path] = None
    segments: List[str] = field(default_factory=list)   # [] at archive root

    @property
    def at_root(self) -> bool:
        return self.archive_root is None

    @property
    def kind(self) -> str:
        if self.at_root:
            return "root"
        if not self.segments:
            return "archive"
        if self.segments[0] == "collections":
            return "collection" if len(self.segments) > 1 else "collections"
        if self.segments[0] == "assets":
            return "asset" if len(self.segments) > 1 else "assets"
        return "session"

    def breadcrumb(self) -> str:
        if self.at_root:
            return "/"
        # archive_text is a short registry nickname most of the time, but
        # an ad hoc archive is cd'd into by literal filesystem path, which
        # already starts with "/" -- don't double it.
        head = self.archive_text if self.archive_text.startswith("/") \
            else "/" + self.archive_text
        return "/".join([head, *self.segments])


# ---------------------------------------------------------------------
# resolving what's typed
# ---------------------------------------------------------------------

def _try_resolve_archive(text: str):
    """Like cli._resolve_archive_cli, but returns (root, name) or None on
    failure instead of exiting the process -- a bad `cd` should not kill
    the whole shell."""
    from nebula import uris

    if uris.is_uri(text):
        try:
            root, ref = uris.resolve(text)
        except uris.UriError:
            return None
        return root, ref.archive

    registry = get_registry()
    try:
        cfg = registry.resolve_one(text)
        return cfg.root, cfg.nickname
    except KeyError:
        pass

    path = Path(text)
    if path.is_dir():
        return path, text
    return None


def _archive_names() -> List[str]:
    reg = get_registry()
    names = set()
    for nickname, cfg in reg.all().items():
        names.add(nickname)
        if cfg.declared_name:
            names.add(cfg.declared_name)
    return sorted(names)


def _session_rows(root):
    conn = index.open_fresh(root)
    try:
        return conn.execute(
            "SELECT run_id, created, status, tags, description, hold_until "
            "FROM sessions ORDER BY created DESC"
        ).fetchall()
    finally:
        conn.close()


def _session_ids(root) -> List[str]:
    return [r["run_id"] for r in _session_rows(root)]


def _resolve_run_id(root, text: str) -> Optional[str]:
    from nebula.cli import _run_id_for

    candidate = _run_id_for(root, text)
    conn = index.open_fresh(root)
    try:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE run_id = ?", (candidate,)
        ).fetchone()
    finally:
        conn.close()
    return candidate if row else None


def _children(state: _State) -> List[str]:
    """Names valid for `cd`/TAB-completion/bare `ls` at this location."""
    kind = state.kind
    if kind == "root":
        return _archive_names()
    if kind == "archive":
        return ["collections", "assets"] + _session_ids(state.archive_root)
    if kind == "collections":
        from nebula import collection as collection_mod

        return [c.name for c in collection_mod.list_all(state.archive_root)]
    if kind == "assets":
        from nebula import assets

        return assets.list_assets(state.archive_root)
    return []  # a session, one collection, or one asset has no further cd targets


# ---------------------------------------------------------------------
# listing / detail
# ---------------------------------------------------------------------

def _fmt_bytes(n) -> str:
    from nebula.cli import _fmt_bytes as _f

    return _f(n)


def _list_here(state: _State, *, uri=False, tags=False, long=False) -> None:
    kind = state.kind
    if kind == "root":
        names = _archive_names()
        if not names:
            print("(no archives registered)")
            return
        for n in names:
            print(f"  {n}/")
        return

    if kind == "archive":
        print("  collections/")
        print("  assets/")
        for row in _session_rows(state.archive_root):
            row_tags = ", ".join(json.loads(row["tags"])) or "-"
            print(f"  {row['run_id']}  {row['created'][:16]}  "
                  f"[{row['status']:7}]  {row_tags:20}  {row['description']}")
        return

    if kind == "session":
        _show_session(state, state.segments[0], uri=uri, tags=tags, long=long)
        return

    if kind == "collections":
        from nebula import collection as collection_mod

        colls = collection_mod.list_all(state.archive_root)
        if not colls:
            print("  (no collections in this archive)")
            return
        for c in colls:
            title = f"  {c.title}" if c.title else ""
            print(f"  {c.name:24} {len(c.entries):3} entrie(s){title}")
        return

    if kind == "collection":
        _show_collection(state, state.segments[1])
        return

    if kind == "assets":
        from nebula import assets

        ids = assets.list_assets(state.archive_root)
        if not ids:
            print("  (no assets in this archive)")
            return
        for asset_id in ids:
            try:
                meta = assets.read_asset(state.archive_root, asset_id)
            except assets.AssetError:
                continue
            print(f"  {meta.id}  {(meta.name or '?'):40.40} {_fmt_bytes(meta.size):>9}")
        return

    if kind == "asset":
        _show_asset(state, state.segments[1])
        return


def _show_session(state: _State, run_id: str, *, uri=False, tags=False, long=False) -> None:
    root = state.archive_root
    conn = index.open_fresh(root)
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            err(f"no session {run_id!r} in index")
            return
        from nebula.cli import _fmt_ref_row, _print_artifact_row

        print(f"{row['run_id']}  [{row['status']}]")
        print(f"  created:     {row['created']}")
        print(f"  tags:        {', '.join(json.loads(row['tags']))}")
        print(f"  description: {row['description']}")
        session_dir = index.session_path(root, row)
        print(f"  path:        {session_dir}")

        related = conn.execute(
            "SELECT ref_user, ref_archive, ref_archive_id, ref_session, ref_file "
            "FROM related_runs WHERE run_id = ?", (run_id,),
        ).fetchall()
        if related:
            print("  related_runs:")
            for r in related:
                print(f"    - {_fmt_ref_row(r)}")

        artifacts = conn.execute(
            "SELECT filename, repo, commit_hash, dirty, entry_point, source, "
            "origin, sha256 FROM artifacts WHERE run_id = ? ORDER BY filename",
            (run_id,),
        ).fetchall()
        print("  artifacts:")
        if not artifacts:
            print("    (none)")
        for a in artifacts:
            _print_artifact_row(root, run_id, session_dir, a, conn,
                                uri=uri, tags=tags, long=long)
    finally:
        conn.close()


def _show_collection(state: _State, name: str) -> None:
    from nebula import collection as collection_mod
    from nebula.cli import _print_collection

    _print_collection(collection_mod.tree(state.archive_root, name))


def _show_asset(state: _State, asset_id: str, *, long: bool = False) -> None:
    from nebula import assets
    from nebula.cli import _fmt_ref_row_dict

    try:
        meta = assets.read_asset(state.archive_root, asset_id)
    except assets.AssetError as e:
        err(str(e))
        return
    path = assets.live_file(state.archive_root, meta.id)
    print(f"{meta.id}  {meta.name}")
    print(f"  path:     {path or '(missing on disk)'}")
    print(f"  size:     {_fmt_bytes(meta.size)}")
    if long:
        print(f"  sha256:   {(meta.sha256 or '-')[:16]}...")
    print(f"  created:  {meta.created}"
          + (f" by {meta.imported_by}" if meta.imported_by else ""))
    if meta.origin:
        print(f"  origin:   {meta.origin}")
    for ref in meta.derived_from:
        print(f"  <- {_fmt_ref_row_dict(ref)}")
    kept = sum(1 for s in meta.snapshots if not s.pending_gc)
    print(f"  snapshots: {len(meta.snapshots)} ({kept} retained)")


# ---------------------------------------------------------------------
# open / reveal / uri / search / tags / annotate
# ---------------------------------------------------------------------

def _target_path(state: _State, name: Optional[str]) -> Optional[Path]:
    """The real filesystem path `open`/`reveal` should act on: the named
    child if given, else whatever the cursor is currently sitting on."""
    from nebula import assets

    kind = state.kind
    if name:
        if kind == "session":
            session_dir = index.session_path(
                state.archive_root, _session_row(state.archive_root, state.segments[0]))
            return session_dir / name
        if kind == "assets":
            return assets.live_file(state.archive_root, name)
        if kind == "collections":
            from nebula import collection as collection_mod

            return collection_mod.path_for(state.archive_root, name)
        return None

    if kind == "archive":
        return state.archive_root
    if kind == "session":
        return index.session_path(
            state.archive_root, _session_row(state.archive_root, state.segments[0]))
    if kind == "asset":
        return assets.live_file(state.archive_root, state.segments[1])
    if kind == "collection":
        from nebula import collection as collection_mod

        return collection_mod.path_for(state.archive_root, state.segments[1])
    return None


def _session_row(root, run_id):
    conn = index.open_fresh(root)
    try:
        return conn.execute(
            "SELECT * FROM sessions WHERE run_id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()


def _cmd_open(state: _State, name: Optional[str], *, reveal: bool) -> None:
    from nebula.navigator import osutil

    path = _target_path(state, name)
    if path is None or not Path(path).exists():
        err(f"nothing to {'reveal' if reveal else 'open'}"
            + (f" for {name!r}" if name else ""))
        return
    ok_ = osutil.reveal_path(path) if reveal else osutil.open_path(path)
    if not ok_:
        err(f"could not {'reveal' if reveal else 'open'} {path}")


def _cmd_uri(state: _State, name: Optional[str]) -> None:
    from nebula import uris

    kind = state.kind
    session = file = collection = asset = None
    if kind == "session":
        session = state.segments[0]
        file = name
    elif kind == "collections" and name:
        collection = name
    elif kind == "collection":
        collection = state.segments[1]
    elif kind == "assets" and name:
        asset = name
    elif kind == "asset":
        asset = state.segments[1]
    elif kind != "archive":
        err("cd into an archive first")
        return

    try:
        info = uris.describe(state.archive_root, session=session, file=file,
                             collection=collection, asset=asset)
    except uris.UriError as e:
        err(str(e))
        return
    print(info.uri)


def _cmd_search(state: _State, query: str) -> None:
    if state.at_root:
        err("cd into an archive first")
        return
    from nebula.navigator import model as model_mod

    result = model_mod.search_items(state.archive_root, query)
    hits = result.get("items", [])
    if not hits:
        print("  (no matches)")
        return
    for hit in hits:
        run_id = hit.get("run_id", "?")
        fname = getattr(hit.get("item"), "name", "")
        print(f"  {run_id}/{fname}" if fname else f"  {run_id}")


def _cmd_tags(state: _State, query: str) -> None:
    if state.at_root:
        err("cd into an archive first")
        return
    from nebula.tags import collect_tags, _print_tag_table, _sorted_tags

    counts = collect_tags(state.archive_root)
    pairs = _sorted_tags(counts)
    if query:
        pairs = [(t, c) for t, c in pairs if query.lower() in t.lower()]
    if not pairs:
        print("  (no matching tags)")
        return
    _print_tag_table(pairs, selected=(), query=query, file=sys.stdout)


def _parse_annotate_flags(tokens: List[str]):
    """Very small flag parser for `annotate <name> --add-tags a,b
    --rm-tags c --set-tags d,e --comment "text"`. Returns
    (name, {"add": ..., "rm": ..., "set": ..., "comment": ...}, error)."""
    if not tokens:
        return None, {}, "usage: annotate <name> [--add-tags a,b] "\
            "[--rm-tags a,b] [--set-tags a,b] [--comment \"...\"]"
    name = tokens[0]
    out = {"add": None, "rm": None, "set": None, "comment": None}
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t in ("--add-tags", "--rm-tags", "--set-tags", "--comment"):
            if i + 1 >= len(tokens):
                return None, {}, f"{t} needs a value"
            key = {"--add-tags": "add", "--rm-tags": "rm",
                   "--set-tags": "set", "--comment": "comment"}[t]
            out[key] = tokens[i + 1]
            i += 2
        else:
            return None, {}, f"unknown flag {t!r}"
    return name, out, None


def _cmd_annotate(state: _State, rest: List[str]) -> None:
    from nebula import annotations

    if state.kind != "session":
        # Only sessions/artifacts carry nebula annotations today --
        # collections and assets have their own separate metadata.
        err("annotate only works on a session's artifacts right now "
            "(cd into a session first)")
        return

    name, flags, error = _parse_annotate_flags(rest)
    if error:
        err(f"  {error}")
        return
    session_dir = index.session_path(
        state.archive_root, _session_row(state.archive_root, state.segments[0]))
    target = name
    try:
        if flags["set"] is not None:
            annotations.set_annotation(session_dir, target,
                                       tags=annotations.split_tags(flags["set"]))
        if flags["add"]:
            annotations.add_tags(session_dir, target, annotations.split_tags(flags["add"]))
        if flags["rm"]:
            annotations.remove_tags(session_dir, target, annotations.split_tags(flags["rm"]))
        if flags["comment"] is not None:
            annotations.set_annotation(session_dir, target, comment=flags["comment"])
    except annotations.TagError as e:
        err(f"bad tag: {e}")
        return

    got = annotations.get(session_dir, target)
    print(f"{target}  tags: {', '.join(got['tags']) if got['tags'] else '(none)'}")
    if got["comment"]:
        print(f"  comment: {got['comment']}")


# ---------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------

def _prompt(state: _State, color: bool, guard: bool) -> str:
    return paint(f"{state.breadcrumb()}>", "bold", color, guard=guard) + " "


def _cd_one(state: _State, target: str) -> bool:
    """Apply one path component to `state` in place. Returns whether it
    resolved -- `_do_cd` uses this to walk a multi-segment path like
    `cd ../../assets` one hop at a time."""
    if target == "/":
        state.archive_text = state.archive_root = None
        state.segments = []
        return True
    if target == "..":
        if not state.segments and not state.at_root:
            state.archive_text = state.archive_root = None
        elif state.segments:
            state.segments.pop()
        return True

    if state.at_root:
        found = _try_resolve_archive(target)
        if found is None:
            err(f"no such archive {target!r} (known: {', '.join(_archive_names()) or 'none'})")
            return False
        state.archive_root, state.archive_text = found
        state.segments = []
        return True

    kind = state.kind
    if kind == "archive":
        if target in ("collections", "assets"):
            state.segments = [target]
            return True
        run_id = _resolve_run_id(state.archive_root, target)
        if run_id is None:
            err(f"no such session {target!r}")
            return False
        state.segments = [run_id]
        return True
    if kind == "collections":
        from nebula import collection as collection_mod

        if collection_mod.read(state.archive_root, target) is None:
            err(f"no such collection {target!r}")
            return False
        state.segments = ["collections", target]
        return True
    if kind == "assets":
        from nebula import assets
        import datetime as _dt

        cand = target
        if not assets.is_asset_id(cand) and cand.isdigit():
            year2 = _dt.datetime.now().year % 100
            cand = assets.format_asset_id(year2, int(cand))
        if not assets.is_asset_id(cand) or cand not in assets.list_assets(state.archive_root):
            err(f"no such asset {target!r}")
            return False
        state.segments = ["assets", cand]
        return True
    err(f"nothing to cd into here (you're at {state.breadcrumb()})")
    return False


def _do_cd(state: _State, target: str) -> None:
    """`cd` proper: handles a multi-segment path (`../../assets`,
    `collections/paper-2026`, an absolute `/archive/S-26-0001`) one hop at
    a time, committing only if every hop resolves -- like a real shell, a
    typo partway through a path leaves the cursor exactly where it started
    rather than half-moved.

    A literal-filesystem-path archive identifier is itself full of "/", so
    it has to be tried *whole* before this ever splits on "/" -- otherwise
    "/Users/me/data" would be misread as three path segments named
    "Users", "me" and "data".
    """
    if not target or target == ".":
        return
    if target == "/":
        state.archive_text = state.archive_root = None
        state.segments = []
        return

    if target.startswith("/"):
        # A real absolute filesystem path is itself full of "/", so try it
        # whole -- as an archive identifier -- before ever treating "/" as
        # our own path separator.
        found = _try_resolve_archive(target)
        if found is not None:
            state.archive_root, state.archive_text = found
            state.segments = []
            return
        # Not a path that exists on disk: read the leading "/" as "start
        # over at the global root", then resolve the rest relative to
        # that (e.g. "/postdoc/S-26-0001").
        state.archive_text = state.archive_root = None
        state.segments = []
        return _do_cd(state, target[1:])

    if state.at_root:
        found = _try_resolve_archive(target)
        if found is not None:
            state.archive_root, state.archive_text = found
            state.segments = []
            return
        if "/" in target:
            first, rest = target.split("/", 1)
            found = _try_resolve_archive(first)
            if found is not None:
                state.archive_root, state.archive_text = found
                state.segments = []
                _do_cd(state, rest)
                return
        err(f"no such archive {target!r} (known: {', '.join(_archive_names()) or 'none'})")
        return

    # Already inside an archive: navigation is purely virtual from here
    # (session ids, collection/asset names, ".."), so splitting on "/" is
    # unambiguous.
    tokens = [t for t in target.split("/") if t not in ("", ".")]
    if not tokens:
        return
    scratch = _State(archive_text=state.archive_text,
                     archive_root=state.archive_root,
                     segments=list(state.segments))
    for tok in tokens:
        if not _cd_one(scratch, tok):
            return
    state.archive_text = scratch.archive_text
    state.archive_root = scratch.archive_root
    state.segments = scratch.segments


def run_browse(start_archive: Optional[str] = None, start_run_id: Optional[str] = None) -> None:
    """Entry point for `nebula browse`. Blocks until the user types
    exit/quit or sends EOF (Ctrl-D). Unlike select_session, there is no
    sensible non-interactive fallback for a shell, so this just refuses
    outright when stdin/stdout aren't both a terminal."""
    if not is_interactive():
        err("nebula browse needs an interactive terminal")
        sys.exit(1)

    color = color_enabled(sys.stdout)
    state = _State()

    if start_archive:
        _do_cd(state, start_archive)
        if state.archive_root is None:
            sys.exit(1)
        if start_run_id:
            _do_cd(state, start_run_id)

    print(paint("nebula browse -- cd/ls/show/info/open/reveal/uri/search/tags/"
                "annotate; `help` for details, `exit` to leave.", "dim", color))

    restore, have_rl = install_completer(_COMMANDS)
    try:
        while True:
            try:
                install_completer(_COMMANDS + _children(state))
                line = input(_prompt(state, color, have_rl)).strip()
            except EOFError:
                print()
                return
            if not line:
                _list_here(state)
                continue

            try:
                parts = shlex.split(line)
            except ValueError as e:
                err(f"  {e}")
                continue
            cmd, rest = parts[0], parts[1:]

            if cmd in ("exit", "quit"):
                return
            if cmd in ("help", "?"):
                print(paint(_HELP, "dim", color))
            elif cmd == "pwd":
                print(state.breadcrumb())
            elif cmd == "cd":
                _do_cd(state, rest[0] if rest else "/")
            elif cmd == "ls":
                uri = "-u" in rest or "--uri" in rest
                tags = "-t" in rest or "--tag" in rest or "--tags" in rest
                long_ = "-l" in rest or "--long" in rest
                _list_here(state, uri=uri, tags=tags, long=long_)
            elif cmd == "show":
                flags = {t for t in rest if t.startswith("-")}
                names = [t for t in rest if not t.startswith("-")]
                uri = "-u" in flags or "--uri" in flags
                tags = "-t" in flags or "--tag" in flags or "--tags" in flags
                long_ = "-l" in flags or "--long" in flags
                if not names:
                    _list_here(state, uri=uri, tags=tags, long=long_)
                elif state.kind == "session":
                    from nebula.cli import _fmt_ref_row, _print_artifact_row

                    conn = index.open_fresh(state.archive_root)
                    try:
                        a = conn.execute(
                            "SELECT filename, repo, commit_hash, dirty, entry_point, "
                            "source, origin, sha256 FROM artifacts "
                            "WHERE run_id = ? AND filename = ?",
                            (state.segments[0], names[0]),
                        ).fetchone()
                        if a is None:
                            err(f"no such artifact {names[0]!r}")
                        else:
                            session_dir = index.session_path(
                                state.archive_root,
                                _session_row(state.archive_root, state.segments[0]))
                            _print_artifact_row(state.archive_root, state.segments[0],
                                                session_dir, a, conn, uri=uri,
                                                tags=tags, long=long_, indent="  ")
                    finally:
                        conn.close()
                elif state.kind == "collections":
                    _show_collection(state, names[0])
                elif state.kind == "assets":
                    _show_asset(state, names[0], long=long_)
                else:
                    err(f"nothing named {names[0]!r} here")
            elif cmd == "info":
                if not rest:
                    err("  usage: info <name> [-l]")
                elif state.kind == "assets":
                    _show_asset(state, rest[0], long=True)
                elif state.kind == "collections":
                    _show_collection(state, rest[0])
                elif state.kind == "session":
                    from nebula.cli import _print_artifact_row

                    conn = index.open_fresh(state.archive_root)
                    try:
                        a = conn.execute(
                            "SELECT filename, repo, commit_hash, dirty, entry_point, "
                            "source, origin, sha256 FROM artifacts "
                            "WHERE run_id = ? AND filename = ?",
                            (state.segments[0], rest[0]),
                        ).fetchone()
                        if a is None:
                            err(f"no such artifact {rest[0]!r}")
                        else:
                            session_dir = index.session_path(
                                state.archive_root,
                                _session_row(state.archive_root, state.segments[0]))
                            _print_artifact_row(state.archive_root, state.segments[0],
                                                session_dir, a, conn, uri=True,
                                                tags=True, long=True, indent="  ")
                    finally:
                        conn.close()
                else:
                    err(f"nothing named {rest[0]!r} here")
            elif cmd == "open":
                _cmd_open(state, rest[0] if rest else None, reveal=False)
            elif cmd == "reveal":
                _cmd_open(state, rest[0] if rest else None, reveal=True)
            elif cmd == "uri":
                _cmd_uri(state, rest[0] if rest else None)
            elif cmd == "search":
                _cmd_search(state, " ".join(rest))
            elif cmd == "tags":
                _cmd_tags(state, " ".join(rest))
            elif cmd == "annotate":
                _cmd_annotate(state, rest)
            else:
                warn(f"  unknown command {cmd!r} -- try `help`")
    finally:
        restore()
