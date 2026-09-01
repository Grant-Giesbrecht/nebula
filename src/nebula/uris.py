"""
Minting and resolving fully-qualified ``nebula://`` URIs.

:mod:`nebula.refs` owns the *grammar* -- one parser, one formatter, and no
knowledge of what is on disk. This module owns the *facts*: which owner and
which archive name a particular directory actually claims, and where a URI
naming somebody else's archive lands on this machine.

The split is deliberate. ``refs`` is imported by ``sidecar``, which is
imported by nearly everything; giving it a dependency on ``config`` (to read
archive.yaml) and ``registry`` (to search this machine) would make a cycle
and would also let a formatting question quietly become a filesystem
question. So the rule is:

    refs.py    -- given the parts, what does the URI look like?
    uris.py    -- what *are* the parts, and what do they point at here?

Everything that produces a URI for a human -- ``nebula uri``, the
Navigator's "Copy URI", the line a script prints when it saves an artifact,
and the ``origin`` stamped into a transferred session -- comes through
:func:`uri_for`, so there is exactly one answer to "what is this thing
called".

**Uniqueness is a property of the owner, not of the syntax.** A URI is
globally unique only as far as its ``value@authority`` owner is: two people
who both call themselves ``grant@local`` mint colliding URIs and nothing
here can tell. :func:`describe` therefore returns the URI *and* the reasons
it might not be unique, and every caller shows them. Saying "here is your
permanent identifier" over an unqualified name would be the one lie this
whole subsystem exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from nebula.refs import (
    ASSETS_SEGMENT,
    COLLECTIONS_SEGMENT,
    Ref,
    URI_SCHEME,
    format_uri,
    parse_ref,
    parse_uri,
)

#: Stands in for an owner an archive does not declare and this machine
#: cannot supply. A URI containing it is *not* resolvable by anyone else --
#: it is emitted only where refusing outright would lose information that
#: has nowhere else to go (see `transfer`'s recorded origin), and every
#: path that can warn about it does.
UNKNOWN_USER = "unknown"


class UriError(ValueError):
    """A URI that cannot be minted, or cannot be resolved on this machine."""


@dataclass(frozen=True)
class UriInfo:
    """A minted URI plus everything a caller needs to present it honestly."""

    uri: str
    ref: Ref
    kind: str                       # archive|session|file|collection|asset
    user: str
    archive: str
    #: The archive's immutable id, or None when it has none (a colleague's
    #: archive, or one on a medium we cannot write to). A URI without one
    #: still resolves -- by name, as every ref did before ids existed -- it
    #: just does not survive a rename.
    archive_id: Optional[str] = None
    #: Absolute path of the thing named, when it is on this machine.
    path: Optional[str] = None
    #: True when `path` exists right now. False is not an error: a URI for a
    #: session that has been moved to another machine is still the correct
    #: name for it.
    exists: bool = False
    #: Why this URI may not be globally unique, in plain language. Empty
    #: means the owner names a real authority -- which is as close to a
    #: guarantee as nebula can offer, since nothing verifies authorities.
    warnings: List[str] = field(default_factory=list)

    @property
    def unique(self) -> bool:
        """False when something about the owner makes collisions possible."""
        return not self.warnings

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uri": self.uri,
            "kind": self.kind,
            "user": self.user,
            "archive": self.archive,
            "archive_id": self.archive_id,
            "path": self.path,
            "exists": self.exists,
            "unique": self.unique,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------
# who and what an archive on disk claims to be
# ---------------------------------------------------------------------

def archive_owner(archive_root, *, fallback: Optional[str] = None) -> str:
    """The owner to put in a URI for things in `archive_root`.

    The archive's own ``archive.yaml`` wins over this machine's identity,
    because that is what travels with a copy: a fragment a colleague sent
    you must keep minting URIs under *their* name, or a citation written
    here would point at an archive that does not exist.
    """
    from nebula import identity
    from nebula.config import read_settings

    settings = read_settings(Path(archive_root), apply_env=False)
    return settings.user or identity.get_user() or fallback or ""


def archive_identifier(archive_root) -> "tuple[str, Optional[str]]":
    """`(label, id)` for an archive on disk, minting the id if it has none.

    The pair that goes into a URI's archive segment. Minting here is what
    makes ids appear without a migration command: asking for a URI is one of
    the moments an archive definitely needs one. It can still come back
    None -- a colleague's archive, a read-only medium -- and a URI without
    an id is a URI that resolves by name, exactly as it always did.
    """
    from nebula.config import ensure_archive_id

    root = Path(archive_root)
    return archive_name(root), ensure_archive_id(root)


def archive_name(archive_root, *, fallback: Optional[str] = None) -> str:
    """The name to put in a URI: what the archive declares for itself.

    Falls back to the directory name, which is what ``config
    .archive_identity`` does, so an archive created before archive.yaml
    carried a name still answers. Never the registry nickname -- that is
    machine-local and means nothing to the person reading the URI.
    """
    from nebula.config import read_settings

    root = Path(archive_root)
    settings = read_settings(root, apply_env=False)
    return settings.name or fallback or root.name


def _owner_warnings(user: str, *, declared: bool) -> List[str]:
    """Everything working against this URI being unique. Ordered most
    serious first, so a caller with room for one line prints the right one."""
    from nebula import identity

    if not user or user == UNKNOWN_USER:
        return ["this archive declares no owner and no identity is set on "
                "this machine, so the URI names nobody -- run 'nebula whoami "
                "--set <name>' and 'nebula config <archive> --user <name>'"]

    ident = identity.parse_identity(user)
    out: List[str] = []
    if ident.is_local:
        lead = (f"the owner {user!r} names no authority, so it reads as "
                f"{ident.qualified}" if not ident.explicit
                else f"the owner {user!r} is a local name")
        out.append(lead + " -- unique to this machine and nowhere else, so "
                   "two people can mint the same URI")
    elif ident.authority == identity.LOCAL_ID_AUTHORITY:
        out.append(f"the owner {user!r} is a local petname, which means "
                   "nothing on any other machine -- it must never leave here")
    if not declared and not ident.is_local:
        out.append("this archive does not declare an owner, so the URI uses "
                   "this machine's identity; a copy sent elsewhere would mint "
                   "a different one")
    return out


# ---------------------------------------------------------------------
# minting
# ---------------------------------------------------------------------

def _ref_for(session: Optional[str], file: Optional[str],
             collection: Optional[str], asset: Optional[str],
             user: Optional[str], archive: str,
             archive_id: Optional[str] = None) -> Ref:
    named = [x for x in (session or file, collection, asset) if x]
    if len(named) > 1:
        raise UriError(
            "a URI names one thing: a session (optionally with a file), a "
            "collection, or an asset -- not several at once")
    if file and not session:
        raise UriError(f"a URI for the file {file!r} needs its session id")
    return Ref(user=user, archive=archive, session=session, file=file,
               collection=collection, asset=asset, archive_id=archive_id)


def mint(user: Optional[str], archive: str, *, session: Optional[str] = None,
         file: Optional[str] = None, collection: Optional[str] = None,
         asset: Optional[str] = None, archive_id: Optional[str] = None,
         allow_unknown: bool = False) -> str:
    """Format a URI from parts already in hand, touching no disk.

    For callers that have read an archive's identity once and are minting
    many URIs from it -- the transfer plan is the motivating case. Everyone
    else wants :func:`describe` or :func:`uri_for`, which work out the parts
    for themselves.

    `allow_unknown=True` substitutes :data:`UNKNOWN_USER` for a missing
    owner. That is a URI nobody can resolve, so it belongs only where the
    alternative is recording nothing at all: an adopted session's `origin`
    has to say *something* about where the data came from, and "an archive
    called X, owner not stated" is more than silence.
    """
    if not user:
        if not allow_unknown:
            raise UriError(
                f"cannot write a nebula URI for archive {archive!r}: no owner")
        user = UNKNOWN_USER
    return format_uri(_ref_for(session, file, collection, asset, user, archive,
                               archive_id))


def target_path(archive_root, ref: Ref) -> Optional[Path]:
    """Where `ref` lands inside `archive_root`, or None when the shape of
    the ref does not name a path (an archive ref names the root itself)."""
    root = Path(archive_root)
    if ref.collection:
        from nebula import collection as collection_mod

        return collection_mod.path_for(root, ref.collection)
    if ref.asset:
        from nebula import assets as assets_mod

        live = assets_mod.live_file(root, ref.asset)
        return live if live else assets_mod.asset_dir(root, ref.asset)
    if ref.session:
        from nebula.session import _find_session_dir

        try:
            session_dir = _find_session_dir(root, ref.session)
        except (FileNotFoundError, KeyError, ValueError):
            return None
        return session_dir / ref.file if ref.file else session_dir
    return root


def describe(archive_root, *, session: Optional[str] = None,
             file: Optional[str] = None, collection: Optional[str] = None,
             asset: Optional[str] = None,
             user: Optional[str] = None,
             archive: Optional[str] = None,
             archive_id: Optional[str] = None) -> UriInfo:
    """Mint the URI for one thing in `archive_root`, with its caveats.

    `user`/`archive` override what the archive says about itself; they exist
    for callers that already know (the transfer plan, which has read the
    source archive's identity once) and should otherwise be left alone.

    Raises :class:`UriError` when no owner can be determined, because a URI
    without one is not a URI -- it is a compact ref wearing a scheme. Use
    :func:`uri_for` with ``allow_unknown=True`` where the caller genuinely
    has nowhere else to put the information.
    """
    root = Path(archive_root)
    from nebula.config import ensure_archive_id, read_settings

    settings = read_settings(root, apply_env=False)
    declared = bool(settings.user)
    owner = user or archive_owner(root)
    name = archive or archive_name(root)
    if not owner:
        raise UriError(
            f"cannot write a nebula URI for {root}: no owner. The archive "
            f"declares none and this machine has no identity set -- "
            f"'nebula whoami --set <name>' fixes the second.")

    ref = _ref_for(session, file, collection, asset, owner, name,
                   archive_id if archive_id is not None else ensure_archive_id(root))
    path = target_path(root, ref)
    return UriInfo(
        uri=format_uri(ref),
        ref=ref,
        kind=ref.kind,
        user=owner,
        archive=name,
        archive_id=ref.archive_id,
        path=str(path) if path else None,
        exists=bool(path and path.exists()),
        warnings=_owner_warnings(owner, declared=declared),
    )


def uri_for(archive_root, *, session: Optional[str] = None,
            file: Optional[str] = None, collection: Optional[str] = None,
            asset: Optional[str] = None, user: Optional[str] = None,
            archive: Optional[str] = None, archive_id: Optional[str] = None,
            allow_unknown: bool = False) -> str:
    """Just the URI string. The common case, when the caller has already
    decided how (or whether) to surface the caveats.

    `allow_unknown=True` substitutes :data:`UNKNOWN_USER` for a missing
    owner instead of raising. Reserved for recording provenance that would
    otherwise be lost outright -- never for something a person will copy.
    """
    try:
        return describe(archive_root, session=session, file=file,
                        collection=collection, asset=asset, user=user,
                        archive=archive, archive_id=archive_id).uri
    except UriError:
        if not allow_unknown:
            raise
        return mint(None, archive or archive_name(archive_root),
                    session=session, file=file, collection=collection,
                    asset=asset, archive_id=archive_id, allow_unknown=True)


# ---------------------------------------------------------------------
# resolving
# ---------------------------------------------------------------------

def is_uri(text: str) -> bool:
    return bool(text) and text.strip().lower().startswith(URI_SCHEME)


def resolve(text: str, *, registry=None) -> "tuple[Path, Ref]":
    """Find what a ``nebula://`` URI points at on this machine.

    Returns the archive root and the parsed ref; the ref still carries the
    session/file/collection/asset, so the caller decides what to do with
    it (open the session, print the path, walk the collection).

    Raises :class:`UriError` when the archive is not registered here or its
    location is not mounted -- deliberately distinguishing the two, because
    "plug the drive in" and "ask them to send it" are different jobs.
    """
    from nebula.registry import get_registry

    if not is_uri(text):
        raise UriError(f"not a nebula URI: {text!r}")
    try:
        ref = parse_uri(text.strip())
    except ValueError as e:
        raise UriError(str(e)) from e

    registry = registry or get_registry()
    cfg = registry.find(ref.archive, ref.user, archive_id=ref.archive_id)
    if cfg is None:
        # A petname resolves only here, and only through contacts -- try it
        # before giving up, since typing a colleague's short name is the
        # whole point of having one.
        resolved_user = _resolve_petname(ref.user)
        if resolved_user and resolved_user != ref.user:
            cfg = registry.find(ref.archive, resolved_user,
                                archive_id=ref.archive_id)
    if cfg is None:
        whose = f" owned by {ref.user!r}" if ref.user else ""
        which = (f"{ref.archive!r} (id {ref.archive_id})" if ref.archive_id
                 else repr(ref.archive))
        raise UriError(
            f"no archive {which}{whose} is registered on this machine. "
            f"'nebula archives' lists what is; 'nebula register' or 'nebula "
            f"receive' adds one.")
    root = Path(cfg.root)
    if not root.is_dir():
        raise UriError(
            f"{ref.archive!r} (owned by {ref.user or 'me'}) is registered but its "
            f"location {root} is not there -- an unplugged drive or an "
            f"unmounted share. 'nebula archives' shows every known location.")
    return root, ref


def _resolve_petname(user: Optional[str]) -> Optional[str]:
    """A petname (`grant@localid`) turned into the identity it currently
    stands for, or None. Failure is not an error here: `resolve` only calls
    this after an ordinary lookup already missed, so "there is no contacts
    file" and "that petname is unknown" both just mean no second chance."""
    if not user:
        return None
    try:
        from nebula import contacts

        if not contacts.is_alias(user):
            return None
        return contacts.get_contacts().resolve(user)
    except Exception:       # noqa: BLE001 -- no contacts file, or an odd one
        return None


def resolve_any(text: str, *, registry=None) -> "tuple[Path, Ref]":
    """Like :func:`resolve`, but also accepts a compact ref against a
    registered archive (``postdoc|S-26-0152/diode.graf``). Used by the CLI,
    where both spellings are legal input everywhere a ref is."""
    if is_uri(text):
        return resolve(text, registry=registry)

    from nebula.registry import get_registry

    ref = parse_ref(text)
    if not ref.archive:
        raise UriError(
            f"{text!r} names no archive, so there is nothing to resolve it "
            f"against. Write it as an archive|ref, or as a full "
            f"{URI_SCHEME}<user>/<archive>/... URI.")
    registry = registry or get_registry()
    cfg = registry.find(ref.archive, ref.user, archive_id=ref.archive_id)
    if cfg is None:
        raise UriError(f"archive {ref.archive!r} is not registered here")
    return Path(cfg.root), ref


# ---------------------------------------------------------------------
# presentation
# ---------------------------------------------------------------------

def label_for(ref: Ref) -> str:
    """A short human label for what a ref points at, for menus and toasts."""
    if ref.collection:
        return f"{COLLECTIONS_SEGMENT}/{ref.collection}"
    if ref.asset:
        return f"{ASSETS_SEGMENT}/{ref.asset}"
    if ref.file and ref.session:
        return f"{ref.session}/{ref.file}"
    return ref.session or ref.archive or "?"
