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
    }


def _item_to_dict(it: "model.Item") -> Dict[str, Any]:
    return {
        "name": it.name,
        "status": it.status,
        "status_label": it.status_label,
        "has_artifact": it.has_artifact,
        "has_sidecar": it.has_sidecar,
        "source": it.source,
        "origin": it.origin,
        "size": it.size,
        "size_human": model._human_size(it.size) if it.size is not None else None,
        "sha256": it.sha256,
        "timestamp": it.timestamp,
        "artifact_path": str(it.artifact_path) if it.artifact_path else None,
        "sidecar_path": str(it.sidecar_path) if it.sidecar_path else None,
        "detail": it.detail,
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


def op_search_items(args: Dict[str, Any]) -> Dict[str, Any]:
    res = model.search_items(
        args["archive"], args.get("query") or "",
        fields=args.get("fields") or None,
        date_from=args.get("date_from") or None,
        date_to=args.get("date_to") or None,
        limit=int(args.get("limit") or 1000),
    )
    # Flatten each hit into an item dict plus the session context the
    # results table shows alongside it.
    return {
        "items": [
            dict(_item_to_dict(hit["item"]),
                 run_id=hit["run_id"],
                 session_path=hit["session_path"],
                 session_description=hit["session_description"],
                 tags=hit["tags"])
            for hit in res["items"]
        ],
        "truncated": res["truncated"],
        "n_sessions": res["n_sessions"],
        "n_scanned": res["n_scanned"],
    }


def op_lineage(args: Dict[str, Any]) -> Dict[str, Any]:
    return model.lineage(args["archive"], args["session_path"], args["filename"])


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
    dests = []
    for p in args["paths"]:
        dest = manual.import_file(
            root, args["run_id"], str(p),
            origin=args.get("origin") or None,
            allow_frozen=bool(args.get("allow_frozen", False)),
        )
        dests.append(str(dest))
    return {"run_id": args["run_id"], "dests": dests}


def op_open_path(args: Dict[str, Any]) -> Dict[str, Any]:
    return {"dispatched": osutil.open_path(args["path"])}


def op_file_manager_name(args: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": osutil.file_manager_name()}


OPS = {
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
    "importable_sessions": op_importable_sessions,
    "frozen_sessions": op_frozen_sessions,
    "import_new": op_import_new,
    "import_file": op_import_file,
    "open_path": op_open_path,
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
