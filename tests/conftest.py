"""
Shared test setup.

The important thing here is isolation from the developer's machine, in both
directions.

**Reading:** an archive that declares no `user:` falls back to the *local*
identity (`config.archive_identity`), so without this the suite reads
whatever is in ~/.nebula/identity.yaml -- results then depend on who is
running it, and a check that reports on owners passes on one laptop and
fails on another.

**Writing:** every one of nebula's machine-local files is redirected at a
temporary path, and `no_leaks_into_the_real_home` *checks* that none of
them changed anyway. That check is here because the redirection quietly
failed once: `Registry()` built with no path read `NEBULA_REGISTRY` in
`get_registry` but not in its own constructor, so a test that happened to
construct one directly wrote its throwaway archives into the developer's
real registry, where they then showed up in `nebula archives` forever. The
env vars are the intent; the guard is what makes the intent true.
"""

import hashlib
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_identity(tmp_path_factory, monkeypatch):
    """Pin the machine identity to a qualified test value.

    Qualified rather than bare on purpose: it is the state nebula now
    considers correct, so archives built by tests do not trip the advisory
    `unqualified_owner` check and bury real findings in noise.

    Individual tests that care about identity still override these -- a
    later monkeypatch in the test wins over this one.
    """
    monkeypatch.setenv("NEBULA_USER", "tester@example.edu")
    monkeypatch.setenv(
        "NEBULA_IDENTITY",
        str(tmp_path_factory.mktemp("identity") / "identity.yaml"))


#: The developer's own nebula state, as absolute paths resolved once at
#: import. Deliberately *not* read from nebula's own module constants: the
#: tests that exercise the registry's default-path logic monkeypatch those,
#: and a guard that followed the patch would be watching the temporary file
#: it was supposed to be checking against.
_REAL_CONFIG_DIR = Path(os.path.expanduser("~/.nebula"))
_REAL_HOME_DIR = Path(os.path.expanduser("~/nebula"))


def _snapshot(config_dir=None, home_dir=None):
    """What the developer's own nebula state looks like right now.

    Every file directly under ~/.nebula, by content, plus the top-level
    listing of ~/nebula. Enumerated rather than named one by one, so a
    machine-local file added to nebula later is covered without anyone
    remembering to add it here.

    Content, not mtime: a file rewritten with identical bytes has changed
    nothing the user would notice, and mtime alone would make the guard
    flap. Absence is recorded as None, so *creating* a file is caught too --
    that is the commonest leak.

    The directories are arguments rather than globals so the guard can bind
    them once and compare like with like. Reading a module global at
    teardown instead would let a test that monkeypatched one -- the
    registry's own default-path tests do exactly that -- move the goalposts
    between the two snapshots.
    """
    config_dir = Path(config_dir or _REAL_CONFIG_DIR)
    home_dir = Path(home_dir or _REAL_HOME_DIR)

    out = {}
    try:
        names = sorted(p.name for p in config_dir.iterdir() if p.is_file())
    except OSError:
        names = []
    out[f"{config_dir.name}/"] = names
    for name in names:
        try:
            out[f"{config_dir.name}/{name}"] = hashlib.sha256(
                (config_dir / name).read_bytes()).hexdigest()
        except OSError:
            out[f"{config_dir.name}/{name}"] = None
    try:
        out["~/nebula/"] = sorted(p.name for p in home_dir.iterdir())
    except OSError:
        out["~/nebula/"] = None
    return out


@pytest.fixture(autouse=True)
def no_leaks_into_the_real_home(request):
    """Fail the test that touches the developer's own nebula state.

    Per test rather than per session so the failure names the culprit --
    "something in the suite wrote to your registry" is a much worse bug
    report than "this test did". Cheap enough to be worth it: a handful of
    small files and two directory listings.
    """
    watched = (_REAL_CONFIG_DIR, _REAL_HOME_DIR)
    before = _snapshot(*watched)
    yield
    after = _snapshot(*watched)
    changed = sorted(k for k in set(before) | set(after)
                     if before.get(k) != after.get(k))
    assert not changed, (
        f"{request.node.name} modified the real nebula state: "
        f"{', '.join(changed)}. Tests must work entirely inside tmp_path -- "
        f"use get_registry() (which honours NEBULA_REGISTRY) rather than "
        f"paths under the home directory, and set the matching NEBULA_* "
        f"override for anything new.")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Point NEBULA_HOME and the contacts file somewhere throwaway.

    `registry.nebula_home()` defaults to ~/nebula, which `discover`,
    `fragments_root` and `plan_receive` all walk and write into; contacts
    default to ~/.nebula/contacts.yaml. Neither had an override set here,
    so a test exercising either reached the developer's own files.
    """
    home = tmp_path_factory.mktemp("nebula_home")
    monkeypatch.setenv("NEBULA_HOME", str(home))
    monkeypatch.setenv(
        "NEBULA_CONTACTS",
        str(tmp_path_factory.mktemp("contacts") / "contacts.yaml"))


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path_factory, monkeypatch):
    """Give each test its own archive registry.

    `registry._default_registry` is a process-wide cache, so without this a
    test that registers an archive leaks it into every test that runs
    after -- and the first one to load it would otherwise read the
    developer's real ~/.nebula/archives.yaml. Several tests already reset
    the global by hand; doing it here means they no longer have to, and
    tests that never thought about it stop being order-dependent.
    """
    from nebula import registry as registry_mod

    monkeypatch.setenv(
        "NEBULA_REGISTRY",
        str(tmp_path_factory.mktemp("registry") / "archives.yaml"))
    monkeypatch.setattr(registry_mod, "_default_registry", None)
    yield
    registry_mod._default_registry = None


@pytest.fixture(autouse=True)
def quiet_saves(monkeypatch):
    """Silence the per-artifact save report for the whole suite.

    `Session.artifact` prints what it saved -- URI, path, size, tags -- to
    stdout by default, which is the right behaviour for a measurement
    script and the wrong one inside a test that asserts on captured output.
    Turned off here rather than in each fixture that happens to write an
    artifact, because the tests that build one as *setup* are exactly the
    ones that would not think to.

    The announce tests set NEBULA_ANNOUNCE themselves; a later monkeypatch
    in a test wins over this one. This is also the knob a user wants in
    their own pytest suite, for the same reason.
    """
    monkeypatch.setenv("NEBULA_ANNOUNCE", "0")
