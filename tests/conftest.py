"""
Shared test setup.

The important thing here is isolation from the developer's machine. An
archive that declares no `user:` falls back to the *local* identity
(`config.archive_identity`), so without this the suite reads whatever is in
~/.nebula/identity.yaml -- results then depend on who is running it, and a
check that reports on owners passes on one laptop and fails on another.
"""

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
