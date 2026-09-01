"""
Structured references between artifacts, sessions, archives and collections.

On disk, refs are strings, and there is **one grammar**: ``/``-separated
segments, where you may drop a *prefix* of them and the missing ones mean
"here". An absolute path versus a relative one::

    "diode.graf"                                   this session
    "S-26-0152"                                    this archive
    "S-26-0152/diode.graf"                         this archive
    "collections/paper-2026"                       this archive
    "assets/AF-26-0017"                            this archive
    "nebula://postdoc~0fe/S-26-0152/diode.graf"    another archive of mine
    "nebula://grant@ncsu.edu/postdoc~0fe/S-26-0152/diode.graf"   someone else's

The scheme marks where the path starts:

    nebula:// whenever you name an archive or a user.
    Bare when you are inside this archive.

Filesystems do not make you write ``file:///`` for a relative path and
neither does this -- the commonest ref by far is a bare filename in a
``derived_from``, and ``nebula://raw.csv`` would be both longer and
misleading, with a filename sitting where a hostname goes.

**The archive segment is ``label~id``.** The id is minted once and never
changes; the label is decorative and is *never compared or resolved on* (see
:meth:`Ref.identity` and ``docs/uri-grammar.md``). That is what lets an
archive be renamed without dangling every ref ever written into it. Ids are
numbers written in hex with optional leading zeros, so ``0fe`` and ``00fe``
are one id -- :mod:`nebula.archive_id` has the arithmetic.

The user segment exists because archive *names* are not globally unique: two
colleagues can each have a "measurements" archive. See
:mod:`nebula.identity` for who "you" are.

Reading ``nebula://A/B/...``, in order:

1. ``A`` contains ``@``     -> ``A`` is a user (the ``value@authority``
                               production; nothing else in a URI has one).
2. ``A`` contains ``~``     -> ``A`` is an archive, user implicit. Nothing
                               else contains ``~``, which is why archive
                               names may not.
3. ``B`` is session-shaped
   or a reserved segment    -> an archive that omitted its id. *Error*,
                               saying so, rather than misreading ``A``.
4. otherwise                -> ``A`` is a bare (unqualified) user and ``B``
                               is the archive. The legacy form, kept because
                               `identity` deliberately permits a bare name
                               and rewriting one would change every URI
                               already pointing at that archive.

Why ``/`` rather than ``user.archive.session.file``: filenames contain dots
(``raw.csv``), so a dot cannot separate the last two components without
guesswork. Slashes are unambiguous, familiar, and keep the whole thing a
single copy-pasteable token. Segments therefore may not contain "/" --
enforced at parse time.

**Legacy.** ``postdoc|S-26-0152/diode.graf`` is still *parsed* and never
*emitted*: refs written before the grammar was unified keep resolving and
get rewritten the next time anything reformats them, the same way
``registry`` still reads the old ``archives.yaml`` filename. So is a URI
whose archive segment carries no id.

There is exactly one parser and one formatter. Anything that accepts a ref
-- ``derived_from``, ``related_runs``, collection entries -- accepts every
spelling, because they all come through here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

#: Legacy separator between an archive and the rest of a compact ref.
#: Parsed forever, never written -- see the module docstring.
REF_ARCHIVE_SEP = "|"
REF_PATH_SEP = "/"

#: Separates an archive's readable label from its immutable id inside one
#: URI segment: ``postdoc~0fe``. Chosen because it is unreserved in RFC 3986
#: (so a URI stays one pasteable token) and because forbidding it in archive
#: names costs nothing -- see config.clean_archive_name.
ARCHIVE_ID_SEP = "~"

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
    #: The archive's immutable id, normalised (`archive_id.normalize`), or
    #: None for a ref that names no archive or an archive that has none.
    #: This -- not `archive` -- is what identifies the archive.
    archive_id: Optional[str] = None

    def identity(self) -> tuple:
        """What this ref *means*, for comparison and de-duplication.

        The archive label is omitted whenever an id is present, because the
        label is decorative: `postdoc~0fe` and `thesis~0fe` are one archive
        renamed, and two refs into it are the same ref. Comparing the whole
        string instead would hand you two `derived_from` edges to one object
        the moment somebody renamed something -- exactly the failure the id
        exists to prevent.

        Everything that dedupes refs (collection membership, related_runs,
        cycle detection) compares this rather than the formatted string.
        """
        return (self.user, self.archive_id or self.archive, self.session,
                self.file, self.collection, self.asset)

    def same_target(self, other: "Ref") -> bool:
        return isinstance(other, Ref) and self.identity() == other.identity()

    @property
    def archive_segment(self) -> Optional[str]:
        """How the archive is written in a URI: ``label~id``, or just the
        label when there is no id (a legacy archive), or None."""
        if not self.archive:
            return None
        if not self.archive_id:
            return self.archive
        return f"{self.archive}{ARCHIVE_ID_SEP}{self.archive_id}"

    def is_cross_archive(self) -> bool:
        return self.archive is not None or self.archive_id is not None

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
                 user: Optional[str] = None,
                 archive_id: Optional[str] = None) -> "Ref":
        """A copy with archive/session/user filled in from context wherever
        this ref left them implicit.

        The id is filled in only alongside the archive it belongs to: giving
        this archive's id to a ref that already names a *different* archive
        would claim the wrong identity for it.
        """
        sibling = self.collection is not None or self.asset is not None
        keeps_archive = self.archive is not None or self.archive_id is not None
        return Ref(
            file=self.file,
            session=self.session or (None if sibling else session),
            archive=self.archive or archive,
            user=self.user or user,
            collection=self.collection,
            asset=self.asset,
            archive_id=self.archive_id if keeps_archive else archive_id,
        )


def _check_segment(value: str, what: str, text: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"malformed ref (empty {what}): {text!r}")
    if REF_PATH_SEP in value:
        raise ValueError(f"malformed ref ({what} contains '/'): {text!r}")
    return value


def _split_archive(segment: str, text: str) -> "tuple[str, Optional[str]]":
    """An archive segment into `(label, id)`.

    Split on the *last* separator, the same rule `identity.parse_identity`
    uses for `@`: it needs no knowledge of what a label may contain. A tail
    that is not id-shaped means the whole segment is the label -- an archive
    named before ids existed, or one whose name contains a `~` from before
    `config.clean_archive_name` forbade it.
    """
    from nebula import archive_id as archive_id_mod

    value = _check_segment(segment, "archive", text)
    if ARCHIVE_ID_SEP not in value:
        return value, None
    label, _, tail = value.rpartition(ARCHIVE_ID_SEP)
    if not label or not archive_id_mod.is_archive_id(tail):
        return value, None
    return label, archive_id_mod.normalize_or_none(tail)


def _is_archive_relative(segment: str) -> bool:
    """Whether a segment starts the *inside* of an archive -- a session id,
    or the reserved word that opens a sibling namespace. Used to catch an
    archive segment that forgot its id, rather than misreading it as a
    user."""
    return bool(_SESSION_RE.match(segment)
                or segment in (COLLECTIONS_SEGMENT, ASSETS_SEGMENT))


def _looks_like_user(segment: str) -> bool:
    return "@" in segment


def _looks_like_archive(segment: str) -> bool:
    return ARCHIVE_ID_SEP in segment


def parse_uri(text: str) -> Ref:
    """Parse a fully-qualified nebula:// URI.

        nebula://<user>/<archive~id>[/<session>[/<file>]]
        nebula://<archive~id>[/<session>[/<file>]]          (user implicit)
        nebula://<user>/<archive~id>/collections/<name>
        nebula://<user>/<archive~id>/assets/<asset-id>

    The four-rule reading of the first segment is in the module docstring;
    the order matters and each branch is there for a reason.
    """
    body = text[len(URI_SCHEME):]
    parts = [p for p in body.split(REF_PATH_SEP) if p != ""]
    if not parts:
        raise ValueError(
            f"malformed nebula URI: {text!r}; expected "
            f"{URI_SCHEME}<user>/<archive~id>[/<session>[/<file>]]")

    first = parts[0].strip()
    second = parts[1].strip() if len(parts) > 1 else ""

    if _looks_like_user(first):
        user = _check_segment(first, "user", text)
        if len(parts) < 2:
            raise ValueError(
                f"malformed nebula URI (no archive): {text!r}; expected "
                f"{URI_SCHEME}<user>/<archive~id>[/<session>[/<file>]]")
        archive, archive_id = _split_archive(parts[1], text)
        rest = parts[2:]
    elif _looks_like_archive(first):
        user = None
        archive, archive_id = _split_archive(first, text)
        rest = parts[1:]
    elif second and _is_archive_relative(second):
        # `nebula://postdoc/S-26-0152`: the author meant an archive and left
        # its id off. Saying so beats reading `postdoc` as a person.
        raise ValueError(
            f"malformed nebula URI: {text!r}; {first!r} looks like an "
            f"archive, but an archive named without a user needs its id "
            f"({first}{ARCHIVE_ID_SEP}0fe). With a user it does not: "
            f"{URI_SCHEME}<user>/{first}/...")
    else:
        # A bare, unqualified user -- the legacy spelling, still legal
        # because `identity` permits a name with no authority and rewriting
        # one would change every URI already pointing at that archive.
        user = _check_segment(first, "user", text)
        if len(parts) < 2:
            raise ValueError(
                f"malformed nebula URI (no archive): {text!r}; expected "
                f"{URI_SCHEME}<user>/<archive~id>[/<session>[/<file>]]")
        archive, archive_id = _split_archive(parts[1], text)
        rest = parts[2:]

    return _finish(user, archive, archive_id, rest, text)


def _finish(user, archive, archive_id, rest, text: str) -> Ref:
    """The part of a URI after the archive, which is the same wherever the
    archive came from."""
    if not rest:
        return Ref(user=user, archive=archive, archive_id=archive_id)
    if rest[0] == COLLECTIONS_SEGMENT:
        if len(rest) != 2:
            raise ValueError(f"malformed collection URI: {text!r}; expected "
                             f".../{COLLECTIONS_SEGMENT}/<name>")
        return Ref(user=user, archive=archive, archive_id=archive_id,
                   collection=_check_segment(rest[1], "collection", text))
    if rest[0] == ASSETS_SEGMENT:
        if len(rest) != 2:
            raise ValueError(f"malformed asset URI: {text!r}; expected "
                             f".../{ASSETS_SEGMENT}/<asset-id>")
        return Ref(user=user, archive=archive, archive_id=archive_id,
                   asset=_check_segment(rest[1], "asset", text))
    if len(rest) > 2:
        raise ValueError(f"malformed nebula URI (too many segments): {text!r}")

    session = _check_segment(rest[0], "session", text)
    file = _check_segment(rest[1], "file", text) if len(rest) == 2 else None
    return Ref(user=user, archive=archive, archive_id=archive_id,
               session=session, file=file)


def parse_ref(text: str) -> Ref:
    """Parse any spelling into a Ref.

    Raises ValueError on anything malformed rather than guessing, since a
    silently-wrong provenance link is worse than a loud failure.
    """
    if not text or not text.strip():
        raise ValueError("empty ref string")
    text = text.strip()

    if text.lower().startswith(URI_SCHEME):
        return parse_uri(URI_SCHEME + text[len(URI_SCHEME):])

    archive: Optional[str] = None
    archive_id: Optional[str] = None
    if REF_ARCHIVE_SEP in text:
        # The legacy compact spelling. Read forever, written never.
        parts = text.split(REF_ARCHIVE_SEP)
        if len(parts) != 2:
            raise ValueError(f"malformed ref (multiple '|'): {text!r}")
        head, text = parts[0].strip(), parts[1].strip()
        if not head:
            raise ValueError(f"malformed ref (empty archive before '|'): {text!r}")
        if not text:
            raise ValueError(f"malformed ref (nothing after '|'): {text!r}")
        archive, archive_id = _split_archive(head, head)

    if REF_PATH_SEP in text:
        head, _, tail = text.partition(REF_PATH_SEP)
        head, tail = head.strip(), tail.strip()
        if not head or not tail:
            raise ValueError(f"malformed ref (empty session/file): {text!r}")
        if head == COLLECTIONS_SEGMENT:
            return Ref(archive=archive, archive_id=archive_id,
                       collection=_check_segment(tail, "collection", text))
        if head == ASSETS_SEGMENT:
            return Ref(archive=archive, archive_id=archive_id,
                       asset=_check_segment(tail, "asset", text))
        if REF_PATH_SEP in tail:
            raise ValueError(f"malformed ref (too many '/'): {text!r}")
        return Ref(session=head, file=tail, archive=archive,
                   archive_id=archive_id)

    # No '/': a bare session id or a bare filename. Session ids are
    # S-<yy>-<nnnn>; anything else is a filename in this same session.
    if _SESSION_RE.match(text):
        return Ref(session=text, archive=archive, archive_id=archive_id)
    # An asset id is as recognisable as a session id and can never collide
    # with a filename, so a bare one resolves without the segment.
    if _ASSET_RE.match(text):
        return Ref(asset=text, archive=archive, archive_id=archive_id)
    return Ref(file=text, archive=archive, archive_id=archive_id)


def format_ref(ref: Ref) -> str:
    """The shortest spelling that still says what the ref means.

    Naming a user or an archive means naming where the path starts, which is
    what ``nebula://`` marks. Anything inside this archive stays bare. The
    legacy ``|`` form is never produced.
    """
    if ref.user or ref.archive_id:
        return format_uri(ref)
    if ref.archive:
        # An archive with no id and no owner. There is no URI spelling for
        # it -- without an id, an archive segment cannot be told from a user
        # segment when it is read back -- so the legacy `|` form is emitted,
        # and only here. It stops happening as soon as the archive has an id
        # (`config.ensure_archive_id`), which is minted the first time
        # anything creates, registers or asks for a URI for it.
        return f"{ref.archive}{REF_ARCHIVE_SEP}{_relative_body(ref)}"

    return _relative_body(ref)


def _relative_body(ref: Ref) -> str:
    """Everything after the archive: what a ref inside this archive looks
    like on its own."""
    if ref.collection:
        return f"{COLLECTIONS_SEGMENT}{REF_PATH_SEP}{ref.collection}"
    if ref.asset:
        # The id alone, not the filename riding alongside it: the name is
        # a label that may already be stale, the id never is.
        return f"{ASSETS_SEGMENT}{REF_PATH_SEP}{ref.asset}"
    if ref.file and ref.session:
        return f"{ref.session}{REF_PATH_SEP}{ref.file}"
    if ref.file:
        return ref.file
    if ref.session:
        return ref.session
    raise ValueError(
        "Ref must name at least a session, file, collection or asset")


def format_uri(ref: Ref, *, user: Optional[str] = None,
               archive: Optional[str] = None,
               archive_id: Optional[str] = None) -> str:
    """The spelling that starts at an archive, or at a user.

    `user`/`archive`/`archive_id` fill in what the ref leaves implicit --
    pass the local identity and archive to turn a bare ref into something a
    colleague can follow.

    A user is optional here, unlike before: ``nebula://postdoc~0fe/...``
    means "my other archive", and requiring an owner on it would force every
    same-user cross-archive ref to hard-code a name that may change. An
    *archive* is not optional, because without one there is nothing for the
    scheme to mark the start of.
    """
    from nebula import archive_id as archive_id_mod

    owner = ref.user or user
    arc = ref.archive or archive
    arc_id = ref.archive_id or archive_id
    if not arc and not arc_id:
        raise ValueError(
            "a nebula URI needs an archive; got "
            f"archive={arc!r} archive_id={arc_id!r}")
    if arc_id:
        arc_id = archive_id_mod.normalize_or_none(arc_id)

    segment = f"{arc}{ARCHIVE_ID_SEP}{arc_id}" if arc_id else arc
    if not owner and arc_id is None:
        # Without an id there is nothing to tell an archive segment from a
        # user segment, so this spelling would not parse back.
        raise ValueError(
            f"a nebula URI naming no user needs the archive's id "
            f"({arc}{ARCHIVE_ID_SEP}0fe), or it cannot be told from a user "
            f"name when it is read back")

    parts = [owner, segment] if owner else [segment]
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
