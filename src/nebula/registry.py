"""
Registry of independent nebula "archives" (e.g. postdoc vs. audio-startup),
so cross-archive refs like "postdoc|S-0152/diode.graf" can be resolved to an
actual filesystem path.

The registry file is intentionally NOT versioned per-archive -- it's a small
piece of machine-local config, similar in spirit to ~/.gitconfig. Each
archive itself has no knowledge of being "in" a registry; it's just a
directory tree with its own S-XXXX sessions and its own index.db.

**One archive, several locations.** An archive can be in more than one
place -- a working copy on a laptop, a backup on a NAS, a copy on a lab
server -- and the registry lists them in priority order. Resolution walks
them and takes the first that is actually there, so unplugging an external
drive falls back to the NAS rather than failing. See :class:`Location`.

The file is ``~/.nebula/registry.yaml``. It used to be ``archives.yaml``,
renamed because an archive's *own* settings file is ``archive.yaml`` and
the two were one character apart. The old name is still read if the new one
is absent, so nothing breaks on upgrade.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Optional

import yaml

DEFAULT_REGISTRY_PATH = Path(os.path.expanduser("~/.nebula/registry.yaml"))

#: What the registry used to be called. Read when the new name is absent,
#: so an existing install keeps working; `Registry.migrate` renames it.
LEGACY_REGISTRY_PATH = Path(os.path.expanduser("~/.nebula/archives.yaml"))

#: Location kinds. Only `path` is reachable today -- a remote location is
#: recorded and reported, but nothing here speaks a network protocol yet
#: (see docs/sync-roadmap.md). Recording one is still useful: it says where
#: the archive also lives, which is exactly what a person needs to go and
#: get it, and it means the registry format does not have to change when a
#: client does exist.
LOCATION_KINDS = ("path", "url")

#: Where archives live by convention. Scanned to discover archives so that
#: dropping one in the right place is enough -- but only ever to *populate*
#: the registry, which stays the single answer to "where does X live". Two
#: mechanisms that both resolve names would eventually disagree.
DEFAULT_HOME = Path(os.path.expanduser("~/nebula"))
HOME_ENV = "NEBULA_HOME"

#: Foreign archives (fragments others sent you) live under this, one folder
#: per user, mirroring the URI: nebula://jane/lab -> fragments/jane/lab.
FRAGMENTS_DIR = "fragments"


def nebula_home() -> Path:
    override = os.environ.get(HOME_ENV)
    return Path(os.path.expanduser(override)) if override else DEFAULT_HOME


def fragments_root() -> Path:
    return nebula_home() / FRAGMENTS_DIR


def fragment_dir(user: str, archive_name: str) -> Path:
    """Where a fragment from <user>'s <archive_name> belongs.

    Isomorphic to the URI on purpose: nebula://jane/lab-archive/S-26-0100
    lives at $NEBULA_HOME/fragments/jane/lab-archive. That makes resolution
    a path join rather than a lookup, and means two deliveries of the same
    source archive -- one via John, one via Bill -- land in one place
    instead of being filed under whoever forwarded them.
    """
    return fragments_root() / (user or "unknown") / archive_name


@dataclass(frozen=True)
class Location:
    """One place an archive lives.

    `path` is a local filesystem location; `url` is remote. Order in
    ArchiveConfig.locations *is* the priority -- first listed is preferred
    -- because an explicit list a human can reorder beats a numeric field
    nobody remembers the direction of.
    """

    kind: str = "path"
    value: str = ""
    #: Free-text, for the listing: "laptop", "lab NAS", "office server".
    label: str = ""

    @property
    def path(self) -> Optional[Path]:
        return Path(os.path.expanduser(self.value)) if self.kind == "path" else None

    @property
    def available(self) -> bool:
        """Whether this location can be used *right now*.

        A remote location is never available: nebula has no client yet. It
        reports False rather than raising, so a remote entry degrades to
        "recorded but not reachable" instead of breaking resolution.
        """
        p = self.path
        return bool(p and p.is_dir())

    def to_dict(self) -> Dict[str, str]:
        out = {self.kind: self.value}
        if self.label:
            out["label"] = self.label
        return out

    @classmethod
    def from_dict(cls, raw) -> "Location":
        if isinstance(raw, str):            # bare string is a path
            return cls(kind="path", value=raw)
        for kind in LOCATION_KINDS:
            if kind in raw:
                return cls(kind=kind, value=str(raw[kind]),
                           label=str(raw.get("label") or ""))
        raise ValueError(
            f"location {raw!r} names neither {' nor '.join(LOCATION_KINDS)}")


@dataclass(frozen=True)
class ArchiveConfig:
    name: str
    #: Every known location, most preferred first. Never empty.
    locations: "tuple[Location, ...]" = ()
    git_org: Optional[str] = None
    #: Who owns this archive, for nebula:// URIs. None means "me" (see
    #: nebula.identity) -- a colleague's archive mounted locally records
    #: their name here so refs into it resolve to this path.
    user: Optional[str] = None

    #: What the archive says it is (standard/intake/fragment), cached from
    #: its archive.yaml at registration time. The file is authoritative;
    #: this is here so listing archives doesn't have to open all of them.
    kind: str = "standard"
    #: The name the archive declares for itself, which is what appears in a
    #: nebula:// URI. Usually the same as the registry key -- but when two
    #: archives claim one name, the key is disambiguated and this is not,
    #: because refs written by their authors still say the plain name.
    declared_name: str = ""

    @property
    def root(self) -> Path:
        """Where the archive is *right now*: the first available location,
        else the first location listed.

        A property rather than a stored field so that every existing caller
        keeps working unchanged while gaining the fallback. Falling back to
        the first location when none is available means callers still get a
        path to name in an error message ("not mounted") instead of None.
        """
        for loc in self.locations:
            if loc.available:
                return loc.path
        first = self.locations[0] if self.locations else None
        return (first.path if first and first.path
                else Path(first.value if first else ""))

    @property
    def available(self) -> bool:
        return any(loc.available for loc in self.locations)

    @property
    def remote_only(self) -> bool:
        """Known, but only in places nebula cannot reach yet."""
        return bool(self.locations) and not any(
            loc.kind == "path" for loc in self.locations)

    @property
    def index_path(self) -> Path:
        return self.root / "index.db"

    @property
    def uri_name(self) -> str:
        return self.declared_name or self.name

    @property
    def key(self) -> "tuple[str, str]":
        """Archives are identified by owner *and* name: two colleagues can
        each have a 'postdoc', and both may be registered here."""
        return (self.user or "", self.uri_name)


def _read_locations(name: str, cfg: Dict, path: Path) -> "tuple[Location, ...]":
    """One archive's locations, accepting both the old and new shapes.

    Old: a single `root:` string. New: a `locations:` list in priority
    order. Both are read, so a registry written before this existed loads
    unchanged and gains a one-element list.
    """
    if "locations" in cfg:
        got = [Location.from_dict(item) for item in (cfg["locations"] or [])]
        if got:
            return tuple(got)
    if "root" in cfg:
        return (Location(kind="path", value=str(cfg["root"])),)
    raise ValueError(
        f"archive {name!r} in {path} has neither 'root' nor 'locations'")


def _write_locations(cfg: "ArchiveConfig") -> Dict:
    """Serialise locations, staying terse in the common case.

    One local path is written back as `root:` -- the overwhelming majority
    of entries, and a hand-edited file should not grow a list-of-dicts for
    something that was a single line.
    """
    locs = list(cfg.locations)
    if len(locs) == 1 and locs[0].kind == "path" and not locs[0].label:
        return {"root": locs[0].value}
    return {"locations": [loc.to_dict() for loc in locs]}


class Registry:
    """Loads and queries the archive registry file.

    Missing registry file is not an error -- a single-archive setup that
    never references another archive doesn't need one. It just means
    cross-archive refs can't be resolved until one is created.
    """

    def __init__(self, path: Optional[Path] = None):
        if path:
            self.path = Path(path)
        else:
            # Prefer the new name; fall back to the old one only if it is
            # the only one there, so an upgrade is invisible.
            self.path = DEFAULT_REGISTRY_PATH
            if not DEFAULT_REGISTRY_PATH.exists() and LEGACY_REGISTRY_PATH.exists():
                self.path = LEGACY_REGISTRY_PATH
        self._archives: Dict[str, ArchiveConfig] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        with open(self.path, "r") as f:
            raw = yaml.safe_load(f) or {}
        for name, cfg in raw.items():
            self._archives[name] = ArchiveConfig(
                name=name,
                locations=_read_locations(name, cfg, self.path),
                git_org=cfg.get("git_org"),
                user=cfg.get("user"),
                kind=cfg.get("kind") or "standard",
                declared_name=cfg.get("name") or name,
            )

    def get(self, name: str) -> ArchiveConfig:
        self._load()
        if name not in self._archives:
            raise KeyError(
                f"unknown archive {name!r}. Known archives: "
                f"{sorted(self._archives) or '(none registered)'}. "
                f"Check {self.path}"
            )
        return self._archives[name]

    def try_get(self, name: str) -> Optional[ArchiveConfig]:
        """Like get(), but returns None instead of raising -- useful for
        gracefully reporting an unresolved external reference (e.g. the
        other archive's NAS share isn't mounted on this machine)."""
        self._load()
        return self._archives.get(name)

    def all(self) -> Dict[str, ArchiveConfig]:
        self._load()
        return dict(self._archives)

    def register(self, name: str, root=None, git_org: Optional[str] = None,
                 user: Optional[str] = None, kind: str = "standard",
                 declared_name: str = "",
                 locations: "Optional[list[Location]]" = None) -> None:
        """Add or update an archive entry and persist it to disk.

        `root` is kept for every existing caller; `locations` is the way to
        register more than one place at once.
        """
        self._load()
        if locations is None:
            if root is None:
                raise ValueError("register needs either root or locations")
            locations = [Location(kind="path", value=str(root))]
        self._archives[name] = ArchiveConfig(
            name=name, locations=tuple(locations), git_org=git_org, user=user,
            kind=kind, declared_name=declared_name or name)
        self._save()

    def add_location(self, name: str, location: Location, *,
                     first: bool = False) -> ArchiveConfig:
        """Record another place this archive lives.

        Appended by default: a newly-added location is usually a backup or
        a server copy, and silently *preferring* it over the working copy
        someone has been using would be a surprise. `first=True` says
        otherwise explicitly.
        """
        self._load()
        cfg = self.get(name)
        existing = [loc for loc in cfg.locations
                    if not (loc.kind == location.kind
                            and loc.value == location.value)]
        ordered = ([location] + existing) if first else (existing + [location])
        self._archives[name] = replace(cfg, locations=tuple(ordered))
        self._save()
        return self._archives[name]

    def remove_location(self, name: str, value: str) -> ArchiveConfig:
        """Forget one location. The last one cannot be removed -- an entry
        with nowhere to look is not a registration, it is a puzzle."""
        self._load()
        cfg = self.get(name)
        kept = [loc for loc in cfg.locations if loc.value != value]
        if not kept:
            raise ValueError(
                f"{value!r} is the only location for {name!r}; unregister the "
                "archive instead of leaving it with nowhere to look")
        if len(kept) == len(cfg.locations):
            raise KeyError(f"{name!r} has no location {value!r}")
        self._archives[name] = replace(cfg, locations=tuple(kept))
        self._save()
        return self._archives[name]

    def migrate(self) -> Optional[Path]:
        """Move a legacy archives.yaml to registry.yaml. Returns the new
        path if anything moved."""
        if self.path != LEGACY_REGISTRY_PATH or not self.path.exists():
            return None
        if DEFAULT_REGISTRY_PATH.exists():
            return None
        self._load()
        DEFAULT_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.path, DEFAULT_REGISTRY_PATH)
        self.path = DEFAULT_REGISTRY_PATH
        return self.path

    def register_archive(self, root, *, git_org: Optional[str] = None,
                         key: Optional[str] = None) -> ArchiveConfig:
        """Register an archive under the name *it* declares.

        The name and owner are part of a nebula URI, so they have to travel
        with the archive rather than being chosen by whoever received it --
        otherwise a fragment stops resolving under the name its author
        cited. Where that name is already taken by a *different* archive,
        the entry is keyed <user>-<name> so both can coexist.
        """
        from nebula.config import archive_identity

        root = Path(root)
        ident = archive_identity(root)
        self._load()
        wanted = key or ident["name"]
        existing = self._archives.get(wanted)
        if existing is not None and Path(existing.root) != root:
            owner = ident["user"] or "unknown"
            wanted = f"{owner}-{ident['name']}"
        self.register(wanted, root, git_org=git_org, user=ident["user"] or None,
                      kind=ident["kind"], declared_name=ident["name"])
        return self._archives[wanted]

    def discover(self, home: Optional[Path] = None) -> "list[ArchiveConfig]":
        """Find archives under NEBULA_HOME and register what is new.

        Convention discovers; the registry still resolves. Nothing here
        overrides an existing entry -- an archive you registered by hand,
        from a NAS or an external drive, keeps the name and path you gave it.
        """
        from nebula.config import ARCHIVE_CONFIG_FILE

        home = Path(home) if home else nebula_home()
        found: "list[ArchiveConfig]" = []
        if not home.is_dir():
            return found
        self._load()
        known = {Path(cfg.root).resolve() for cfg in self._archives.values()}

        candidates = [p for p in sorted(home.iterdir()) if p.is_dir()]
        frags = home / FRAGMENTS_DIR
        if frags.is_dir():
            candidates.remove(frags) if frags in candidates else None
            for user_dir in sorted(frags.iterdir()):
                if user_dir.is_dir():
                    candidates.extend(p for p in sorted(user_dir.iterdir()) if p.is_dir())

        for path in candidates:
            if not (path / ARCHIVE_CONFIG_FILE).is_file():
                continue        # a directory is an archive when it says so
            if path.resolve() in known:
                continue
            try:
                found.append(self.register_archive(path))
            except Exception:   # noqa: BLE001 -- one bad archive must not stop the scan
                continue
        return found

    def find(self, name: str, user: Optional[str] = None) -> Optional[ArchiveConfig]:
        """Look up an archive by name and (optionally) owner.

        `user=None` means "whoever, as long as the name matches" -- the
        compact ref case. A named user must match the entry's `user`, or
        the local identity when the entry does not name one: an archive
        with no recorded owner is assumed to be mine.
        """
        self._load()
        from nebula.identity import get_user

        # Match the name an author would have written in a ref, which is the
        # name the archive declares -- not the registry key, which may have
        # been disambiguated when two archives claimed the same one.
        candidates = [cfg for cfg in self._archives.values()
                      if cfg.uri_name == name or cfg.name == name]
        if not candidates:
            return None
        if user is None:
            return candidates[0]
        for cfg in candidates:
            if cfg.user == user:
                return cfg
            if not cfg.user and get_user() == user:
                return cfg          # unowned entries are mine
        return None

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            name: {
                **_write_locations(cfg),
                **({"git_org": cfg.git_org} if cfg.git_org else {}),
                **({"user": cfg.user} if cfg.user else {}),
                **({"kind": cfg.kind} if cfg.kind and cfg.kind != "standard" else {}),
                **({"name": cfg.declared_name}
                   if cfg.declared_name and cfg.declared_name != name else {}),
            }
            for name, cfg in self._archives.items()
        }
        with open(self.path, "w") as f:
            yaml.safe_dump(raw, f, sort_keys=True)


_default_registry: Optional[Registry] = None


def get_registry() -> Registry:
    """Process-wide default registry, loaded lazily from
    ~/.nebula/registry.yaml (or $NEBULA_REGISTRY if set)."""
    global _default_registry
    if _default_registry is None:
        override = os.environ.get("NEBULA_REGISTRY")
        _default_registry = Registry(Path(override) if override else None)
    return _default_registry


def resolve_archive(
    identifier, registry: Optional[Registry] = None
) -> "tuple[Path, Optional[str]]":
    """Resolve an archive identifier for the Python API. The *type* of the
    argument decides intent, deliberately:

      - a Path         -> treated as a literal filesystem root, name=None
                           (or "local" by convention at the call site).
      - a plain str     -> treated as a registered archive name; raises
                           KeyError if it isn't registered.

    This is intentionally strict (no silent str-as-path fallback): a
    typo'd archive name should fail loudly, not quietly create a new
    session folder under a relative path in the current working
    directory. The CLI uses a more lenient resolver (see cli.py) since
    ad hoc/unregistered paths are a normal thing to poke at from a
    terminal.
    """
    registry = registry or get_registry()
    if isinstance(identifier, Path):
        return identifier, None
    if isinstance(identifier, str):
        cfg = registry.get(identifier)  # raises KeyError if unknown
        return cfg.root, identifier
    raise TypeError(
        f"archive identifier must be a str (registered name) or Path "
        f"(literal root), got {type(identifier).__name__}"
    )
