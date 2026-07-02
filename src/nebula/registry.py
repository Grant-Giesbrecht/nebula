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


@dataclass(frozen=True)
class ArchiveConfig:
    name: str
    root: Path
    git_org: Optional[str] = None

    @property
    def index_path(self) -> Path:
        return self.root / "index.db"


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

    def register(self, name: str, root: Path, git_org: Optional[str] = None) -> None:
        """Add or update an archive entry and persist it to disk."""
        self._load()
        self._archives[name] = ArchiveConfig(name=name, root=Path(root), git_org=git_org)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            name: {"root": str(cfg.root), **({"git_org": cfg.git_org} if cfg.git_org else {})}
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
