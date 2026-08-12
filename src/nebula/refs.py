"""
Structured references between artifacts, sessions, archives and collections.

On disk, refs are strings. Two spellings, one meaning:

**Compact** -- for pointing at something nearby, which is the common case::

    "diode.graf"                    -> same-session file
    "S-26-0152"                     -> whole session, same archive
    "S-26-0152/diode.graf"          -> session + file, same archive
    "postdoc|S-26-0152/diode.graf"  -> another archive of your own

**Nebula URI** -- fully qualified, including *whose* archive it is::

    nebula://grant@ncsu.edu/postdoc/S-26-0152/diode.graf
    nebula://grant@ncsu.edu/postdoc/S-26-0152
    nebula://grant@ncsu.edu/postdoc
    nebula://grant@ncsu.edu/postdoc/collections/paper-2026

The user segment exists because archive names are not globally unique: two
colleagues can each have a "measurements" archive, and without an owner a
ref between them is ambiguous. See :mod:`nebula.identity` for who "you" are.

Why this shape rather than ``user.archive.session.file``: filenames contain
dots (``raw.csv``), so a dot cannot separate the last two components without
guesswork. Slashes inside a URI are unambiguous, familiar, and make the
whole thing a single copy-pasteable token. Names therefore may not contain
"/" -- enforced at parse time.

There is exactly one parser and one formatter. Anything that accepts a ref
-- ``derived_from``, ``related_runs``, collection entries -- accepts *both*
spellings, because they all come through here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

REF_ARCHIVE_SEP = "|"
REF_PATH_SEP = "/"

#: The URI scheme. Deliberately distinctive so a ref pasted into a note,
#: an issue or a chat message is recognisable as one.
URI_SCHEME = "nebula://"

#: Kept in sync with session.SESSION_ID_PREFIX. Used to disambiguate a bare
#: token ("S-26-0152" vs "diode.graf") when there's no '/' to split on.
SESSION_PREFIX = "S-"

#: Every prefix a session id can carry, kept in sync with config.KIND_PREFIX
#: (not imported, to keep this module free of config). Intake archives mint
#: I- ids, so a bare "I-26-0001" -- an ordinary thing to write in
#: related_runs while filling one in -- has to read as a session and not as
#: a filename in the current one.
SESSION_PREFIXES = ("S-", "I-")

#: Path segment marking a collection inside an archive. A session id can
#: never look like this, so the two namespaces can share a URI space.
COLLECTIONS_SEGMENT = "collections"

#: Likewise for assets -- mutable files that live outside any session.
#: An asset ref names the asset's opaque id (AF-26-0017), not its
#: filename, because the filename is a label the user may change at any
#: time. The readable name rides alongside in the stored ref dict; see
#: nebula.assets on why identity and name are kept apart.
ASSETS_SEGMENT = "assets"

#: Kept in sync with assets.ASSET_PREFIX. Not imported from there: assets
#: reaches this module through sidecar, and a shared two-character
#: constant is not worth a cycle.
ASSET_PREFIX = "AF-"

_SESSION_RE = re.compile(
    "^(?:" + "|".join(re.escape(p) for p in SESSION_PREFIXES) + r")\d{2}-\d+$")
_ASSET_RE = re.compile(rf"^{re.escape(ASSET_PREFIX)}\d{{2}}-\d+$")


@dataclass(frozen=True)
class Ref:
    """A reference to an artifact, a session, an archive, or a collection.

    file:       the artifact filename, or None.
    session:    the session id, or None to mean "this same session" (only
                meaningful for same-archive refs).
    archive:    the archive name, or None to mean "this same archive".
    user:       who owns that archive, or None to mean "me" (see identity).
    collection: a collection name, or None. Mutually exclusive with
                session/file -- a collection is a sibling namespace.
    asset:      an asset id (AF-26-0017), or None. Also a sibling
                namespace, and likewise mutually exclusive with
                session/file.
    """

    file: Optional[str] = None
    session: Optional[str] = None
    archive: Optional[str] = None
    user: Optional[str] = None
    collection: Optional[str] = None
    asset: Optional[str] = None

    def is_cross_archive(self) -> bool:
        return self.archive is not None

    def is_cross_user(self) -> bool:
        return self.user is not None

    def is_same_session(self) -> bool:
        return (self.session is None and self.collection is None
                and self.asset is None)

    @property
    def kind(self) -> str:
        """What this points at: file | session | collection | asset | archive."""
        if self.collection:
            return "collection"
        if self.asset:
            return "asset"
        if self.file:
            return "file"
        if self.session:
            return "session"
        return "archive"

    def resolved(self, *, archive: str, session: str,
                 user: Optional[str] = None) -> "Ref":
        """A copy with archive/session/user filled in from context wherever
        this ref left them implicit."""
        sibling = self.collection is not None or self.asset is not None
        return Ref(
            file=self.file,
            session=self.session or (None if sibling else session),
            archive=self.archive or archive,
            user=self.user or user,
            collection=self.collection,
            asset=self.asset,
        )


def _check_segment(value: str, what: str, text: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"malformed ref (empty {what}): {text!r}")
    if REF_PATH_SEP in value:
        raise ValueError(f"malformed ref ({what} contains '/'): {text!r}")
    return value


def parse_uri(text: str) -> Ref:
    """Parse a fully-qualified nebula:// URI.

        nebula://<user>/<archive>[/<session>[/<file>]]
        nebula://<user>/<archive>/collections/<name>
        nebula://<user>/<archive>/assets/<asset-id>
    """
    body = text[len(URI_SCHEME):]
    parts = [p for p in body.split(REF_PATH_SEP)]
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError(
            f"malformed nebula URI: {text!r}; expected "
            f"{URI_SCHEME}<user>/<archive>[/<session>[/<file>]]")

    user = _check_segment(parts[0], "user", text)
    archive = _check_segment(parts[1], "archive", text)
    rest = [p for p in parts[2:] if p != ""]

    if not rest:
        return Ref(user=user, archive=archive)
    if rest[0] == COLLECTIONS_SEGMENT:
        if len(rest) != 2:
            raise ValueError(f"malformed collection URI: {text!r}; expected "
                             f".../{COLLECTIONS_SEGMENT}/<name>")
        return Ref(user=user, archive=archive,
                   collection=_check_segment(rest[1], "collection", text))
    if rest[0] == ASSETS_SEGMENT:
        if len(rest) != 2:
            raise ValueError(f"malformed asset URI: {text!r}; expected "
                             f".../{ASSETS_SEGMENT}/<asset-id>")
        return Ref(user=user, archive=archive,
                   asset=_check_segment(rest[1], "asset", text))
    if len(rest) > 2:
        raise ValueError(f"malformed nebula URI (too many segments): {text!r}")

    session = _check_segment(rest[0], "session", text)
    file = _check_segment(rest[1], "file", text) if len(rest) == 2 else None
    return Ref(user=user, archive=archive, session=session, file=file)


def parse_ref(text: str) -> Ref:
    """Parse either spelling into a Ref.

    Raises ValueError on anything malformed rather than guessing, since a
    silently-wrong provenance link is worse than a loud failure.
    """
    if not text or not text.strip():
        raise ValueError("empty ref string")
    text = text.strip()

    if text.lower().startswith(URI_SCHEME):
        return parse_uri(URI_SCHEME + text[len(URI_SCHEME):])

    archive: Optional[str] = None
    if REF_ARCHIVE_SEP in text:
        parts = text.split(REF_ARCHIVE_SEP)
        if len(parts) != 2:
            raise ValueError(f"malformed ref (multiple '|'): {text!r}")
        archive, text = parts[0].strip(), parts[1].strip()
        if not archive:
            raise ValueError(f"malformed ref (empty archive before '|'): {text!r}")
        if not text:
            raise ValueError(f"malformed ref (nothing after '|'): {text!r}")

    if REF_PATH_SEP in text:
        head, _, tail = text.partition(REF_PATH_SEP)
        head, tail = head.strip(), tail.strip()
        if not head or not tail:
            raise ValueError(f"malformed ref (empty session/file): {text!r}")
        if head == COLLECTIONS_SEGMENT:
            return Ref(archive=archive,
                       collection=_check_segment(tail, "collection", text))
        if head == ASSETS_SEGMENT:
            return Ref(archive=archive,
                       asset=_check_segment(tail, "asset", text))
        if REF_PATH_SEP in tail:
            raise ValueError(f"malformed ref (too many '/'): {text!r}")
        return Ref(session=head, file=tail, archive=archive)

    # No '/': a bare session id or a bare filename. Session ids are
    # S-<yy>-<nnnn>; anything else is a filename in this same session.
    if _SESSION_RE.match(text):
        return Ref(session=text, archive=archive)
    # An asset id is as recognisable as a session id and can never collide
    # with a filename, so a bare one resolves without the segment.
    if _ASSET_RE.match(text):
        return Ref(asset=text, archive=archive)
    return Ref(file=text, archive=archive)


def format_ref(ref: Ref) -> str:
    """The shortest spelling that still says what the ref means.

    A ref naming a user is always a full URI -- there is no compact form
    that can carry an owner.
    """
    if ref.user:
        return format_uri(ref)

    if ref.collection:
        body = f"{COLLECTIONS_SEGMENT}{REF_PATH_SEP}{ref.collection}"
    elif ref.asset:
        # The id alone, not the filename riding alongside it: the name is
        # a label that may already be stale, the id never is.
        body = f"{ASSETS_SEGMENT}{REF_PATH_SEP}{ref.asset}"
    elif ref.file and ref.session:
        body = f"{ref.session}{REF_PATH_SEP}{ref.file}"
    elif ref.file:
        body = ref.file
    elif ref.session:
        body = ref.session
    elif ref.archive:
        return ref.archive
    else:
        raise ValueError(
            "Ref must name at least a session, file, collection or asset")

    if ref.archive:
        return f"{ref.archive}{REF_ARCHIVE_SEP}{body}"
    return body


def format_uri(ref: Ref, *, user: Optional[str] = None,
               archive: Optional[str] = None) -> str:
    """The fully-qualified spelling. `user`/`archive` fill in what the ref
    leaves implicit -- pass the local identity and archive name to turn a
    compact ref into something a colleague can follow."""
    owner = ref.user or user
    arc = ref.archive or archive
    if not owner or not arc:
        raise ValueError(
            "a nebula URI needs a user and an archive; got "
            f"user={owner!r} archive={arc!r}")

    parts = [owner, arc]
    if ref.collection:
        parts += [COLLECTIONS_SEGMENT, ref.collection]
    elif ref.asset:
        parts += [ASSETS_SEGMENT, ref.asset]
    else:
        if ref.session:
            parts.append(ref.session)
        if ref.file:
            if not ref.session:
                raise ValueError("a URI for a file needs its session")
            parts.append(ref.file)
    return URI_SCHEME + REF_PATH_SEP.join(parts)
