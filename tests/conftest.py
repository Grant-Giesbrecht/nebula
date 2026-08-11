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
