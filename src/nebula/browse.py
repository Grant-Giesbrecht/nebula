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
from typing import Dict, List, Optional

from nebula import index
from nebula._termui import color_enabled, err, is_interactive, ok, paint, warn
from nebula.registry import get_registry

_COMMANDS = ["cd", "ls", "show", "info", "open", "reveal", "uri", "copy",
             "search", "tags", "annotate", "pwd", "help", "exit", "quit"]

_HELP = """\
commands:
  cd <name>  cd ..  cd /       move around (archive -> session, or
                                collections/assets -> one item)
  ls  [-u] [-t] [-l]           list what's here (bare Enter repeats it)
  show [name] [-u] [-t] [-l] [-c]  detail on here, or a named child,
                                without cd (-c: comment)
  info <name> [-l]             same as `show <name> -l -u -t -c`
  open <name>                  open with the OS default app
  reveal <name>                reveal in the file manager
  uri [name]                   print the nebula:// URI
  copy <name> [--path]         copy its URI (or --path: on-disk path) to
                                the clipboard
  search <query> [--tag] [--comment] [| text]
                                search artifacts in the current archive;
                                --tag/--comment print that under each hit;
                                `| text` filters the results (grep-like)
  tags [query]                  tag frequency table for the current archive
  annotate <name> --add-tags a,b | --rm-tags a,b | --set-tags a,b | --comment "..."
  pwd                           print the current location
  help  ?                       show this help
  exit  quit  (or Ctrl-D)        leave
-u/--uri, -t/--tag, -l/--long, -c/--comment on `ls`/`show` mean the same
thing they do on `nebula show`. Every listing is numbered -- anywhere a
name is expected you can type that number instead, e.g. `show 7` for
whatever `ls` just printed as "7. ...". A name can also be any ref nebula
understands, at any level of brevity: a bare local filename, a bare
session id from anywhere in the archive (not just the sessions listing),
`S-26-0152/raw.csv` (another session in this archive), `A!/S-26-0152/raw.csv`
(the default archive, from anywhere), or a full `nebula://...` URI
(another archive entirely) -- `cd` on one lands at the session it names
(a URI cannot cd into a specific file, only the session holding it);
show/info/open/reveal/uri/copy act on the file itself. TAB completes
commands, names and refs -- including mid-ref, e.g. `A!/S-26-0152/p<TAB>`
-- where the terminal supports it."""


@dataclass
class _State:
    archive_text: Optional[str] = None     # as typed / registry nickname
    archive_root: Optional[Path] = None
    segments: List[str] = field(default_factory=list)   # [] at archive root
    #: Names from the most recently displayed listing, in the order shown
    #: -- what a bare number typed afterwards refers to (see _resolve_token).
    last_listing: List[str] = field(default_factory=list)

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

def _resolve_token(state: _State, token: str) -> str:
    """A bare number typed after a listing refers to that entry by its
    1-based position, the way it was just printed -- so `show 7` works
    instead of typing a long filename or run id. Anything that isn't a
    valid index into the current listing is passed through unchanged and
    treated as a literal name."""
    if token.isdigit():
        idx = int(token)
        if 1 <= idx <= len(state.last_listing):
            return state.last_listing[idx - 1]
    return token


def _try_resolve_archive(text: str):
    """Like cli._resolve_archive_cli, but returns (root, name) or None on
    failure instead of exiting the process -- a bad `cd` should not kill
    the whole shell."""
    from nebula import uris
    from nebula.cli import DEFAULT_ARCHIVE_TOKEN

    if (text or "").strip().upper() == DEFAULT_ARCHIVE_TOKEN:
        default = get_registry().default_nickname()
        if default is None:
            return None
        text = default

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


#: Short-lived cache for tab completion only -- keyed by (kind, archive
#: root, extra), each entry (fetched_at, value). Typing one ref is many
#: keystrokes, and each keystroke re-asks "what are this archive's
#: session ids" / "what files does this session have" via a fresh index
#: read (a full freshness sweep -- see index.ensure_fresh) even though
#: the answer cannot have changed since the *previous* keystroke a moment
#: ago. Every other caller (`ls`, `cd`, `show`, ...) still reads the
#: index live -- correctness matters there; a completion list a couple of
#: seconds stale is a fine trade for not re-sweeping the whole archive on
#: every character typed.
_COMPLETION_CACHE_TTL = 2.0
_completion_cache: Dict[tuple, "tuple[float, list]"] = {}


def _cached(key, compute):
    import time

    now = time.monotonic()
    hit = _completion_cache.get(key)
    if hit is not None and now - hit[0] < _COMPLETION_CACHE_TTL:
        return hit[1]
    value = compute()
    _completion_cache[key] = (now, value)
    return value


def _completion_session_ids(root) -> List[str]:
    return _cached(("sessions", str(root)), lambda: _session_ids(root))


def _completion_artifact_names(root, run_id: str) -> List[str]:
    def fetch():
        conn = index.open_fresh(root)
        try:
            rows = conn.execute(
                "SELECT filename FROM artifacts WHERE run_id = ? ORDER BY filename",
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
        return [r["filename"] for r in rows]

    return _cached(("artifacts", str(root), run_id), fetch)


def _completion_asset_ids(root) -> List[str]:
    from nebula import assets

    return _cached(("assets", str(root)), lambda: assets.list_assets(root))


def _completion_collection_names(root) -> List[str]:
    from nebula import collection as collection_mod

    return _cached(("collections", str(root)),
                   lambda: [c.name for c in collection_mod.list_all(root)])


def _resolve_run_id(root, text: str, *, cached: bool = False) -> Optional[str]:
    """Expand/validate a typed session id against the archive.

    `cached=True` (tab completion only -- see _apply_cd/_walk_path) skips
    every network round-trip: no index freshness sweep, and no
    archive.yaml read for the id prefix (S- vs I-) -- that's inferred
    from whatever ids are already in the completion cache instead. On a
    network-mounted archive this is the difference between a session-id
    guess costing a round-trip per keystroke and costing nothing after
    the first one. `cached=False` (real navigation) stays fully live."""
    text_stripped = (text or "").strip()
    from nebula.cli import REUSE_SESSION_TOKEN

    if text_stripped.upper() == REUSE_SESSION_TOKEN:
        from nebula.session_select import reuse_candidate

        return reuse_candidate(root)

    if cached:
        ids = _completion_session_ids(root)
        upper = text_stripped.upper()
        if upper in ids:
            return upper
        if upper.isdigit():
            import datetime as _dt

            year2 = _dt.datetime.now().year % 100
            prefix = ids[0].split("-", 1)[0] + "-" if ids else "S-"
            candidate = f"{prefix}{year2:02d}-{int(upper):04d}"
            return candidate if candidate in ids else None
        return None

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


def _completion_children(state: _State) -> List[str]:
    """Like _children, but also a session's own artifact filenames --
    valid completions for show/info/open/reveal/uri/copy even though `cd`
    has nowhere to go with them (a session has no further cd target).

    Unlike _children (used by `ls`/`cd`, which must stay live), the
    index-backed lookups here go through the short-lived completion
    cache -- see _cached's docstring."""
    kind = state.kind
    if kind == "archive":
        return ["collections", "assets"] + _completion_session_ids(state.archive_root)
    if kind == "session":
        names = list(_children(state))
        names.extend(_completion_artifact_names(state.archive_root, state.segments[0]))
        return names
    return list(_children(state))


# ---------------------------------------------------------------------
# listing / detail
# ---------------------------------------------------------------------

def _fmt_bytes(n) -> str:
    from nebula.cli import _fmt_bytes as _f

    return _f(n)


#: Colour for the leading "N." on every numbered listing line -- dim, so
#: it reads as an index rather than data, the same visual role dim plays
#: for a closed session's status.
_NUM_STYLE = "dim"


def _num(i: int, color: bool) -> str:
    return paint(f"{i:>3}.", _NUM_STYLE, color)


def _list_here(state: _State, *, uri=False, tags=False, long=False) -> None:
    from nebula.cli import _RUNID_STYLE, _STATUS_STYLE, _TAG_STYLE, _DIM_STYLE, _HELD_STYLE
    from nebula.session import _hold_value_active

    color = color_enabled(sys.stdout)
    kind = state.kind
    if kind == "root":
        names = _archive_names()
        state.last_listing = names
        if not names:
            print("(no archives registered)")
            return
        for i, n in enumerate(names, 1):
            print(f"  {_num(i, color)} {n}/")
        return

    if kind == "archive":
        entries = ["collections", "assets"]
        print(f"  {_num(1, color)} collections/")
        print(f"  {_num(2, color)} assets/")
        rows = _session_rows(state.archive_root)
        for i, row in enumerate(rows, start=3):
            row_tags_list = json.loads(row["tags"])
            row_tags = ", ".join(paint(t, _TAG_STYLE, color) for t in row_tags_list) \
                if row_tags_list else paint("-", _DIM_STYLE, color)
            status_padded = f"{row['status']:7}"
            held = paint("  HELD", _HELD_STYLE, color) \
                if _hold_value_active(row["hold_until"]) else ""
            print(f"  {_num(i, color)} {paint(row['run_id'], _RUNID_STYLE, color)}  "
                  f"{row['created'][:16]}  "
                  f"[{paint(status_padded, _STATUS_STYLE.get(row['status'], ''), color)}]  "
                  f"{row_tags}  {row['description']}{held}")
            entries.append(row["run_id"])
        state.last_listing = entries
        return

    if kind == "session":
        state.last_listing = _show_session(state, state.segments[0],
                                           uri=uri, tags=tags, long=long)
        return

    if kind == "collections":
        from nebula import collection as collection_mod

        colls = collection_mod.list_all(state.archive_root)
        state.last_listing = [c.name for c in colls]
        if not colls:
            print("  (no collections in this archive)")
            return
        for i, c in enumerate(colls, 1):
            title = f"  {c.title}" if c.title else ""
            print(f"  {_num(i, color)} {c.name:24} {len(c.entries):3} entrie(s){title}")
        return

    if kind == "collection":
        _show_collection(state, state.segments[1])
        return

    if kind == "assets":
        from nebula import assets

        ids = assets.list_assets(state.archive_root)
        state.last_listing = ids
        if not ids:
            print("  (no assets in this archive)")
            return
        for i, asset_id in enumerate(ids, 1):
            try:
                meta = assets.read_asset(state.archive_root, asset_id)
            except assets.AssetError:
                continue
            print(f"  {_num(i, color)} {meta.id}  {(meta.name or '?'):40.40} "
                  f"{_fmt_bytes(meta.size):>9}")
        return

    if kind == "asset":
        _show_asset(state, state.segments[1])
        return


def _show_session(state: _State, run_id: str, *, uri=False, tags=False, long=False) -> List[str]:
    """Print the session and its artifacts, numbered, and return the
    filenames in the order shown -- what a following bare number refers
    to (see _resolve_token)."""
    root = state.archive_root
    conn = index.open_fresh(root)
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            err(f"no session {run_id!r} in index")
            return []
        from nebula.cli import (_DIM_STYLE, _RUNID_STYLE, _STATUS_STYLE,
                                _TAG_STYLE, _fmt_ref_row, _print_artifact_row)

        color = color_enabled(sys.stdout)
        status = row["status"]
        print(f"{paint(row['run_id'], _RUNID_STYLE, color)}  "
              f"[{paint(status, _STATUS_STYLE.get(status, ''), color)}]")
        print(f"  created:     {row['created']}")
        row_tags = json.loads(row["tags"])
        tags_str = ", ".join(paint(t, _TAG_STYLE, color) for t in row_tags) \
            if row_tags else paint("-", _DIM_STYLE, color)
        print(f"  tags:        {tags_str}")
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
        for i, a in enumerate(artifacts, 1):
            _print_artifact_row(root, run_id, session_dir, a, conn,
                                uri=uri, tags=tags, long=long, number=i)
        return [a["filename"] for a in artifacts]
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
# resolving a name/ref to a target -- what show/info/open/reveal/uri/copy
# and `cd` all operate on. A "ref" here is anything nebula.refs.parse_ref
# understands: a nebula:// URI, the legacy archive|session/file spelling,
# or a same-archive session/file (or collections/x, assets/x) shorthand --
# exactly what a search hit or a derived_from line prints back at you, so
# pasting one in should always work, not just a bare local name.
# ---------------------------------------------------------------------

def _session_row(root, run_id):
    conn = index.open_fresh(root)
    try:
        return conn.execute(
            "SELECT * FROM sessions WHERE run_id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()


def _looks_like_ref(token: str) -> bool:
    """True when `token` has structure parse_ref should interpret -- a
    "/", the legacy "|" separator, a nebula:// URI, or the unmistakable
    shape of a session/asset id (S-26-0152, AF-26-0017) -- as opposed to
    a bare word that only makes sense relative to wherever the cursor is
    (a collection name, an asset id typed while browsing assets/, ...),
    which parse_ref cannot disambiguate without that context.

    The session/asset-id case is what lets a bare `cd S-26-0152` (or
    `show`/`info`/... naming one) work from *anywhere* in the archive --
    not only from the sessions listing -- so you don't have to `cd ..`
    back out first just to name a session by id."""
    from nebula import refs as refs_mod

    return bool(
        refs_mod.REF_PATH_SEP in token or refs_mod.REF_ARCHIVE_SEP in token
        or token.lower().startswith(refs_mod.URI_SCHEME)
        or refs_mod._SESSION_RE.match(token) or refs_mod._ASSET_RE.match(token)
    )


def _resolve_ref_string(token: str, state: "_State"):
    """Parse `token` as a nebula ref and resolve the archive it names.

    Returns (archive_root, archive_display, Ref), or None when `token`
    doesn't parse as a ref at all, or names an archive this machine
    doesn't have registered/mounted. A ref with no archive segment
    resolves against the archive the cursor is currently inside (None if
    the cursor is at the global root).

    A leading `A!/...` is rewritten to the legacy `<default>|...` spelling
    before parsing -- nebula.refs has no idea what A! means (it's a
    browse/cli-only shortcut), but that legacy separator is exactly the
    bare-archive-name-plus-path grammar A! needs, so this is the only
    place that has to know the two are related."""
    from nebula import refs as refs_mod

    from nebula.cli import DEFAULT_ARCHIVE_TOKEN

    n = len(DEFAULT_ARCHIVE_TOKEN)
    if token[:n].upper() == DEFAULT_ARCHIVE_TOKEN.upper() and token[n:n + 1] == "/":
        from nebula.registry import get_registry

        default = get_registry().default_nickname()
        if default is None:
            return None
        token = f"{default}{refs_mod.REF_ARCHIVE_SEP}{token[n + 1:]}"

    try:
        ref = refs_mod.parse_ref(token)
    except ValueError:
        return None

    if ref.archive or ref.archive_id or ref.user:
        from nebula.registry import get_registry

        cfg = get_registry().find(ref.archive, ref.user, archive_id=ref.archive_id)
        if cfg is None:
            return None
        return Path(cfg.root), cfg.nickname, ref

    if state.archive_root is None:
        return None
    return state.archive_root, state.archive_text, ref


def _segments_exist(archive_root, segments: List[str], *, cached: bool = False) -> bool:
    """Whether `segments` names something real, not just something
    syntactically well-formed -- parse_ref happily parses "nonexistent/
    deeper" into a session ref without knowing "nonexistent" was never a
    session; this is the check that catches that before `cd` commits to
    it.

    `cached=True` (tab completion only) answers from the short-lived
    completion cache instead of a live index/filesystem read -- see
    _resolve_run_id's docstring for why that matters on a network-mounted
    archive. `cached=False` (real navigation) always reads live."""
    if not segments:
        return True
    if segments[0] == "collections":
        if len(segments) < 2:
            return True
        if cached:
            return segments[1] in _completion_collection_names(archive_root)
        from nebula import collection as collection_mod

        return collection_mod.read(archive_root, segments[1]) is not None
    if segments[0] == "assets":
        if len(segments) < 2:
            return True
        if cached:
            return segments[1] in _completion_asset_ids(archive_root)
        from nebula import assets

        return segments[1] in assets.list_assets(archive_root)
    if cached:
        return segments[0] in _completion_session_ids(archive_root)
    conn = index.open_fresh(archive_root)
    try:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE run_id = ?", (segments[0],)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _ref_to_segments(ref) -> Optional[List[str]]:
    """The browse-tree location a ref names, dropping any file component
    -- `cd` can land on the session that holds a file, never on the file
    itself, matching what a filesystem `cd` does with a path to a file's
    parent."""
    if ref.asset:
        return ["assets", ref.asset]
    if ref.collection:
        return ["collections", ref.collection]
    if ref.session:
        return [ref.session]
    return None


@dataclass
class _Target:
    """What show/info/open/reveal/uri/copy act on, once a name or ref has
    been resolved -- possibly in a different archive/session than the
    cursor's current location."""
    kind: str   # "artifact" | "session" | "asset" | "collection"
    archive_root: Path
    archive_display: str
    run_id: Optional[str] = None
    filename: Optional[str] = None
    asset_id: Optional[str] = None
    collection_name: Optional[str] = None


def _resolve_show_target(state: "_State", token: str) -> Optional[_Target]:
    """Resolve one argument to show/info/open/reveal/uri/copy: a ref (see
    _looks_like_ref) resolved anywhere it points, or -- for a plain local
    word -- whatever it means at the cursor's current location, exactly
    like a single `cd` hop would interpret it."""
    token = _resolve_token(state, token)

    if _looks_like_ref(token):
        loc = _resolve_ref_string(token, state)
        if loc is None:
            return None
        archive_root, archive_display, ref = loc
        if ref.asset:
            return _Target("asset", archive_root, archive_display, asset_id=ref.asset)
        if ref.collection:
            return _Target("collection", archive_root, archive_display,
                           collection_name=ref.collection)
        if ref.session and ref.file:
            return _Target("artifact", archive_root, archive_display,
                           run_id=ref.session, filename=ref.file)
        if ref.session:
            return _Target("session", archive_root, archive_display, run_id=ref.session)
        if ref.file and state.kind == "session":
            # A bare-filename ref (no session of its own) only means
            # something inside the session the cursor is already in.
            return _Target("artifact", state.archive_root, state.archive_text,
                           run_id=state.segments[0], filename=token)
        return None

    kind = state.kind
    if kind == "session":
        return _Target("artifact", state.archive_root, state.archive_text,
                       run_id=state.segments[0], filename=token)
    if kind == "collections":
        return _Target("collection", state.archive_root, state.archive_text,
                       collection_name=token)
    if kind == "assets":
        from nebula import assets
        import datetime as _dt

        cand = token
        if not assets.is_asset_id(cand) and cand.isdigit():
            year2 = _dt.datetime.now().year % 100
            cand = assets.format_asset_id(year2, int(cand))
        return _Target("asset", state.archive_root, state.archive_text, asset_id=cand)
    if kind == "archive":
        run_id = _resolve_run_id(state.archive_root, token)
        if run_id is None:
            return None
        return _Target("session", state.archive_root, state.archive_text, run_id=run_id)
    return None


def _render_target(target: _Target, *, uri=False, tags=False, long=False,
                    comment=False) -> None:
    """Print the same detail `show`/`info` would for wherever `target`
    actually is, reusing the exact printers _list_here uses locally."""
    if target.kind == "artifact":
        conn = index.open_fresh(target.archive_root)
        try:
            a = conn.execute(
                "SELECT filename, repo, commit_hash, dirty, entry_point, source, "
                "origin, sha256 FROM artifacts WHERE run_id = ? AND filename = ?",
                (target.run_id, target.filename),
            ).fetchone()
            if a is None:
                err(f"no such artifact {target.filename!r} in {target.run_id}")
                return
            session_row = _session_row(target.archive_root, target.run_id)
            if session_row is None:
                err(f"no such session {target.run_id!r}")
                return
            from nebula.cli import _print_artifact_row

            session_dir = index.session_path(target.archive_root, session_row)
            _print_artifact_row(target.archive_root, target.run_id, session_dir, a, conn,
                                uri=uri, tags=tags, long=long, comment=comment, indent="  ")
        finally:
            conn.close()
        return

    if target.kind == "session":
        tmp = _State(archive_text=target.archive_display, archive_root=target.archive_root,
                     segments=[target.run_id])
        _show_session(tmp, target.run_id, uri=uri, tags=tags, long=long)
        return

    if target.kind == "collection":
        _show_collection(
            _State(archive_text=target.archive_display, archive_root=target.archive_root),
            target.collection_name)
        return

    if target.kind == "asset":
        _show_asset(
            _State(archive_text=target.archive_display, archive_root=target.archive_root),
            target.asset_id, long=long)


def _target_path_for(target: _Target) -> Optional[Path]:
    """The real filesystem path `open`/`reveal`/`copy --path` should act
    on."""
    from nebula import assets

    if target.kind == "archive":
        return target.archive_root
    if target.kind == "artifact":
        row = _session_row(target.archive_root, target.run_id)
        if row is None:
            return None
        return index.session_path(target.archive_root, row) / target.filename
    if target.kind == "session":
        row = _session_row(target.archive_root, target.run_id)
        return index.session_path(target.archive_root, row) if row is not None else None
    if target.kind == "asset":
        return assets.live_file(target.archive_root, target.asset_id)
    if target.kind == "collection":
        from nebula import collection as collection_mod

        return collection_mod.path_for(target.archive_root, target.collection_name)
    return None


def _target_uri_for(target: _Target):
    """The `nebula://` URI for `target`. Raises uris.UriError, same as
    uris.describe, on anything the caller should report rather than
    silently swallow."""
    from nebula import uris

    return uris.describe(target.archive_root, session=target.run_id,
                         file=target.filename, collection=target.collection_name,
                         asset=target.asset_id)


# ---------------------------------------------------------------------
# open / reveal / uri / copy / search / tags / annotate
# ---------------------------------------------------------------------

def _cmd_open(state: _State, token: Optional[str], *, reveal: bool) -> None:
    from nebula.navigator import osutil

    if token is None:
        path = _target_path_for(_here_target(state))
    else:
        target = _resolve_show_target(state, token)
        path = _target_path_for(target) if target is not None else None
    if path is None or not Path(path).exists():
        err(f"nothing to {'reveal' if reveal else 'open'}"
            + (f" for {token!r}" if token else ""))
        return
    ok_ = osutil.reveal_path(path) if reveal else osutil.open_path(path)
    if not ok_:
        err(f"could not {'reveal' if reveal else 'open'} {path}")


def _here_target(state: _State) -> Optional[_Target]:
    """The _Target for wherever the cursor is sitting right now, for a
    bare `open`/`reveal`/`uri`/`copy` with no argument."""
    kind = state.kind
    if kind == "archive":
        return _Target("archive", state.archive_root, state.archive_text)
    if kind == "session":
        return _Target("session", state.archive_root, state.archive_text,
                       run_id=state.segments[0])
    if kind == "asset":
        return _Target("asset", state.archive_root, state.archive_text,
                       asset_id=state.segments[1])
    if kind == "collection":
        return _Target("collection", state.archive_root, state.archive_text,
                       collection_name=state.segments[1])
    return None


def _cmd_uri(state: _State, token: Optional[str]) -> None:
    from nebula import uris
    from nebula.cli import _URI_STYLE

    target = _resolve_show_target(state, token) if token is not None else _here_target(state)
    if target is None:
        err("cd into an archive first" if state.at_root else f"no such item {token!r}")
        return
    try:
        info = _target_uri_for(target)
    except uris.UriError as e:
        err(str(e))
        return
    print(paint(info.uri, _URI_STYLE, color_enabled(sys.stdout)))


def _cmd_copy(state: _State, rest: List[str]) -> None:
    from nebula.navigator import osutil

    want_path = "--path" in rest
    tokens = [t for t in rest if t != "--path"]
    if not tokens:
        target = _here_target(state)
        if target is None:
            err("  usage: copy <name> [--path]")
            return
    else:
        target = _resolve_show_target(state, tokens[0])
        if target is None:
            err(f"no such item {tokens[0]!r}")
            return

    if want_path:
        path = _target_path_for(target)
        if path is None:
            err("nothing to copy a path for")
            return
        text = str(path)
    else:
        from nebula import uris

        try:
            text = _target_uri_for(target).uri
        except uris.UriError as e:
            err(str(e))
            return

    if osutil.copy_to_clipboard(text):
        ok(f"copied: {text}")
    else:
        warn("  could not reach the system clipboard -- here it is, to copy by hand:")
        print(text)


def _cmd_search(state: _State, rest: List[str]) -> None:
    """search <query...> [--tag] [--comment] [| <filter text>]

    --tag/--comment print that item's tags/comment under each hit, and a
    trailing `| text` narrows the results afterward to only the hits
    whose header or printed detail contains `text` (case-insensitive) --
    a grep over what the search already found, so "every RP23D item, but
    only the ones mentioning s21" is one line: `search --tag RP23D | s21`.
    """
    if state.at_root:
        err("cd into an archive first")
        return

    if "|" in rest:
        i = rest.index("|")
        query_tokens, filter_tokens = rest[:i], rest[i + 1:]
    else:
        query_tokens, filter_tokens = rest, []
    filter_pattern = " ".join(filter_tokens).strip()

    flags = {t for t in query_tokens if t in ("--tag", "--tags", "--comment")}
    query_words = [t for t in query_tokens if t not in flags]
    show_tags = "--tag" in flags or "--tags" in flags
    show_comment = "--comment" in flags
    query = " ".join(query_words)

    from nebula.navigator import model as model_mod

    result = model_mod.search_items(state.archive_root, query)
    hits = result.get("items", [])
    if not hits:
        print("  (no matches)")
        return

    from nebula import annotations
    from nebula.cli import _DIM_STYLE, _TAG_STYLE

    color = color_enabled(sys.stdout)
    records = []
    for hit in hits:
        run_id = hit.get("run_id", "?")
        fname = getattr(hit.get("item"), "name", "")
        header = f"{run_id}/{fname}" if fname else run_id
        detail = []
        if show_tags or show_comment:
            session_row = _session_row(state.archive_root, run_id)
            if session_row is not None:
                session_dir = index.session_path(state.archive_root, session_row)
                note = annotations.get(session_dir, fname or None)
                if show_tags:
                    tag_str = ", ".join(paint(t, _TAG_STYLE, color) for t in note["tags"]) \
                        if note["tags"] else paint("-", _DIM_STYLE, color)
                    detail.append(f"tags: {tag_str}")
                if show_comment and note.get("comment"):
                    detail.append(f"comment: {note['comment']}")
        records.append((header, detail))

    if filter_pattern:
        needle = filter_pattern.lower()
        records = [(h, d) for h, d in records
                  if needle in h.lower() or any(needle in x.lower() for x in d)]
        if not records:
            print("  (nothing matches the filter)")
            return

    for header, detail in records:
        print(f"  {header}")
        for line in detail:
            print(f"      {line}")


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
    name = _resolve_token(state, name)
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


def _cd_one(state: _State, target: str, *, quiet: bool = False, cached: bool = False) -> bool:
    """Apply one path component to `state` in place. Returns whether it
    resolved -- `_apply_cd` uses this to walk a multi-segment path like
    `cd ../../assets` one hop at a time. `quiet` suppresses the error
    message and `cached` sources existence checks from the completion
    cache instead of the network/index (both used only for tab
    completion's trial resolution, never for an actual `cd`)."""
    def report(msg):
        if not quiet:
            err(msg)

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
            report(f"no such archive {target!r} (known: {', '.join(_archive_names()) or 'none'})")
            return False
        state.archive_root, state.archive_text = found
        state.segments = []
        return True

    kind = state.kind
    if kind == "archive":
        if target in ("collections", "assets"):
            state.segments = [target]
            return True
        run_id = _resolve_run_id(state.archive_root, target, cached=cached)
        if run_id is None:
            report(f"no such session {target!r}")
            return False
        state.segments = [run_id]
        return True
    if kind == "collections":
        if cached:
            found = target in _completion_collection_names(state.archive_root)
        else:
            from nebula import collection as collection_mod

            found = collection_mod.read(state.archive_root, target) is not None
        if not found:
            report(f"no such collection {target!r}")
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
        ids = _completion_asset_ids(state.archive_root) if cached else assets.list_assets(
            state.archive_root)
        if not assets.is_asset_id(cand) or cand not in ids:
            report(f"no such asset {target!r}")
            return False
        state.segments = ["assets", cand]
        return True
    report(f"nothing to cd into here (you're at {state.breadcrumb()})")
    return False


def _apply_cd(state: _State, target: str, *, quiet: bool = False, cached: bool = False) -> bool:
    """`cd`'s actual resolution logic: handles a multi-segment path
    (`../../assets`, `collections/paper-2026`, an absolute
    `/archive/S-26-0001`) one hop at a time, mutating `state` in place
    and returning whether every hop resolved.

    A literal-filesystem-path archive identifier is itself full of "/", so
    it has to be tried *whole* before this ever splits on "/" -- otherwise
    "/Users/me/data" would be misread as three path segments named
    "Users", "me" and "data".

    `quiet` suppresses error messages, and `cached` sources every
    existence check from the short-lived completion cache instead of a
    live index/filesystem read -- both used by the tab completer, which
    calls this against a scratch copy purely to find out where a
    partially-typed ref's already-typed prefix would land, must never
    print anything mid-completion, and -- on a network-mounted archive
    especially -- cannot afford a round-trip on every keystroke just to
    answer a question the *previous* keystroke already answered a moment
    ago. `_do_cd` (the real command) and `_walk_path` (the completer's
    read-only lookup) are the two callers; only `_do_cd` ever passes
    cached=False, and only it commits the result, so a typo partway
    through a path -- or a partial one still being typed -- never leaves
    the real cursor half-moved, and a stale completion guess never
    becomes a wrong `cd`.
    """
    def report(msg):
        if not quiet:
            err(msg)

    if not target or target == ".":
        return True
    if target == "/":
        state.archive_text = state.archive_root = None
        state.segments = []
        return True
    if target.isdigit():
        # A bare number is only meaningful as the very next hop -- resolve
        # it against the listing shown here before treating it as a path
        # at all (a resolved name may itself contain no "/", so this can't
        # recurse into the general splitting logic below by accident).
        target = _resolve_token(state, target)

    if (not target.startswith("/") and not target.startswith(".")
            and _looks_like_ref(target)):
        # A nebula:// URI, the legacy archive|session/file spelling, A!
        # (the default-archive shortcut), or a same-archive session/file
        # shorthand -- exactly what a search hit or a derived_from line
        # prints back. Try it as a ref before ever falling back to the
        # plain "/"-splitting below, which would otherwise misread
        # "S-26-0002/raw.csv" as two path segments (a session to cd into,
        # then a nonexistent further hop named "raw.csv") instead of
        # landing on the session that holds it.
        loc = _resolve_ref_string(target, state)
        if loc is not None:
            archive_root, archive_display, ref = loc
            segments = _ref_to_segments(ref)
            if segments is not None and _segments_exist(archive_root, segments, cached=cached):
                state.archive_root = archive_root
                state.archive_text = archive_display
                state.segments = segments
                return True
            if ref.archive or ref.archive_id or ref.user:
                # Named a *different* archive explicitly (a nebula:// URI,
                # A!, or the legacy archive|... spelling) -- the old
                # same-archive splitting logic below cannot make sense of
                # that string at all, so report it here rather than
                # falling through to a confusing mismatched error.
                report(f"no such session/asset/collection: {target!r}")
                return False
            # Same-archive shorthand that parsed but doesn't check out
            # (e.g. a typo, or a bare file with no session of its own) --
            # fall through to the ordinary splitting logic below, which
            # reports a precise per-segment error.

    if target.startswith("/"):
        # A real absolute filesystem path is itself full of "/", so try it
        # whole -- as an archive identifier -- before ever treating "/" as
        # our own path separator.
        found = _try_resolve_archive(target)
        if found is not None:
            state.archive_root, state.archive_text = found
            state.segments = []
            return True
        # Not a path that exists on disk: read the leading "/" as "start
        # over at the global root", then resolve the rest relative to
        # that (e.g. "/postdoc/S-26-0001").
        state.archive_text = state.archive_root = None
        state.segments = []
        return _apply_cd(state, target[1:], quiet=quiet, cached=cached)

    if state.at_root:
        found = _try_resolve_archive(target)
        if found is not None:
            state.archive_root, state.archive_text = found
            state.segments = []
            return True
        if "/" in target:
            first, rest = target.split("/", 1)
            found = _try_resolve_archive(first)
            if found is not None:
                state.archive_root, state.archive_text = found
                state.segments = []
                return _apply_cd(state, rest, quiet=quiet, cached=cached)
        report(f"no such archive {target!r} (known: {', '.join(_archive_names()) or 'none'})")
        return False

    # Already inside an archive: navigation is purely virtual from here
    # (session ids, collection/asset names, ".."), so splitting on "/" is
    # unambiguous.
    tokens = [t for t in target.split("/") if t not in ("", ".")]
    if not tokens:
        return True
    for tok in tokens:
        if not _cd_one(state, tok, quiet=quiet, cached=cached):
            return False
    return True


def _scratch_state(state: _State) -> _State:
    """A copy of `state` safe to mutate speculatively -- including
    last_listing, since _resolve_token (bare-number substitution) reads
    it, and a scratch copy that silently dropped it would make `cd 3`
    stop resolving the number the moment it went through a scratch hop."""
    return _State(archive_text=state.archive_text, archive_root=state.archive_root,
                  segments=list(state.segments), last_listing=list(state.last_listing))


def _do_cd(state: _State, target: str) -> None:
    """The `cd` command: resolves `target` against a scratch copy of
    `state` and commits it only on full success, so a typo partway
    through a multi-segment path leaves the cursor exactly where it
    started rather than half-moved."""
    scratch = _scratch_state(state)
    if _apply_cd(scratch, target, quiet=False):
        state.archive_text = scratch.archive_text
        state.archive_root = scratch.archive_root
        state.segments = scratch.segments


def _walk_path(state: _State, target: str) -> Optional[_State]:
    """Resolve `target` exactly the way `cd` would, without mutating
    `state` or printing anything. Used by tab completion to find out
    what a partially-typed ref's already-typed prefix (everything before
    the last "/") resolves to, so it can list *that* location's children
    as candidates for what comes next. Returns None if `target` doesn't
    resolve to anything (in which case there's nothing to complete
    against)."""
    scratch = _scratch_state(state)
    if not target:
        return scratch
    return scratch if _apply_cd(scratch, target, quiet=True, cached=True) else None


# ---------------------------------------------------------------------
# tab completion -- any name/ref argument, at any level of brevity
# ---------------------------------------------------------------------

def _ref_completions(state: _State, text: str) -> List[str]:
    """Completions for one name/ref argument (cd/show/info/open/reveal/
    uri/copy/annotate's first word), matching everything those commands
    themselves accept as input -- not just a bare name local to wherever
    the cursor happens to be:

      - no "/" yet: local children of here, PLUS (if inside an archive)
        every session id in it and the collections/assets sibling names,
        PLUS A!/S! -- so a session can be named by id from anywhere, not
        only while looking at the sessions listing.
      - a "/" already typed: resolve everything before the last "/" the
        same way `cd` would (_walk_path -- so A!/..., a full nebula://
        URI, or a same-archive session/file shorthand all work), then
        offer that location's children for what comes after.
    """
    if "/" in text:
        prefix, _, partial = text.rpartition("/")
        loc_state = _walk_path(state, prefix)
        if loc_state is None:
            return []
        try:
            names = _completion_children(loc_state)
        except Exception:
            return []
        return [f"{prefix}/{n}" for n in names if n.startswith(partial)]

    candidates = set(_completion_children(state))
    # _completion_children already includes every session id (plus
    # collections/assets) when the cursor is at the sessions listing
    # itself -- only fetch them again when it's sitting somewhere else,
    # to avoid a second redundant index read on every keystroke.
    if state.archive_root is not None and state.kind != "archive":
        try:
            candidates.update(_completion_session_ids(state.archive_root))
        except Exception:
            pass
        candidates.update(("collections", "assets"))
    if state.at_root:
        candidates.update(_archive_names())
    from nebula.cli import DEFAULT_ARCHIVE_TOKEN, REUSE_SESSION_TOKEN

    candidates.add(DEFAULT_ARCHIVE_TOKEN)
    candidates.add(REUSE_SESSION_TOKEN)
    return sorted(c for c in candidates if c.startswith(text))


def _install_completer(state: _State):
    """Wire up TAB completion against the *current* `state` -- a closure,
    not a fixed word list, so candidates are computed fresh on every
    keystroke and this only needs installing once per session rather than
    reinstalled every time the cursor moves (contrast
    _termui.install_completer, built for a fixed option list).

    The first word on the line completes against _COMMANDS; anything
    after that (unless it starts with "-", a flag) completes via
    _ref_completions. Same libedit-vs-GNU-readline handling as
    _termui.install_completer -- see that function's docstring for why."""
    try:
        import readline
    except ImportError:
        return (lambda: None), False

    # readline/libedit calls the completer once per candidate it wants
    # (state=0, then 1, then 2, ...) for what is, to us, a single
    # completion request -- so without this, listing N matches means
    # recomputing the *entire* candidate set (each of which may touch the
    # index) N times over. A single-slot cache, invalidated the instant
    # the request actually changes (a different line, or the cursor
    # having moved since), turns that back into one real computation.
    cache = {"key": None, "matches": []}

    def complete(text, idx):
        line = readline.get_line_buffer()
        before = line[:readline.get_begidx()]
        key = (line, state.breadcrumb())
        if key != cache["key"]:
            if before.strip() == "":
                matches = [c for c in _COMMANDS if c.startswith(text)]
            elif text.startswith("-"):
                matches = []
            else:
                matches = _ref_completions(state, text)
            cache["key"] = key
            cache["matches"] = matches
        matches = cache["matches"]
        return matches[idx] if idx < len(matches) else None

    prev_completer = readline.get_completer()
    prev_delims = readline.get_completer_delims()
    readline.set_completer(complete)
    readline.set_completer_delims(" \t\n,")
    if "libedit" in (getattr(readline, "__doc__", "") or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")

    def restore():
        readline.set_completer(prev_completer)
        readline.set_completer_delims(prev_delims)

    return restore, True


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

    restore, have_rl = _install_completer(state)
    try:
        while True:
            try:
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
                comment = "-c" in flags or "--comment" in flags
                if not names:
                    _list_here(state, uri=uri, tags=tags, long=long_)
                else:
                    target = _resolve_show_target(state, names[0])
                    if target is None:
                        err(f"no such item {names[0]!r}")
                    else:
                        _render_target(target, uri=uri, tags=tags, long=long_, comment=comment)
            elif cmd == "info":
                if not rest:
                    err("  usage: info <name> [-l]")
                    continue
                target = _resolve_show_target(state, rest[0])
                if target is None:
                    err(f"no such item {rest[0]!r}")
                else:
                    _render_target(target, uri=True, tags=True, long=True, comment=True)
            elif cmd == "open":
                _cmd_open(state, rest[0] if rest else None, reveal=False)
            elif cmd == "reveal":
                _cmd_open(state, rest[0] if rest else None, reveal=True)
            elif cmd == "uri":
                _cmd_uri(state, rest[0] if rest else None)
            elif cmd == "copy":
                _cmd_copy(state, rest)
            elif cmd == "search":
                _cmd_search(state, rest)
            elif cmd == "tags":
                _cmd_tags(state, " ".join(rest))
            elif cmd == "annotate":
                _cmd_annotate(state, rest)
            else:
                warn(f"  unknown command {cmd!r} -- try `help`")
    finally:
        restore()
