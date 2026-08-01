"""Archive layout and session id format.

    <archive>/archive.yaml
    <archive>/index.db
    <archive>/code/
    <archive>/data/<year>/S-<yy>-<nnnn>/
"""

import datetime
import subprocess
import sys
from pathlib import Path

import pytest

import nebula
from nebula import index
from nebula.session import _ID_RE, id_year, resolve_run_id
from nebula.sidecar import SessionMeta, write_session_yaml


def _new(archive, **kw):
    s = nebula.new(archive, description=kw.pop("description", "t"), **kw)
    with s.artifact("d.csv") as fn:
        fn.write_text("1")
    s.close()
    return s


# ---------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------

def test_sessions_live_under_data_year(tmp_path):
    archive = tmp_path / "archive"
    s = _new(archive)
    year = datetime.date.today().year
    assert s.path == archive / "data" / str(year) / s.id
    assert s.path.is_dir()


def test_no_month_nesting(tmp_path):
    archive = tmp_path / "archive"
    s = _new(archive)
    year_dir = archive / "data" / str(datetime.date.today().year)
    assert [p.name for p in year_dir.iterdir()] == [s.id]


def test_archive_root_holds_only_known_entries(tmp_path):
    archive = tmp_path / "archive"
    _new(archive)
    index.rebuild(archive)
    names = {p.name for p in archive.iterdir() if not p.name.startswith(".")}
    assert names <= {"data", "code", "index.db", "archive.yaml"}
    assert "data" in names and "code" in names


def test_code_store_is_not_hidden(tmp_path):
    archive = tmp_path / "archive"
    _new(archive)
    assert (archive / "code").is_dir()
    assert not (archive / ".code").exists()


def test_walkers_ignore_non_session_dirs_at_root(tmp_path):
    """code/ sits next to data/ and is not hidden, so the session walk must
    not descend it (correctness *and* speed as the store grows)."""
    archive = tmp_path / "archive"
    s = _new(archive)
    (archive / "code" / "blobs" / "aa" / "bb").mkdir(parents=True, exist_ok=True)
    (archive / "notes").mkdir()

    from nebula import check as check_mod
    from nebula.navigator import model

    assert [d.name for d in index._iter_session_dirs(archive)] == [s.id]
    assert [d.name for d in check_mod._iter_all_session_dirs(archive)] == [s.id]
    assert [x.run_id for x in model.list_sessions(archive)] == [s.id]
    assert check_mod.check(archive) == []          # code/ is not an integrity problem


def test_index_rebuild_finds_sessions_in_new_layout(tmp_path):
    archive = tmp_path / "archive"
    s = _new(archive)
    index.rebuild(archive)
    conn = index.open_index(archive)
    rows = [r["run_id"] for r in conn.execute("SELECT run_id FROM sessions")]
    conn.close()
    assert rows == [s.id]


# ---------------------------------------------------------------------
# ids
# ---------------------------------------------------------------------

def test_id_format_carries_the_year(tmp_path):
    archive = tmp_path / "archive"
    s = _new(archive)
    yy = datetime.date.today().year % 100
    assert s.id == f"S-{yy:02d}-0001"
    assert _ID_RE.match(s.id)
    assert id_year(s.id) == datetime.date.today().year


def test_ids_increment_within_a_year(tmp_path):
    archive = tmp_path / "archive"
    first, second, third = _new(archive), _new(archive), _new(archive)
    assert [s.id[-4:] for s in (first, second, third)] == ["0001", "0002", "0003"]


def test_numbering_restarts_each_year(tmp_path):
    """Ids are per-year, which is what makes a bare number resolvable."""
    archive = tmp_path / "archive"
    old_year = archive / "data" / "2020"
    old_year.mkdir(parents=True)
    for n in (1, 2, 3):
        d = old_year / f"S-20-{n:04d}"
        d.mkdir()
        write_session_yaml(d, SessionMeta(
            run_id=d.name, created="2020-01-01T00:00:00+00:00", status="closed"))

    s = _new(archive)                       # this year starts fresh at 0001
    assert s.id.endswith("-0001")
    assert id_year(s.id) == datetime.date.today().year


def test_find_session_dir_works_across_years(tmp_path):
    archive = tmp_path / "archive"
    old_year = archive / "data" / "2020"
    old_year.mkdir(parents=True)
    d = old_year / "S-20-0007"
    d.mkdir()
    write_session_yaml(d, SessionMeta(
        run_id="S-20-0007", created="2020-01-01T00:00:00+00:00", status="closed"))
    this = _new(archive)

    from nebula.session import _find_session_dir
    assert _find_session_dir(archive, "S-20-0007") == d
    assert _find_session_dir(archive, this.id) == this.path
    with pytest.raises(FileNotFoundError):
        _find_session_dir(archive, "S-20-9999")


# ---------------------------------------------------------------------
# shorthand resolution
# ---------------------------------------------------------------------

NOW = datetime.datetime(2026, 3, 4).astimezone()


@pytest.mark.parametrize("text,expected", [
    ("S-26-0012", "S-26-0012"),
    ("s-26-0012", "S-26-0012"),      # case-insensitive
    ("  S-26-0012  ", "S-26-0012"),  # whitespace
    ("26-0012", "S-26-0012"),        # year-qualified, no prefix
    ("0012", "S-26-0012"),           # bare number -> current year
    ("12", "S-26-0012"),             # unpadded
    ("S-23-0001", "S-23-0001"),      # a different year stays put
])
def test_resolve_run_id(text, expected):
    assert resolve_run_id(text, now=NOW) == expected


@pytest.mark.parametrize("bad", ["", "   ", "banana", "S-0012", "a|b", "S-26-"])
def test_resolve_run_id_rejects_junk(bad):
    with pytest.raises(ValueError):
        resolve_run_id(bad, now=NOW)


def test_old_style_id_is_rejected_not_mangled():
    """The pre-year format must fail loudly rather than resolve to
    something plausible-looking."""
    with pytest.raises(ValueError):
        resolve_run_id("S-0012", now=NOW)


# ---------------------------------------------------------------------
# CLI shorthand
# ---------------------------------------------------------------------

def _nebula(*args):
    return subprocess.run([sys.executable, "-m", "nebula.cli", *args],
                          capture_output=True, text=True)


def test_cli_accepts_bare_number(tmp_path):
    archive = tmp_path / "archive"
    s = _new(archive)
    index.rebuild(archive)

    full = _nebula("show", str(archive), s.id)
    short = _nebula("show", str(archive), s.id[-4:])
    assert full.returncode == 0, full.stderr
    assert short.returncode == 0, short.stderr
    assert short.stdout == full.stdout


def test_cli_rejects_an_unparseable_id(tmp_path):
    res = _nebula("show", str(tmp_path), "banana")
    assert res.returncode != 0
    assert "not a session id" in res.stderr
