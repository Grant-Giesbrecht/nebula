"""
The build stamp: commit count + short hash, so two machines running dev
builds can tell which is newer.
"""

import shutil
import subprocess
import sys

import pytest

import nebula
from nebula import buildinfo

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def _repo(tmp_path, commits=2):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    for i in range(commits):
        (repo / "f.txt").write_text(str(i))
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-q", "-m", f"c{i}")
    return repo


def test_format_version():
    assert buildinfo.format_version("0.1.0", 77, "970093a", False) \
        == "0.1.0-dev.77+970093a"
    assert buildinfo.format_version("0.1.0", 77, "970093a", True) \
        == "0.1.0-dev.77+970093a.dirty"
    # No git information: just the declared version, never "dev.None".
    assert buildinfo.format_version("0.1.0", None, None, False) == "0.1.0"


@needs_git
def test_stamp_counts_commits_and_flags_changes(tmp_path):
    repo = _repo(tmp_path, commits=3)
    s = buildinfo.stamp_from_git(repo)
    assert s["count"] == 3 and len(s["sha"]) == 7 and s["dirty"] is False

    (repo / "scratch.txt").write_text("untracked")     # not a code change
    assert buildinfo.stamp_from_git(repo)["dirty"] is False

    (repo / "f.txt").write_text("edited")
    assert buildinfo.stamp_from_git(repo)["dirty"] is True


@needs_git
def test_rebuilt_bridge_binary_is_not_a_change(tmp_path):
    """The sidecar binary is tracked but rewritten by every sidecar build;
    if it counted, every app build after one would read as dirty."""
    repo = _repo(tmp_path)
    binary = repo / "navigator-tauri" / "src-tauri" / "binaries" / "nebula-bridge"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"v1")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "binary")
    binary.write_bytes(b"v2")
    assert buildinfo.stamp_from_git(repo)["dirty"] is False


def test_not_a_checkout(tmp_path):
    assert buildinfo.stamp_from_git(tmp_path) is None


@needs_git
def test_frozen_build_reads_the_written_stamp(tmp_path, monkeypatch):
    repo = _repo(tmp_path, commits=2)
    stamp_file = tmp_path / "stamp" / "nebula_build_stamp.py"
    written = buildinfo.write_stamp(stamp_file, repo)

    monkeypatch.syspath_prepend(str(stamp_file.parent))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delitem(sys.modules, "nebula_build_stamp", raising=False)
    buildinfo.build_info.cache_clear()
    try:
        info = buildinfo.build_info()
    finally:
        buildinfo.build_info.cache_clear()
    assert info["source"] == "frozen"
    assert info["count"] == 2 and info["sha"] == written["sha"]
    assert info["version"] == f"{nebula.__version__}-dev.2+{written['sha']}"
    assert info["built"] == written["built"]


def test_cli_version(capsys):
    from nebula import cli

    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith(f"nebula {nebula.__version__}")
