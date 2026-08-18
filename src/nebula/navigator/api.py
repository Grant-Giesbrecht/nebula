"""
Nebula Navigator -- JSON bridge for the Tauri front-end.

The Tauri app has a Rust core and a webview UI, but all of Nebula's real
logic lives in Python. Rather than duplicate any of it in Rust, this module
is bundled as a Tauri *sidecar*: a long-lived child process the Rust layer
spawns once and talks to over stdin/stdout with line-delimited JSON.

Wire protocol (one JSON object per line, both directions):

    -> {"id": 7, "op": "list_items", "args": {"session_path": "...", "verify": false}}
    <- {"id": 7, "ok": true,  "data": [ ... ]}
    <- {"id": 7, "ok": false, "error": "message"}

Every ``op`` is a thin adapter over :mod:`nebula.navigator.model` (the
GUI-toolkit-independent data layer) plus :mod:`nebula.manual` for imports and
:mod:`nebula.navigator.osutil` for "open in the OS". Nothing here imports a
GUI toolkit, so it stays importable and unit-testable without a display.

A one-shot mode is provided for scripting/tests::

    python -m nebula.navigator.api list_sessions '{"archive": "/path/to/archive"}'
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List

from nebula import manual
from nebula.navigator import model, osutil

PROTOCOL_VERSION = 1


# -- serialization --------------------------------------------------------

def _session_to_dict(s: "model.SessionInfo") -> Dict[str, Any]:
    return {
        "run_id": s.run_id,
        "path": str(s.path),
        "created": s.created,
        "status": s.status,
        "tags": list(s.tags),
        "description": s.description,
        "held": bool(s.held),
        "n_items": s.n_items,
        "n_problems": s.n_problems,
        "user_tags": list(s.user_tags),
        "user_comment": s.user_comment,
    }


def _item_to_dict(it: "model.Item") -> Dict[str, Any]:
    return {
        "name": it.name,
        "status": it.status,
        "status_label": it.status_label,
        "has_artifact": it.has_artifact,
        "has_sidecar": it.has_sidecar,
        "source": it.source,
        "source_recorded": it.source_recorded,
        "source_facet": model.item_source_facet(it),
        "origin": it.origin,
        "size": it.size,
        "size_human": model._human_size(it.size) if it.size is not None else None,
        "sha256": it.sha256,
        "timestamp": it.timestamp,
        "artifact_path": str(it.artifact_path) if it.artifact_path else None,
        "sidecar_path": str(it.sidecar_path) if it.sidecar_path else None,
        "detail": it.detail,
        "repo": it.repo,
        "commit": it.commit,
        "dirty": it.dirty,
        "entry_point": it.entry_point,
        "n_derived_from": it.n_derived_from,
        "user_tags": list(it.user_tags),
        "user_comment": it.user_comment,
        "display_name": it.display_name,
        "original_name": it.original_name,
        "position": it.position,
        "total": it.total,
        "is_duplicate": it.is_duplicate,
    }


# -- operations -----------------------------------------------------------
#
# Each handler takes the request's ``args`` dict and returns a JSON-able
# value. They keep archive resolution explicit: the front-end passes an
# archive identifier (registered name or path) and we resolve it to a root
# Path once, exactly as the Flet view does via ``model.resolve``.

def op_ping(args: Dict[str, Any]) -> Dict[str, Any]:
    return {"protocol": PROTOCOL_VERSION, "pid": None}


def op_resolve(args: Dict[str, Any]) -> Dict[str, Any]:
    root, label = model.resolve(args["archive"])
    return {"root": str(root), "label": label}


def op_list_sessions(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    root, _ = model.resolve(args["archive"])
    return [_session_to_dict(s) for s in model.list_sessions(root)]


def op_list_items(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    session_path = Path(args["session_path"])
    verify = bool(args.get("verify", False))
    return [_item_to_dict(it)
            for it in model.list_items(session_path, verify_checksums=verify)]


def op_sidecar_display(args: Dict[str, Any]) -> Dict[str, Any]:
    return {"text": model.sidecar_display(args["sidecar_path"])}


def op_sidecar_info(args: Dict[str, Any]) -> Dict[str, Any]:
    return model.sidecar_info(args["sidecar_path"])


def op_session_info(args: Dict[str, Any]) -> Dict[str, Any]:
    return model.session_info(args["session_path"])


def _search_result_to_dict(res: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten each hit into an item dict plus the session context the
    results table shows alongside it. Shared by search and saved views, so
    both render through exactly the same path."""
    return {
        "items": [
            dict(_item_to_dict(hit["item"]),
                 run_id=hit["run_id"],
                 session_path=hit["session_path"],
                 session_description=hit["session_description"],
                 tags=hit["tags"])
            for hit in res["items"]
        ],
        "truncated": res.get("truncated", False),
        "n_sessions": res.get("n_sessions", 0),
        "n_scanned": res.get("n_scanned", 0),
    }


def op_search_items(args: Dict[str, Any]) -> Dict[str, Any]:
    res = model.search_items(
        args["archive"], args.get("query") or "",
        fields=args.get("fields") or None,
        date_from=args.get("date_from") or None,
        date_to=args.get("date_to") or None,
        sources=args.get("sources") or None,
        limit=int(args.get("limit") or 1000),
    )
    return _search_result_to_dict(res)


def op_lineage(args: Dict[str, Any]) -> Dict[str, Any]:
    return model.lineage(args["archive"], args["session_path"], args["filename"])


def op_provenance_tree(args: Dict[str, Any]) -> Dict[str, Any]:
    """The relations view: a nested, depth-capped provenance tree rooted at
    an artefact or a whole session."""
    return model.provenance_tree(
        args["archive"], args["run_id"], args.get("filename") or None,
        direction=args.get("direction") or "both",
        depth=int(args.get("depth") or model.DEFAULT_TREE_DEPTH))


def op_identity(args: Dict[str, Any]) -> Dict[str, Any]:
    """Who this machine says you are, for the user segment of nebula URIs."""
    from nebula import identity

    user = identity.get_user()
    out = {"user": user or "", "set": bool(user),
           "path": str(identity.identity_path())}
    # The badge shows verification status, so it needs the same breakdown
    # the CLI prints rather than re-deriving one in JavaScript.
    if user:
        out.update(identity.describe_identity(user))
    return out


def op_set_identity(args: Dict[str, Any]) -> Dict[str, Any]:
    from nebula import identity

    try:
        warnings = identity.validate_identity(args["user"])
        path = identity.set_user(args["user"])
    except identity.IdentityError as e:
        return {"ok": False, "error": str(e)}
    user = identity.get_user()
    return {"ok": True, "user": user, "path": str(path),
            "warnings": warnings,
            "identity": identity.describe_identity(user or "")}


def op_check_identity(args: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a candidate identity without storing it.

    Lets the identity dialog show the ORCID check-digit failure as the user
    types, rather than only after committing to it.
    """
    from nebula import identity

    candidate = args.get("user") or ""
    try:
        warnings = identity.validate_identity(candidate)
    except identity.IdentityError as e:
        return {"ok": False, "error": str(e),
                "identity": identity.describe_identity(candidate)}
    return {"ok": True, "warnings": warnings,
            "identity": identity.describe_identity(candidate)}


def op_create_archive(args: Dict[str, Any]) -> Dict[str, Any]:
    """Create an archive, with its settings chosen up front."""
    from nebula import transfer
    from nebula.config import ArchiveSettings
    from nebula.registry import get_registry

    settings = ArchiveSettings(
        on_overwrite=args.get("on_overwrite") or "duplicate",
        capture_code=bool(args.get("capture_code", True)),
        auto_index=bool(args.get("auto_index", True)),
        code_max_file_bytes=int(args.get("code_max_file_bytes") or 1048576),
    )
    try:
        root = transfer.init_archive(
            args["root"], kind=args.get("kind") or "standard",
            name=args.get("name") or "", user=args.get("user") or "",
            settings=settings)
    except Exception as e:      # noqa: BLE001 -- reported to the dialog
        return {"ok": False, "error": str(e)}
    registered = None
    if args.get("register", True):
        try:
            registered = get_registry().register_archive(root).name
        except Exception:       # noqa: BLE001 -- the archive exists either way
            registered = None
    from nebula.config import archive_identity

    return {"ok": True, "root": str(root), "registered": registered,
            "identity": archive_identity(root)}


def op_create_intake(args: Dict[str, Any]) -> Dict[str, Any]:
    """Create a timestamped intake archive. The name is generated, not
    supplied: it is the coordinate a notebook entry will cite."""
    from nebula import transfer
    from nebula.config import ArchiveSettings, archive_identity

    settings = ArchiveSettings(
        on_overwrite=args.get("on_overwrite") or "duplicate",
        capture_code=bool(args.get("capture_code", True)),
        auto_index=bool(args.get("auto_index", True)),
    )
    try:
        root = transfer.new_intake(args["parent"], label=args.get("label") or "",
                                   user=args.get("user") or "", settings=settings)
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": True, "root": str(root), "registered": None,
            "identity": archive_identity(root)}


def op_receive_fragment(args: Dict[str, Any]) -> Dict[str, Any]:
    """File a fragment into NEBULA_HOME/fragments, not into an archive."""
    from nebula import transfer

    try:
        if args.get("dry_run"):
            return {"ok": True, "plan": transfer.plan_receive(args["source"])}
        got = transfer.receive(args["source"],
                               overwrite_foreign=bool(args.get("overwrite_foreign")))
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": True, "result": got}


def op_archive_kind(args: Dict[str, Any]) -> Dict[str, Any]:
    """What kind of archive this is, and what that permits."""
    from nebula.config import archive_identity

    root, label = model.resolve(args["archive"])
    ident = archive_identity(root)
    ident["label"] = label
    ident["writable"] = ident["kind"] != "fragment" and not ident["locked"]
    return ident


def op_transfer_plan(args: Dict[str, Any]) -> Dict[str, Any]:
    """Dry run for export/merge/adopt: what would move, before it moves."""
    from nebula import transfer

    op = args.get("op")
    try:
        if op == "export":
            plan = transfer.plan_export(
                args["archive"], args["dest"],
                sessions=args.get("sessions") or None,
                refs=args.get("refs") or None,
                collection=args.get("collection") or None,
                include_foreign=args.get("include_foreign", True))
        elif op == "merge":
            plan = transfer.plan_merge(args["source"], args["archive"])
        elif op == "adopt":
            plan = transfer.plan_adopt(args["source"], args["archive"],
                                       sessions=args.get("sessions") or None)
        else:
            return {"ok": False, "error": f"unknown transfer {op!r}"}
    except Exception as e:      # noqa: BLE001 -- reported, not raised at the GUI
        return {"ok": False, "error": str(e)}
    return {"ok": True, "plan": plan.to_dict()}


def op_transfer_run(args: Dict[str, Any]) -> Dict[str, Any]:
    """Do it, having shown the plan first."""
    from nebula import transfer

    op = args.get("op")
    try:
        if op == "export":
            plan = transfer.export(
                args["archive"], args["dest"],
                sessions=args.get("sessions") or None,
                refs=args.get("refs") or None,
                collection=args.get("collection") or None,
                include_foreign=args.get("include_foreign", True))
        elif op == "merge":
            plan = transfer.merge(args["source"], args["archive"],
                                  verify=args.get("verify", True))
        elif op == "adopt":
            plan = transfer.adopt(args["source"], args["archive"],
                                  sessions=args.get("sessions") or None,
                                  verify=args.get("verify", True))
        else:
            return {"ok": False, "error": f"unknown transfer {op!r}"}
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": True, "plan": plan.to_dict()}


def op_index_view(args: Dict[str, Any]) -> Dict[str, Any]:
    """A read-only page of index.db, for the index inspector."""
    return model.index_view(
        args["archive"], table=args.get("table") or "sessions",
        query=args.get("query") or "", run_id=args.get("run_id") or "",
        limit=int(args.get("limit") or 200), offset=int(args.get("offset") or 0))


def op_index_sweep(args: Dict[str, Any]) -> Dict[str, Any]:
    """Run the freshness sweep on demand and report what it did."""
    from nebula import index as index_mod

    root, _ = model.resolve(args["archive"])
    return {"swept": index_mod.ensure_fresh(root),
            "status": index_mod.status(root)}


def op_code_info(args: Dict[str, Any]) -> Dict[str, Any]:
    return model.code_info(args["archive"], args["code"])


def op_entry_point_link(args: Dict[str, Any]) -> Dict[str, Any]:
    return model.entry_point_link(args["archive"], args.get("item") or {})


def op_open_url(args: Dict[str, Any]) -> Dict[str, Any]:
    return {"dispatched": osutil.open_url(args["url"])}


def op_restore_code(args: Dict[str, Any]) -> Dict[str, Any]:
    return model.restore_code(args["archive"], args["code"], args["dest_parent"])


def op_get_annotations(args: Dict[str, Any]) -> Dict[str, Any]:
    from nebula import annotations

    return annotations.get(args["session_path"], args.get("filename") or None)


def op_set_annotation(args: Dict[str, Any]) -> Dict[str, Any]:
    """Replace a target's tags and/or comment. Tag errors come back as
    normal op errors, so the GUI can show what was rejected."""
    from nebula import annotations

    return annotations.set_annotation(
        args["session_path"], args.get("filename") or None,
        tags=args.get("tags"), comment=args.get("comment"),
    )


def op_bulk_annotate(args: Dict[str, Any]) -> Dict[str, Any]:
    """Edit tags and/or append a comment line on several targets at once --
    the multi-select bulk editor. Each target keeps its own existing tags
    and comment; this only adds/removes tags and appends a line, it never
    replaces a target's whole annotation the way ``set_annotation`` does.

    A bad tag on one target must not lose the edits already made to the
    others, so failures are collected per-target rather than raised.
    """
    from nebula import annotations

    targets = args.get("targets") or []
    try:
        add_tags = annotations.split_tags(args.get("add_tags") or "")
        rm_tags = annotations.split_tags(args.get("remove_tags") or "")
    except annotations.TagError as e:
        return {"results": [], "error": str(e)}
    comment_line = (args.get("append_comment") or "").strip()

    results = []
    for t in targets:
        session_path = t.get("session_path")
        filename = t.get("filename")
        try:
            if add_tags:
                annotations.add_tags(session_path, filename, add_tags)
            if rm_tags:
                annotations.remove_tags(session_path, filename, rm_tags)
            if comment_line:
                annotations.append_comment(session_path, filename, comment_line)
            got = annotations.get(session_path, filename)
            results.append({"session_path": session_path, "filename": filename,
                            "ok": True, "tags": got["tags"], "comment": got["comment"]})
        except (annotations.TagError, OSError) as e:
            # One target's session having vanished (a stale search result,
            # a session that was just deleted in another window) must not
            # lose the edits already applied to every other target.
            results.append({"session_path": session_path, "filename": filename,
                            "ok": False, "error": str(e)})
    return {"results": results}


def op_activity(args: Dict[str, Any]) -> Dict[str, Any]:
    return model.activity(args["archive"])


def op_archive_stats(args: Dict[str, Any]) -> Dict[str, Any]:
    return model.archive_stats(args["archive"])


def op_rebuild_index(args: Dict[str, Any]) -> Dict[str, Any]:
    from nebula import index as index_mod

    root, _ = model.resolve(args["archive"])
    path = index_mod.rebuild(root)
    return {"path": str(path), "stats": model.archive_stats(root)}


def op_check(args: Dict[str, Any]) -> Dict[str, Any]:
    """Run the fsck. Checksum verification is off by default here: it
    re-hashes every file, which is fine from a terminal but not something a
    panel should do without being asked."""
    from nebula import check as check_mod

    root, label = model.resolve(args["archive"])
    issues = check_mod.check(root, verify_checksums=bool(args.get("verify", False)),
                             archive_label=label)
    return {
        "issues": [
            {"kind": i.kind, "session": i.session, "file": i.file,
             "detail": i.detail, "fix": i.fix, "severity": i.severity}
            for i in issues
        ],
        "n_errors": sum(1 for i in issues if i.severity == "error"),
        "n_info": sum(1 for i in issues if i.severity != "error"),
        "verified": bool(args.get("verify", False)),
    }


def op_gc(args: Dict[str, Any]) -> Dict[str, Any]:
    """Sweep unreferenced captured source. Dry run unless told otherwise --
    the caller is expected to show the dry run first."""
    from nebula import assetstore, codestore

    root, _ = model.resolve(args["archive"])
    dry = not args.get("delete", False)
    trash = not args.get("ignore_trash", False)
    res = codestore.gc(root, dry_run=dry, include_trash=trash)
    # Asset versions are a separate store with separate liveness rules, so
    # they are reported separately rather than folded into one total the
    # caller cannot break down.
    res["assets"] = assetstore.gc(root, dry_run=dry, include_trash=trash)
    res["assets"]["human"] = model._human_size(res["assets"]["bytes"])
    res["human"] = model._human_size(res["bytes"])
    return res


def op_delete_file(args: Dict[str, Any]) -> Dict[str, Any]:
    root, _ = model.resolve(args["archive"])
    trashed = manual.delete_file(root, args["run_id"], args["filename"],
                                 reason=args.get("reason") or None,
                                 force=bool(args.get("force", False)))
    return {"trashed": str(trashed)}


def op_reseal(args: Dict[str, Any]) -> Dict[str, Any]:
    root, _ = model.resolve(args["archive"])
    return {"sha256": manual.reseal(root, args["run_id"], args["filename"])}


def op_adopt_file(args: Dict[str, Any]) -> Dict[str, Any]:
    """Write a sidecar for a file someone dropped into a session by hand."""
    return {"sidecar": str(manual.adopt_file(args["path"],
                                             origin=args.get("origin") or None))}


def op_delete_session(args: Dict[str, Any]) -> Dict[str, Any]:
    root, _ = model.resolve(args["archive"])
    trashed = manual.delete_session(root, args["run_id"],
                                    reason=args.get("reason") or None,
                                    force=bool(args.get("force", False)))
    return {"trashed": str(trashed)}


def op_hold(args: Dict[str, Any]) -> Dict[str, Any]:
    # nebula.session is the context-manager *function* in the package
    # namespace, not the module -- take these from the package exports.
    from nebula import hold as hold_session

    root, _ = model.resolve(args["archive"])
    seconds = args.get("seconds")
    return {"hold_until": hold_session(root, args["run_id"],
                                       seconds=float(seconds) if seconds else None)}


def op_release(args: Dict[str, Any]) -> Dict[str, Any]:
    from nebula import release as release_session

    root, _ = model.resolve(args["archive"])
    return {"had_hold": release_session(root, args["run_id"])}


# -- collections and saved views ------------------------------------------

def op_list_collections(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    from nebula import collection as collection_mod

    root, _ = model.resolve(args["archive"])
    return [
        {"name": c.name, "title": c.title, "description": c.description,
         "n_entries": len(c.entries)}
        for c in collection_mod.list_all(root)
    ]


def op_collections_overview(args: Dict[str, Any]) -> Dict[str, Any]:
    """The shape of the collection hierarchy, for a tree in the rail.

    `roots` are the collections nothing else contains -- a nested folder
    should not also appear at the top level. A collection reachable only
    through a cycle would have no root, so any collection left unvisited is
    added back as a root rather than becoming invisible.
    """
    from nebula import collection as collection_mod

    root, _ = model.resolve(args["archive"])
    colls = collection_mod.list_all(root)
    children: Dict[str, List[str]] = {}
    contained: set = set()
    for coll in colls:
        kids = []
        for entry in coll.entries:
            if entry.kind != "collection":
                continue
            ref = entry.parsed
            if ref.archive or ref.user:      # elsewhere: not part of this tree
                continue
            kids.append(ref.collection)
            contained.add(ref.collection)
        children[coll.name] = kids

    known = {c.name for c in colls}
    roots = [c.name for c in colls if c.name not in contained]
    # Cycles (hand-edited) would otherwise hide their members completely.
    reachable: set = set()

    def walk(name, seen):
        if name in seen or name not in known:
            return
        reachable.add(name)
        for kid in children.get(name, []):
            walk(kid, seen | {name})

    for name in roots:
        walk(name, set())
    roots += sorted(known - reachable)

    return {
        "roots": roots,
        "collections": [
            {"name": c.name, "title": c.title, "n_entries": len(c.entries),
             "children": children.get(c.name, [])}
            for c in colls
        ],
    }


def op_get_item(args: Dict[str, Any]) -> Dict[str, Any]:
    """One artifact, as list_items would describe it. Lets a view that is
    not the session's file list (a collection, say) select a file and show
    its properties without navigating away."""
    session_path = Path(args["session_path"])
    name = args["filename"]
    for it in model.list_items(session_path):
        if it.name == name:
            return dict(_item_to_dict(it), session_path=str(session_path),
                        run_id=args.get("run_id") or session_path.name)
    raise FileNotFoundError(f"no artifact {name!r} in {session_path}")


def op_collection_tree(args: Dict[str, Any]) -> Dict[str, Any]:
    from nebula import collection as collection_mod

    root, _ = model.resolve(args["archive"])
    return collection_mod.tree(root, args["name"])


def op_create_collection(args: Dict[str, Any]) -> Dict[str, Any]:
    from nebula import collection as collection_mod

    root, _ = model.resolve(args["archive"])
    coll = collection_mod.create(root, args["name"], title=args.get("title") or "")
    return {"name": coll.name}


def op_delete_collection(args: Dict[str, Any]) -> Dict[str, Any]:
    from nebula import collection as collection_mod

    root, _ = model.resolve(args["archive"])
    return {"deleted": collection_mod.delete(root, args["name"])}


def op_collection_add(args: Dict[str, Any]) -> Dict[str, Any]:
    """Add refs to a collection, creating it if asked. Errors (a cycle, a
    duplicate, an unparseable ref) come back as normal op errors."""
    from nebula import collection as collection_mod

    root, _ = model.resolve(args["archive"])
    name = args["name"]
    if args.get("create") and collection_mod.read(root, name) is None:
        collection_mod.create(root, name, title=args.get("title") or "")
    for ref in args["refs"]:
        collection_mod.add(root, name, ref, note=args.get("note") or "")
    return {"name": name, "n_entries": len(collection_mod.read(root, name).entries)}


def op_collection_remove(args: Dict[str, Any]) -> Dict[str, Any]:
    from nebula import collection as collection_mod

    root, _ = model.resolve(args["archive"])
    for ref in args["refs"]:
        collection_mod.remove(root, args["name"], ref)
    return {"name": args["name"]}


def op_rename_collection(args: Dict[str, Any]) -> Dict[str, Any]:
    from nebula import collection as collection_mod

    root, _ = model.resolve(args["archive"])
    coll = collection_mod.rename(root, args["name"], args.get("new") or None,
                                 title=args.get("title"))
    return {"name": coll.name, "title": coll.title}


def op_slugify(args: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a typed display name into a storable one, so the GUI and the
    CLI agree on what a given title becomes on disk."""
    from nebula import collection as collection_mod

    return {"slug": collection_mod.slugify(args["text"])}


def op_collection_move(args: Dict[str, Any]) -> Dict[str, Any]:
    """Move entries between collections (what a drag-and-drop does)."""
    from nebula import collection as collection_mod

    root, _ = model.resolve(args["archive"])
    for ref in args["refs"]:
        collection_mod.move(root, args["from"], args["to"], ref)
    return {"from": args["from"], "to": args["to"], "n": len(args["refs"])}


def op_collections_containing(args: Dict[str, Any]) -> List[str]:
    from nebula import collection as collection_mod

    root, _ = model.resolve(args["archive"])
    return collection_mod.containing(root, args["ref"])


def op_list_views(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    from nebula import views as views_mod

    root, _ = model.resolve(args["archive"])
    return [v.to_dict() for v in views_mod.list_all(root)]


def op_run_view(args: Dict[str, Any]) -> Dict[str, Any]:
    from nebula import views as views_mod

    root, _ = model.resolve(args["archive"])
    res = views_mod.run(root, args["name"], limit=int(args.get("limit") or 1000))
    out = _search_result_to_dict(res)
    out["view"] = res["view"]
    return out


def op_save_view(args: Dict[str, Any]) -> Dict[str, Any]:
    from nebula import views as views_mod

    root, _ = model.resolve(args["archive"])
    view = views_mod.save(
        root, args["name"], query=args.get("query") or "",
        title=args.get("title") or "", fields=args.get("fields") or None,
        date_from=args.get("date_from") or None, date_to=args.get("date_to") or None)
    return view.to_dict()


def op_delete_view(args: Dict[str, Any]) -> Dict[str, Any]:
    from nebula import views as views_mod

    root, _ = model.resolve(args["archive"])
    return {"deleted": views_mod.delete(root, args["name"])}


def op_list_archives(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    return model.registered_archives()


def op_importable_sessions(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    root, _ = model.resolve(args["archive"])
    return [_session_to_dict(s) for s in model.importable_sessions(root)]


def op_frozen_sessions(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    root, _ = model.resolve(args["archive"])
    return [_session_to_dict(s) for s in model.frozen_sessions(root)]


def op_import_new(args: Dict[str, Any]) -> Dict[str, Any]:
    root, _ = model.resolve(args["archive"])
    session = manual.import_new(
        root, [str(p) for p in args["paths"]],
        tags=args.get("tags") or [],
        description=args.get("description") or "",
        origin=args.get("origin") or None,
    )
    return {"run_id": session.id, "path": str(session.path)}


def op_import_file(args: Dict[str, Any]) -> Dict[str, Any]:
    root, _ = model.resolve(args["archive"])
    # derived_from mirrors the CLI's `nebula import --derived-from`: the
    # same refs are recorded on every file in this import.
    derived_from = [r for r in (args.get("derived_from") or []) if str(r).strip()]
    dests = []
    for p in args["paths"]:
        dest = manual.import_file(
            root, args["run_id"], str(p),
            origin=args.get("origin") or None,
            derived_from=derived_from or None,
            allow_frozen=bool(args.get("allow_frozen", False)),
        )
        dests.append(str(dest))
    return {"run_id": args["run_id"], "dests": dests}


def op_resolve_refs(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    return model.resolve_refs(args["archive"], args["run_id"], args.get("refs") or [])


def op_reveal_path(args: Dict[str, Any]) -> Dict[str, Any]:
    return {"dispatched": osutil.reveal_path(args["path"])}


def op_open_path(args: Dict[str, Any]) -> Dict[str, Any]:
    return {"dispatched": osutil.open_path(args["path"])}


def op_file_manager_name(args: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": osutil.file_manager_name()}


# -- assets ---------------------------------------------------------------

def op_list_assets(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    return model.list_assets(
        args["archive"], policy=args.get("policy", ""),
        sort=args.get("sort", "recent"), query=args.get("query", ""))


def op_asset_info(args: Dict[str, Any]) -> Dict[str, Any]:
    return model.asset_info(args["archive"], args["asset_id"])


def op_asset_preview(args: Dict[str, Any]) -> Dict[str, Any]:
    return model.asset_preview(args["archive"], args["asset_id"])


def op_asset_import_defaults(args: Dict[str, Any]) -> Dict[str, Any]:
    return model.asset_import_defaults(args["archive"], args.get("paths") or [])


def op_asset_import(args: Dict[str, Any]) -> Dict[str, Any]:
    """Import one or more files as assets. Per-file failures are reported
    rather than aborting the batch: a dialog that drops nine good files
    because the tenth vanished is worse than one that says so."""
    from nebula import assets as assets_mod

    root, _ = model.resolve(args["archive"])
    out, errors = [], []
    for path in args.get("paths") or []:
        try:
            meta = assets_mod.import_asset(
                root, path,
                policy=args.get("policy") or None,
                derived_from=args.get("derived_from") or None,
                origin=args.get("origin") or None,
                move=bool(args.get("move")),
            )
            out.append(meta.id)
        except Exception as e:
            errors.append({"path": path, "error": str(e)})

    # Tags and collections are applied after import, because both are
    # keyed on the asset id that import is what mints.
    for asset_id in out:
        for name in args.get("collections") or []:
            try:
                _collection_add_asset(root, name, asset_id)
            except Exception as e:
                errors.append({"path": asset_id, "error": str(e)})
    return {"imported": out, "errors": errors}


def _collection_add_asset(root, collection_name: str, asset_id: str) -> None:
    from nebula import collection as collection_mod

    collection_mod.add(root, collection_name, f"assets/{asset_id}")


def op_asset_commit(args: Dict[str, Any]) -> Dict[str, Any]:
    from nebula import assets as assets_mod

    root, _ = model.resolve(args["archive"])
    snap = assets_mod.commit(root, args["asset_id"], note=args.get("note"),
                             force=bool(args.get("force")))
    if snap is None:
        return {"committed": False, "reason": "no change since the last snapshot"}
    return {"committed": True, "sha256": snap.sha256, "at": snap.at}


def op_asset_set_policy(args: Dict[str, Any]) -> Dict[str, Any]:
    from nebula import assets as assets_mod

    root, _ = model.resolve(args["archive"])
    assets_mod.set_policy(
        root, args["asset_id"], args.get("policy") or None,
        period_days=args.get("period_days"),
        max_snapshots=args.get("max_snapshots"),
        max_snapshot_bytes=args.get("max_snapshot_bytes"))
    return model.asset_info(root, args["asset_id"])


def op_asset_scan(args: Dict[str, Any]) -> Dict[str, Any]:
    from nebula import assets as assets_mod

    root, _ = model.resolve(args["archive"])
    ids = ([args["asset_id"]] if args.get("asset_id")
           else assets_mod.list_assets(root))
    changed = []
    for asset_id in ids:
        try:
            state = assets_mod.scan(root, asset_id)
        except assets_mod.AssetError:
            continue
        if state["renamed"] or state["changed"] or state["missing"]:
            changed.append({"id": asset_id, **{
                "renamed": list(state["renamed"]) if state["renamed"] else None,
                "changed": state["changed"], "missing": state["missing"]}})
    return {"scanned": len(ids), "changed": changed}


def op_asset_reveal(args: Dict[str, Any]) -> Dict[str, Any]:
    """Show the asset in the OS file manager. The storage layout is opaque
    by design, so this is how a user gets to their file to edit it."""
    from nebula import assets as assets_mod

    root, _ = model.resolve(args["archive"])
    path = assets_mod.live_file(root, args["asset_id"])
    if path is None:
        return {"ok": False, "reason": "file missing"}
    return {"ok": osutil.reveal_path(path), "path": str(path)}


def op_asset_open(args: Dict[str, Any]) -> Dict[str, Any]:
    from nebula import assets as assets_mod

    root, _ = model.resolve(args["archive"])
    path = assets_mod.live_file(root, args["asset_id"])
    if path is None:
        return {"ok": False, "reason": "file missing"}
    return {"ok": osutil.open_path(path), "path": str(path)}


def op_asset_settings(args: Dict[str, Any]) -> Dict[str, Any]:
    return model.asset_settings(args["archive"])


def op_set_asset_settings(args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        settings = model.set_asset_settings(
            args["archive"], args.get("changes") or {})
    except model.AssetSettingsError as e:
        # A rejected setting is an ordinary outcome the form must explain,
        # not a bridge failure -- so it comes back as data, not an exception.
        return {"ok": False, "error": str(e)}
    return {"ok": True, "settings": settings}


def op_asset_settings_preview(args: Dict[str, Any]) -> Dict[str, Any]:
    """How many `auto` assets would change policy under proposed settings."""
    return model.asset_settings_preview(
        args["archive"], args.get("changes") or {})


OPS = {
    "list_assets": op_list_assets,
    "asset_info": op_asset_info,
    "asset_preview": op_asset_preview,
    "asset_import_defaults": op_asset_import_defaults,
    "asset_import": op_asset_import,
    "asset_commit": op_asset_commit,
    "asset_set_policy": op_asset_set_policy,
    "asset_scan": op_asset_scan,
    "asset_reveal": op_asset_reveal,
    "asset_open": op_asset_open,
    "asset_settings": op_asset_settings,
    "set_asset_settings": op_set_asset_settings,
    "asset_settings_preview": op_asset_settings_preview,
    "ping": op_ping,
    "resolve": op_resolve,
    "list_sessions": op_list_sessions,
    "list_items": op_list_items,
    "sidecar_display": op_sidecar_display,
    "sidecar_info": op_sidecar_info,
    "session_info": op_session_info,
    "list_archives": op_list_archives,
    "search_items": op_search_items,
    "lineage": op_lineage,
    "provenance_tree": op_provenance_tree,
    "index_view": op_index_view,
    "archive_kind": op_archive_kind,
    "identity": op_identity,
    "set_identity": op_set_identity,
    "check_identity": op_check_identity,
    "create_archive": op_create_archive,
    "create_intake": op_create_intake,
    "receive_fragment": op_receive_fragment,
    "transfer_plan": op_transfer_plan,
    "transfer_run": op_transfer_run,
    "index_sweep": op_index_sweep,
    "resolve_refs": op_resolve_refs,
    "code_info": op_code_info,
    "restore_code": op_restore_code,
    "list_collections": op_list_collections,
    "collection_tree": op_collection_tree,
    "collections_overview": op_collections_overview,
    "get_item": op_get_item,
    "create_collection": op_create_collection,
    "delete_collection": op_delete_collection,
    "collection_add": op_collection_add,
    "collection_remove": op_collection_remove,
    "collection_move": op_collection_move,
    "rename_collection": op_rename_collection,
    "slugify": op_slugify,
    "collections_containing": op_collections_containing,
    "list_views": op_list_views,
    "run_view": op_run_view,
    "save_view": op_save_view,
    "delete_view": op_delete_view,
    "activity": op_activity,
    "archive_stats": op_archive_stats,
    "rebuild_index": op_rebuild_index,
    "check": op_check,
    "gc": op_gc,
    "delete_file": op_delete_file,
    "reseal": op_reseal,
    "adopt_file": op_adopt_file,
    "delete_session": op_delete_session,
    "hold": op_hold,
    "release": op_release,
    "get_annotations": op_get_annotations,
    "set_annotation": op_set_annotation,
    "bulk_annotate": op_bulk_annotate,
    "entry_point_link": op_entry_point_link,
    "open_url": op_open_url,
    "importable_sessions": op_importable_sessions,
    "frozen_sessions": op_frozen_sessions,
    "import_new": op_import_new,
    "import_file": op_import_file,
    "open_path": op_open_path,
    "reveal_path": op_reveal_path,
    "file_manager_name": op_file_manager_name,
}


def dispatch(op: str, args: Dict[str, Any]) -> Any:
    """Run one operation by name. Raises KeyError for an unknown op; the
    handlers raise their own domain errors (FileNotFoundError, etc.)."""
    if op not in OPS:
        raise KeyError(f"unknown op {op!r}; known: {sorted(OPS)}")
    return OPS[op](args or {})


# -- stdio server ---------------------------------------------------------

def serve(stdin=None, stdout=None) -> int:
    """Read line-delimited JSON requests until EOF, writing one JSON
    response line per request. Never lets a handler error take the whole
    process down -- a bad request gets an error response and the loop
    continues, so the front-end stays connected."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            data = dispatch(req["op"], req.get("args") or {})
            resp = {"id": req_id, "ok": True, "data": data}
        except Exception as exc:  # noqa: BLE001 -- boundary; report, don't crash
            resp = {
                "id": req_id,
                "ok": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "traceback": traceback.format_exc(),
            }
        stdout.write(json.dumps(resp) + "\n")
        stdout.flush()
    return 0


def main(argv=None) -> int:
    """No args -> run the stdio server (how Tauri launches it). With args,
    run a single op and print its JSON result, for scripting and tests:

        python -m nebula.navigator.api <op> '<json-args>'
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return serve()
    op = argv[0]
    args = json.loads(argv[1]) if len(argv) > 1 else {}
    try:
        data = dispatch(op, args)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc),
                          "error_type": type(exc).__name__}))
        return 1
    print(json.dumps({"ok": True, "data": data}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
