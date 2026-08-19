"""`nebula intake --auto`: registers a freshly-created intake under one
fixed nickname (AUTO_INTAKE_NICKNAME) so a daily script can hardcode
ARCHIVE = "auto-intake" forever, instead of being re-edited every day to
name that day's timestamped intake folder.

Also covers the plain `nebula intake` path, which used to crash outright
with a NameError (an undefined `Fore`/`Style` reference left in a stray
confirmation prompt) before that dead code was removed here.
"""

import time

import pytest

import nebula
from nebula.cli import AUTO_INTAKE_NICKNAME, main
from nebula.registry import ArchiveNotInitialized, get_registry


def test_plain_intake_does_not_crash(tmp_path, capsys):
    # Regression: this used to NameError on Fore/Style with no colorama
    # import anywhere in the module -- every invocation failed.
    main(["intake", str(tmp_path)])
    out = capsys.readouterr().out
    assert "created intake archive" in out
    assert not get_registry().all()  # no --auto: nothing registered


def test_intake_auto_registers_fixed_nickname(tmp_path):
    main(["intake", str(tmp_path), "--auto"])
    cfg = get_registry().get(AUTO_INTAKE_NICKNAME)
    assert cfg.kind == "intake"  # not the register()-default "standard"
    assert cfg.available


def test_intake_auto_lets_scripts_use_fixed_name_unchanged(tmp_path):
    main(["intake", str(tmp_path), "--auto"])
    s = nebula.new(AUTO_INTAKE_NICKNAME, description="run 1")
    assert s.meta.run_id.startswith("I-")
    s.close()


def test_intake_auto_second_run_overwrites_pointer_and_warns(tmp_path, capsys):
    main(["intake", str(tmp_path), "--auto"])
    first_root = get_registry().get(AUTO_INTAKE_NICKNAME).root
    time.sleep(1.1)  # timestamp resolution is whole seconds
    main(["intake", str(tmp_path), "--auto"])
    second_root = get_registry().get(AUTO_INTAKE_NICKNAME).root

    assert second_root != first_root
    assert first_root.is_dir()  # overwriting the pointer touches no files
    err = capsys.readouterr().err
    assert "still exists on disk" in err


def test_intake_auto_after_merge_and_delete_fails_validation(tmp_path):
    # The daily workflow's step 6: merge today's intake into the standard
    # archive, then delete the now-empty intake folder. A script that
    # still says ARCHIVE = "auto-intake" without a fresh `--auto` run must
    # get a clear abort, not a silent write into a ghost directory.
    main(["intake", str(tmp_path), "--auto"])
    root = get_registry().get(AUTO_INTAKE_NICKNAME).root

    import shutil
    shutil.rmtree(root)  # stand-in for merge + delete

    with pytest.raises(ArchiveNotInitialized):
        nebula.validate_archive(AUTO_INTAKE_NICKNAME)


def test_intake_auto_reused_same_second_reports_cleanly(tmp_path, capsys):
    # transfer.new_intake()'s name has second resolution, so calling
    # --auto twice within the same second collides on disk. That used to
    # be an uncaught TransferError traceback; it should exit(1) cleanly.
    main(["intake", str(tmp_path), "--auto"])
    with pytest.raises(SystemExit) as exc:
        main(["intake", str(tmp_path), "--auto"])
    assert exc.value.code == 1
    assert "already exists" in capsys.readouterr().err
