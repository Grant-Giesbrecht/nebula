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
from pathlib import Path

from nebula import graph, index
from nebula.registry import get_registry
from nebula.sidecar import read_session_yaml


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
    query = "SELECT run_id, created, status, tags, description FROM sessions"
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
        print(f"{row['run_id']}  {row['created']}  [{row['status']:7}]  "
              f"{tag_str:20}  {row['description']}")


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

    related = conn.execute(
        "SELECT ref_archive, ref_session, ref_file FROM related_runs WHERE run_id = ?",
        (args.run_id,),
    ).fetchall()
    if related:
        print("  related_runs:")
        for r in related:
            print(f"    - {_fmt_ref_row(r)}")

    artifacts = conn.execute(
        "SELECT filename, repo, commit_hash, dirty, entry_point FROM artifacts "
        "WHERE run_id = ? ORDER BY filename",
        (args.run_id,),
    ).fetchall()
    print("  artifacts:")
    for a in artifacts:
        dirty_flag = " (dirty)" if a["dirty"] else ""
        commit_short = (a["commit_hash"] or "")[:8]
        print(
            f"    - {a['filename']:30} "
            f"{a['repo'] or '-'}@{commit_short or '-'}{dirty_flag}"
        )
        derived = conn.execute(
            "SELECT ref_archive, ref_session, ref_file FROM derived_from "
            "WHERE run_id = ? AND filename = ?",
            (args.run_id, a["filename"]),
        ).fetchall()
        for d in derived:
            print(f"        <- {_fmt_ref_row(d)}")
    conn.close()


def _fmt_ref_row(row) -> str:
    archive = row["ref_archive"] or "(local)"
    sess = row["ref_session"] or "(same session)"
    file = row["ref_file"] or "(whole session)"
    return f"{archive}|{sess}/{file}"


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
