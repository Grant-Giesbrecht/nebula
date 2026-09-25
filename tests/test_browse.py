"""
`nebula browse` -- the interactive cd/ls-style shell.

Driven the same way test_session_select.py drives its picker: monkeypatch
is_interactive() to True and builtins.input() to replay scripted lines,
then assert on what landed on stdout.
"""

import builtins
import contextlib
import io

import pytest

import nebula
from nebula import assets, browse, collection


def _force_interactive(monkeypatch):
    monkeypatch.setattr(browse, "is_interactive", lambda: True)


def _feed_input(monkeypatch, lines):
    it = iter(lines)

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(builtins, "input", fake_input)


def _run(archive, monkeypatch, lines, *, start_archive=None, start_run_id=None):
    """Drive run_browse with `lines` (an "exit" is appended so the loop
    always terminates cleanly) and return everything it printed, stdout
    and stderr interleaved (errors/warnings go to stderr, same convention
    as the rest of the CLI -- see _termui.err/warn)."""
    _force_interactive(monkeypatch)
    _feed_input(monkeypatch, list(lines) + ["exit"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        browse.run_browse(start_archive=start_archive, start_run_id=start_run_id)
    return buf.getvalue()


@pytest.fixture
def populated_archive(tmp_path):
    root = tmp_path / "archive"
    s = nebula.new(root, description="a run", announce=False)
    with s.artifact("raw.csv", tags=["RP23D"]) as fn:
        fn.write_text("x")
    s.close()

    collection.create(root, "paper-2026", title="Paper 2026")
    collection.add(root, "paper-2026", f"{s.id}/raw.csv")

    calib = tmp_path / "calib.json"
    calib.write_text("{}")
    meta = assets.import_asset(root, str(calib), name="calib.json")

    return root, s.id, meta.id


# ---------------------------------------------------------------------
# non-interactive refusal
# ---------------------------------------------------------------------

def test_refuses_when_not_interactive(monkeypatch, tmp_path):
    monkeypatch.setattr(browse, "is_interactive", lambda: False)
    with pytest.raises(SystemExit) as exc:
        browse.run_browse()
    assert exc.value.code == 1


# ---------------------------------------------------------------------
# navigation
# ---------------------------------------------------------------------

def test_cd_into_archive_and_session_lists_artifacts(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", f"cd {run_id}", "ls"])
    assert "raw.csv" in out


def test_bare_enter_repeats_the_listing(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", ""])
    assert run_id in out


def test_multi_segment_cd_and_dotdot_chain(populated_archive, monkeypatch):
    root, run_id, asset_id = populated_archive
    out = _run(root, monkeypatch, [
        f"cd {root}",
        "cd collections/paper-2026",
        "pwd",
        "cd ../../assets",
        "pwd",
        f"cd {asset_id}",
        "pwd",
    ])
    assert "collections/paper-2026" in out
    lines = [l for l in out.splitlines() if l.startswith("/")]
    assert any(l.endswith("/assets") for l in lines)
    assert any(l.endswith(f"/assets/{asset_id}") for l in lines)


def test_bad_cd_leaves_cursor_unchanged(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [
        f"cd {root}", "pwd", "cd nonexistent/deeper", "pwd",
    ])
    pwds = [l for l in out.splitlines() if l.startswith("/") and l != "/"]
    assert len(pwds) == 2
    assert pwds[0] == pwds[1]
    assert "no such" in out


def test_cd_to_root_and_back(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", f"cd {run_id}", "cd /", "pwd"])
    assert out.strip().splitlines()[-1] == "/"


def test_cd_unknown_archive_reports_known_ones(tmp_path, monkeypatch):
    out = _run(tmp_path, monkeypatch, ["cd nope"])
    assert "no such archive" in out


# ---------------------------------------------------------------------
# show / info / uri
# ---------------------------------------------------------------------

def test_show_flags_inside_a_session(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", f"cd {run_id}", "show -u -t -l"])
    assert "uri:" in out
    assert "tags: RP23D" in out
    assert "sha256:" in out


def test_info_is_show_dash_l(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", f"cd {run_id}", "info raw.csv"])
    assert "sha256:" in out
    assert "uri:" in out
    assert "tags:" in out


def test_uri_command_prints_a_nebula_uri(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", f"cd {run_id}", "uri raw.csv"])
    assert "nebula://" in out
    assert "raw.csv" in out


def test_show_collection_prints_its_tree(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [
        f"cd {root}", "cd collections", "show paper-2026",
    ])
    assert "paper-2026" in out
    assert f"{run_id}/raw.csv" in out


def test_show_asset_prints_its_detail(populated_archive, monkeypatch):
    root, run_id, asset_id = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", "cd assets", f"show {asset_id}"])
    assert asset_id in out
    assert "calib.json" in out


# ---------------------------------------------------------------------
# annotate
# ---------------------------------------------------------------------

def test_annotate_add_tags(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [
        f"cd {root}", f"cd {run_id}", "annotate raw.csv --add-tags extra",
    ])
    assert "extra" in out

    from nebula import annotations
    from nebula.session import _find_session_dir

    session_dir = _find_session_dir(root, run_id)
    assert "extra" in annotations.get(session_dir, "raw.csv")["tags"]


def test_annotate_outside_a_session_is_refused(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, [f"cd {root}", "annotate raw.csv --add-tags x"])
    assert "annotate only works on a session" in out


# ---------------------------------------------------------------------
# open / reveal
# ---------------------------------------------------------------------

def test_open_and_reveal_are_mocked_not_launched(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    calls = []
    from nebula.navigator import osutil

    monkeypatch.setattr(osutil, "open_path", lambda p: calls.append(("open", p)) or True)
    monkeypatch.setattr(osutil, "reveal_path", lambda p: calls.append(("reveal", p)) or True)

    _run(root, monkeypatch, [f"cd {root}", f"cd {run_id}", "open raw.csv", "reveal raw.csv"])

    kinds = [c[0] for c in calls]
    assert kinds == ["open", "reveal"]
    assert all(str(p).endswith("raw.csv") for _, p in calls)


# ---------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------

def test_unknown_command(populated_archive, monkeypatch):
    out = _run(populated_archive[0], monkeypatch, ["bogus"])
    assert "unknown command" in out


def test_help_lists_commands(populated_archive, monkeypatch):
    out = _run(populated_archive[0], monkeypatch, ["help"])
    assert "cd <name>" in out
    assert "annotate <name>" in out


def test_start_archive_and_run_id_land_directly_in_the_session(populated_archive, monkeypatch):
    root, run_id, _ = populated_archive
    out = _run(root, monkeypatch, ["pwd"], start_archive=str(root), start_run_id=run_id)
    assert out.strip().splitlines()[-1].endswith(f"/{run_id}")
