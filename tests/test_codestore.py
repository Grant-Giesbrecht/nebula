import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import nebula
from nebula import check as check_mod, codestore
from nebula.config import ArchiveSettings, read_settings, write_settings
from nebula.sidecar import ProducedBy, SidecarMeta, read_sidecar

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _run_script(repo_dir: Path, archive: Path, body: str = "") -> str:
    """Run a script from inside a real git repo so provenance capture sees
    a genuine first-party caller. Returns the run id."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    helper = repo_dir / "helper.py"
    if not helper.exists():      # seed once, so a test can edit it between runs
        helper.write_text("VALUE = 1\n\ndef helper():\n    return VALUE\n")
    script = repo_dir / "run.py"
    script.write_text(
        f"import sys\n"
        f"sys.path.insert(0, {SRC!r})\n"
        f"sys.path.insert(0, {str(repo_dir)!r})\n"
        f"import nebula, helper\n"
        f"from pathlib import Path\n"
        f"with nebula.session(Path({str(archive)!r}), description='t') as s:\n"
        f"    (s.path / 'd.csv').write_text(str(helper.helper()))\n"
        f"    s.write_meta_for('d.csv')\n"
        f"{body}"
        f"    print(s.id)\n"
    )
    res = subprocess.run([sys.executable, str(script)], cwd=repo_dir,
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip().splitlines()[-1]


def _sidecar(archive: Path, run_id: str, name: str = "d.csv"):
    session_dir = next(archive.rglob(run_id))
    return read_sidecar(session_dir / name)


# ---------------------------------------------------------------------
# forward compatibility (step 1)
# ---------------------------------------------------------------------

def test_produced_by_keeps_unknown_keys():
    """An older nebula must not choke on a sidecar from a newer one."""
    pb = ProducedBy.from_dict({"repo": "r", "future_field": 42})
    assert pb.repo == "r"
    assert pb.extra == {"future_field": 42}
    assert pb.to_dict()["future_field"] == 42       # and writes it back out


def test_sidecar_round_trips_unknown_produced_by_keys(tmp_path):
    raw = {"created": "2026-01-01T00:00:00+00:00",
           "produced_by": {"repo": "r", "invented_later": {"a": 1}}}
    meta = SidecarMeta.from_dict(raw)
    assert meta.to_dict()["produced_by"]["invented_later"] == {"a": 1}


# ---------------------------------------------------------------------
# blobs and manifests (step 2)
# ---------------------------------------------------------------------

def test_blob_is_content_addressed_and_deduped(tmp_path):
    a = codestore.store_blob(tmp_path, b"print(1)\n")
    b = codestore.store_blob(tmp_path, b"print(1)\n")
    c = codestore.store_blob(tmp_path, b"print(2)\n")
    assert a == b and a != c
    assert len(list((tmp_path / "code" / "blobs").rglob("*"))) >= 2
    assert codestore.blob_path(tmp_path, a).read_bytes() == b"print(1)\n"


def test_blob_path_is_fanned_out(tmp_path):
    digest = codestore.store_blob(tmp_path, b"x")
    path = codestore.blob_path(tmp_path, digest)
    assert path.parent.name == digest[2:4]
    assert path.parent.parent.name == digest[:2]


def test_manifest_id_depends_only_on_content(tmp_path):
    files = {"repo/a.py": "aaa", "repo/b.py": "bbb"}
    first = codestore.store_manifest(tmp_path, "repo/a.py", files)
    # same content, different insertion order -> same id
    second = codestore.store_manifest(tmp_path, "repo/a.py",
                                      {"repo/b.py": "bbb", "repo/a.py": "aaa"})
    assert first == second
    # a changed file -> different id
    third = codestore.store_manifest(tmp_path, "repo/a.py",
                                     {"repo/a.py": "aaa", "repo/b.py": "ccc"})
    assert third != first


def test_manifest_holds_no_volatile_fields(tmp_path):
    """Anything per-run inside the manifest would defeat dedupe silently."""
    digest = codestore.store_manifest(tmp_path, "r/a.py", {"r/a.py": "aaa"})
    manifest = codestore.read_manifest(tmp_path, digest)
    assert set(manifest) == {"entry", "files"}


def test_read_manifest_missing_is_none(tmp_path):
    assert codestore.read_manifest(tmp_path, "0" * 64) is None


# ---------------------------------------------------------------------
# capture during a real run
# ---------------------------------------------------------------------

def test_capture_records_entry_and_first_party_import(tmp_path):
    archive = tmp_path / "archive"
    run_id = _run_script(tmp_path / "repo", archive)
    meta = _sidecar(archive, run_id)

    assert meta.produced_by.code, "no code snapshot recorded"
    manifest = codestore.read_manifest(archive, meta.produced_by.code)
    assert manifest["entry"] == "repo/run.py"
    assert set(manifest["files"]) == {"repo/run.py", "repo/helper.py"}
    # the bytes are really there
    blob = manifest["files"]["repo/helper.py"]
    assert b"def helper" in codestore.blob_path(archive, blob).read_bytes()


def test_capture_excludes_nebula_itself(tmp_path):
    """Nebula is the instrument, not the experiment -- capturing it would
    balloon the store on every nebula edit."""
    archive = tmp_path / "archive"
    run_id = _run_script(tmp_path / "repo", archive)
    manifest = codestore.read_manifest(archive, _sidecar(archive, run_id).produced_by.code)
    assert not any("nebula/src" in key for key in manifest["files"])


def test_capture_records_every_contributing_repo(tmp_path):
    archive = tmp_path / "archive"
    run_id = _run_script(tmp_path / "repo", archive)
    repos = _sidecar(archive, run_id).produced_by.repos
    assert "repo" in repos
    assert set(repos["repo"]) == {"commit", "dirty"}


def test_git_fields_still_recorded_alongside_code(tmp_path):
    """The snapshot is *in addition to* commit/dirty/entry_point."""
    archive = tmp_path / "archive"
    run_id = _run_script(tmp_path / "repo", archive)
    pb = _sidecar(archive, run_id).produced_by
    assert pb.repo == "repo"
    assert pb.entry_point == "run.py"
    assert pb.dirty is True          # nothing committed in the fake repo
    assert pb.code


def test_unchanged_code_reuses_manifest_and_writes_nothing(tmp_path):
    archive = tmp_path / "archive"
    repo = tmp_path / "repo"
    first = _run_script(repo, archive)
    blobs_after_first = list((archive / "code" / "blobs").rglob("*"))
    second = _run_script(repo, archive)

    assert first != second
    assert (_sidecar(archive, first).produced_by.code
            == _sidecar(archive, second).produced_by.code)
    assert list((archive / "code" / "blobs").rglob("*")) == blobs_after_first


def test_changed_code_makes_a_new_manifest(tmp_path):
    archive = tmp_path / "archive"
    repo = tmp_path / "repo"
    first = _run_script(repo, archive)
    (repo / "helper.py").write_text("VALUE = 2\n\ndef helper():\n    return VALUE\n")
    second = _run_script(repo, archive)

    m1 = _sidecar(archive, first).produced_by.code
    m2 = _sidecar(archive, second).produced_by.code
    assert m1 != m2
    # only the changed file is a new blob; run.py is shared
    f1 = codestore.read_manifest(archive, m1)["files"]
    f2 = codestore.read_manifest(archive, m2)["files"]
    assert f1["repo/run.py"] == f2["repo/run.py"]
    assert f1["repo/helper.py"] != f2["repo/helper.py"]


def test_size_cap_skips_large_files(tmp_path):
    archive = tmp_path / "archive"
    entry = tmp_path / "big.py"
    entry.write_text("x = '" + "y" * 5000 + "'\n")
    got = codestore.capture(archive, entry, max_file_bytes=100)
    assert got == {} or "big.py" in got.get("skipped", [])


# ---------------------------------------------------------------------
# archive settings (step 3)
# ---------------------------------------------------------------------

def test_settings_default_to_capture_on(tmp_path):
    assert read_settings(tmp_path).capture_code is True


def test_settings_file_can_disable_capture(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    write_settings(archive, ArchiveSettings(capture_code=False))
    assert read_settings(archive).capture_code is False

    run_id = _run_script(tmp_path / "repo", archive)
    assert _sidecar(archive, run_id).produced_by.code is None
    assert not (archive / "code").exists()
    # ...but the cheap git provenance is still recorded
    assert _sidecar(archive, run_id).produced_by.repo == "repo"


def test_settings_survive_a_write_read_cycle(tmp_path):
    write_settings(tmp_path, ArchiveSettings(capture_code=False, code_max_file_bytes=99))
    got = read_settings(tmp_path)
    assert got.capture_code is False and got.code_max_file_bytes == 99


def test_unreadable_settings_fall_back_to_defaults(tmp_path):
    (tmp_path / "archive.yaml").write_text("{{{ not yaml")
    assert read_settings(tmp_path).capture_code is True


def test_env_override_beats_the_file(tmp_path, monkeypatch):
    write_settings(tmp_path, ArchiveSettings(capture_code=True))
    monkeypatch.setenv("NEBULA_CAPTURE_CODE", "0")
    assert read_settings(tmp_path).capture_code is False


# ---------------------------------------------------------------------
# check + gc (step 4)
# ---------------------------------------------------------------------

def test_check_flags_dangling_code_ref(tmp_path):
    archive = tmp_path / "archive"
    run_id = _run_script(tmp_path / "repo", archive)
    manifest_id = _sidecar(archive, run_id).produced_by.code
    codestore.manifest_path(archive, manifest_id).unlink()

    kinds = [i.kind for i in check_mod.check(archive)]
    assert "dangling_code_ref" in kinds


def test_check_flags_missing_blob(tmp_path):
    archive = tmp_path / "archive"
    run_id = _run_script(tmp_path / "repo", archive)
    manifest = codestore.read_manifest(archive, _sidecar(archive, run_id).produced_by.code)
    codestore.blob_path(archive, manifest["files"]["repo/helper.py"]).unlink()

    issues = [i for i in check_mod.check(archive) if i.kind == "missing_code_blob"]
    assert issues and "helper.py" in issues[0].detail


def test_check_is_quiet_when_the_store_is_intact(tmp_path):
    archive = tmp_path / "archive"
    _run_script(tmp_path / "repo", archive)
    kinds = [i.kind for i in check_mod.check(archive)]
    assert "dangling_code_ref" not in kinds and "missing_code_blob" not in kinds


def test_gc_keeps_referenced_code(tmp_path):
    archive = tmp_path / "archive"
    _run_script(tmp_path / "repo", archive)
    before = sorted(p.name for p in (archive / "code").rglob("*") if p.is_file())

    res = codestore.gc(archive, dry_run=False)
    after = sorted(p.name for p in (archive / "code").rglob("*") if p.is_file())
    assert before == after
    assert res["manifests"] == [] and res["blobs"] == []


def test_gc_collects_unreferenced_code(tmp_path):
    archive = tmp_path / "archive"
    _run_script(tmp_path / "repo", archive)
    orphan_blob = codestore.store_blob(archive, b"nobody references me\n")
    orphan_manifest = codestore.store_manifest(archive, "x/y.py", {"x/y.py": orphan_blob})

    res = codestore.gc(archive, dry_run=True)
    assert orphan_manifest in res["manifests"] and orphan_blob in res["blobs"]
    assert codestore.blob_path(archive, orphan_blob).is_file()   # dry run kept it

    codestore.gc(archive, dry_run=False)
    assert not codestore.blob_path(archive, orphan_blob).is_file()
    assert not codestore.manifest_path(archive, orphan_manifest).is_file()


def test_gc_treats_trashed_sessions_as_live_by_default(tmp_path):
    """A soft-deleted session can be restored; deleting its code would make
    that restore a lie."""
    archive = tmp_path / "archive"
    run_id = _run_script(tmp_path / "repo", archive)
    manifest_id = _sidecar(archive, run_id).produced_by.code
    nebula.delete_session(archive, run_id)          # -> archive .trash/

    assert codestore.gc(archive, dry_run=True)["manifests"] == []
    ignored = codestore.gc(archive, dry_run=True, include_trash=False)
    assert manifest_id in ignored["manifests"]


def test_gc_ignores_os_dropped_files(tmp_path):
    """Finder/MEGA leave .DS_Store inside synced folders; they are not
    store objects and must not be reported as collectable garbage."""
    archive = tmp_path / "archive"
    _run_script(tmp_path / "repo", archive)
    junk = archive / "code" / "blobs" / ".DS_Store"
    junk.write_bytes(b"\x00junk")

    res = codestore.gc(archive, dry_run=True)
    assert res["blobs"] == [] and res["manifests"] == []
    codestore.gc(archive, dry_run=False)
    assert junk.is_file()


def test_gc_never_touches_anything_outside_the_code_store(tmp_path):
    archive = tmp_path / "archive"
    run_id = _run_script(tmp_path / "repo", archive)
    session_dir = next(archive.rglob(run_id))
    codestore.store_blob(archive, b"orphan\n")

    codestore.gc(archive, dry_run=False)
    assert (session_dir / "d.csv").is_file()
    assert (session_dir / "d.csv.meta.json").is_file()
    assert (session_dir / "session.yaml").is_file()


# ---------------------------------------------------------------------
# nebula config
# ---------------------------------------------------------------------

def _nebula(*args, **kwargs):
    return subprocess.run([sys.executable, "-m", "nebula.cli", *args],
                          capture_output=True, text=True, **kwargs)


def test_config_shows_defaults_without_a_file(tmp_path):
    res = _nebula("config", str(tmp_path))
    assert res.returncode == 0, res.stderr
    assert "not present" in res.stdout
    assert "capture_code: True" in res.stdout


def test_config_writes_and_reads_back(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    assert _nebula("config", str(archive), "--capture-code", "false").returncode == 0
    assert read_settings(archive).capture_code is False

    out = _nebula("config", str(archive), "--max-file-bytes", "4096").stdout
    assert "code_max_file_bytes: 4096" in out
    settings = read_settings(archive)
    assert settings.code_max_file_bytes == 4096
    assert settings.capture_code is False        # unrelated setting preserved


def test_config_rejects_a_non_boolean(tmp_path):
    res = _nebula("config", str(tmp_path), "--capture-code", "maybe")
    assert res.returncode != 0
    assert "true or false" in res.stderr


def test_config_does_not_persist_the_env_override(tmp_path, monkeypatch):
    """A one-off NEBULA_CAPTURE_CODE in the shell must not be written into
    the archive as though the user had chosen it."""
    archive = tmp_path / "archive"
    archive.mkdir()
    _nebula("config", str(archive), "--capture-code", "false")

    env = dict(os.environ, NEBULA_CAPTURE_CODE="1")
    res = _nebula("config", str(archive), "--max-file-bytes", "2048", env=env)
    assert res.returncode == 0
    assert "NEBULA_CAPTURE_CODE is set" in res.stdout

    monkeypatch.delenv("NEBULA_CAPTURE_CODE", raising=False)
    assert read_settings(archive).capture_code is False


def test_env_override_is_reported_but_file_wins_on_disk(tmp_path, monkeypatch):
    write_settings(tmp_path, ArchiveSettings(capture_code=False))
    monkeypatch.setenv("NEBULA_CAPTURE_CODE", "1")
    assert read_settings(tmp_path).capture_code is True              # effective
    assert read_settings(tmp_path, apply_env=False).capture_code is False   # on disk


# ---------------------------------------------------------------------
# panel data: snapshot stats + entry point resolution
# ---------------------------------------------------------------------

def test_manifest_stats_counts_files_and_repos(tmp_path):
    from nebula.navigator import model

    archive = tmp_path / "archive"
    run_id = _run_script(tmp_path / "repo", archive)
    code = _sidecar(archive, run_id).produced_by.code

    stats = model.code_info(archive, code)
    assert stats["ok"]
    assert stats["short"] == code[:12] and len(stats["short"]) == 12
    assert stats["n_files"] == 2
    assert stats["repos"] == {"repo": 2}
    assert stats["blobs_present"] == stats["n_blobs"]


def test_manifest_stats_kept_vs_unique(tmp_path):
    """Files shared with another snapshot were kept rather than re-stored."""
    from nebula.navigator import model

    archive = tmp_path / "archive"
    repo = tmp_path / "repo"
    first = _run_script(repo, archive)
    # change one of the two files -> second snapshot shares run.py only
    (repo / "helper.py").write_text("VALUE = 99\n\ndef helper():\n    return VALUE\n")
    second = _run_script(repo, archive)

    stats = model.code_info(archive, _sidecar(archive, second).produced_by.code)
    assert stats["n_files"] == 2
    assert stats["shared"] == 1      # run.py, also in the first snapshot
    assert stats["unique"] == 1      # the edited helper.py
    assert stats["shared"] + stats["unique"] == stats["n_files"]


def test_manifest_stats_reports_a_missing_snapshot(tmp_path):
    from nebula.navigator import model

    archive = tmp_path / "archive"
    run_id = _run_script(tmp_path / "repo", archive)
    code = _sidecar(archive, run_id).produced_by.code
    codestore.manifest_path(archive, code).unlink()

    stats = model.code_info(archive, code)
    assert stats["ok"] is False and "not in the archive" in stats["error"]


def test_entry_point_link_finds_the_local_checkout(tmp_path, monkeypatch):
    from nebula.navigator import model

    archive = tmp_path / "archive"
    repo = tmp_path / "repo"
    run_id = _run_script(repo, archive)
    monkeypatch.setenv(model.REPO_PATHS_ENV, str(tmp_path))

    item = {"entry_point": "run.py", "repo": "repo",
            "commit": _sidecar(archive, run_id).produced_by.commit, "dirty": True}
    link = model.entry_point_link(archive, item)
    assert link["local"]["exists"] is True
    assert link["local"]["path"] == str(repo / "run.py")
    assert link["local"]["repo_root"] == str(repo)


def test_entry_point_link_reports_a_missing_checkout(tmp_path, monkeypatch):
    from nebula.navigator import model

    monkeypatch.setenv(model.REPO_PATHS_ENV, str(tmp_path / "nowhere"))
    link = model.entry_point_link(tmp_path, {"entry_point": "run.py", "repo": "ghost"})
    assert link["local"] is None and link["remote"] is None
    assert "no checkout of 'ghost'" in link["note"]


def test_entry_point_link_handles_an_absolute_entry_point(tmp_path):
    """A script outside any repo is recorded as an absolute path."""
    from nebula.navigator import model

    script = tmp_path / "loose.py"
    script.write_text("print(1)\n")
    link = model.entry_point_link(tmp_path, {"entry_point": str(script)})
    assert link["local"]["exists"] is True and link["local"]["repo_root"] is None
    assert link["remote"] is None


def test_entry_point_link_without_an_entry_point(tmp_path):
    from nebula.navigator import model

    link = model.entry_point_link(tmp_path, {})
    assert link["note"] == "no entry point recorded"


def test_entry_point_link_warns_when_the_tree_was_dirty(tmp_path, monkeypatch):
    """A hosted link at the recorded commit is not what ran, if it was dirty."""
    from nebula.navigator import model

    archive = tmp_path / "archive"
    repo = tmp_path / "repo"
    run_id = _run_script(repo, archive)
    monkeypatch.setenv(model.REPO_PATHS_ENV, str(tmp_path))
    subprocess.run(["git", "remote", "add", "origin",
                    "git@github.com:someorg/repo.git"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "x"], cwd=repo, check=True)

    pb = _sidecar(archive, run_id).produced_by
    link = model.entry_point_link(archive, {
        "entry_point": "run.py", "repo": "repo", "commit": "abc123", "dirty": True})
    assert link["remote"]["url"] == "https://github.com/someorg/repo/blob/abc123/run.py"
    assert "dirty" in link["remote"]["warning"]


def test_sidecar_info_exposes_the_code_snapshot(tmp_path):
    """The panel reads produced_by out of sidecar_info, which builds an
    explicit dict -- a field missing here renders as nothing at all, with
    no error anywhere."""
    from nebula.navigator import model

    archive = tmp_path / "archive"
    run_id = _run_script(tmp_path / "repo", archive)
    session_dir = next(archive.rglob(run_id))
    info = model.sidecar_info(session_dir / "d.csv.meta.json")

    on_disk = json.loads((session_dir / "d.csv.meta.json").read_text())["produced_by"]
    exposed = info["produced_by"]
    # every recorded field must survive the trip to the view
    assert set(on_disk) <= set(exposed), f"dropped: {set(on_disk) - set(exposed)}"
    assert exposed["code"] == on_disk["code"] and exposed["code"]
    assert exposed["repos"] == on_disk["repos"] and exposed["repos"]


# ---------------------------------------------------------------------
# restoring a snapshot to a folder
# ---------------------------------------------------------------------

def test_restore_writes_files_at_their_original_paths(tmp_path):
    from nebula.navigator import model

    archive = tmp_path / "archive"
    run_id = _run_script(tmp_path / "repo", archive)
    code = _sidecar(archive, run_id).produced_by.code

    out = tmp_path / "out"
    out.mkdir()
    res = model.restore_code(archive, code, out)
    dest = Path(res["dest"])

    assert dest.name == f"nebula-code-{code[:12]}"
    assert (dest / "repo" / "run.py").is_file()
    assert (dest / "repo" / "helper.py").read_text().startswith("VALUE = 1")
    assert res["n_written"] == 2 and res["missing"] == []
    assert res["entry_path"] == str(dest / "repo" / "run.py")


def test_restore_bytes_match_what_ran_not_the_working_tree(tmp_path):
    """The point of the store: the tree has moved on, the snapshot hasn't."""
    from nebula.navigator import model

    archive = tmp_path / "archive"
    repo = tmp_path / "repo"
    run_id = _run_script(repo, archive)
    code = _sidecar(archive, run_id).produced_by.code
    (repo / "helper.py").write_text("VALUE = 999  # edited after the run\n")

    out = tmp_path / "out"
    out.mkdir()
    dest = Path(model.restore_code(archive, code, out)["dest"])
    assert "VALUE = 1" in (dest / "repo" / "helper.py").read_text()
    assert "999" not in (dest / "repo" / "helper.py").read_text()


def test_restore_writes_a_snapshot_note(tmp_path):
    from nebula.navigator import model

    archive = tmp_path / "archive"
    run_id = _run_script(tmp_path / "repo", archive)
    code = _sidecar(archive, run_id).produced_by.code
    out = tmp_path / "out"
    out.mkdir()
    dest = Path(model.restore_code(archive, code, out)["dest"])

    note = (dest / "SNAPSHOT.txt").read_text()
    assert code in note
    assert "entry point: repo/run.py" in note


def test_restore_twice_does_not_collide(tmp_path):
    from nebula.navigator import model

    archive = tmp_path / "archive"
    run_id = _run_script(tmp_path / "repo", archive)
    code = _sidecar(archive, run_id).produced_by.code
    out = tmp_path / "out"
    out.mkdir()

    first = model.restore_code(archive, code, out)["dest"]
    second = model.restore_code(archive, code, out)["dest"]
    assert first != second
    assert Path(first).is_file() is False and Path(second).is_dir()


def test_restore_refuses_an_existing_directory(tmp_path):
    archive = tmp_path / "archive"
    run_id = _run_script(tmp_path / "repo", archive)
    code = _sidecar(archive, run_id).produced_by.code
    dest = tmp_path / "already"
    dest.mkdir()
    with pytest.raises(FileExistsError):
        codestore.restore(archive, code, dest)


def test_restore_reports_missing_blobs_instead_of_pretending(tmp_path):
    from nebula.navigator import model

    archive = tmp_path / "archive"
    run_id = _run_script(tmp_path / "repo", archive)
    code = _sidecar(archive, run_id).produced_by.code
    manifest = codestore.read_manifest(archive, code)
    codestore.blob_path(archive, manifest["files"]["repo/helper.py"]).unlink()

    out = tmp_path / "out"
    out.mkdir()
    res = model.restore_code(archive, code, out)
    assert res["missing"] == ["repo/helper.py"]
    assert res["n_written"] == 1
    assert "MISSING" in (Path(res["dest"]) / "SNAPSHOT.txt").read_text()


def test_restore_rejects_path_traversal_in_a_manifest(tmp_path):
    """A manifest is a file in a directory anyone can edit; a key of
    '../../etc/x' must not write outside the destination."""
    archive = tmp_path / "archive"
    blob = codestore.store_blob(archive, b"pwned\n")
    code = codestore.store_manifest(archive, "ok.py", {
        "ok.py": blob, "../escaped.py": blob, "/abs.py": blob})

    dest = tmp_path / "out"
    res = codestore.restore(archive, code, dest)
    assert sorted(res["rejected"]) == ["../escaped.py", "/abs.py"]
    assert res["written"] == ["ok.py"]
    assert not (tmp_path / "escaped.py").exists()
    assert (dest / "ok.py").is_file()


def test_restore_unknown_snapshot_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        codestore.restore(tmp_path, "0" * 64, tmp_path / "out")
