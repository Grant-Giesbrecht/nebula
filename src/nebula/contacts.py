"""
Local petnames, and the identity trails behind them.

People change identity. Someone starts with an institutional email because
they had nothing else, moves to a GitHub handle, gets an ORCID when they
first publish, and may one day get an account on a hub. Every one of those
is stamped into archives written at the time, and all of them are the same
person. This is the file that says so::

    ~/.nebula/contacts.yaml

    grant:
      display: Grant Giesbrecht
      ids:
        - grant@ncsu.edu
        - Grant-Giesbrecht@github.com
        - id: 0000-0003-2885-4801@orcid.org
          since: 2022-01

`ids` is **oldest first**, so the last entry is who they are now. That
ordering is the whole data structure: a set of equivalent ids would say
they are the same person but not which to use for a *new* reference, and
"use their current id" is the main thing this is for.

Three properties this deliberately has:

**It is local, and it is yours.** These are assertions *you* make about who
is who. They live on this machine, they are never written into an archive,
and they never travel in a fragment. That is a security property, not an
oversight: a succession record that propagated would let anyone claim
"their id superseded yours", and every machine that imported the claim
would start attributing their work to the claimant. When a hub exists it
can serve *authenticated* successions -- the person proved control of both
ids -- and those would be safe to accept from a stranger. Nothing here is.

**It never rewrites history.** An archive written in 2019 says
`grant@ncsu.edu`, and that stays true -- it is what was the case. Contacts
affect how an identity is *displayed* and which id a *new* ref is written
with. They never touch a stored ref. (Same rule as the rename log, which
resolves old names without rewriting them.)

**A petname is not an identity.** `grant@localid` is a handle for your own
use; it means nothing on anyone else's machine and must never be written
into a ref that leaves this one. `LOCAL_ID_AUTHORITY` exists so that is
checkable rather than a convention people forget.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from nebula import identity

DEFAULT_CONTACTS_PATH = Path(os.path.expanduser("~/.nebula/contacts.yaml"))

#: Overrides the file, for tests and scripts.
CONTACTS_ENV = "NEBULA_CONTACTS"


class ContactError(ValueError):
    """A contacts entry that cannot mean what it says."""


def contacts_path() -> Path:
    override = os.environ.get(CONTACTS_ENV)
    return Path(override) if override else DEFAULT_CONTACTS_PATH


@dataclass(frozen=True)
class Contact:
    """One person, and the identities they have used, oldest first."""

    petname: str
    ids: "tuple[str, ...]" = ()
    display: str = ""
    #: Parallel to `ids`: when each became current, where known. Purely
    #: informational -- order decides currency, not these dates, because a
    #: date is often unknown and order never is.
    since: "tuple[Optional[str], ...]" = ()
    note: str = ""

    @property
    def current(self) -> Optional[str]:
        """The identity to use when writing something new."""
        return self.ids[-1] if self.ids else None

    @property
    def former(self) -> "tuple[str, ...]":
        return self.ids[:-1] if self.ids else ()

    @property
    def label(self) -> str:
        return self.display or self.petname

    @property
    def alias(self) -> str:
        """How this contact is written as a ref-shaped token, locally."""
        return f"{self.petname}@{identity.LOCAL_ID_AUTHORITY}"

    def knows(self, user: str) -> bool:
        return user in self.ids

    def to_dict(self) -> Dict[str, Any]:
        ids: List[Any] = []
        for i, one in enumerate(self.ids):
            when = self.since[i] if i < len(self.since) else None
            ids.append({"id": one, "since": when} if when else one)
        out: Dict[str, Any] = {"ids": ids}
        if self.display:
            out["display"] = self.display
        if self.note:
            out["note"] = self.note
        return out

    @classmethod
    def from_dict(cls, petname: str, raw: Any) -> "Contact":
        if isinstance(raw, str):                 # petname: <single id>
            raw = {"ids": [raw]}
        if not isinstance(raw, dict):
            raise ContactError(f"contact {petname!r} is not a mapping")
        ids, since = [], []
        for item in raw.get("ids") or []:
            if isinstance(item, str):
                ids.append(item)
                since.append(None)
            elif isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
                since.append(str(item["since"]) if item.get("since") else None)
            else:
                raise ContactError(
                    f"contact {petname!r} has an id entry that names no id: {item!r}")
        return cls(petname=petname, ids=tuple(ids), since=tuple(since),
                   display=str(raw.get("display") or ""),
                   note=str(raw.get("note") or ""))


@dataclass
class Contacts:
    """Every contact known to this machine."""

    path: Path = field(default_factory=contacts_path)
    _by_petname: Dict[str, Contact] = field(default_factory=dict)
    _loaded: bool = False

    # -- loading ---------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            raw = yaml.safe_load(self.path.read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            raise ContactError(f"could not read {self.path}: {e}") from None
        if not isinstance(raw, dict):
            raise ContactError(f"{self.path} is not a mapping of petname -> contact")
        for petname, entry in raw.items():
            self._by_petname[str(petname)] = Contact.from_dict(str(petname), entry)
        self._check_unique()

    def _check_unique(self) -> None:
        """No identity may belong to two contacts.

        This is the one internal inconsistency worth refusing outright: if
        an id maps to two people, every answer this module gives about it
        is a coin flip, and a silent wrong answer here misattributes work.
        """
        seen: Dict[str, str] = {}
        for contact in self._by_petname.values():
            for one in contact.ids:
                if one in seen and seen[one] != contact.petname:
                    raise ContactError(
                        f"{one!r} is listed under both {seen[one]!r} and "
                        f"{contact.petname!r} in {self.path}; an identity "
                        "belongs to one person")
                seen[one] = contact.petname

    # -- queries ---------------------------------------------------------

    def all(self) -> Dict[str, Contact]:
        self._load()
        return dict(self._by_petname)

    def get(self, petname: str) -> Optional[Contact]:
        self._load()
        return self._by_petname.get(_strip_alias(petname))

    def find(self, user: str) -> Optional[Contact]:
        """The contact who has used this identity, if any."""
        self._load()
        for contact in self._by_petname.values():
            if contact.knows(user):
                return contact
        return None

    def resolve(self, user: str) -> str:
        """Turn a petname alias into a real identity; pass anything else
        through untouched.

        `grant@localid` -> that contact's current id. Unknown petnames
        raise, because writing an unresolved alias into a ref would put a
        name in someone else's archive that means nothing there.
        """
        if not is_alias(user):
            return user
        petname = _strip_alias(user)
        contact = self.get(petname)
        if contact is None:
            raise ContactError(
                f"no contact {petname!r} in {self.path}; "
                f"'{identity.LOCAL_ID_AUTHORITY}' names a local petname, so it "
                "has to be one you have recorded")
        if not contact.current:
            raise ContactError(f"contact {petname!r} lists no identities")
        return contact.current

    def display(self, user: str) -> str:
        """How to show an identity: the petname if known, else itself."""
        contact = self.find(user)
        return contact.label if contact else user

    def current_for(self, user: str) -> str:
        """The identity this person uses *now*.

        Given any id in a contact's trail, returns the newest. This is what
        makes "write new refs with their current id" work when what you
        have in hand is an old archive's owner string.
        """
        contact = self.find(user)
        return contact.current if (contact and contact.current) else user

    def same_person(self, a: str, b: str) -> bool:
        if a == b:
            return True
        got = self.find(a)
        return bool(got and got.knows(b))

    def describe(self, user: str) -> Dict[str, Any]:
        """Display-ready, for `nebula contacts` and the Navigator."""
        contact = self.find(user)
        out = dict(identity.describe_identity(user))
        out.update({
            "petname": contact.petname if contact else None,
            "label": contact.label if contact else user,
            "known_contact": contact is not None,
            "is_current": bool(contact and contact.current == user),
            "current": contact.current if contact else user,
            "former": list(contact.former) if contact else [],
        })
        return out

    # -- editing ---------------------------------------------------------

    def put(self, contact: Contact) -> None:
        self._load()
        self._by_petname[contact.petname] = contact
        self._check_unique()
        self._save()

    def add_identity(self, petname: str, user: str, *,
                     since: Optional[str] = None, display: str = "") -> Contact:
        """Record that `petname` now uses `user`.

        Appended, so it becomes the current identity -- which is what
        "they moved to an ORCID" means. Validated first: an id with a typo
        in it would quietly become the one every new ref uses.
        """
        # Checked before validate_identity so the message names the actual
        # mistake -- a petname pointing at a petname -- rather than the
        # generic "localid is reserved".
        if is_alias(user):
            raise ContactError(
                f"{user!r}: a contact's identity cannot itself be a local "
                "petname; record the real id it stands for")
        identity.validate_identity(user)
        self._load()
        got = self._by_petname.get(petname)
        if got is None:
            got = Contact(petname=petname, display=display)
        if user in got.ids:
            raise ContactError(f"{petname!r} already lists {user!r}")
        merged = Contact(
            petname=petname, ids=got.ids + (user,),
            since=got.since + (since,),
            display=display or got.display, note=got.note)
        self._by_petname[petname] = merged
        self._check_unique()
        self._save()
        return merged

    def forget(self, petname: str) -> None:
        self._load()
        self._by_petname.pop(petname, None)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = {name: c.to_dict() for name, c in sorted(self._by_petname.items())}
        with open(self.path, "w") as f:
            yaml.safe_dump(raw, f, sort_keys=True, default_flow_style=False)


def is_alias(user: str) -> bool:
    """Whether this names a local petname rather than a real identity."""
    return identity.parse_identity(user or "").authority == \
        identity.LOCAL_ID_AUTHORITY


def _strip_alias(text: str) -> str:
    return identity.parse_identity(text or "").value if is_alias(text) else text


_default: Optional[Contacts] = None


def get_contacts() -> Contacts:
    """Process-wide contacts, loaded lazily."""
    global _default
    if _default is None:
        _default = Contacts(path=contacts_path())
    return _default
