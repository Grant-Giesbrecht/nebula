"""
Registry of independent nebula "archives" (e.g. postdoc vs. audio-startup),
so cross-archive refs like "postdoc|S-0152/diode.graf" can be resolved to an
actual filesystem path.

The registry file is intentionally NOT versioned per-archive -- it's a small
piece of machine-local config, similar in spirit to ~/.gitconfig. Each
archive itself has no knowledge of being "in" a registry; it's just a
directory tree with its own S-XXXX sessions and its own index.db.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import yaml

DEFAULT_REGISTRY_PATH = Path(os.path.expanduser("~/.nebula/archives.yaml"))

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
class ArchiveConfig:
    name: str
    root: Path
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


class Registry:
    """Loads and queries the archive registry file.

    Missing registry file is not an error -- a single-archive setup that
    never references another archive doesn't need one. It just means
    cross-archive refs can't be resolved until one is created.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else DEFAULT_REGISTRY_PATH
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
            if "root" not in cfg:
                raise ValueError(
                    f"archive {name!r} in {self.path} is missing a 'root' key"
                )
            self._archives[name] = ArchiveConfig(
                name=name,
                root=Path(os.path.expanduser(cfg["root"])),
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

    def register(self, name: str, root: Path, git_org: Optional[str] = None,
                 user: Optional[str] = None, kind: str = "standard",
                 declared_name: str = "") -> None:
        """Add or update an archive entry and persist it to disk."""
        self._load()
        self._archives[name] = ArchiveConfig(
            name=name, root=Path(root), git_org=git_org, user=user, kind=kind,
            declared_name=declared_name or name)
        self._save()

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
                "root": str(cfg.root),
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
    ~/.nebula/archives.yaml (or $NEBULA_REGISTRY if set)."""
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
