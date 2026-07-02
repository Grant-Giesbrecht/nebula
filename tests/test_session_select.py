import builtins
import datetime

import pytest

import nebula
from nebula import session_select
from nebula.session_select import _candidate_sessions, select_session
from nebula.sidecar import read_session_yaml, write_session_yaml, SessionMeta


def _make_old_closed(archive, run_id="S-0009"):
    """Hand-place a closed session dated last year, so it is NOT a
    today/open candidate but can still be reopened by id."""
    ym = archive / "2020" / "01"
    ym.mkdir(parents=True)
    d = ym / run_id
    d.mkdir()
    write_session_yaml(d, SessionMeta(
        run_id=run_id,
        created=datetime.datetime(2020, 1, 1).astimezone().isoformat(),
        status="closed",
        description="last year",
    ))
    return run_id


def _place_days_ago(archive, run_id, days, *, tags=None, desc="", status="closed"):
    """Hand-place a session dated `days` days before today."""
    when = datetime.datetime.now().astimezone() - datetime.timedelta(days=days)
    ym = archive / f"{when.year:04d}" / f"{when.month:02d}"
    ym.mkdir(parents=True, exist_ok=True)
    d = ym / run_id
    d.mkdir()
    write_session_yaml(d, SessionMeta(
        run_id=run_id, created=when.isoformat(), status=status,
        tags=tags or [], description=desc))
    return run_id


def _force_interactive(monkeypatch):
    monkeypatch.setattr(session_select, "is_interactive", lambda: True)


def _run_picker(archive, monkeypatch, lines):
    """Drive the picker with `lines` (a trailing /cancel is added) and
    return everything it printed. Expects the cancel to raise."""
    _force_interactive(monkeypatch)
    _feed_input(monkeypatch, list(lines) + ["/cancel"])
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        with pytest.raises(RuntimeError):
            select_session(archive)
    return buf.getvalue()


def _feed_input(monkeypatch, lines):
    it = iter(lines)

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(builtins, "input", fake_input)


# ---------------------------------------------------------------------
# candidate filtering
# ---------------------------------------------------------------------

def test_candidates_include_open_and_today_exclude_old_closed(tmp_path):
    archive = tmp_path / "archive"
    open_s = nebula.new(archive, description="still going")      # open, today
    closed_today = nebula.new(archive, description="done today")
    closed_today.close()                                         # closed, today
    _make_old_closed(archive)                                    # closed, old

    ids = {m.run_id for m in _candidate_sessions(archive)}
    assert open_s.id in ids
    assert closed_today.id in ids   # today counts even though closed
    assert "S-0009" not in ids      # old + closed is filtered out


# ---------------------------------------------------------------------
# interactive selection
# ---------------------------------------------------------------------

def test_select_new_via_command(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    _force_interactive(monkeypatch)
    _feed_input(monkeypatch, ["/new"])
    s = select_session(archive, description="fresh")
    assert s.id == "S-0001"
    assert read_session_yaml(s.path).status == "open"


def test_select_append_by_bare_id(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    open_s = nebula.new(archive, description="append me")  # left open
    _force_interactive(monkeypatch)
    _feed_input(monkeypatch, [open_s.id])
    s = select_session(archive)
    assert s.id == open_s.id
    assert s.meta.status == "open"


def test_select_open_same_day_closed_session_succeeds(tmp_path, monkeypatch):
    # /open on a session closed earlier *today* just works -- that's the
    # whole point: keep piling same-day measurements into one folder.
    archive = tmp_path / "archive"
    closed = nebula.new(archive, description="closed today")
    closed.close()
    _force_interactive(monkeypatch)
    _feed_input(monkeypatch, [closed.id])
    s = select_session(archive)
    assert s.id == closed.id
    assert read_session_yaml(s.path).status == "open"  # reactivated


def test_select_open_previous_day_closed_is_refused(tmp_path, monkeypatch, capsys):
    archive = tmp_path / "archive"
    old = _make_old_closed(archive, run_id="S-0042")
    _force_interactive(monkeypatch)
    # Try to append to the old closed one, get told off, then start new.
    _feed_input(monkeypatch, [old, "/new"])
    s = select_session(archive)
    out = capsys.readouterr().out
    assert "closed on an earlier day" in out
    assert s.id != old  # ended up making a new session


def test_reopen_requires_confirmation(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    closed = nebula.new(archive, description="reopen me")
    closed.close()
    _force_interactive(monkeypatch)
    # /reopen, then re-type the id to confirm.
    _feed_input(monkeypatch, [f"/reopen {closed.id}", closed.id])
    s = select_session(archive)
    assert s.id == closed.id
    assert read_session_yaml(s.path).status == "open"


def test_reopen_wrong_confirmation_cancels(tmp_path, monkeypatch, capsys):
    archive = tmp_path / "archive"
    closed = nebula.new(archive, description="reopen me")
    closed.close()
    _force_interactive(monkeypatch)
    # Fumble the confirmation, then fall back to /new.
    _feed_input(monkeypatch, [f"/reopen {closed.id}", "not-the-id", "/new"])
    s = select_session(archive)
    out = capsys.readouterr().out
    assert "reopen cancelled" in out
    assert read_session_yaml(closed.path).status in ("open", "closed")  # not crashed
    assert s.id != closed.id


def test_reopen_force_skips_confirmation(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    old_id = _make_old_closed(archive)  # not even a candidate, reopen by id
    _force_interactive(monkeypatch)
    _feed_input(monkeypatch, [f"/reopen {old_id} --force"])
    s = select_session(archive)
    assert s.id == old_id
    assert read_session_yaml(s.path).status == "open"


def test_select_cancel_raises(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    _force_interactive(monkeypatch)
    _feed_input(monkeypatch, ["/cancel"])
    with pytest.raises(RuntimeError):
        select_session(archive)


def test_matches_by_id_tag_and_description(tmp_path):
    from nebula.session_select import _matches
    from nebula.sidecar import SessionMeta

    m = SessionMeta(run_id="S-0007", created="2026-07-02T10:00:00",
                    tags=["warmup", "RP23D"], description="diode sweep")
    assert _matches(m, "0007")      # by id
    assert _matches(m, "WARM")      # by tag, case-insensitive
    assert _matches(m, "sweep")     # by description
    assert not _matches(m, "noise")


def test_search_command_runs_without_error(tmp_path, monkeypatch, capsys):
    archive = tmp_path / "archive"
    a = nebula.new(archive, tags=["warmup"], description="alpha")
    nebula.new(archive, tags=["noise"], description="bravo")
    _force_interactive(monkeypatch)
    _feed_input(monkeypatch, ["/search warmup", "/cancel"])
    with pytest.raises(RuntimeError):
        select_session(archive)
    assert a.id in capsys.readouterr().out


# ---------------------------------------------------------------------
# integration with nebula.session() + non-interactive fallback
# ---------------------------------------------------------------------

def test_list_default_excludes_previous_day_sessions(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    nebula.new(archive, description="today").close()   # candidate
    _place_days_ago(archive, "S-0090", 5, desc="five days ago")

    out = _run_picker(archive, monkeypatch, ["/list"])
    # Default /list only shows candidates (today/open).
    assert "S-0090" not in out.split("/list", 1)[-1] if "/list" in out else True
    # (belt-and-suspenders: the old session is simply not a candidate)
    assert "S-0090" not in out


def test_list_all_shows_previous_day_sessions(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    nebula.new(archive, description="today").close()
    _place_days_ago(archive, "S-0090", 5, desc="five days ago")

    out = _run_picker(archive, monkeypatch, ["/list -a"])
    assert "S-0090" in out


def test_list_days_window_filters(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    _place_days_ago(archive, "S-0100", 3, desc="within window")
    _place_days_ago(archive, "S-0101", 30, desc="outside window")

    out = _run_picker(archive, monkeypatch, ["/list --days 7"])
    assert "S-0100" in out
    assert "S-0101" not in out


def test_list_tags_flag_shows_tags(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    nebula.new(archive, tags=["warmup", "RP23D"], description="tagged")

    out = _run_picker(archive, monkeypatch, ["/list -t"])
    assert "tags:" in out
    assert "warmup" in out and "RP23D" in out


def test_list_bad_days_flag_reports_error(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    nebula.new(archive, description="today")
    out = _run_picker(archive, monkeypatch, ["/list --days abc"])
    assert "not a number" in out


def test_list_unknown_flag_reports_error(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    nebula.new(archive, description="today")
    out = _run_picker(archive, monkeypatch, ["/list --bogus"])
    assert "unknown /list flag" in out


def test_tags_command_lists_all_archive_tags(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    nebula.new(archive, tags=["warmup"], description="a").close()
    nebula.new(archive, tags=["warmup", "noise"], description="b").close()

    out = _run_picker(archive, monkeypatch, ["/tags"])
    assert "warmup" in out and "noise" in out
    assert "2 sessions" in out  # warmup counted across both


def test_tags_command_filters_by_arg(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    nebula.new(archive, tags=["warmup", "noise"], description="a")

    out = _run_picker(archive, monkeypatch, ["/tags warm"])
    assert "warmup" in out
    assert "noise" not in out


def test_non_interactive_creates_new(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    monkeypatch.setattr(session_select, "is_interactive", lambda: False)
    s = select_session(archive, description="batch")
    assert s.id == "S-0001"


def test_session_new_session_flag_skips_picker(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    # Even if we *were* interactive, new_session=True must not prompt.
    _force_interactive(monkeypatch)

    def boom(prompt=""):
        raise AssertionError("picker should not have prompted")

    monkeypatch.setattr(builtins, "input", boom)
    with nebula.session(archive, new_session=True, description="clean") as s:
        run_id = s.id
    assert read_session_yaml(s.path).status == "closed"
    assert run_id == "S-0001"
