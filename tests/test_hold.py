import datetime

import pytest

import nebula
from nebula.session import parse_duration, _hold_active, _find_session_dir, HOLD_FOREVER
from nebula.sidecar import read_session_yaml, write_session_yaml, SessionMeta


def _make_previous_day(archive, run_id="S-0007", *, status="closed"):
    """Hand-place a session dated well before today."""
    ym = archive / "2020" / "01"
    ym.mkdir(parents=True, exist_ok=True)
    d = ym / run_id
    d.mkdir()
    write_session_yaml(d, SessionMeta(
        run_id=run_id,
        created=datetime.datetime(2020, 1, 1).astimezone().isoformat(),
        status=status,
        description="last year",
    ))
    return run_id


# ---------------------------------------------------------------------
# duration parsing
# ---------------------------------------------------------------------

@pytest.mark.parametrize("text,seconds", [
    ("2h", 7200),
    ("90m", 5400),
    ("45s", 45),
    ("1d", 86400),
    ("1.5h", 5400),
    ("30", 30),  # bare number = seconds
])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


def test_parse_duration_bad():
    with pytest.raises(ValueError):
        parse_duration("soon")


# ---------------------------------------------------------------------
# hold / release state
# ---------------------------------------------------------------------

def test_hold_indefinite_sets_forever(tmp_path):
    archive = tmp_path / "archive"
    run_id = _make_previous_day(archive)
    value = nebula.hold(archive, run_id)
    assert value == HOLD_FOREVER
    assert read_session_yaml(_dir(archive, run_id)).hold_until == HOLD_FOREVER


def test_hold_timed_sets_future_timestamp(tmp_path):
    archive = tmp_path / "archive"
    run_id = _make_previous_day(archive)
    nebula.hold(archive, run_id, seconds=3600)
    meta = read_session_yaml(_dir(archive, run_id))
    assert _hold_active(meta)
    when = datetime.datetime.fromisoformat(meta.hold_until)
    assert when > datetime.datetime.now().astimezone()


def test_release_clears_hold(tmp_path):
    archive = tmp_path / "archive"
    run_id = _make_previous_day(archive)
    nebula.hold(archive, run_id)
    assert nebula.release(archive, run_id) is True     # had a hold
    assert read_session_yaml(_dir(archive, run_id)).hold_until is None
    assert nebula.release(archive, run_id) is False    # nothing left to clear


def test_expired_hold_is_inactive(tmp_path):
    archive = tmp_path / "archive"
    run_id = _make_previous_day(archive)
    d = _dir(archive, run_id)
    meta = read_session_yaml(d)
    past = datetime.datetime.now().astimezone() - datetime.timedelta(hours=1)
    meta.hold_until = past.isoformat()
    write_session_yaml(d, meta)
    assert not _hold_active(read_session_yaml(d))


# ---------------------------------------------------------------------
# the point of it all: a held previous-day session stays appendable
# ---------------------------------------------------------------------

def test_previous_day_closed_is_frozen_without_hold(tmp_path):
    archive = tmp_path / "archive"
    run_id = _make_previous_day(archive)
    with pytest.raises(RuntimeError):
        nebula.append_to(archive, run_id)


def test_held_previous_day_session_is_appendable(tmp_path):
    archive = tmp_path / "archive"
    run_id = _make_previous_day(archive)
    nebula.hold(archive, run_id)                 # place the hold
    s = nebula.append_to(archive, run_id)        # now allowed despite age
    assert s.id == run_id
    assert s.meta.status == "open"               # reactivated
    s.close()
    # Hold survives the close, so the next script can append again.
    assert read_session_yaml(_dir(archive, run_id)).hold_until == HOLD_FOREVER
    s2 = nebula.append_to(archive, run_id)
    assert s2.id == run_id
    s2.close()


def test_expired_hold_does_not_unfreeze(tmp_path):
    archive = tmp_path / "archive"
    run_id = _make_previous_day(archive)
    nebula.hold(archive, run_id, seconds=-1)     # already in the past
    with pytest.raises(RuntimeError):
        nebula.append_to(archive, run_id)


def test_held_session_is_a_picker_candidate(tmp_path):
    from nebula.session_select import _candidate_sessions

    archive = tmp_path / "archive"
    run_id = _make_previous_day(archive)
    # Not a candidate before the hold...
    assert run_id not in {m.run_id for m in _candidate_sessions(archive)}
    nebula.hold(archive, run_id)
    # ...but is once held.
    assert run_id in {m.run_id for m in _candidate_sessions(archive)}


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def test_cli_hold_then_release(tmp_path):
    from nebula import cli

    archive = tmp_path / "archive"
    run_id = _make_previous_day(archive)

    cli.main(["hold", str(archive), run_id, "2h"])
    assert _hold_active(read_session_yaml(_dir(archive, run_id)))

    # The held session is now appendable despite being a previous day.
    nebula.append_to(archive, run_id).close()

    cli.main(["release", str(archive), run_id])
    assert read_session_yaml(_dir(archive, run_id)).hold_until is None


def test_cli_close_is_release_alias(tmp_path):
    from nebula import cli

    archive = tmp_path / "archive"
    run_id = _make_previous_day(archive)
    nebula.hold(archive, run_id)
    cli.main(["close", str(archive), run_id])  # alias for release
    assert read_session_yaml(_dir(archive, run_id)).hold_until is None


def test_cli_hold_bad_duration_exits(tmp_path):
    from nebula import cli

    archive = tmp_path / "archive"
    run_id = _make_previous_day(archive)
    with pytest.raises(SystemExit):
        cli.main(["hold", str(archive), run_id, "soon"])


def _dir(archive, run_id):
    return _find_session_dir(archive, run_id)
