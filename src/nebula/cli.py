"""
nebula CLI.

<archive> below may be either a registered archive name (see `nebula archives`)
or a literal filesystem path -- the registry is checked first, and if
the argument isn't a registered name, it's treated as a raw path. This
lets ad hoc/unregistered directories still work from the terminal.

Usage:
    nebula rebuild <archive>
    nebula ls <archive> [--tag TAG] [--status open|closed|crashed] [--today]
    nebula show <archive> <run_id>
    nebula import <archive> <run_id> FILE... [--from NOTE] [--as NAME] [--move] [--reopen]
    nebula import-new <archive> FILE... [--tags a,b] [--description D] [--from NOTE] [--move]
    nebula reconcile <archive> [run_id]         # write sidecars for hand-added files
    nebula rm <archive> <run_id> <file> [--reason R] [--force]
    nebula replace <archive> <run_id> <file> <new_file> [--reason R] [--from NOTE]
    nebula rm-session <archive> <run_id> [--reason R] [--force]
    nebula reseal <archive> <run_id> <file>     # re-record checksum after an intended edit
    nebula check <archive> [--no-checksums]     # integrity report (fsck), with fix hints
    nebula hold <archive> <run_id> [DURATION]   # e.g. 2h; omit to hold until Ctrl-C
    nebula release <archive> <run_id>           # (alias: close) clear a hold
    nebula upstream <archive> <run_id> <filename>
    nebula downstream <archive> <run_id> <filename> [--also-search ARCHIVE ...]
    nebula stale <archive> [--hours N]
    nebula archives
    nebula register <name> <root> [--git-org ORG]
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from pathlib import Path

from nebula import check as check_mod
from nebula import graph, index, manual
from nebula.registry import get_registry
from nebula.sidecar import read_session_yaml
# Import from the submodule directly: `nebula.session` the *name* is the
# session() context manager (re-exported in __init__), so `import nebula
# .session as ...` would grab the function, not the module.
from nebula.session import (
    HOLD_FOREVER,
    _find_session_dir,
    _hold_value_active,
    hold as hold_session,
    parse_duration,
    release as release_session,
)


def _resolve_archive_cli(text: str):
    """Lenient resolution for CLI use: try the registry first (so
    `nebula ls postdoc` works), fall back to treating the argument as a
    literal filesystem path (so `nebula ls /some/scratch/dir` also works
    for ad hoc/unregistered archives). Returns (root: Path, name: str)."""
    registry = get_registry()
    cfg = registry.try_get(text)
    if cfg is not None:
        return cfg.root, text
    return Path(text), "local"


def cmd_rebuild(args):
    root, _ = _resolve_archive_cli(args.archive)
    path = index.rebuild(root)
    print(f"rebuilt index at {path}")


def cmd_ls(args):
    root, _ = _resolve_archive_cli(args.archive)
    conn = index.open_index(root)
    query = "SELECT run_id, created, status, tags, description, hold_until FROM sessions"
    clauses = []
    params = []
    if args.status:
        clauses.append("status = ?")
        params.append(args.status)
    if args.today:
        clauses.append("substr(created, 1, 10) = ?")
        params.append(datetime.date.today().isoformat())
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    for row in rows:
        tags = json.loads(row["tags"])
        if args.tag and args.tag not in tags:
            continue
        tag_str = ",".join(tags) if tags else "-"
        held = "  HELD" if _hold_value_active(row["hold_until"]) else ""
        print(f"{row['run_id']}  {row['created']}  [{row['status']:7}]  "
              f"{tag_str:20}  {row['description']}{held}")


def cmd_show(args):
    root, _ = _resolve_archive_cli(args.archive)
    conn = index.open_index(root)
    session_row = conn.execute(
        "SELECT * FROM sessions WHERE run_id = ?", (args.run_id,)
    ).fetchone()
    if session_row is None:
        print(f"no session {args.run_id!r} in index", file=sys.stderr)
        sys.exit(1)

    print(f"{session_row['run_id']}  [{session_row['status']}]")
    print(f"  created:     {session_row['created']}")
    print(f"  tags:        {', '.join(json.loads(session_row['tags']))}")
    print(f"  description: {session_row['description']}")
    print(f"  path:        {session_row['path']}")
    hold_until = session_row["hold_until"]
    if hold_until:
        active = _hold_value_active(hold_until)
        when = "indefinite" if hold_until == HOLD_FOREVER else hold_until
        state = "active" if active else "expired"
        print(f"  hold:        {when} ({state})")

    related = conn.execute(
        "SELECT ref_archive, ref_session, ref_file FROM related_runs WHERE run_id = ?",
        (args.run_id,),
    ).fetchall()
    if related:
        print("  related_runs:")
        for r in related:
            print(f"    - {_fmt_ref_row(r)}")

    artifacts = conn.execute(
        "SELECT filename, repo, commit_hash, dirty, entry_point, source, origin "
        "FROM artifacts WHERE run_id = ? ORDER BY filename",
        (args.run_id,),
    ).fetchall()
    print("  artifacts:")
    for a in artifacts:
        if a["source"] == "external":
            # No git commit to show -- report where it actually came from.
            prov = f"external: {a['origin'] or '(no origin recorded)'}"
        else:
            dirty_flag = " (dirty)" if a["dirty"] else ""
            commit_short = (a["commit_hash"] or "")[:8]
            prov = f"{a['repo'] or '-'}@{commit_short or '-'}{dirty_flag}"
        print(f"    - {a['filename']:30} {prov}")
        derived = conn.execute(
            "SELECT ref_archive, ref_session, ref_file FROM derived_from "
            "WHERE run_id = ? AND filename = ?",
            (args.run_id, a["filename"]),
        ).fetchall()
        for d in derived:
            print(f"        <- {_fmt_ref_row(d)}")

    history = json.loads(session_row["history"] or "[]")
    if history:
        print("  history:")
        for h in history:
            note = f" -- {h['note']}" if h.get("note") else ""
            by = f" by {h['by']}" if h.get("by") else ""
            print(f"    - {h.get('at', '?')}  {h.get('action')} {h.get('file') or ''}"
                  f"{by}{note}")
    conn.close()


def _fmt_ref_row(row) -> str:
    archive = row["ref_archive"] or "(local)"
    sess = row["ref_session"] or "(same session)"
    file = row["ref_file"] or "(whole session)"
    return f"{archive}|{sess}/{file}"


def cmd_import(args):
    root, _ = _resolve_archive_cli(args.archive)
    if args.dest_name and len(args.files) != 1:
        print("--as can only be used with a single file", file=sys.stderr)
        sys.exit(1)
    try:
        for f in args.files:
            dest = manual.import_file(
                root, args.run_id, f,
                dest_name=args.dest_name, origin=args.origin,
                derived_from=args.derived_from, move=args.move,
                allow_frozen=args.reopen,
            )
            print(f"imported {f} -> {dest}")
    except (FileNotFoundError, FileExistsError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    index.rebuild(root)


def cmd_import_new(args):
    root, _ = _resolve_archive_cli(args.archive)
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    try:
        s = manual.import_new(
            root, args.files, tags=tags, description=args.description,
            origin=args.origin, move=args.move,
        )
    except (FileNotFoundError, FileExistsError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"created {s.id} with {len(args.files)} file(s) at {s.path}")
    index.rebuild(root)


def cmd_reconcile(args):
    root, _ = _resolve_archive_cli(args.archive)
    orphans = manual.find_orphan_files(root, args.run_id)
    if not orphans:
        print("no orphan files -- everything has a sidecar")
        return
    print(f"found {len(orphans)} file(s) without a sidecar:")
    for p in orphans:
        print(f"  {p}")
    choice = input(
        "\n(A) auto-stub all, (B) fill in each manually, (C) cancel? "
    ).strip().lower()

    if choice == "a":
        for p in orphans:
            manual.adopt_file(p, origin="reconciled: found without sidecar")
            print(f"  stubbed {p.name}")
    elif choice == "b":
        for p in orphans:
            print(f"\n{p}")
            origin = input("  origin / notes (where did this come from?): ").strip()
            df = input("  derived from (comma-separated refs, optional): ").strip()
            derived = [x.strip() for x in df.split(",") if x.strip()]
            manual.adopt_file(p, origin=origin or None, derived_from=derived)
            print(f"  wrote sidecar for {p.name}")
    else:
        print("cancelled")
        return
    index.rebuild(root)


def cmd_rm(args):
    root, _ = _resolve_archive_cli(args.archive)
    try:
        trashed = manual.delete_file(
            root, args.run_id, args.file,
            reason=args.reason, force=args.force,
        )
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"deleted {args.file} (moved to {trashed})")
    index.rebuild(root)


def cmd_replace(args):
    root, _ = _resolve_archive_cli(args.archive)
    try:
        dest = manual.replace_file(
            root, args.run_id, args.file, args.new_file,
            reason=args.reason, origin=args.origin, move=args.move,
        )
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"replaced {args.file} (old version in {dest.parent / manual.TRASH_DIRNAME})")
    index.rebuild(root)


def cmd_rm_session(args):
    root, _ = _resolve_archive_cli(args.archive)
    try:
        trashed = manual.delete_session(
            root, args.run_id, reason=args.reason, force=args.force,
        )
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"deleted session {args.run_id} (moved to {trashed})")
    index.rebuild(root)


def cmd_reseal(args):
    root, _ = _resolve_archive_cli(args.archive)
    try:
        new_sha = manual.reseal(root, args.run_id, args.file)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"resealed {args.file} -> sha256 {new_sha[:12]}...")
    index.rebuild(root)


def cmd_check(args):
    root, _ = _resolve_archive_cli(args.archive)
    # Pass the archive as the user typed it so suggested fixes are copy-pasteable.
    issues = check_mod.check(root, verify_checksums=not args.no_checksums,
                             archive_label=args.archive)
    if not issues:
        print("ok -- no integrity problems found")
        return
    for issue in issues:
        print(str(issue))
        if issue.fix:
            print(f"    fix: {issue.fix}")
    errors = [i for i in issues if i.severity == "error"]
    info = len(issues) - len(errors)
    print(f"\n{len(errors)} error(s), {info} info", file=sys.stderr)
    if errors:
        sys.exit(1)


def cmd_hold(args):
    root, _ = _resolve_archive_cli(args.archive)
    try:
        _find_session_dir(root, args.run_id)
    except FileNotFoundError:
        print(f"no session {args.run_id!r} under {root}", file=sys.stderr)
        sys.exit(1)

    if args.duration:
        try:
            seconds = parse_duration(args.duration)
        except ValueError:
            print(f"bad duration {args.duration!r} (try 2h, 90m, 45s, 1d)",
                  file=sys.stderr)
            sys.exit(1)
        until = hold_session(root, args.run_id, seconds=seconds)
        print(f"holding {args.run_id} until {until}")
        return

    # No duration: hold indefinitely and block, so the hold lasts exactly
    # as long as this command runs. The hold is also written to disk, so if
    # this process is killed uncleanly the session stays held -- run
    # `nebula release {id}` to clear a leftover hold.
    hold_session(root, args.run_id, seconds=None)
    print(f"holding {args.run_id} indefinitely. Press Ctrl-C to release.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        release_session(root, args.run_id)
        print(f"\nreleased {args.run_id}")


def cmd_release(args):
    root, _ = _resolve_archive_cli(args.archive)
    try:
        had_hold = release_session(root, args.run_id)
    except FileNotFoundError:
        print(f"no session {args.run_id!r} under {root}", file=sys.stderr)
        sys.exit(1)
    if had_hold:
        print(f"released hold on {args.run_id}")
    else:
        print(f"{args.run_id} had no hold")


def cmd_upstream(args):
    root, name = _resolve_archive_cli(args.archive)
    nodes = graph.upstream(root, args.run_id, args.filename, archive_name=name)
    if not nodes:
        print("(no upstream dependencies recorded)")
        return
    for n in nodes:
        print(str(n))


def cmd_downstream(args):
    root, name = _resolve_archive_cli(args.archive)
    nodes = graph.downstream(
        root,
        args.run_id,
        args.filename,
        archive_name=name,
        also_search_archives=args.also_search or [],
    )
    if not nodes:
        print("(nothing downstream recorded)")
        return
    for n in nodes:
        print(str(n))


def cmd_stale(args):
    root, _ = _resolve_archive_cli(args.archive)
    conn = index.open_index(root)
    stale = index.flag_stale_open_sessions(conn, older_than_hours=args.hours)
    conn.close()
    if not stale:
        print(f"no sessions open longer than {args.hours}h")
        return
    for row in stale:
        print(f"{row['run_id']}  opened {row['created']}  {row['path']}")


def cmd_archives(args):
    reg = get_registry()
    archives = reg.all()
    if not archives:
        print(f"no archives registered in {reg.path}")
        return
    for name, cfg in archives.items():
        exists = "✓" if cfg.root.exists() else "✗ (not mounted?)"
        print(f"{name:15} {cfg.root}  {exists}")


def cmd_register(args):
    reg = get_registry()
    reg.register(args.name, Path(args.root), git_org=args.git_org)
    print(f"registered archive {args.name!r} -> {args.root}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nebula")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("rebuild", help="rebuild the SQLite index from sidecar files")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.set_defaults(func=cmd_rebuild)

    p = sub.add_parser("ls", help="list sessions")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("--tag")
    p.add_argument("--status", choices=["open", "closed", "crashed"])
    p.add_argument("--today", action="store_true")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("show", help="show details for one session")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("import", help="add external file(s) to an existing session")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id")
    p.add_argument("files", nargs="+", help="file(s) to import")
    p.add_argument("--from", dest="origin", help="free-text note on where it came from")
    p.add_argument("--as", dest="dest_name", help="rename the file (single file only)")
    p.add_argument("--move", action="store_true", help="move instead of copy")
    p.add_argument("--reopen", action="store_true",
                   help="allow importing into a session closed on a previous day")
    p.add_argument("--derived-from", nargs="*", dest="derived_from",
                   help="ref(s) this file was derived from")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("import-new", help="create a new session seeded with external files")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("files", nargs="+", help="file(s) to seed the session with")
    p.add_argument("--from", dest="origin", help="free-text note on where it came from")
    p.add_argument("--tags", help="comma-separated tags")
    p.add_argument("--description", default="")
    p.add_argument("--move", action="store_true", help="move instead of copy")
    p.set_defaults(func=cmd_import_new)

    p = sub.add_parser("reconcile", help="write sidecars for files added to a session by hand")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id", nargs="?", help="limit to one session (default: whole archive)")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("rm", help="soft-delete an artifact (moves it to the session's .trash/)")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id")
    p.add_argument("file")
    p.add_argument("--reason", help="why it's being deleted (recorded in history)")
    p.add_argument("--force", action="store_true",
                   help="delete even if another artifact derives from it")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("replace", help="replace an artifact's bytes, trashing the old version")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id")
    p.add_argument("file")
    p.add_argument("new_file", help="file whose bytes replace the artifact")
    p.add_argument("--reason", help="why it's being replaced (recorded in history)")
    p.add_argument("--from", dest="origin", help="free-text note on where the new bytes came from")
    p.add_argument("--move", action="store_true", help="move instead of copy the new file")
    p.set_defaults(func=cmd_replace)

    p = sub.add_parser("rm-session", help="soft-delete a whole session (moves it to the archive .trash/)")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id")
    p.add_argument("--reason", help="why it's being deleted (recorded in history)")
    p.add_argument("--force", action="store_true",
                   help="delete even if another session references it")
    p.set_defaults(func=cmd_rm_session)

    p = sub.add_parser("reseal", help="re-record an artifact's checksum after an intended edit")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id")
    p.add_argument("file")
    p.set_defaults(func=cmd_reseal)

    p = sub.add_parser("check", help="report integrity problems in an archive (fsck)")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("--no-checksums", action="store_true",
                   help="skip re-hashing files (faster on large archives)")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser(
        "hold",
        help="keep a session appendable past its start day (e.g. across midnight)",
    )
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id")
    p.add_argument(
        "duration",
        nargs="?",
        help="how long to hold, e.g. 2h / 90m / 45s / 1d. Omit to hold "
             "until this command is stopped with Ctrl-C.",
    )
    p.set_defaults(func=cmd_hold)

    p = sub.add_parser(
        "release", aliases=["close"],
        help="clear a hold placed with 'hold' (does not change open/closed status)",
    )
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("upstream", help="what did this artifact depend on")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id")
    p.add_argument("filename")
    p.set_defaults(func=cmd_upstream)

    p = sub.add_parser("downstream", help="what depends on this artifact")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id")
    p.add_argument("filename")
    p.add_argument("--also-search", nargs="*", help="other registered archive names to scan")
    p.set_defaults(func=cmd_downstream)

    p = sub.add_parser("stale", help="find sessions left open too long")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("--hours", type=float, default=24.0)
    p.set_defaults(func=cmd_stale)

    p = sub.add_parser("archives", help="list registered archives")
    p.set_defaults(func=cmd_archives)

    p = sub.add_parser("register", help="register an archive in ~/.nebula/archives.yaml")
    p.add_argument("name")
    p.add_argument("root")
    p.add_argument("--git-org")
    p.set_defaults(func=cmd_register)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
