"""
Content-addressed store for the source code that produced artifacts.

Git records *which commit* a script ran at; it cannot record what ran when
the working tree was dirty -- and in practice a majority of real runs are
dirty. This store keeps the bytes themselves, so "what code made this
file?" is answerable from the archive alone, with no remote, no network,
and no dependence on history not being rewritten.

Two levels of content addressing, because two different things repeat:

    code/blobs/<aa>/<bb>/<sha256>          one copy per distinct file version
    code/manifests/<aa>/<bb>/<sha256>.json {entry, files: {path: blob}}

Deliberately not hidden: the archive is meant to be browsable by hand when
something needs troubleshooting.

A blob is one file's exact bytes. A manifest is the file list, hashed over
its own canonical serialization -- so a session whose code is unchanged
since last time reuses the *same manifest id* and writes nothing at all.
The sidecar then carries a single short string (produced_by.code).

The manifest deliberately contains **content only**: no timestamps, no
commit hashes. Anything varying per run would make every manifest unique
and silently defeat the dedupe. Per-run git state lives in the sidecar's
produced_by (repo/commit/dirty/repos), where it belongs.

Paths in a manifest are repo-relative and prefixed with the repo name
("nebula/src/nebula/session.py") so they neither leak the machine's
directory layout nor collide between repos, and so the same code captured
from two different checkouts dedupes to one manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import sysconfig
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CODE_DIR = "code"
BLOBS = "blobs"
MANIFESTS = "manifests"

#: Skip any single source file larger than this (bytes). Guards against a
#: module with a giant embedded table turning every run into a big write.
DEFAULT_MAX_FILE_BYTES = 1 << 20


# -- paths ----------------------------------------------------------------

def _fan(archive_root, kind: str, digest: str, suffix: str = "") -> Path:
    """Two-level fanout: keeps any one directory small, which matters a lot
    when the archive is syncing through Dropbox/MEGA."""
    return (Path(archive_root) / CODE_DIR / kind
            / digest[:2] / digest[2:4] / (digest + suffix))


def blob_path(archive_root, digest: str) -> Path:
    return _fan(archive_root, BLOBS, digest)


def manifest_path(archive_root, digest: str) -> Path:
    return _fan(archive_root, MANIFESTS, digest, ".json")


# -- writing --------------------------------------------------------------

def _write_once(path: Path, data: bytes) -> None:
    """Write content-addressed data. Existing content is left alone: the
    name *is* the hash, so a present file is already byte-identical."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def store_blob(archive_root, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    _write_once(blob_path(archive_root, digest), data)
    return digest


def store_blob_file(archive_root, src: "str | Path", *,
                    digest: Optional[str] = None, _chunk: int = 1 << 20) -> str:
    """Store a file's bytes without ever holding them all in memory.

    Copy in chunks via mkstemp + os.replace, so the store never contains a
    partial blob under a name that claims to be its hash. `digest` skips
    the hashing pass when the caller already knows it.
    """
    src = Path(src)
    if digest is None:
        h = hashlib.sha256()
        with open(src, "rb") as f:
            for block in iter(lambda: f.read(_chunk), b""):
                h.update(block)
        digest = h.hexdigest()

    path = blob_path(archive_root, digest)
    if path.exists():
        return digest          # name is the hash; present means identical
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp.")
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as f:
            for block in iter(lambda: f.read(_chunk), b""):
                out.write(block)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return digest


def canonical_manifest(entry: Optional[str], files: Dict[str, str]) -> bytes:
    """The exact bytes a manifest hashes over. Content only -- see module
    docstring on why nothing volatile may go in here."""
    return json.dumps(
        {"entry": entry, "files": dict(sorted(files.items()))},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def store_manifest(archive_root, entry: Optional[str], files: Dict[str, str]) -> str:
    data = canonical_manifest(entry, files)
    digest = hashlib.sha256(data).hexdigest()
    _write_once(manifest_path(archive_root, digest), data)
    return digest


def read_manifest(archive_root, digest: str) -> Optional[dict]:
    path = manifest_path(archive_root, digest)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def copy_manifest(src_root, dest_root, digest: str) -> dict:
    """Copy one snapshot (its manifest and every blob it names) between
    archives.

    Content addressing makes this the easy half of a transfer: an id is a
    hash of the bytes, so anything already present is by definition
    identical and is skipped. Two archives can never disagree about what a
    digest means, which is why no rewriting is needed here -- unlike
    session ids, a code id is the same everywhere.
    """
    out = {"manifest": digest, "blobs_copied": 0, "blobs_present": 0, "missing": []}
    manifest = read_manifest(src_root, digest)
    if manifest is None:
        out["missing"].append(digest)
        return out

    for blob in sorted(set((manifest.get("files") or {}).values())):
        dest = blob_path(dest_root, blob)
        if dest.is_file():
            out["blobs_present"] += 1
            continue
        src = blob_path(src_root, blob)
        if not src.is_file():
            out["missing"].append(blob)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        _write_once(dest, src.read_bytes())
        out["blobs_copied"] += 1

    target = manifest_path(dest_root, digest)
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_once(target, manifest_path(src_root, digest).read_bytes())
    return out


# -- first-party detection ------------------------------------------------

def _sysconfig_roots() -> List[Path]:
    out = []
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        p = sysconfig.get_paths().get(key)
        if p:
            out.append(Path(p).resolve())
    return out


def _nebula_package_dir() -> Path:
    return Path(__file__).resolve().parent


def _is_third_party(path: Path, sys_roots: List[Path]) -> bool:
    if any(part in ("site-packages", "dist-packages") for part in path.parts):
        return True
    # Nebula itself is the instrument doing the recording, not the code
    # that produced the measurement. Capturing it would balloon the store
    # with a copy of nebula per edit whenever it is an editable install
    # from a checkout -- and it tells you nothing about the experiment.
    # (Scripts elsewhere in the nebula *repo*, e.g. examples/, still count.)
    try:
        path.relative_to(_nebula_package_dir())
        return True
    except ValueError:
        pass
    for root in sys_roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _repo_key(path: Path, git_root: Optional[Path]) -> str:
    """The manifest key for a file: <repo>/<path-within-repo>, or the bare
    filename for a script that isn't in a repo at all."""
    if git_root is None:
        return path.name
    return f"{git_root.name}/{path.relative_to(git_root).as_posix()}"


def first_party_sources(entry_file) -> Tuple[Dict[str, Path], Dict[str, Optional[Path]]]:
    """Every loaded module that looks like the user's own code, plus the
    entry script itself.

    Returns ({manifest key: absolute path}, {repo name: git root}).

    "First party" means: has a real file, is not stdlib or site-packages,
    and lives inside a git repo -- with the entry script always included
    even if it is not in a repo. Modules imported *after* this runs are not
    visible; capture happens per artifact write, so top-of-file imports
    (the normal case) are always covered.
    """
    from nebula.session import _find_git_root  # local: avoids an import cycle

    sys_roots = _sysconfig_roots()
    files: Dict[str, Path] = {}
    repos: Dict[str, Optional[Path]] = {}

    def consider(raw_path, *, always: bool = False) -> None:
        # Namespace packages and builtins have __file__ = None.
        if not raw_path:
            return
        try:
            path = Path(raw_path).resolve()
        except (OSError, ValueError, TypeError):
            return
        if not path.is_file():
            return
        if not always and _is_third_party(path, sys_roots):
            return
        git_root = _find_git_root(path)
        if git_root is None and not always:
            return
        files[_repo_key(path, git_root)] = path
        if git_root is not None:
            repos[git_root.name] = git_root

    if entry_file:
        consider(entry_file, always=True)
    for module in list(sys.modules.values()):
        consider(getattr(module, "__file__", None))

    return files, repos


def _repo_states(repos: Dict[str, Optional[Path]]) -> Dict[str, dict]:
    """commit/dirty for each contributing repo -- the whole set, not just
    the entry point's repo."""
    from nebula.session import _git

    out: Dict[str, dict] = {}
    for name, root in repos.items():
        if root is None:
            continue
        commit = _git(["rev-parse", "HEAD"], cwd=root)
        status = _git(["status", "--porcelain"], cwd=root)
        out[name] = {
            "commit": commit,
            "dirty": (bool(status) if status is not None else None),
        }
    return out


# Repeated captures within one process are the norm (one per artifact), and
# the file set rarely changes between them. Keyed on (root, entry, stat
# signature) so an unchanged tree skips re-reading and re-hashing entirely.
_capture_cache: Dict[tuple, dict] = {}


def capture(archive_root, entry_file, *,
            max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> dict:
    """Snapshot the first-party source behind the current call into the
    archive's code store.

    Returns {"code": manifest id, "repos": {...}, "n_files": int,
    "skipped": [...]}, or {} if there was nothing to capture.
    """
    archive_root = Path(archive_root)
    files, repos = first_party_sources(entry_file)
    if not files:
        return {}

    entry_key = None
    if entry_file:
        entry_path = Path(entry_file).resolve()
        for key, path in files.items():
            if path == entry_path:
                entry_key = key
                break

    sig = []
    for key in sorted(files):
        try:
            st = files[key].stat()
            sig.append((key, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((key, None, None))
    cache_key = (str(archive_root), entry_key, tuple(sig), max_file_bytes)
    hit = _capture_cache.get(cache_key)
    if hit is not None:
        return dict(hit)

    blobs: Dict[str, str] = {}
    skipped: List[str] = []
    for key in sorted(files):
        path = files[key]
        try:
            if path.stat().st_size > max_file_bytes:
                skipped.append(key)
                continue
            blobs[key] = store_blob(archive_root, path.read_bytes())
        except OSError:
            skipped.append(key)

    if not blobs:
        return {}

    result = {
        "code": store_manifest(archive_root, entry_key, blobs),
        "repos": _repo_states(repos),
        "n_files": len(blobs),
        "skipped": skipped,
    }
    _capture_cache[cache_key] = dict(result)
    return result


def manifest_stats(archive_root, digest: str) -> Optional[dict]:
    """What a snapshot contains, and how much of it is shared.

    "shared" counts files whose bytes are also referenced by at least one
    *other* manifest -- i.e. files that were already in the store and were
    kept rather than written again. "unique" is the rest: the versions this
    snapshot is the only holder of. Together they are the storage story for
    one artifact's code.
    """
    archive_root = Path(archive_root)
    manifest = read_manifest(archive_root, digest)
    if manifest is None:
        return None
    files = manifest.get("files") or {}

    # How many manifests reference each blob.
    blob_users: Dict[str, int] = {}
    for path in _iter_stored(archive_root, MANIFESTS):
        try:
            other = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for blob in set((other.get("files") or {}).values()):
            blob_users[blob] = blob_users.get(blob, 0) + 1

    shared = sum(1 for blob in files.values() if blob_users.get(blob, 0) > 1)
    repos: Dict[str, int] = {}
    for key in files:
        repos[key.split("/")[0]] = repos.get(key.split("/")[0], 0) + 1

    present = sum(1 for blob in set(files.values())
                  if blob_path(archive_root, blob).is_file())
    return {
        "id": digest,
        "short": digest[:12],
        "entry": manifest.get("entry"),
        "n_files": len(files),
        "n_blobs": len(set(files.values())),
        "blobs_present": present,
        "repos": dict(sorted(repos.items(), key=lambda kv: (-kv[1], kv[0]))),
        "shared": shared,
        "unique": len(files) - shared,
        "files": dict(sorted(files.items())),
    }


def _safe_relpath(key: str) -> Optional[Path]:
    """Manifest keys become paths on disk during a restore, so they get the
    same scrutiny as any archive input: no absolute paths, no '..', no
    drive letters. A manifest is machine-written, but it is also just a
    file in a directory anyone can edit."""
    path = Path(key)
    if path.is_absolute() or path.drive or any(p == ".." for p in path.parts):
        return None
    return path


def restore(archive_root, digest: str, dest_dir) -> dict:
    """Write a snapshot's files back out under `dest_dir`, at their
    original repo-relative paths -- the "give me back exactly what ran"
    operation the store exists for.

    Never overwrites: `dest_dir` must not already exist. Missing blobs are
    reported rather than silently skipped, since a partial restore that
    looks complete is the dangerous outcome.
    """
    archive_root = Path(archive_root)
    dest_dir = Path(dest_dir)
    manifest = read_manifest(archive_root, digest)
    if manifest is None:
        raise FileNotFoundError(f"no code manifest {digest} in this archive")
    if dest_dir.exists():
        raise FileExistsError(f"{dest_dir} already exists")

    files = manifest.get("files") or {}
    entry = manifest.get("entry")
    written: List[str] = []
    missing: List[str] = []
    rejected: List[str] = []

    dest_dir.mkdir(parents=True)
    for key in sorted(files):
        rel = _safe_relpath(key)
        if rel is None:
            rejected.append(key)
            continue
        blob = blob_path(archive_root, files[key])
        if not blob.is_file():
            missing.append(key)
            continue
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob.read_bytes())
        written.append(key)

    repos = sorted({key.split("/")[0] for key in written if "/" in key})
    notes = [
        f"nebula captured source snapshot {digest}",
        f"entry point: {entry or '(unknown)'}",
        f"files: {len(written)} restored"
        + (f", {len(missing)} missing from the store" if missing else "")
        + (f", {len(rejected)} rejected as unsafe paths" if rejected else ""),
        f"repos: {', '.join(repos) if repos else '(none)'}",
        "",
        "These are the exact bytes recorded when the artifact was written.",
        "Git commit/dirty state for the run is in the artifact's sidecar.",
    ]
    if missing:
        notes += ["", "MISSING (not restored):"] + [f"  {k}" for k in missing]
    (dest_dir / "SNAPSHOT.txt").write_text("\n".join(notes) + "\n")

    return {
        "dest": str(dest_dir), "entry": entry,
        "n_written": len(written), "written": written,
        "missing": missing, "rejected": rejected,
        "entry_path": str(dest_dir / entry) if entry and _safe_relpath(entry) else None,
    }


# -- reachability, for check and gc ---------------------------------------

def _iter_sidecar_files(archive_root: Path, *, include_trash: bool = True):
    from nebula.sidecar import SIDECAR_SUFFIX

    for path in Path(archive_root).rglob(f"*{SIDECAR_SUFFIX}"):
        if not include_trash and ".trash" in path.parts:
            continue
        yield path


def referenced_manifests(archive_root, *, include_trash: bool = True) -> Dict[str, List[str]]:
    """{manifest id: [sidecar paths referencing it]} across the archive.

    Trashed sessions count as live by default: a soft-deleted session can
    be restored, and deleting its code would make that restore a lie.
    """
    out: Dict[str, List[str]] = {}
    for path in _iter_sidecar_files(Path(archive_root), include_trash=include_trash):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        code = (data.get("produced_by") or {}).get("code")
        if code:
            out.setdefault(code, []).append(str(path))
    return out


def _iter_stored(archive_root: Path, kind: str):
    base = Path(archive_root) / CODE_DIR / kind
    if not base.is_dir():
        return
    for path in base.rglob("*"):
        # Skip our own temp files, and anything the OS drops in here
        # (.DS_Store arrives via Finder/MEGA) -- those are not store
        # objects, and reporting them as collectable garbage is noise.
        if path.is_file() and not path.name.startswith("."):
            yield path


def gc(archive_root, *, dry_run: bool = True, include_trash: bool = True) -> dict:
    """Delete manifests and blobs nothing can reach (mark and sweep).

    Dry run by default: it reports what *would* go. Nothing outside
    .code/ is ever touched.

    Only captured source lives here. Asset snapshots have their own store
    (see nebula.assetstore) precisely so this sweep cannot reach them: when
    the two shared one store, this function's liveness walk did not know
    about assets and deleted bytes a session had pinned.
    """
    archive_root = Path(archive_root)
    live_manifests = set(referenced_manifests(archive_root, include_trash=include_trash))

    live_blobs: set = set()
    for digest in live_manifests:
        manifest = read_manifest(archive_root, digest)
        if manifest:
            live_blobs.update((manifest.get("files") or {}).values())

    dead_manifests, dead_blobs, freed = [], [], 0
    for path in _iter_stored(archive_root, MANIFESTS):
        if path.stem not in live_manifests:
            dead_manifests.append(path)
    for path in _iter_stored(archive_root, BLOBS):
        if path.name not in live_blobs:
            dead_blobs.append(path)

    for path in dead_manifests + dead_blobs:
        try:
            freed += path.stat().st_size
        except OSError:
            pass
        if not dry_run:
            try:
                path.unlink()
            except OSError:
                pass

    return {
        "dry_run": dry_run,
        "manifests": [p.stem for p in dead_manifests],
        "blobs": [p.name for p in dead_blobs],
        "bytes": freed,
        "live_manifests": len(live_manifests),
        "live_blobs": len(live_blobs),
    }
