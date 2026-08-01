"""
Who "you" are, for the user segment of a nebula URI.

Archive names are not globally unique: two colleagues can each keep a
"measurements" archive, so a cross-archive ref needs an owner to be
unambiguous. That owner is a plain string -- today whatever you choose
(an email, a handle, a domain); later, if there is ever a server handing
out account names, the same field holds those without a format change.

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
from pathlib import Path
from typing import Optional

import yaml

DEFAULT_IDENTITY_PATH = Path(os.path.expanduser("~/.nebula/identity.yaml"))

#: Overrides the file, for scripts and tests.
USER_ENV = "NEBULA_USER"

#: Users appear in URI path segments, so no slashes; no whitespace either,
#: since a ref must stay a single copy-pasteable token.
_INVALID = re.compile(r"[\s/|]")


class IdentityError(ValueError):
    """A user name that cannot appear in a URI."""


def clean_user(name: str) -> str:
    value = (name or "").strip()
    if not value:
        raise IdentityError("empty user name")
    if _INVALID.search(value):
        raise IdentityError(
            f"user names cannot contain spaces, '/' or '|': {name!r}")
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
