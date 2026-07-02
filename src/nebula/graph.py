"""
Provenance graph queries over the index: "what did this artifact depend
on" (upstream) and "everything that depends on this artifact"
(downstream), transitively.

This is what answers the original motivating question from the design
discussion: "reprocess all old raw runs with my new fitting algorithm" is
just `downstream(archive, "S-0152", "raw.graf")` to find every derived
artifact, or `upstream(...)` to trace a result back to its raw inputs.

Single-archive traversal only needs that archive's index. Crossing into
another archive (a ref with an explicit `archive` field) requires the
registry to resolve where that archive lives; if the archive isn't
registered or its root isn't mounted/reachable, the traversal reports an
"unresolved" node instead of raising, since a stale/offline collaborator
archive shouldn't crash a query about your own data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from nebula.index import open_index
from nebula.registry import Registry, get_registry, resolve_archive


@dataclass(frozen=True)
class ArtifactNode:
    """One artifact in the provenance graph."""

    archive: str  # archive name this artifact actually lives in (never None)
    run_id: str
    filename: str
    path: Optional[str] = None  # filesystem path, if resolvable
    unresolved: bool = False  # True if we couldn't reach this archive to verify it exists

    def key(self) -> Tuple[str, str, str]:
        return (self.archive, self.run_id, self.filename)

    def __str__(self) -> str:
        suffix = " (unresolved)" if self.unresolved else ""
        return f"{self.archive}|{self.run_id}/{self.filename}{suffix}"


def _resolve_archive_root(
    archive_name: str, local_archive_root: Path, local_archive_name: str, registry: Registry
) -> Optional[Path]:
    if archive_name == local_archive_name:
        return local_archive_root
    cfg = registry.try_get(archive_name)
    return cfg.root if cfg else None


def upstream(
    archive: "str | Path",
    run_id: str,
    filename: str,
    *,
    archive_name: Optional[str] = None,
    registry: Optional[Registry] = None,
    max_depth: int = 50,
) -> List[ArtifactNode]:
    """Return every artifact this one was (transitively) derived from,
    in breadth-first order. Cross-archive edges are followed if the
    referenced archive is registered and reachable; otherwise the node is
    included with unresolved=True and traversal stops on that branch.

    `archive` follows the same resolution rule as nebula.session(): a str
    is looked up as a registered archive name, a Path is used literally.
    archive_name overrides the label used for THIS archive in results
    (normally unnecessary -- a registered name resolves its own label).
    """
    registry = registry or get_registry()
    local_archive_root, resolved_name = resolve_archive(archive, registry=registry)
    archive_name = archive_name or resolved_name or "local"
    visited: Set[Tuple[str, str, str]] = set()
    result: List[ArtifactNode] = []
    frontier: List[Tuple[str, Path, str, str]] = [
        (archive_name, Path(local_archive_root), run_id, filename)
    ]
    depth = 0

    while frontier and depth < max_depth:
        depth += 1
        next_frontier = []
        for cur_archive_name, cur_root, cur_run_id, cur_filename in frontier:
            key = (cur_archive_name, cur_run_id, cur_filename)
            if key in visited:
                continue
            visited.add(key)

            try:
                conn = open_index(cur_root)
            except FileNotFoundError:
                continue

            rows = conn.execute(
                "SELECT ref_archive, ref_session, ref_file FROM derived_from "
                "WHERE run_id = ? AND filename = ?",
                (cur_run_id, cur_filename),
            ).fetchall()
            conn.close()

            for row in rows:
                parent_archive = row["ref_archive"] or cur_archive_name
                parent_session = row["ref_session"] or cur_run_id
                parent_file = row["ref_file"]
                if parent_file is None:
                    continue  # whole-session ref, not an artifact edge

                parent_root = _resolve_archive_root(
                    parent_archive, Path(local_archive_root), archive_name, registry
                )
                if parent_root is None:
                    result.append(
                        ArtifactNode(
                            archive=parent_archive,
                            run_id=parent_session,
                            filename=parent_file,
                            unresolved=True,
                        )
                    )
                    continue

                node = ArtifactNode(
                    archive=parent_archive,
                    run_id=parent_session,
                    filename=parent_file,
                    path=str(parent_root / "**" / parent_session / parent_file),
                )
                result.append(node)
                next_frontier.append(
                    (parent_archive, parent_root, parent_session, parent_file)
                )
        frontier = next_frontier

    return result


def downstream(
    archive: "str | Path",
    run_id: str,
    filename: str,
    *,
    archive_name: Optional[str] = None,
    registry: Optional[Registry] = None,
    also_search_archives: Optional[List[str]] = None,
    max_depth: int = 50,
) -> List[ArtifactNode]:
    """Return every artifact (transitively) derived from this one.

    Downstream search is inherently more expensive than upstream: a
    derived artifact could in principle live in any archive, so we can only
    search archives we're explicitly told about. By default this searches
    just the given archive; pass also_search_archives=[...] (registry names)
    to additionally scan other archives for cross-archive children.

    `archive` follows the same resolution rule as nebula.session().
    """
    registry = registry or get_registry()
    local_archive_root, resolved_name = resolve_archive(archive, registry=registry)
    archive_name = archive_name or resolved_name or "local"
    archives_to_scan: List[Tuple[str, Path]] = [(archive_name, Path(local_archive_root))]
    for name in also_search_archives or []:
        cfg = registry.try_get(name)
        if cfg is not None:
            archives_to_scan.append((name, cfg.root))

    visited: Set[Tuple[str, str, str]] = set()
    result: List[ArtifactNode] = []
    frontier: List[Tuple[str, str, str]] = [(archive_name, run_id, filename)]
    depth = 0

    while frontier and depth < max_depth:
        depth += 1
        next_frontier = []
        for cur_archive_name, cur_run_id, cur_filename in frontier:
            key = (cur_archive_name, cur_run_id, cur_filename)
            if key in visited:
                continue
            visited.add(key)

            for scan_archive_name, scan_root in archives_to_scan:
                try:
                    conn = open_index(scan_root)
                except FileNotFoundError:
                    continue

                # A derived_from row in this archive's index points at
                # (cur_run_id, cur_filename) via a ref that's either
                # implicit-same-archive (ref_archive IS NULL, only valid
                # when scan_archive_name == cur_archive_name, and
                # ref_session IS NULL means "same session as the row's
                # own run_id") or fully explicit (ref_archive = X,
                # ref_session = Y).
                if scan_archive_name == cur_archive_name:
                    rows = conn.execute(
                        """
                        SELECT run_id, filename FROM derived_from
                        WHERE ref_file = ?
                          AND (ref_archive IS NULL OR ref_archive = ?)
                          AND (
                                ref_session = ?
                             OR (ref_session IS NULL AND run_id = ?)
                          )
                        """,
                        (cur_filename, cur_archive_name, cur_run_id, cur_run_id),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT run_id, filename FROM derived_from
                        WHERE ref_file = ?
                          AND ref_archive = ?
                          AND ref_session = ?
                        """,
                        (cur_filename, cur_archive_name, cur_run_id),
                    ).fetchall()
                conn.close()

                for row in rows:
                    node = ArtifactNode(
                        archive=scan_archive_name,
                        run_id=row["run_id"],
                        filename=row["filename"],
                    )
                    if node.key() in visited:
                        continue
                    result.append(node)
                    next_frontier.append((scan_archive_name, row["run_id"], row["filename"]))
        frontier = next_frontier

    return result
