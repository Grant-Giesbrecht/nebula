"""
Which build of nebula is this?

The declared version (0.1.0) never changes between dev builds, so on its
own it can't tell two machines apart. The build stamp adds where in git
history the code came from:

    0.1.0-dev.77+970093a          77 commits on HEAD, at 970093a
    0.1.0-dev.77+970093a.dirty    ...plus uncommitted changes

The commit count orders builds from the same branch (dev.77 is older than
dev.80); the hash says exactly which commit. The Navigator's Rust shell
(src-tauri/build.rs) produces the same format, so the app and its bridge
can be compared at a glance.

Where the stamp comes from, in order:

1. A frozen bridge (PyInstaller) has no git checkout to ask, so
   build-sidecar.sh writes the stamp into a module, ``nebula_build_stamp``,
   that is bundled alongside nebula. It lives in the build directory, never
   in src/, so a stamp from last week's sidecar build can't masquerade as
   the version of the checkout you're running today.
2. Running from a git checkout (including ``pip install -e``): ask git,
   live.
3. Otherwise (an installed copy with no .git around it): just the declared
   version.
"""

from __future__ import annotations

import datetime
import functools
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

#: The build artefact that changes on every sidecar build and is tracked
#: in git. Counting it as a local change would make every app build that
#: follows a sidecar build "dirty", which is noise, not information.
#: Kept in step with the same list in navigator-tauri/src-tauri/build.rs.
_DIRTY_EXCLUDES = (":(exclude)navigator-tauri/src-tauri/binaries",)


def _base_version() -> str:
    from nebula import __version__

    return __version__


def _git(repo: Path, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def format_version(base: str, count: Optional[int], sha: Optional[str],
                   dirty: bool) -> str:
    """The display string: `base` alone when there is no git information."""
    if count is None or not sha:
        return base
    return f"{base}-dev.{count}+{sha}" + (".dirty" if dirty else "")


def stamp_from_git(repo: Path) -> Optional[Dict[str, Any]]:
    """Describe the checkout at `repo`, or None if it isn't one (or git is
    unavailable). Untracked files don't count as dirty: scratch files lying
    around a checkout are not changes to the code."""
    top = _git(repo, "rev-parse", "--show-toplevel")
    if not top:
        return None
    count = _git(repo, "rev-list", "--count", "HEAD")
    sha = _git(repo, "rev-parse", "--short=7", "HEAD")
    if not count or not sha:
        return None
    status = _git(Path(top), "status", "--porcelain", "--untracked-files=no",
                  "--", ".", *_DIRTY_EXCLUDES)
    return {"count": int(count), "sha": sha, "dirty": bool(status)}


def _frozen_stamp() -> Optional[Dict[str, Any]]:
    try:
        import nebula_build_stamp as s  # written by build-sidecar.sh
    except ImportError:
        return None
    return {"count": s.COUNT, "sha": s.SHA, "dirty": s.DIRTY, "built": s.BUILT}


@functools.lru_cache(maxsize=1)
def build_info() -> Dict[str, Any]:
    """This process's build, as a JSON-able dict:

    version  display string, e.g. "0.1.0-dev.77+970093a"
    base     the declared version
    count    commits on HEAD (None without git information)
    sha      short commit hash (None without git information)
    dirty    built from a tree with uncommitted changes
    built    when it was built, epoch seconds (None when read live from git:
             the code is whatever is on disk right now)
    source   "frozen", "git" or "package" -- where this came from
    """
    base = _base_version()
    stamp, source = None, "package"
    if getattr(sys, "frozen", False):
        stamp, source = _frozen_stamp(), "frozen"
    if stamp is None:
        stamp = stamp_from_git(Path(__file__).resolve().parent)
        source = "git" if stamp else "package"
    stamp = stamp or {}
    count, sha = stamp.get("count"), stamp.get("sha")
    dirty = bool(stamp.get("dirty"))
    return {
        "version": format_version(base, count, sha, dirty),
        "base": base, "count": count, "sha": sha, "dirty": dirty,
        "built": stamp.get("built"), "source": source,
    }


def write_stamp(path: Path, repo: Path) -> Dict[str, Any]:
    """Write the ``nebula_build_stamp`` module a frozen build reads (step 1
    above). Used by build-sidecar.sh / .ps1: ``python -m nebula.buildinfo
    --write <file>``."""
    stamp = stamp_from_git(repo) or {"count": None, "sha": None, "dirty": False}
    built = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Generated by nebula.buildinfo at sidecar build time. Do not edit.\n"
        f"COUNT = {stamp['count']!r}\n"
        f"SHA = {stamp['sha']!r}\n"
        f"DIRTY = {stamp['dirty']!r}\n"
        f"BUILT = {built!r}\n"
    )
    return {**stamp, "built": built}


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m nebula.buildinfo")
    parser.add_argument("--write", metavar="FILE",
                        help="write the frozen-build stamp module to FILE")
    args = parser.parse_args(argv)
    if args.write:
        s = write_stamp(Path(args.write), Path(__file__).resolve().parent)
        print(format_version(_base_version(), s["count"], s["sha"], s["dirty"]))
    else:
        print(build_info()["version"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
