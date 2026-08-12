"""
Who "you" are, for the user segment of a nebula URI.

Archive names are not globally unique: two colleagues can each keep a
"measurements" archive, so a cross-archive ref needs an owner to be
unambiguous.

An identity is read as **who**, issued by **which authority**, joined by
``@``::

    grant@local                       unverified, means nothing but itself
    grant@ncsu.edu                    an institution vouches
    Grant-Giesbrecht@github.com       a platform vouches
    0000-0003-2885-4801@orcid.org     a registry vouches

One rule parses all of them: *split on the last ``@``*. ORCIDs, handles and
domains contain none, and an email contains exactly one -- so an email
needs no special case, because an email already **is** a domain-scoped
identity whose authority is the domain that issued it.

Nothing here verifies anything. What an authority buys today is (a) a
namespace, so two people cannot collide by accident, and (b) an *offline*
check that catches typos -- an ORCID carries a MOD 11-2 check digit, a
GitHub handle has a known shape. See ``docs/identity-trust-roadmap.md`` for
why verification is deliberately not attempted yet.

**A missing authority is not an error.** A bare ``grant`` is read as
``grant@local`` for display, but the string on disk is left exactly as
written: rewriting it would change every URI already pointing at that
archive. ``local`` is reserved by RFC 6762, so it cannot collide with a
real authority.

Machine-local config, like the registry, and for the same reason: it says
who is sitting at this computer, not anything about an archive's contents::

    ~/.nebula/identity.yaml
    user: grant@ncsu.edu

An archive can also record its owner (registry `user:`), which is how a ref
to *someone else's* archive resolves to a path on this machine.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

DEFAULT_IDENTITY_PATH = Path(os.path.expanduser("~/.nebula/identity.yaml"))

#: Overrides the file, for scripts and tests.
USER_ENV = "NEBULA_USER"

#: Users appear in URI path segments, so no slashes; no whitespace either,
#: since a ref must stay a single copy-pasteable token.
_INVALID = re.compile(r"[\s/|]")

#: Stands in when no authority was given. Reserved by RFC 6762, so adopting
#: it cannot shadow a real one.
LOCAL_AUTHORITY = "local"

#: Reserved for the self-certifying key identities in the roadmap's option
#: 3. Nothing generates these yet; named here so the namespace is not
#: squatted by someone typing it in the meantime.
KEY_AUTHORITY = "key"

#: Reserved for **local petnames**: `grant@localid` is a shorthand this
#: machine resolves through ~/.nebula/contacts.yaml to whatever identity
#: that person currently uses. See :mod:`nebula.contacts`.
#:
#: Distinct from LOCAL_AUTHORITY, and the difference matters:
#:
#:   grant@local     -- an identity with no authority behind it. Usually
#:                      *your own*, before you have picked one. It stands
#:                      for nobody but itself.
#:   grant@localid   -- an alias *for someone else*, which only means
#:                      anything because this machine has a contacts entry
#:                      saying who it points at.
#:
#: A petname must never be written into a ref that leaves this machine --
#: it would name someone who does not exist anywhere else. `contacts.resolve`
#: turns one into a real identity; this constant is what makes "is this a
#: petname?" checkable rather than a convention people forget.
LOCAL_ID_AUTHORITY = "localid"

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,}$")

#: 1-39 chars, alphanumeric or single interior hyphens.
_GITHUB_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


class IdentityError(ValueError):
    """A user name that cannot appear in a URI."""


def orcid_check_digit(digits: str) -> str:
    """The ISO 7064 MOD 11-2 check character for an ORCID's first 15 digits.

    This is the whole reason to special-case ORCID: it makes a mistyped
    identifier detectable with no network and no registry lookup.
    """
    total = 0
    for ch in digits:
        total = (total + int(ch)) * 2
    remainder = total % 11
    result = (12 - remainder) % 11
    return "X" if result == 10 else str(result)


def _validate_orcid(value: str) -> Optional[str]:
    if not _ORCID_RE.match(value):
        return ("an ORCID looks like 0000-0003-2885-4801 "
                "(16 characters, four groups of four)")
    digits = value.replace("-", "")[:15]
    expected = orcid_check_digit(digits)
    if value[-1] != expected:
        return (f"ORCID check digit is wrong: {value} should end in "
                f"{expected!r}, so this is a typo somewhere in the number")
    return None


def _validate_github(value: str) -> Optional[str]:
    if not _GITHUB_RE.match(value):
        return ("a GitHub username is 1-39 letters, digits or single "
                "hyphens, and cannot start or end with a hyphen")
    return None


def _validate_localid(value: str) -> Optional[str]:
    if not value:
        return "a petname cannot be empty"
    return None


def _validate_key(value: str) -> Optional[str]:
    return ("the '@key' authority is reserved for signed identities, which "
            "nebula does not issue yet -- see docs/identity-trust-roadmap.md")


def _validate_local(value: str) -> Optional[str]:
    return None


def _validate_domain_local_part(value: str) -> Optional[str]:
    if not value:
        return "nothing before the '@'"
    return None


@dataclass(frozen=True)
class Authority:
    """Who issues a class of identities, and how to sanity-check one."""

    name: str
    label: str
    validate: Callable[[str], Optional[str]]
    #: How this authority could be verified if verification is ever built.
    #: Recorded so the roadmap's tier table lives next to the code.
    verifiable_by: Optional[str] = None


AUTHORITIES: Dict[str, Authority] = {
    LOCAL_AUTHORITY: Authority(
        LOCAL_AUTHORITY, "local name, not unique", _validate_local),
    "orcid.org": Authority(
        "orcid.org", "ORCID", _validate_orcid, verifiable_by="OAuth"),
    "github.com": Authority(
        "github.com", "GitHub", _validate_github, verifiable_by="OAuth"),
    KEY_AUTHORITY: Authority(
        KEY_AUTHORITY, "signed key (not yet issued)", _validate_key),
    LOCAL_ID_AUTHORITY: Authority(
        LOCAL_ID_AUTHORITY, "local petname", _validate_localid),
}


@dataclass(frozen=True)
class Identity:
    """A parsed ``value@authority``, plus how it was actually written."""

    value: str
    authority: str
    #: False when no '@' was present and `authority` was inferred as local.
    explicit: bool
    #: Exactly the string on disk. URIs and refs must use this, never the
    #: qualified form, or an identity gains a second spelling.
    raw: str

    @property
    def qualified(self) -> str:
        """``value@authority``, with the inferred authority made visible."""
        return f"{self.value}@{self.authority}"

    @property
    def known(self) -> bool:
        return self.authority in AUTHORITIES

    @property
    def is_local(self) -> bool:
        return self.authority == LOCAL_AUTHORITY

    @property
    def label(self) -> str:
        known = AUTHORITIES.get(self.authority)
        return known.label if known else self.authority

    @property
    def verified(self) -> bool:
        """Always False today, and deliberately present anyway.

        Every display path reads this rather than assuming, so turning
        verification on later does not mean hunting for the places that
        quietly implied it.
        """
        return False

    @property
    def status(self) -> str:
        return "verified" if self.verified else "unverified"


def parse_identity(name: str) -> Identity:
    """Split an identity into value and authority. Never raises.

    Reading is permissive on purpose: archives predate this convention, and
    refusing to display an owner because it is oddly shaped would make old
    archives unopenable. Validation is a separate, stricter step applied
    only to *new* input.
    """
    raw = (name or "").strip()
    if "@" in raw:
        value, _, authority = raw.rpartition("@")
        return Identity(value=value, authority=authority.lower(),
                        explicit=True, raw=raw)
    return Identity(value=raw, authority=LOCAL_AUTHORITY,
                    explicit=False, raw=raw)


def validate_identity(name: str) -> List[str]:
    """Check a *new* identity. Returns warnings; raises on a certain typo.

    The asymmetry is the point, and follows the "break loudly and fixably"
    rule. A known authority with a malformed value is a mistake we can be
    sure about -- a bad ORCID check digit is arithmetic, not opinion -- so
    it is refused while the user is still looking at it. An authority we
    simply do not recognise (a lab hostname, a future hub) is only warned
    about, because refusing it would be nebula inventing a whitelist of who
    is allowed to exist.
    """
    ident = parse_identity(clean_user(name))
    warnings: List[str] = []

    if ident.authority == LOCAL_ID_AUTHORITY:
        raise IdentityError(
            f"{ident.raw!r}: '{LOCAL_ID_AUTHORITY}' is reserved for local "
            "petnames of *other* people (see nebula.contacts). It cannot be "
            "your own identity, and it must never appear in a ref that "
            "leaves this machine.")
    if not ident.value:
        raise IdentityError(
            f"{ident.raw!r}: nothing before the '@' -- an identity is "
            "who@where, as in grant@ncsu.edu")
    if ident.explicit and not ident.authority:
        raise IdentityError(
            f"{ident.raw!r}: nothing after the '@' -- name the authority "
            "that issued it, as in grant@ncsu.edu, or drop the '@' for a "
            "local-only name")

    # Checked before the authority table, because `local` is *in* that table
    # and would otherwise report a clean bill of health for the one identity
    # that is guaranteed not to be unique.
    if ident.is_local:
        lead = (f"{ident.raw!r} is a local name"
                if ident.explicit else
                f"{ident.raw!r} names no authority, so it reads as "
                f"{ident.qualified}")
        warnings.append(
            f"{lead} -- unique to this machine and nowhere else. Something "
            f"like {ident.value}@orcid.org, {ident.value}@github.com or "
            f"{ident.value}@your-institution.edu stays unambiguous once "
            "archives are shared.")
        return warnings

    known = AUTHORITIES.get(ident.authority)
    if known is not None:
        problem = known.validate(ident.value)
        if problem:
            raise IdentityError(f"{ident.raw!r}: {problem}")
        return warnings

    if not _DOMAIN_RE.match(ident.authority):
        warnings.append(
            f"{ident.authority!r} is not a domain name, so nothing can ever "
            "vouch for this identity. A domain you control, or a registry "
            "like orcid.org, is safer.")
    else:
        problem = _validate_domain_local_part(ident.value)
        if problem:
            warnings.append(f"{ident.raw!r}: {problem}")
    return warnings


def describe_identity(name: str) -> Dict[str, Any]:
    """Display-ready identity, for `nebula whoami` and the Navigator badge.

    Single source for how an identity is *shown*, so the CLI and the GUI
    cannot drift into implying different amounts of trust.
    """
    ident = parse_identity(name)
    return {
        "user": ident.raw,
        "value": ident.value,
        "authority": ident.authority,
        "authority_label": ident.label,
        "qualified": ident.qualified,
        "explicit": ident.explicit,
        "known": ident.known,
        "local": ident.is_local,
        "verified": ident.verified,
        "status": ident.status,
    }


def describe_owner(owner: str, *, local_user: Optional[str] = None,
                   declared: bool = True) -> Dict[str, Any]:
    """How to present an archive's declared owner.

    Adds one thing `describe_identity` cannot know: whether this owner is
    *someone else's assertion that arrived with the data*. An archive
    carries its owner in `archive.yaml`, so a fragment a colleague sends
    you states who made it and nothing anywhere checks that. Presenting
    that string the same way as your own name would quietly upgrade a
    claim into a fact.

    `declared` False means the archive names no owner and the local
    identity is standing in, which is not a claim by anyone.
    """
    info = describe_identity(owner)
    mine = bool(owner) and owner == (local_user or "")
    info["declared"] = bool(declared and owner)
    info["claimed"] = bool(declared and owner and not mine)
    info["display"] = owner + (" (claimed)" if info["claimed"] else "")
    info["note"] = (
        f"{owner} is what this archive says about itself. Nebula does not "
        "check owners, so treat it as a claim by whoever sent it."
        if info["claimed"] else "")
    return info


def clean_user(name: str) -> str:
    value = (name or "").strip()
    if not value:
        raise IdentityError("empty user name")
    if _INVALID.search(value):
        hint = ""
        if "/" in value:
            # The commonest paste is an ORCID or GitHub profile URL. Say the
            # answer rather than only the rule.
            tail = value.rstrip("/").rsplit("/", 1)[-1]
            head = value.split("//")[-1].split("/")[0]
            if tail and head and tail != head:
                hint = f" -- did you mean {tail}@{head}?"
        raise IdentityError(
            f"user names cannot contain spaces, '/' or '|': {name!r}{hint}")
    return value


def identity_path() -> Path:
    override = os.environ.get("NEBULA_IDENTITY")
    return Path(override) if override else DEFAULT_IDENTITY_PATH


def get_user() -> Optional[str]:
    """The local user name, or None if this machine has not set one.

    None is not an error: everything except fully-qualified URIs works
    without an identity, so nebula stays usable before you have picked a
    name. Only URI *formatting* insists on one.
    """
    env = os.environ.get(USER_ENV)
    if env and env.strip():
        try:
            return clean_user(env)
        except IdentityError:
            return None
    try:
        raw = yaml.safe_load(identity_path().read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    user = raw.get("user") if isinstance(raw, dict) else None
    try:
        return clean_user(user) if user else None
    except IdentityError:
        return None


def set_user(name: str) -> Path:
    """Write the local identity, refusing a value we can prove is a typo.

    Callers wanting the advisory warnings too should call
    `validate_identity` first; this re-runs it only to enforce the errors,
    so no caller can skip the check by forgetting.
    """
    validate_identity(name)
    path = identity_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump({"user": clean_user(name)}, f, sort_keys=True)
    return path


def require_user() -> str:
    user = get_user()
    if not user:
        raise IdentityError(
            "no nebula user name set on this machine, so a full URI cannot be "
            "written. Set one with 'nebula whoami --set <name>' (an email or "
            f"handle is fine); it is stored in {identity_path()}.")
    return user
