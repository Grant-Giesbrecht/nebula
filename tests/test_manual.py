import datetime

import pytest

import nebula
from nebula import manual
from nebula.session import _find_session_dir
from nebula.sidecar import read_sidecar, read_session_yaml, sha256_file, write_session_yaml, SessionMeta


def _src(tmp_path, name="incoming.csv", content="1,2,3\n"):
    p = tmp_path / name
    p.write_text(content)
    return p


def _make_previous_day(archive, run_id="S-0050", *, status="closed"):
    ym = archive / "2020" / "01"
    ym.mkdir(parents=True, exist_ok=True)
    d = ym / run_id
    d.mkdir()
    write_session_yaml(d, SessionMeta(
        run_id=run_id,
        created=datetime.datetime(2020, 1, 1).astimezone().isoformat(),
        status=status, description="last year"))
    return run_id


# ---------------------------------------------------------------------
# import into an existing session
# ---------------------------------------------------------------------

def test_import_file_writes_external_sidecar(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="host")
    s.close()
    src = _src(tmp_path, content="hello nebula\n")

    dest = manual.import_file(archive, s.id, src, origin="emailed by Jane")

    assert dest.exists()
    meta = read_sidecar(dest)
    assert meta.produced_by.source == "external"
    assert meta.produced_by.origin == "emailed by Jane"
    assert meta.produced_by.imported_by  # some username
    assert meta.produced_by.imported_at
    assert meta.produced_by.commit is None  # no faked git provenance
    assert meta.sha256 == sha256_file(src)


def test_import_records_history(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="host")
    s.close()
    manual.import_file(archive, s.id, _src(tmp_path), origin="from a coworker")

    history = read_session_yaml(_find_session_dir(archive, s.id)).history
    assert len(history) == 1
    assert history[0]["action"] == "import"
    assert history[0]["file"] == "incoming.csv"
    assert history[0]["note"] == "from a coworker"


def test_import_does_not_reopen_closed_today_session(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="host")
    s.close()
    manual.import_file(archive, s.id, _src(tmp_path))
    # A discrete import shouldn't flip a closed session back to open.
    assert read_session_yaml(_find_session_dir(archive, s.id)).status == "closed"


def test_import_move_removes_source(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="host"); s.close()
    src = _src(tmp_path)
    manual.import_file(archive, s.id, src, move=True)
    assert not src.exists()


def test_import_duplicate_name_refused(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="host"); s.close()
    manual.import_file(archive, s.id, _src(tmp_path))
    with pytest.raises(FileExistsError):
        manual.import_file(archive, s.id, _src(tmp_path))


def test_import_as_renames(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="host"); s.close()
    dest = manual.import_file(archive, s.id, _src(tmp_path), dest_name="renamed.csv")
    assert dest.name == "renamed.csv"
    assert read_sidecar(dest).produced_by.source == "external"


# ---------------------------------------------------------------------
# frozen-session gate
# ---------------------------------------------------------------------

def test_import_into_previous_day_closed_is_refused(tmp_path):
    archive = tmp_path / "archive"
    run_id = _make_previous_day(archive)
    with pytest.raises(RuntimeError):
        manual.import_file(archive, run_id, _src(tmp_path))


def test_import_into_previous_day_with_allow_frozen(tmp_path):
    archive = tmp_path / "archive"
    run_id = _make_previous_day(archive)
    dest = manual.import_file(archive, run_id, _src(tmp_path), allow_frozen=True)
    assert dest.exists()


def test_import_into_held_previous_day_session(tmp_path):
    archive = tmp_path / "archive"
    run_id = _make_previous_day(archive)
    nebula.hold(archive, run_id)  # a hold makes it writable without --reopen
    dest = manual.import_file(archive, run_id, _src(tmp_path))
    assert dest.exists()


# ---------------------------------------------------------------------
# new session from files
# ---------------------------------------------------------------------

def test_import_new_creates_session_with_files(tmp_path):
    archive = tmp_path / "archive"
    a = _src(tmp_path, "a.csv")
    b = _src(tmp_path, "b.csv")
    s = manual.import_new(archive, [a, b], tags=["shared"], description="from coworker",
                          origin="emailed 2026-07-01")

    session_dir = _find_session_dir(archive, s.id)
    assert read_session_yaml(session_dir).status == "closed"
    for name in ("a.csv", "b.csv"):
        assert (session_dir / name).exists()
        assert read_sidecar(session_dir / name).produced_by.source == "external"
    hist = read_session_yaml(session_dir).history
    assert {h["file"] for h in hist} == {"a.csv", "b.csv"}


# ---------------------------------------------------------------------
# adopt / reconcile
# ---------------------------------------------------------------------

def test_find_orphan_files(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="host")
    with s.artifact("good.csv") as fn:
        fn.write_text("x")
    s.close()
    # File dragged into the already-closed session folder by hand (the case
    # the close-time audit never saw).
    (s.path / "dropped.csv").write_text("y")

    orphans = manual.find_orphan_files(archive)
    names = {p.name for p in orphans}
    assert "dropped.csv" in names
    assert "good.csv" not in names


def test_adopt_file_writes_sidecar_in_place(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="host")
    s.close()
    (s.path / "dropped.csv").write_text("payload\n")  # dropped in after close

    orphan = manual.find_orphan_files(archive)[0]
    manual.adopt_file(orphan, origin="found on the NAS")

    meta = read_sidecar(orphan)
    assert meta.produced_by.source == "external"
    assert meta.produced_by.origin == "found on the NAS"
    assert meta.extra.get("reconciled") is True
    assert meta.sha256 == sha256_file(orphan)
    # and it's no longer an orphan
    assert manual.find_orphan_files(archive) == []


def test_adopt_refuses_if_sidecar_exists(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="host")
    with s.artifact("good.csv") as fn:
        fn.write_text("x")
    s.close()
    with pytest.raises(FileExistsError):
        manual.adopt_file(s.path / "good.csv")


# ---------------------------------------------------------------------
# checksums flow through the normal script path too
# ---------------------------------------------------------------------

def test_script_artifacts_get_sha256(tmp_path):
    archive = tmp_path / "archive"
    with nebula.session(archive, new_session=True, description="host") as s:
        with s.artifact("data.csv") as fn:
            fn.write_text("measured\n")
        run_id = s.id
    session_dir = _find_session_dir(archive, run_id)
    meta = read_sidecar(session_dir / "data.csv")
    assert meta.sha256 == sha256_file(session_dir / "data.csv")


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def test_cli_import_and_reconcile(tmp_path, monkeypatch):
    from nebula import cli

    archive = tmp_path / "archive"
    s = nebula.new(archive, description="host"); s.close()

    cli.main(["import", str(archive), s.id, str(_src(tmp_path)), "--from", "cli test"])
    dest = _find_session_dir(archive, s.id) / "incoming.csv"
    assert read_sidecar(dest).produced_by.origin == "cli test"

    # Drop an orphan and reconcile it with the auto-stub choice.
    (_find_session_dir(archive, s.id) / "byhand.dat").write_text("z")
    monkeypatch.setattr("builtins.input", lambda prompt="": "A")
    cli.main(["reconcile", str(archive), s.id])
    meta = read_sidecar(_find_session_dir(archive, s.id) / "byhand.dat")
    assert meta.extra.get("reconciled") is True
