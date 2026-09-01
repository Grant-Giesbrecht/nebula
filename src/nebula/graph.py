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

**An archive is identified by owner *and* name, never by name alone.** Two
colleagues can each keep a "postdoc", so a ref carrying an owner is looked
up with `Registry.find(name, user)` -- which matches the name the *author*
wrote (the archive's own declared name) against the owner they meant.
Resolving by registry nickname instead, as this module did before
2026-09-01, silently answered with whichever archive this machine happened
to file under that word: a name that is local to one laptop and travels
with nothing. A node therefore carries its `user`, and identity is
(user, archive, session, filename) all the way through.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from nebula.index import open_fresh
from nebula.refs import Ref, format_ref, format_uri
from nebula.registry import Registry, get_registry, resolve_archive


@dataclass(frozen=True)
class ArtifactNode:
    """One artifact in the provenance graph."""

    archive: str  # archive *label* this artifact lives in (never None)
    run_id: str
    filename: str
    path: Optional[str] = None  # filesystem path, if resolvable
    unresolved: bool = False  # True if we couldn't reach this archive to verify it exists
    #: Who owns `archive`. None means the traversal never learned an owner
    #: -- an unregistered scratch archive, or one whose archive.yaml names
    #: nobody. Kept distinct from the local user rather than defaulted to
    #: it: "mine" and "unknown" are different claims.
    user: Optional[str] = None
    #: The archive's immutable id, when known. Identity is on this, not on
    #: `archive`: the label is free to change and two nodes that differ only
    #: by it are one artifact seen before and after a rename.
    archive_id: Optional[str] = None

    def key(self) -> Tuple[Optional[str], str, str, str]:
        """Graph identity. Includes the owner, so a ref into a colleague's
        `postdoc` is a different node from one into your own -- and prefers
        the archive id over the label wherever one is known."""
        return (self.user, self.archive_id or self.archive, self.run_id,
                self.filename)

    def ref(self, *, user: Optional[str] = None,
            archive_id: Optional[str] = None) -> Ref:
        """This node as a :class:`~nebula.refs.Ref`, relative to a context.

        `user` and `archive_id` say where the reader is standing. Whatever
        matches is dropped, so what comes back is the shortest ref that
        still names this artifact from there -- which is the same rule
        `refs.format_ref` applies, rather than a second one living here.
        """
        from nebula import archive_id as archive_id_mod

        same_archive = bool(self.archive_id and archive_id
                            and archive_id_mod.same_id(self.archive_id,
                                                       archive_id))
        same_user = self.user is None or (user is not None and self.user == user)
        if same_archive and same_user:
            return Ref(session=self.run_id, file=self.filename)
        return Ref(user=None if same_user else self.user,
                   archive=self.archive, archive_id=self.archive_id,
                   session=self.run_id, file=self.filename)

    def describe(self, relative_to: Optional[str] = None, *,
                 archive_id: Optional[str] = None) -> str:
        """How to name this node to a reader in `relative_to`'s archive.

        A traversal that never leaves home prints bare `S-26-0152/raw.csv`;
        one that crosses into another archive prints a URI naming it. Always
        a ref in the current grammar, so anything printed can be pasted
        straight back into a `derived_from`.
        """
        try:
            text = format_ref(self.ref(user=relative_to, archive_id=archive_id))
        except ValueError:
            text = f"{self.archive}/{self.run_id}/{self.filename}"
        return text + (" (unresolved)" if self.unresolved else "")

    def __str__(self) -> str:
        return self.describe()

    @property
    def uri(self) -> Optional[str]:
        """The fully-qualified name of this artifact, or None when its owner
        is unknown -- in which case there is no URI to give, only a local
        one, and inventing an owner would be a guess."""
        if not self.user:
            return None
        return format_uri(Ref(user=self.user, archive=self.archive,
                              archive_id=self.archive_id,
                              session=self.run_id, file=self.filename))


def _archive_id_of(archive_root: Path) -> Optional[str]:
    """An archive's declared id, or None. Read, never minted: a traversal is
    a query, and a query should not write to somebody's archive.yaml."""
    try:
        from nebula.config import read_settings

        return read_settings(archive_root, apply_env=False).id or None
    except Exception:       # noqa: BLE001
        return None


def _archive_owner(archive_root: Path) -> Optional[str]:
    """Who an archive on disk says it belongs to, or None.

    Only what is *declared* counts. Falling back to this machine's identity
    would stamp your name onto a colleague's archive that happens to be
    mounted here, and every node built from it would then claim to be
    yours.
    """
    try:
        from nebula.config import read_settings

        return read_settings(archive_root, apply_env=False).user or None
    except Exception:       # noqa: BLE001 -- an unreadable archive.yaml is not fatal here
        return None


def _resolve_archive_root(
    archive_name: str,
    user: Optional[str],
    local_archive_root: Path,
    local_archive_name: str,
    local_user: Optional[str],
    registry: Registry,
    archive_id: Optional[str] = None,
    local_archive_id: Optional[str] = None,
) -> Optional[Path]:
    """Where an archive lives on this machine, or None.

    Identified by id where the ref carries one, falling back to
    (name, owner) where it does not -- which is every ref written before ids
    existed, and every ref into an archive we have never given one.

    The local archive short-circuits, so a traversal that never leaves home
    does not need the archive registered at all -- `nebula upstream
    /some/scratch/dir ...` is a supported, tested way to work.
    """
    from nebula import archive_id as archive_id_mod

    if archive_id and local_archive_id and archive_id_mod.same_id(
            archive_id, local_archive_id):
        return local_archive_root
    if not archive_id and archive_name == local_archive_name and (
            user is None or local_user is None or user == local_user):
        return local_archive_root
    cfg = registry.find(archive_name, user, archive_id=archive_id)
    if cfg is None and user is not None:
        # An owner we cannot place is worth one more try without it: an
        # archive registered here before owners existed records none, and
        # refusing to follow the edge would report a chain as broken when
        # it is merely old. `find(name, None)` is the compact-ref rule --
        # "whoever, as long as the name matches" -- and is only reached
        # after the precise lookup has already failed.
        cfg = registry.find(archive_name, None)
        if cfg is not None and cfg.user and cfg.user != user:
            return None     # a different person's archive: not this one
    return cfg.root if cfg else None


def _same_archive(name_a: str, id_a: Optional[str],
                  name_b: str, id_b: Optional[str]) -> bool:
    """Whether two archives are the same one.

    By id when both sides have one -- that survives a rename, which is the
    whole reason ids exist. By name only when at least one side has no id to
    compare, which is every archive that predates them.
    """
    from nebula import archive_id as archive_id_mod

    if id_a and id_b:
        return archive_id_mod.same_id(id_a, id_b)
    return name_a == name_b


def _same_owner(a: Optional[str], b: Optional[str]) -> bool:
    """Whether two owner strings name the same person, for the purpose of
    matching a ref that left its owner implicit.

    An unknown owner on either side counts as a match. That is the
    permissive answer, and it is the right one here: an archive that
    declares no owner is overwhelmingly the user's own scratch archive, and
    treating "unknown" as "definitely somebody else" would break every
    traversal in an archive created before owners were recorded.
    """
    return a is None or b is None or a == b


class _Indexes:
    """One connection per archive for the life of a traversal.

    Both directions revisit the same archives many times, and each open
    would otherwise re-run the freshness sweep. Sweeping once per archive
    per query keeps a deep traversal proportional to the graph rather than
    to the graph times the size of the archive.
    """

    def __init__(self):
        self._conns: Dict[str, Optional[object]] = {}
        self._sessions: Dict[Tuple[str, str], Optional[str]] = {}

    def get(self, archive_root: Path):
        key = str(archive_root)
        if key not in self._conns:
            try:
                self._conns[key] = open_fresh(archive_root)
            except Exception:   # noqa: BLE001 -- no index, or an unusable one
                self._conns[key] = None
        return self._conns[key]

    def artifact_path(self, archive_root: Path, run_id: str, filename: str) -> Optional[str]:
        """Where an artifact actually lives. Session paths are stored
        relative to the archive root, so this resolves correctly for an
        archive that has moved or is mounted elsewhere on this machine.
        Returns None when the session isn't indexed, rather than inventing
        a path that may not exist."""
        key = (str(archive_root), run_id)
        if key not in self._sessions:
            conn = self.get(archive_root)
            rel = None
            if conn is not None:
                try:
                    row = conn.execute(
                        "SELECT rel_path FROM sessions WHERE run_id = ?", (run_id,)
                    ).fetchone()
                    rel = row["rel_path"] if row else None
                except Exception:   # noqa: BLE001 -- a broken index isn't fatal here
                    rel = None
            self._sessions[key] = rel
        rel = self._sessions[key]
        return str(Path(archive_root) / rel / filename) if rel else None

    def close(self) -> None:
        for conn in self._conns.values():
            if conn is not None:
                try:
                    conn.close()
                except Exception:   # noqa: BLE001
                    pass
        self._conns.clear()


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
    local_user = _archive_owner(Path(local_archive_root))
    local_id = _archive_id_of(Path(local_archive_root))
    visited: Set[Tuple[Optional[str], str, str, str]] = set()
    result: List[ArtifactNode] = []
    frontier: List[Tuple[Optional[str], str, Optional[str], Path, str, str]] = [
        (local_user, archive_name, local_id, Path(local_archive_root),
         run_id, filename)
    ]
    depth = 0
    indexes = _Indexes()

    try:
        while frontier and depth < max_depth:
            depth += 1
            next_frontier = []
            for (cur_user, cur_archive_name, cur_archive_id, cur_root,
                 cur_run_id, cur_filename) in frontier:
                key = (cur_user, cur_archive_id or cur_archive_name,
                       cur_run_id, cur_filename)
                if key in visited:
                    continue
                visited.add(key)

                conn = indexes.get(cur_root)
                if conn is None:
                    continue

                rows = conn.execute(
                    "SELECT ref_user, ref_archive, ref_archive_id, "
                    "ref_session, ref_file FROM derived_from "
                    "WHERE run_id = ? AND filename = ?",
                    (cur_run_id, cur_filename),
                ).fetchall()

                for row in rows:
                    # A NULL ref_user means "the same owner as the archive
                    # holding this row" -- the same rule NULL ref_archive
                    # follows for the name. Inheriting from cur_user rather
                    # than from the local identity is what keeps a chain
                    # that has already crossed into someone else's archive
                    # attributed to them for the rest of the walk.
                    parent_user = row["ref_user"] or cur_user
                    parent_archive = row["ref_archive"] or cur_archive_name
                    # A ref that names no archive means this one, id and all;
                    # one that names an archive but no id has to resolve by
                    # name, so the current archive's id must not be lent to
                    # it -- that would claim the wrong identity.
                    parent_archive_id = (row["ref_archive_id"] or
                                         (cur_archive_id
                                          if not row["ref_archive"] else None))
                    parent_session = row["ref_session"] or cur_run_id
                    parent_file = row["ref_file"]
                    if parent_file is None:
                        continue  # whole-session ref, not an artifact edge

                    parent_root = _resolve_archive_root(
                        parent_archive, parent_user, Path(local_archive_root),
                        archive_name, local_user, registry,
                        archive_id=parent_archive_id, local_archive_id=local_id,
                    )
                    if parent_root is None:
                        result.append(
                            ArtifactNode(
                                archive=parent_archive,
                                run_id=parent_session,
                                filename=parent_file,
                                unresolved=True,
                                user=parent_user,
                                archive_id=parent_archive_id,
                            )
                        )
                        continue

                    node = ArtifactNode(
                        archive=parent_archive,
                        run_id=parent_session,
                        filename=parent_file,
                        path=indexes.artifact_path(parent_root, parent_session, parent_file),
                        user=parent_user,
                        archive_id=parent_archive_id or _archive_id_of(parent_root),
                    )
                    result.append(node)
                    next_frontier.append(
                        (parent_user, parent_archive,
                         node.archive_id, parent_root,
                         parent_session, parent_file)
                    )
            frontier = next_frontier
    finally:
        indexes.close()

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
    local_user = _archive_owner(Path(local_archive_root))
    local_id = _archive_id_of(Path(local_archive_root))
    # Each scanned archive is carried with its own owner and id: a row in
    # *its* index that names no archive means "this one", which is not
    # necessarily the one the query started in.
    archives_to_scan: List[Tuple[Optional[str], str, Optional[str], Path]] = [
        (local_user, archive_name, local_id, Path(local_archive_root))
    ]
    for name in also_search_archives or []:
        cfg = registry.try_get(name) or registry.find(name)
        if cfg is not None:
            archives_to_scan.append(
                (cfg.user or _archive_owner(Path(cfg.root)), name,
                 cfg.archive_id or _archive_id_of(Path(cfg.root)), cfg.root))

    visited: Set[Tuple[Optional[str], str, str, str]] = set()
    result: List[ArtifactNode] = []
    frontier: List[Tuple[Optional[str], str, Optional[str], str, str]] = [
        (local_user, archive_name, local_id, run_id, filename)
    ]
    depth = 0
    indexes = _Indexes()

    try:
        while frontier and depth < max_depth:
            depth += 1
            next_frontier = []
            for (cur_user, cur_archive_name, cur_archive_id, cur_run_id,
                 cur_filename) in frontier:
                key = (cur_user, cur_archive_id or cur_archive_name,
                       cur_run_id, cur_filename)
                if key in visited:
                    continue
                visited.add(key)

                for (scan_user, scan_archive_name, scan_archive_id,
                     scan_root) in archives_to_scan:
                    conn = indexes.get(scan_root)
                    if conn is None:
                        continue

                    # A derived_from row in this archive's index points at
                    # (cur_run_id, cur_filename) via a ref that's either
                    # implicit-same-archive (ref_archive IS NULL, only valid
                    # when scan_archive_name == cur_archive_name, and
                    # ref_session IS NULL means "same session as the row's
                    # own run_id") or fully explicit (ref_archive = X,
                    # ref_session = Y).
                    #
                    # ref_user follows the same NULL-means-implicit rule,
                    # and is the reason this query can no longer key on the
                    # archive name alone: a row saying `postdoc|S-26-0152`
                    # in a colleague's archive means *their* postdoc.
                    same_owner = _same_owner(scan_user, cur_user)
                    same_archive = _same_archive(
                        scan_archive_name, scan_archive_id,
                        cur_archive_name, cur_archive_id)
                    if same_archive and same_owner:
                        rows = conn.execute(
                            """
                            SELECT run_id, filename FROM derived_from
                            WHERE ref_file = ?
                              AND (ref_archive IS NULL OR ref_archive = ?
                                   OR ref_archive_id = ?)
                              AND (ref_user IS NULL OR ref_user = ?)
                              AND (
                                    ref_session = ?
                                 OR (ref_session IS NULL AND run_id = ?)
                              )
                            """,
                            (cur_filename, cur_archive_name, cur_archive_id,
                             cur_user, cur_run_id, cur_run_id),
                        ).fetchall()
                    else:
                        # A NULL ref_user here means the row's author meant
                        # their *own* archive, so it can only be the target
                        # when the two archives share an owner. Written as
                        # two statements rather than one clever predicate:
                        # the condition is about which rows are eligible at
                        # all, not about a column's value.
                        owner_clause = ("(ref_user = ? OR ref_user IS NULL)"
                                        if same_owner else "ref_user = ?")
                        rows = conn.execute(
                            f"""
                            SELECT run_id, filename FROM derived_from
                            WHERE ref_file = ?
                              AND (ref_archive = ? OR ref_archive_id = ?)
                              AND ref_session = ?
                              AND {owner_clause}
                            """,
                            (cur_filename, cur_archive_name, cur_archive_id,
                             cur_run_id, cur_user),
                        ).fetchall()

                    for row in rows:
                        node = ArtifactNode(
                            archive=scan_archive_name,
                            run_id=row["run_id"],
                            filename=row["filename"],
                            path=indexes.artifact_path(scan_root, row["run_id"], row["filename"]),
                            user=scan_user,
                            archive_id=scan_archive_id,
                        )
                        if node.key() in visited:
                            continue
                        result.append(node)
                        next_frontier.append(
                            (scan_user, scan_archive_name, scan_archive_id,
                             row["run_id"], row["filename"]))
            frontier = next_frontier
    finally:
        indexes.close()

    return result
