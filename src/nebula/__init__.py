"""
nebula — a lightweight provenance-and-storage layer for measurement data.

Core ideas:
  - Data lives in "sessions" (S-XXXX folders), one per day-scoped unit of work.
  - Each artifact in a session gets a small JSON sidecar recording how it was
    produced (repo/commit/entry point) and what it was derived from.
  - Sessions carry a human-edited session.yaml (tags, description, status).
  - Multiple independent "archives" (e.g. postdoc vs. personal) can reference
    each other via structured refs, resolved through a small registry.
  - Everything is regeneratable: the SQLite index is a disposable cache
    rebuilt by walking sidecar files. The filesystem is the source of truth.
"""

from nebula.refs import Ref, parse_ref, format_ref
from nebula.registry import Registry, get_registry
from nebula.session import (
    Session,
    MissingMetadataError,
    MissingMetadataWarning,
    new,
    append_to,
    reopen,
    session,
    hold,
    release,
)
from nebula.sidecar import write_sidecar, read_sidecar
from nebula.tags import collect_tags, input_tag
from nebula.session_select import select_session
from nebula import graph

__all__ = [
    "Ref",
    "parse_ref",
    "format_ref",
    "Registry",
    "get_registry",
    "Session",
    "MissingMetadataError",
    "MissingMetadataWarning",
    "new",
    "append_to",
    "reopen",
    "session",
    "hold",
    "release",
    "select_session",
    "write_sidecar",
    "read_sidecar",
    "collect_tags",
    "input_tag",
    "graph",
]

__version__ = "0.1.0"
