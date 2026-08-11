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
    nebula asset import <archive> <file>...     # bring a mutable file under management
    nebula asset ls|show|path <archive> [id]
    nebula asset commit <archive> <id> [-m ...] # save the current bytes as a version
    nebula asset history|policy|scan <archive> <id>
    nebula check <archive> [--no-checksums]     # integrity report (fsck), with fix hints
    nebula hold <archive> <run_id> [DURATION]   # e.g. 2h; omit to hold until Ctrl-C
    nebula release <archive> <run_id>           # (alias: close) clear a hold
    nebula upstream <archive> <run_id> <filename>
    nebula downstream <archive> <run_id> <filename> [--also-search ARCHIVE ...]
    nebula stale <archive> [--hours N]
    nebula index <archive> [--rebuild]          # index status / freshness
    nebula seal <archive> <year> [--force]      # declare a year finished
    nebula unseal <archive> <year>
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

from nebula.config import ASSET_POLICIES, OVERWRITE_POLICIES
from nebula import check as check_mod
from nebula import assets, config, graph, index, manual
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
    st = index.status(root)
    print(f"rebuilt index at {path} ({st['sessions']} session(s))")


def cmd_index(args):
    """Index status, and the freshness sweep on demand."""
    root, _ = _resolve_archive_cli(args.archive)
    if args.rebuild:
        cmd_rebuild(args)
        return
    before = index.status(root)
    if not before["exists"]:
        print("no index yet (any read will build one; or run `nebula rebuild`)")
        return
    swept = index.ensure_fresh(root)
    st = index.status(root)
    print(f"index:     {st['path']}")
    print(f"  built:      {st['built'] or '-'}")
    print(f"  sessions:   {st['sessions']}")
    print(f"  size:       {st['size']} bytes")
    print(f"  schema:     v{st['schema_version'] or '?'} "
          f"(current v{st['current_schema']})")
    if swept.get("rebuilt"):
        print(f"  swept:      full rebuild -- {swept.get('reason')}")
    else:
        print(f"  swept:      {swept['checked_sessions']} session(s) checked, "
              f"{swept['added']} added, {swept['updated']} updated, "
              f"{swept['removed']} removed")
        if swept["skipped_years"]:
            print(f"  skipped:    {', '.join(swept['skipped_years'])} (sealed)")
    for seal in st["sealed_years"]:
        print(f"  sealed:     {seal['year']}  {seal['sessions']} session(s)  "
              f"{(seal['digest'] or '')[:12]}  {seal['sealed']}")


def cmd_seal(args):
    """Declare a year finished so freshness sweeps can skip it."""
    root, _ = _resolve_archive_cli(args.archive)
    try:
        path = index.seal_year(root, args.year, force=args.force)
    except index.IndexError_ as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    seal = index.read_seal(root, args.year) or {}
    print(f"sealed {args.year}: {seal.get('sessions')} session(s), "
          f"digest {(seal.get('digest') or '')[:12]}")
    print(f"  {path}")
    print("  freshness sweeps will now skip this year; "
          "`nebula check` still verifies it")


def cmd_unseal(args):
    root, _ = _resolve_archive_cli(args.archive)
    if index.unseal_year(root, args.year):
        print(f"unsealed {args.year}; it will be swept normally again")
    else:
        print(f"{args.year} was not sealed")


def cmd_ls(args):
    root, _ = _resolve_archive_cli(args.archive)
    conn = index.open_fresh(root)
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
    conn = index.open_fresh(root)
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
    print(f"  path:        {index.session_path(root, session_row)}")
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


# ---------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------

def _fmt_bytes(n) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return str(n)


def _asset_id_arg(text: str) -> str:
    """argparse type for asset ids. Accepts a bare number for the current
    year, mirroring `nebula show arc 12` for sessions -- the id is opaque
    but its shape is not, and typing the whole thing is friction the
    session commands already decided not to impose."""
    raw = (text or "").strip()
    if assets.is_asset_id(raw):
        return raw
    if raw.isdigit():
        year2 = datetime.datetime.now().year % 100
        return assets.format_asset_id(year2, int(raw))
    raise argparse.ArgumentTypeError(
        f"not an asset id: {text!r} (expected AF-26-0017, or 0017 for the "
        f"current year)")


def cmd_asset_import(args):
    root, _ = _resolve_archive_cli(args.archive)
    settings = config.read_settings(root, apply_env=False)
    try:
        for f in args.files:
            size = Path(f).stat().st_size
            meta = assets.import_asset(
                root, f, name=args.dest_name, policy=args.policy,
                derived_from=args.derived_from, origin=args.origin,
                move=args.move,
            )
            resolved = meta.effective_policy(settings)
            # Say which policy it landed on and why, so the size ladder is
            # something the user learns rather than something that happens
            # to them.
            why = f"auto -> {resolved}, {_fmt_bytes(size)}" if args.policy is None \
                else resolved
            print(f"{meta.id}  {meta.name}  [{why}]")
    except (assets.AssetError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_asset_ls(args):
    root, _ = _resolve_archive_cli(args.archive)
    settings = config.read_settings(root, apply_env=False)
    rows = []
    for asset_id in assets.list_assets(root):
        try:
            meta = assets.read_asset(root, asset_id)
        except assets.AssetError:
            continue
        policy = meta.effective_policy(settings)
        if args.policy and policy != args.policy:
            continue
        rows.append((meta, policy))

    if args.sort == "name":
        rows.sort(key=lambda r: (r[0].name or "").lower())
    elif args.sort == "size":
        rows.sort(key=lambda r: r[0].size or 0, reverse=True)
    else:
        # Recency is the default because assets are re-used, not
        # discovered: what you touched last is usually what you want.
        rows.sort(key=lambda r: r[0].scanned_at or r[0].created, reverse=True)

    if not rows:
        print("no assets")
        return
    for meta, policy in rows:
        kept = sum(1 for s in meta.snapshots if not s.pending_gc)
        marker = "*" if meta.policy == "auto" else ""
        print(f"{meta.id}  {(meta.name or '?'):40.40} "
              f"{_fmt_bytes(meta.size):>9}  {policy + marker:14} {kept} snap")
    print(f"\n{len(rows)} asset(s)   (* = auto, resolved from size)",
          file=sys.stderr)


def cmd_asset_show(args):
    root, _ = _resolve_archive_cli(args.archive)
    settings = config.read_settings(root, apply_env=False)
    try:
        meta = assets.read_asset(root, args.asset_id)
    except assets.AssetError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    path = assets.live_file(root, meta.id)
    print(f"{meta.id}  {meta.name}")
    print(f"  path:     {path or '(missing on disk)'}")
    print(f"  size:     {_fmt_bytes(meta.size)}")
    print(f"  sha256:   {(meta.sha256 or '-')[:16]}...")
    print(f"  created:  {meta.created}"
          + (f" by {meta.imported_by}" if meta.imported_by else ""))
    declared = meta.policy or assets.AUTO_ASSET_POLICY
    resolved = meta.effective_policy(settings)
    print(f"  policy:   {declared}"
          + (f" -> {resolved}" if declared == "auto" else ""))
    if resolved == "periodic":
        print(f"  period:   every {meta.effective_period_days(settings)} day(s)")
    for label, val, dflt in (
        ("max snaps", meta.max_snapshots, settings.asset_max_snapshots),
        ("max bytes", meta.max_snapshot_bytes, settings.asset_max_snapshot_bytes),
    ):
        shown = val if val is not None else dflt
        if shown:
            src = "" if val is not None else " (archive default)"
            print(f"  {label}: {shown}{src}")
    if meta.origin:
        print(f"  origin:   {meta.origin}")
    for ref in meta.derived_from:
        print(f"  <- {_fmt_ref_row_dict(ref)}")
    if meta.renames:
        print("  renames:")
        for r in meta.renames:
            print(f"    - {r.get('at', '?')}  {r.get('from')} -> {r.get('to')}")
    kept = sum(1 for s in meta.snapshots if not s.pending_gc)
    print(f"  snapshots: {len(meta.snapshots)} ({kept} retained)")


def _fmt_ref_row_dict(d) -> str:
    archive = d.get("archive") or "(local)"
    sess = d.get("session") or "(same session)"
    file = d.get("file") or "(whole session)"
    return f"{archive}|{sess}/{file}"


def cmd_asset_commit(args):
    root, _ = _resolve_archive_cli(args.archive)
    try:
        snap = assets.commit(root, args.asset_id, note=args.note,
                             force=args.force)
    except assets.AssetError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    if snap is None:
        print("no change since the last snapshot -- nothing committed")
        return
    # Print the full sha: the stated use is quoting it in notes alongside
    # the filename, and a truncated hash is not quotable.
    print(f"committed {args.asset_id} @ {snap.sha256}")


def cmd_asset_history(args):
    root, _ = _resolve_archive_cli(args.archive)
    try:
        snaps = assets.history(root, args.asset_id)
    except assets.AssetError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    if not snaps:
        print("no snapshots")
        return
    for s in snaps:
        flag = " (evicted, pending gc)" if s.pending_gc else ""
        by = f" by {s.by}" if s.by else ""
        note = f" -- {s.note}" if s.note else ""
        print(f"{s.at}  {s.sha256[:12]}  {_fmt_bytes(s.bytes):>9}  "
              f"{s.trigger}{by}{note}{flag}")


def cmd_asset_policy(args):
    root, _ = _resolve_archive_cli(args.archive)
    settings = config.read_settings(root, apply_env=False)
    try:
        meta = assets.set_policy(
            root, args.asset_id, args.policy,
            period_days=args.period_days,
            max_snapshots=args.max_snapshots,
            max_snapshot_bytes=args.max_snapshot_bytes,
        )
    except assets.AssetError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    resolved = meta.effective_policy(settings)
    declared = meta.policy or assets.AUTO_ASSET_POLICY
    print(f"{meta.id} policy: {declared}"
          + (f" -> {resolved}" if declared == "auto" else ""))


def cmd_asset_scan(args):
    root, _ = _resolve_archive_cli(args.archive)
    ids = [args.asset_id] if args.asset_id else assets.list_assets(root)
    changed = 0
    for asset_id in ids:
        try:
            state = assets.scan(root, asset_id)
        except assets.AssetError as e:
            print(f"error: {e}", file=sys.stderr)
            continue
        if state["missing"]:
            print(f"{asset_id}: file missing on disk")
            changed += 1
        elif state["renamed"]:
            old, new = state["renamed"]
            print(f"{asset_id}: renamed {old} -> {new}")
            changed += 1
        elif state["changed"]:
            print(f"{asset_id}: edited ({state['sha256'][:12]})")
            changed += 1
    print(f"scanned {len(ids)} asset(s), {changed} changed", file=sys.stderr)


def cmd_asset_path(args):
    """Print the asset's path and nothing else, so it composes:
    `open $(nebula asset path arc 17)`. The storage layout is opaque by
    design, which makes this the supported way to open one by hand."""
    root, _ = _resolve_archive_cli(args.archive)
    path = assets.live_file(root, args.asset_id)
    if path is None:
        print(f"error: {args.asset_id} has no file on disk", file=sys.stderr)
        sys.exit(1)
    print(path)


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


def _run_id_arg(text: str) -> str:
    """argparse type for session ids: accepts the canonical S-<yy>-<nnnn>
    or a bare number for the current year, so `nebula show arc 12` works.

    The prefix a bare number expands to depends on the archive, which
    argparse cannot see here -- so this assumes S- and the commands that
    know their archive re-resolve (see _run_id_for)."""
    from nebula.session import resolve_run_id

    try:
        return resolve_run_id(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def _run_id_for(root, text: str) -> str:
    """Re-resolve a session id against the archive it belongs to, so a bare
    number typed at an intake archive becomes I-26-0012 rather than S-."""
    from nebula.config import read_settings
    from nebula.session import resolve_run_id

    try:
        return resolve_run_id(text, prefix=read_settings(root, apply_env=False).prefix)
    except ValueError:
        return text


def _bool_arg(text: str) -> bool:
    val = text.strip().lower()
    if val in ("true", "yes", "on", "1"):
        return True
    if val in ("false", "no", "off", "0"):
        return False
    raise argparse.ArgumentTypeError(f"expected true or false, got {text!r}")


def cmd_config(args):
    """Show or edit an archive's settings (<archive>/archive.yaml)."""
    from nebula import config as config_mod

    root, _ = _resolve_archive_cli(args.archive)
    path = config_mod.config_path(root)
    changes = {
        "capture_code": args.capture_code,
        "code_max_file_bytes": args.max_file_bytes,
        "on_overwrite": args.on_overwrite,
        "auto_index": args.auto_index,
    }
    changes = {k: v for k, v in changes.items() if v is not None}

    if changes:
        if not root.is_dir():
            print(f"no archive at {root}", file=sys.stderr)
            sys.exit(1)
        # Read the file's own values, not the env-overridden ones, so a
        # temporary NEBULA_CAPTURE_CODE never gets written into the archive.
        settings = config_mod.read_settings(root, apply_env=False)
        for key, value in changes.items():
            setattr(settings, key, value)
        config_mod.write_settings(root, settings)
        print(f"wrote {path}")

    on_disk = config_mod.read_settings(root, apply_env=False)
    effective = config_mod.read_settings(root)
    print(f"{path}{'' if path.exists() else '  (not present -- using defaults)'}")
    for key in ("on_overwrite", "capture_code", "code_max_file_bytes", "auto_index"):
        print(f"  {key}: {getattr(on_disk, key)}")

    override = config_mod.env_override()
    if override is not None and override != on_disk.capture_code:
        print(f"\nnote: {config_mod.CAPTURE_ENV} is set in this environment, so "
              f"capture_code is currently {effective.capture_code} regardless of the file")


def cmd_annotate(args):
    """Show or edit the mutable user tags/comment on a session or file."""
    from nebula import annotations
    from nebula.session import _find_session_dir

    root, _ = _resolve_archive_cli(args.archive)
    try:
        session_dir = _find_session_dir(root, args.run_id)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    target = args.file
    changed = False
    try:
        if args.set_tags is not None:
            annotations.set_annotation(session_dir, target,
                                       tags=annotations.split_tags(args.set_tags))
            changed = True
        if args.add_tags:
            annotations.add_tags(session_dir, target,
                                 annotations.split_tags(args.add_tags))
            changed = True
        if args.rm_tags:
            annotations.remove_tags(session_dir, target,
                                    annotations.split_tags(args.rm_tags))
            changed = True
        if args.comment is not None:
            annotations.set_annotation(session_dir, target, comment=args.comment)
            changed = True
    except annotations.TagError as e:
        print(f"bad tag: {e}", file=sys.stderr)
        sys.exit(1)

    got = annotations.get(session_dir, target)
    where = f"{args.run_id}/{target}" if target else args.run_id
    print(f"{where}{'  (updated)' if changed else ''}")
    print(f"  user tags: {', '.join(got['tags']) if got['tags'] else '(none)'}")
    if got["comment"]:
        print("  comment:")
        for line in got["comment"].splitlines():
            print(f"    {line}")
    else:
        print("  comment: (none)")


def cmd_gc(args):
    """Sweep captured source, and asset versions, that nothing references.

    The two stores are swept separately because their liveness rules
    differ -- see nebula.assetstore on why they are not one store.
    """
    from nebula import assetstore, codestore

    root, _ = _resolve_archive_cli(args.archive)
    _gc_assets(root, args)
    res = codestore.gc(root, dry_run=not args.delete,
                       include_trash=not args.ignore_trash)
    n = len(res["manifests"]) + len(res["blobs"])
    kb = res["bytes"] / 1024
    print(f"reachable: {res['live_manifests']} manifest(s), {res['live_blobs']} blob(s)")
    if not n:
        print("nothing to collect")
        return
    verb = "would delete" if res["dry_run"] else "deleted"
    print(f"{verb}: {len(res['manifests'])} manifest(s), {len(res['blobs'])} blob(s) "
          f"({kb:.1f} KB)")
    if res["dry_run"]:
        print("(dry run -- pass --delete to actually remove them)")


def _gc_assets(root, args) -> None:
    from nebula import assetstore

    res = assetstore.gc(root, dry_run=not args.delete,
                        include_trash=not args.ignore_trash)
    if res["skipped"]:
        # Never let a partial sweep read as a clean one: the blobs spared
        # are exactly the ones whose safety could not be established.
        print(f"warning: asset blob collection skipped -- {res['skipped']}",
              file=sys.stderr)
        print(f"    fix: repair the asset record (see 'nebula check "
              f"{args.archive}'), then re-run", file=sys.stderr)
        return
    print(f"assets: {res['live_blobs']} version(s) still referenced")
    if res["blobs"]:
        verb = "would delete" if res["dry_run"] else "deleted"
        print(f"{verb}: {len(res['blobs'])} asset version(s) "
              f"({res['bytes'] / 1024:.1f} KB)")


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
    conn = index.open_fresh(root)
    stale = index.flag_stale_open_sessions(conn, older_than_hours=args.hours)
    conn.close()
    if not stale:
        print(f"no sessions open longer than {args.hours}h")
        return
    for row in stale:
        print(f"{row['run_id']}  opened {row['created']}  "
              f"{index.session_path(root, row)}")


def cmd_archives(args):
    reg = get_registry()
    archives = reg.all()
    if not archives:
        print(f"no archives registered in {reg.path}")
        return
    for name, cfg in archives.items():
        exists = "✓" if cfg.root.exists() else "✗ (not mounted?)"
        print(f"{name:15} {cfg.root}  {exists}")


def cmd_collection(args):
    """Collections: nestable, curated sets of refs."""
    from nebula import collection as collection_mod

    root, label = _resolve_archive_cli(args.archive)
    act = args.collection_action
    try:
        if act == "list":
            colls = collection_mod.list_all(root)
            if not colls:
                print("(no collections in this archive)")
                return
            for c in colls:
                title = f"  {c.title}" if c.title else ""
                print(f"{c.name:24} {len(c.entries):3} entrie(s){title}")
            return

        if act == "new":
            c = collection_mod.create(root, args.name, title=args.title or "")
            print(f"created {collection_mod.path_for(root, c.name)}")
            return

        if act == "rm":
            print("removed" if collection_mod.delete(root, args.name)
                  else f"no collection {args.name!r}")
            return

        if act == "rename":
            coll = collection_mod.rename(root, args.name, args.new,
                                         title=args.title)
            print(f"renamed to {coll.name}")
            print(f"  {collection_mod.path_for(root, coll.name)}")
            return

        if act == "add":
            for ref in args.refs:
                collection_mod.add(root, args.name, ref, note=args.note or "")
                print(f"added {ref} to {args.name}")
            return

        if act == "remove":
            for ref in args.refs:
                collection_mod.remove(root, args.name, ref)
                print(f"removed {ref} from {args.name}")
            return

        if act == "show":
            _print_collection(collection_mod.tree(root, args.name))
            return
    except (collection_mod.CollectionError, ValueError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


def _print_collection(node, indent=0):
    pad = " " * indent
    if node.get("missing"):
        print(f"{pad}[{node['name']}]  (no such collection)")
        return
    title = f"  -- {node['title']}" if node.get("title") else ""
    print(f"{pad}[{node['name']}]{title}"
          + ("   (cycle)" if node.get("cycle") else "")
          + ("   (depth limit)" if node.get("truncated") else ""))
    for e in node.get("entries", []):
        mark = "ok" if e["exists"] else ("??" if not e["resolved"] else "!!")
        note = f"   -- {e['note']}" if e.get("note") else ""
        why = f"   ({e['note_error']})" if e.get("note_error") else ""
        print(f"{pad}  {mark} {e['kind']:10} {e['ref']}{note}{why}")
        if e.get("child"):
            _print_collection(e["child"], indent + 4)


def cmd_view(args):
    """Saved searches."""
    from nebula import views as views_mod

    root, _ = _resolve_archive_cli(args.archive)
    act = args.view_action
    try:
        if act == "list":
            found = views_mod.list_all(root)
            if not found:
                print("(no saved views in this archive)")
                return
            for v in found:
                bits = [f"query={v.query!r}" if v.query else "no query"]
                if v.fields:
                    bits.append("fields=" + ",".join(v.fields))
                if v.date_from or v.date_to:
                    bits.append(f"dates={v.date_from or '*'}..{v.date_to or '*'}")
                print(f"{v.name:24} {'; '.join(bits)}")
            return

        if act == "save":
            v = views_mod.save(root, args.name, query=args.query or "",
                               title=args.title or "",
                               fields=(args.fields.split(",") if args.fields else None),
                               date_from=args.date_from, date_to=args.date_to)
            print(f"saved {views_mod.path_for(root, v.name)}")
            return

        if act == "rm":
            print("removed" if views_mod.delete(root, args.name)
                  else f"no view {args.name!r}")
            return

        if act == "run":
            res = views_mod.run(root, args.name)
            hits = res["items"]
            print(f"{len(hits)} match(es) in {res['n_sessions']} session(s)")
            for hit in hits:
                print(f"  {hit['run_id']}/{hit['item'].name}")
            return
    except (views_mod.ViewError, ValueError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


def cmd_whoami(args):
    from nebula import identity

    if args.set_user:
        try:
            path = identity.set_user(args.set_user)
        except identity.IdentityError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        print(f"wrote {path}")

    user = identity.get_user()
    if user:
        print(user)
    else:
        print("(no user name set -- 'nebula whoami --set <name>' to pick one)")
        print(f"would be stored in {identity.identity_path()}", file=sys.stderr)


def cmd_register(args):
    """Register an archive under the name it declares for itself.

    The name and owner are part of a nebula URI, so an archive has to keep
    the ones its author used -- otherwise a fragment stops resolving under
    the name they cited. A name can still be forced with --as for the rare
    collision the automatic <user>-<name> fallback doesn't suit.
    """
    from nebula.config import archive_identity

    reg = get_registry()
    root = Path(args.root)
    if args.name and not root.exists() and Path(args.name).exists():
        root, args.name = Path(args.name), None      # tolerate reversed arguments
    ident = archive_identity(root)
    cfg = reg.register_archive(root, git_org=args.git_org, key=args.name or None)
    kind = f" ({ident['kind']})" if ident["kind"] != "standard" else ""
    owner = f" owned by {ident['user']}" if ident["user"] else ""
    print(f"registered {cfg.name!r}{kind}{owner} -> {cfg.root}")
    if not ident["declared"]:
        print(f"  note: {root/'archive.yaml'} does not name this archive, so its "
              f"folder name was used. 'nebula init' records one.")


def cmd_scan(args):
    """Discover archives under NEBULA_HOME and register what is new."""
    from nebula.registry import nebula_home

    home = Path(args.home) if args.home else nebula_home()
    found = get_registry().discover(home)
    if not found:
        print(f"no new archives under {home}")
        return
    for cfg in found:
        kind = f" ({cfg.kind})" if cfg.kind != "standard" else ""
        print(f"registered {cfg.name!r}{kind} -> {cfg.root}")


def cmd_init(args):
    """Create an archive that knows its own name, owner and kind."""
    from nebula import identity, transfer
    from nebula.config import ArchiveSettings, archive_identity

    settings = ArchiveSettings(
        on_overwrite=args.on_overwrite or "duplicate",
        capture_code=True if args.capture_code is None else args.capture_code,
        auto_index=True if args.auto_index is None else args.auto_index,
        code_max_file_bytes=args.max_file_bytes or 1048576,
    )
    try:
        root = transfer.init_archive(Path(args.root), kind=args.kind,
                                     name=args.name or "", user=args.user or "",
                                     settings=settings)
    except transfer.TransferError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    ident = archive_identity(root)
    print(f"created {ident['kind']} archive {ident['name']!r} at {root}")
    print(f"  archive.yaml, data/ and code/")
    print(f"  on_overwrite={settings.on_overwrite} capture_code={settings.capture_code} "
          f"auto_index={settings.auto_index}")
    if not ident["user"]:
        # The owner is half of a nebula:// URI, so an archive without one
        # cannot be referred to unambiguously once it leaves this machine.
        print("\nno user name is set on this machine, so this archive records no "
              "owner.\nRefs into it from elsewhere will be ambiguous -- set one with "
              "'nebula whoami --set <name>',\nthen 'nebula config' this archive again.",
              file=sys.stderr)
    if args.register:
        cfg = get_registry().register_archive(root)
        print(f"registered as {cfg.name!r}")


def cmd_intake(args):
    """Create a timestamped intake archive for capturing data."""
    from nebula import transfer

    root = transfer.new_intake(Path(args.parent), label=args.label or "")
    print(f"created intake archive {root.name}")
    print(f"  {root}")
    print(f"  sessions here are numbered I-<yy>-<nnnn> and are provisional: "
          f"merging renames them and records what they became.")


def _print_plan(plan, *, verb: str) -> None:
    d = plan.to_dict()
    print(f"{verb}: {d['n_sessions']} session(s), {d['n_files']} file(s), "
          f"{_human_bytes(d['bytes'])}")
    if d["foreign_bytes"]:
        print(f"  including {_human_bytes(d['foreign_bytes'])} belonging to other "
              f"archives")
    for s in d["sessions"]:
        arrow = f" -> {s['new_run_id']}" if s["new_run_id"] != s["run_id"] else ""
        extra = []
        if s["partial"]:
            extra.append(f"{s['omitted']} file(s) omitted")
        if s["note"]:
            extra.append(s["note"])
        print(f"  {s['run_id']}{arrow}  {len(s['files'])} file(s)"
              + (f"  [{'; '.join(extra)}]" if extra else ""))
    for skip in d["skipped"]:
        print(f"  skipped {skip['run_id']}: {skip['note']}")
    for item in d["dangling"]:
        print(f"  dangling {item['ref']}: {item['note']}")
    for warning in d["warnings"]:
        print(f"  warning: {warning}", file=sys.stderr)


def _human_bytes(n: int) -> str:
    step = 1024.0
    value = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < step or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TB"


def cmd_export(args):
    from nebula import transfer

    root, _ = _resolve_archive_cli(args.archive)
    try:
        plan = transfer.plan_export(
            root, Path(args.dest), sessions=args.session or None,
            refs=args.ref or None, collection=args.collection,
            include_foreign=not args.exclude_foreign)
    except transfer.TransferError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    _print_plan(plan, verb="would export" if args.dry_run else "exporting")
    if args.dry_run:
        return
    transfer.export(root, Path(args.dest), plan=plan)
    print(f"wrote fragment to {args.dest}")


def cmd_merge(args):
    from nebula import transfer

    src, _ = _resolve_archive_cli(args.source)
    dst, _ = _resolve_archive_cli(args.dest)
    try:
        plan = transfer.plan_merge(src, dst)
    except transfer.TransferError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    _print_plan(plan, verb="would merge" if args.dry_run else "merging")
    if args.dry_run:
        return
    if not plan.sessions:
        print("nothing to merge")
        return
    try:
        transfer.merge(src, dst, verify=not args.no_verify, lock=not args.no_lock)
    except transfer.TransferError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    print(f"merged into {plan.dest}; {plan.source} is now locked "
          f"(nebula unlock {args.source} to keep using it)")


def cmd_adopt(args):
    from nebula import transfer

    src, _ = _resolve_archive_cli(args.source)
    dst, _ = _resolve_archive_cli(args.dest)
    try:
        plan = transfer.plan_adopt(src, dst, sessions=args.session or None)
    except transfer.TransferError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    _print_plan(plan, verb="would adopt" if args.dry_run else "adopting")
    if args.dry_run:
        return
    if not plan.sessions:
        print("nothing to adopt")
        return
    transfer.adopt(src, dst, plan=plan, verify=not args.no_verify)
    print(f"adopted {len(plan.sessions)} session(s) into {plan.dest}; "
          f"{plan.source} was not modified")


def cmd_receive(args):
    """File an incoming fragment where refs into it resolve."""
    from nebula import transfer

    try:
        plans = transfer.plan_receive(args.source, home=args.home)
    except transfer.TransferError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    for item in plans:
        where = "already here" if item["exists"] else "new"
        nested = " (nested)" if item["nested"] else ""
        print(f"{item['user'] or 'unknown'}/{item['name']}{nested} -> {item['dest']}"
              f"  [{where}]")
    if args.dry_run:
        return
    got = transfer.receive(args.source, home=args.home,
                           overwrite_foreign=args.overwrite_foreign)
    print(f"installed {len(got['installed'])} fragment(s): "
          f"{got['added']} session(s) added, {got['skipped']} already present")
    for c in got["conflicts"]:
        print(f"  conflict: {c['archive']}/{c['run_id']} differs "
              f"({', '.join(c['files'][:3])}) -- {c['note']}", file=sys.stderr)


def cmd_prune(args):
    from nebula import transfer

    root, _ = _resolve_archive_cli(args.archive)
    try:
        got = transfer.prune(root, force=args.force)
    except transfer.TransferError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    print(f"deleted {got['removed']}")


def cmd_unlock(args):
    from nebula import transfer

    root, _ = _resolve_archive_cli(args.archive)
    got = transfer.unlock(root)
    was = got["was"]
    if was["merged_at"]:
        print(f"unlocked; it had been merged into {was['merged_to']} on {was['merged_at']}")
        print("anything written now will need merging again")
    else:
        print("it was not locked")


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
    p.add_argument("run_id", type=_run_id_arg, help="session id -- S-26-0012, or 0012 for the current year")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("import", help="add external file(s) to an existing session")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id", type=_run_id_arg, help="session id -- S-26-0012, or 0012 for the current year")
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
    p.add_argument("run_id", nargs="?", type=_run_id_arg,
                   help="limit to one session (default: whole archive)")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("rm", help="soft-delete an artifact (moves it to the session's .trash/)")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id", type=_run_id_arg, help="session id -- S-26-0012, or 0012 for the current year")
    p.add_argument("file")
    p.add_argument("--reason", help="why it's being deleted (recorded in history)")
    p.add_argument("--force", action="store_true",
                   help="delete even if another artifact derives from it")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("replace", help="replace an artifact's bytes, trashing the old version")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id", type=_run_id_arg, help="session id -- S-26-0012, or 0012 for the current year")
    p.add_argument("file")
    p.add_argument("new_file", help="file whose bytes replace the artifact")
    p.add_argument("--reason", help="why it's being replaced (recorded in history)")
    p.add_argument("--from", dest="origin", help="free-text note on where the new bytes came from")
    p.add_argument("--move", action="store_true", help="move instead of copy the new file")
    p.set_defaults(func=cmd_replace)

    p = sub.add_parser("rm-session", help="soft-delete a whole session (moves it to the archive .trash/)")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id", type=_run_id_arg, help="session id -- S-26-0012, or 0012 for the current year")
    p.add_argument("--reason", help="why it's being deleted (recorded in history)")
    p.add_argument("--force", action="store_true",
                   help="delete even if another session references it")
    p.set_defaults(func=cmd_rm_session)

    p = sub.add_parser("reseal", help="re-record an artifact's checksum after an intended edit")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id", type=_run_id_arg, help="session id -- S-26-0012, or 0012 for the current year")
    p.add_argument("file")
    p.set_defaults(func=cmd_reseal)

    p = sub.add_parser("check", help="report integrity problems in an archive (fsck)")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("--no-checksums", action="store_true",
                   help="skip re-hashing files (faster on large archives)")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("config", help="show or edit an archive's settings")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("--capture-code", type=_bool_arg, metavar="true|false",
                   help="snapshot first-party source into <archive>/.code on save")
    p.add_argument("--max-file-bytes", type=int, dest="max_file_bytes",
                   help="per-file ceiling for that snapshot")
    p.add_argument("--on-overwrite", choices=OVERWRITE_POLICIES, dest="on_overwrite",
                   help="what a write that would clobber an artifact does: "
                        "duplicate (default), overwrite, or cancel")
    p.add_argument("--auto-index", type=_bool_arg, metavar="true|false", dest="auto_index",
                   help="re-index a session in index.db as it closes "
                        "(off just means readers do that work instead)")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("collection", help="curated, nestable sets of refs")
    p.add_argument("archive", help="registered archive name, or a literal path")
    csub = p.add_subparsers(dest="collection_action", required=True)
    csub.add_parser("list", help="list collections")
    q = csub.add_parser("show", help="show a collection and everything under it")
    q.add_argument("name")
    q = csub.add_parser("new", help="create an empty collection")
    q.add_argument("name")
    q.add_argument("--title")
    q = csub.add_parser("rm", help="delete a collection (members are untouched)")
    q.add_argument("name")
    q = csub.add_parser("rename", help="rename a collection (rewrites every "
                                       "collections/<old> ref that points at it)")
    q.add_argument("name")
    q.add_argument("new", nargs="?", help="the new name; omit to only set --title")
    q.add_argument("--title", help="one-line description (not a second name)")
    q.set_defaults(collection_action="rename")

    q = csub.add_parser("add", help="add refs -- files, sessions, or collections/<name>")
    q.add_argument("name")
    q.add_argument("refs", nargs="+")
    q.add_argument("--note")
    q = csub.add_parser("remove", help="remove refs from a collection")
    q.add_argument("name")
    q.add_argument("refs", nargs="+")
    p.set_defaults(func=cmd_collection)

    p = sub.add_parser("view", aliases=["saved-search"], help="saved searches")
    p.add_argument("archive", help="registered archive name, or a literal path")
    vsub = p.add_subparsers(dest="view_action", required=True)
    vsub.add_parser("list", help="list saved views")
    q = vsub.add_parser("save", help="create or overwrite a view")
    q.add_argument("name")
    q.add_argument("--query", help="search terms (ANDed)")
    q.add_argument("--title")
    q.add_argument("--fields", help="comma-separated: filename,tags,origin,session,"
                                    "user_tags,comments")
    q.add_argument("--date-from", dest="date_from", metavar="YYYY-MM-DD")
    q.add_argument("--date-to", dest="date_to", metavar="YYYY-MM-DD")
    q = vsub.add_parser("rm", help="delete a view")
    q.add_argument("name")
    q = vsub.add_parser("run", help="run a view and list what it matches")
    q.add_argument("name")
    p.set_defaults(func=cmd_view)

    p = sub.add_parser(
        "annotate",
        help="show or edit user tags/comment on a session or file (mutable; "
             "never touches sidecars)")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id", type=_run_id_arg,
                   help="session id -- S-26-0012, or 0012 for the current year")
    p.add_argument("file", nargs="?",
                   help="artifact filename; omit to annotate the session itself")
    p.add_argument("--set-tags", metavar="T,T",
                   help="replace the user tags (empty string clears them)")
    p.add_argument("--add-tags", metavar="T,T", help="add user tags")
    p.add_argument("--rm-tags", metavar="T,T", help="remove user tags")
    p.add_argument("--comment", help="set the comment (empty string clears it)")
    p.set_defaults(func=cmd_annotate)

    p = sub.add_parser("gc", help="delete captured source code nothing references")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("--delete", action="store_true",
                   help="actually delete (default is a dry run)")
    p.add_argument("--ignore-trash", action="store_true",
                   help="also collect code referenced only by trashed sessions "
                        "(they can no longer be restored intact)")
    p.set_defaults(func=cmd_gc)

    p = sub.add_parser(
        "hold",
        help="keep a session appendable past its start day (e.g. across midnight)",
    )
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id", type=_run_id_arg, help="session id -- S-26-0012, or 0012 for the current year")
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
    p.add_argument("run_id", type=_run_id_arg, help="session id -- S-26-0012, or 0012 for the current year")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("upstream", help="what did this artifact depend on")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id", type=_run_id_arg, help="session id -- S-26-0012, or 0012 for the current year")
    p.add_argument("filename")
    p.set_defaults(func=cmd_upstream)

    p = sub.add_parser("downstream", help="what depends on this artifact")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("run_id", type=_run_id_arg, help="session id -- S-26-0012, or 0012 for the current year")
    p.add_argument("filename")
    p.add_argument("--also-search", nargs="*", help="other registered archive names to scan")
    p.set_defaults(func=cmd_downstream)

    p = sub.add_parser("index", help="index status and freshness")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("--rebuild", action="store_true", help="rebuild from scratch instead")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("seal", help="declare a past year finished (sweeps skip it)")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("year")
    p.add_argument("--force", action="store_true",
                   help="seal even the current year, or one with open sessions")
    p.set_defaults(func=cmd_seal)

    p = sub.add_parser("unseal", help="remove a year's seal")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("year")
    p.set_defaults(func=cmd_unseal)

    p = sub.add_parser("stale", help="find sessions left open too long")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("--hours", type=float, default=24.0)
    p.set_defaults(func=cmd_stale)

    p = sub.add_parser("archives", help="list registered archives")
    p.set_defaults(func=cmd_archives)

    p = sub.add_parser("init", help="create an archive")
    p.add_argument("root")
    p.add_argument("--kind", choices=("standard", "intake", "fragment"),
                   default="standard")
    p.add_argument("--name", help="the name it will carry in nebula:// URIs "
                                  "(default: the folder name)")
    p.add_argument("--user", help="who owns it (default: your local identity)")
    p.add_argument("--register", action="store_true", help="also register it")
    p.add_argument("--on-overwrite", choices=OVERWRITE_POLICIES, dest="on_overwrite",
                   help="what a colliding write does (default: duplicate)")
    p.add_argument("--capture-code", type=_bool_arg, metavar="true|false",
                   dest="capture_code", help="snapshot first-party source on save")
    p.add_argument("--auto-index", type=_bool_arg, metavar="true|false",
                   dest="auto_index", help="re-index a session as it closes")
    p.add_argument("--max-file-bytes", type=int, dest="max_file_bytes",
                   help="per-file ceiling for the code snapshot")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("intake", help="create a timestamped intake archive")
    p.add_argument("parent", help="where to create it")
    p.add_argument("--label", help="appended to the name, e.g. the instrument")
    p.set_defaults(func=cmd_intake)

    p = sub.add_parser("export", help="write a fragment: an excerpt others can read")
    p.add_argument("archive", help="registered archive name, or a literal path")
    p.add_argument("dest", help="directory to create")
    p.add_argument("--session", action="append", type=_run_id_arg,
                   help="a whole session (repeatable)")
    p.add_argument("--ref", action="append",
                   help="a single file, e.g. S-26-0012/raw.csv (repeatable)")
    p.add_argument("--collection", help="everything in a collection")
    p.add_argument("--exclude-foreign", action="store_true",
                   help="list, but do not embed, data belonging to other archives")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("merge", help="merge an intake archive into a standard one")
    p.add_argument("source", help="the intake archive")
    p.add_argument("dest", help="the standard archive")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-verify", action="store_true",
                   help="skip re-hashing each file after copying")
    p.add_argument("--no-lock", action="store_true",
                   help="leave the intake archive writable afterwards")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("adopt", help="copy sessions out of a fragment into your archive")
    p.add_argument("source", help="the fragment")
    p.add_argument("dest", help="your standard archive")
    p.add_argument("--session", action="append", help="only these sessions")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-verify", action="store_true")
    p.set_defaults(func=cmd_adopt)

    p = sub.add_parser("receive", help="file a fragment someone sent you")
    p.add_argument("source", help="the fragment directory")
    p.add_argument("--home", help="override NEBULA_HOME")
    p.add_argument("--overwrite-foreign", action="store_true",
                   help="replace differing copies instead of keeping what is here")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_receive)

    p = sub.add_parser("prune", help="delete a merged intake archive")
    p.add_argument("archive")
    p.add_argument("--force", action="store_true",
                   help="delete even if some sessions were never merged")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("unlock", help="let a merged intake archive be written to again")
    p.add_argument("archive")
    p.set_defaults(func=cmd_unlock)

    p = sub.add_parser("scan", help="discover archives under NEBULA_HOME")
    p.add_argument("--home", help="override NEBULA_HOME for this scan")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("register", help="register an archive in ~/.nebula/archives.yaml")
    p.add_argument("root")
    p.add_argument("name", nargs="?", help="override the name it declares "
                                           "(normally unnecessary)")
    p.add_argument("--git-org", help="GitHub org/user hosting this archive's repos")
    p.add_argument("--user", help="who owns this archive, for nebula:// URIs "
                                  "(omit for your own archives)")
    p.set_defaults(func=cmd_register)

    # Assets get a nested subparser rather than the flat `asset-commit`
    # spelling used elsewhere: there are enough verbs that flattening them
    # would double the top-level command list for one noun.
    p = sub.add_parser("asset", help="manage mutable assets (files you keep editing)")
    asub = p.add_subparsers(dest="asset_cmd", required=True)

    def _asset_parser(name, help_text):
        q = asub.add_parser(name, help=help_text)
        q.add_argument("archive", help="registered archive name, or a literal path")
        return q

    q = _asset_parser("import", "bring file(s) under management as assets")
    q.add_argument("files", nargs="+", help="file(s) to import")
    q.add_argument("--as", dest="dest_name", help="store under a different name")
    q.add_argument("--policy", choices=ASSET_POLICIES,
                   help="snapshot policy (default: auto, from the size ladder)")
    q.add_argument("--from", dest="origin", help="free-text note on where it came from")
    q.add_argument("--derived-from", nargs="*", dest="derived_from",
                   help="ref(s) this asset was derived from -- capture this now, "
                        "it cannot be reconstructed later")
    q.add_argument("--move", action="store_true", help="move instead of copy")
    q.set_defaults(func=cmd_asset_import)

    q = _asset_parser("ls", "list assets")
    q.add_argument("--policy", choices=ASSET_POLICIES,
                   help="only assets whose effective policy is this")
    q.add_argument("--sort", choices=("recent", "name", "size"), default="recent")
    q.set_defaults(func=cmd_asset_ls)

    q = _asset_parser("show", "show one asset in full")
    q.add_argument("asset_id", type=_asset_id_arg,
                   help="asset id -- AF-26-0017, or 0017 for the current year")
    q.set_defaults(func=cmd_asset_show)

    q = _asset_parser("path", "print an asset's path on disk (for opening it)")
    q.add_argument("asset_id", type=_asset_id_arg)
    q.set_defaults(func=cmd_asset_path)

    q = _asset_parser("commit", "save the asset's current bytes as a snapshot")
    q.add_argument("asset_id", type=_asset_id_arg)
    q.add_argument("-m", "--message", dest="note",
                   help="why this version is worth keeping")
    q.add_argument("--force", action="store_true",
                   help="snapshot even past the size ceiling, or re-store "
                        "bytes already stored")
    q.set_defaults(func=cmd_asset_commit)

    q = _asset_parser("history", "list an asset's stored versions")
    q.add_argument("asset_id", type=_asset_id_arg)
    q.set_defaults(func=cmd_asset_history)

    q = _asset_parser("policy", "show or change one asset's snapshot policy")
    q.add_argument("asset_id", type=_asset_id_arg)
    q.add_argument("policy", nargs="?", choices=ASSET_POLICIES)
    q.add_argument("--period-days", type=int,
                   help="gap between periodic snapshots (-1 to use the archive default)")
    q.add_argument("--max-snapshots", type=int,
                   help="retained snapshot count (-1 to use the archive default)")
    q.add_argument("--max-snapshot-bytes", type=int,
                   help="retained snapshot bytes (-1 to use the archive default)")
    q.set_defaults(func=cmd_asset_policy)

    q = _asset_parser("scan", "reconcile assets with what is on disk")
    q.add_argument("asset_id", nargs="?", type=_asset_id_arg,
                   help="limit to one asset (default: all)")
    q.set_defaults(func=cmd_asset_scan)

    p = sub.add_parser("whoami", help="show or set your nebula user name (used in URIs)")
    p.add_argument("--set", dest="set_user", metavar="NAME",
                   help="set the local user name (an email or handle)")
    p.set_defaults(func=cmd_whoami)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
