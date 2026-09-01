from pathlib import Path

import pytest

from nebula.registry import Registry


def test_register_and_get(tmp_path):
    reg = Registry(path=tmp_path / "archives.yaml")
    reg.register("postdoc", tmp_path / "postdoc-data", git_org="grant-nist")

    cfg = reg.get("postdoc")
    assert cfg.nickname == "postdoc"
    assert cfg.root == tmp_path / "postdoc-data"
    assert cfg.git_org == "grant-nist"


def test_get_unknown_raises(tmp_path):
    reg = Registry(path=tmp_path / "archives.yaml")
    with pytest.raises(KeyError):
        reg.get("nonexistent")


def test_try_get_unknown_returns_none(tmp_path):
    reg = Registry(path=tmp_path / "archives.yaml")
    assert reg.try_get("nonexistent") is None


def test_persists_across_instances(tmp_path):
    path = tmp_path / "archives.yaml"
    reg1 = Registry(path=path)
    reg1.register("audio", tmp_path / "audio-data")

    reg2 = Registry(path=path)
    cfg = reg2.get("audio")
    assert cfg.root == tmp_path / "audio-data"


def test_all_returns_copy(tmp_path):
    reg = Registry(path=tmp_path / "archives.yaml")
    reg.register("postdoc", tmp_path / "postdoc-data")
    archives = reg.all()
    archives["postdoc_hack"] = None  # mutating the returned dict...
    assert "postdoc_hack" not in reg.all()  # ...should not affect the registry


def test_missing_registry_file_is_not_an_error(tmp_path):
    reg = Registry(path=tmp_path / "does_not_exist.yaml")
    assert reg.all() == {}
    assert reg.try_get("anything") is None


# ---------------------------------------------------------------------
# Isolation from the developer's own machine
# ---------------------------------------------------------------------

def _reg():
    from nebula.registry import get_registry

    return get_registry()


def test_a_bare_registry_honours_the_env_override(tmp_path, monkeypatch):
    """The bug that leaked throwaway test archives into a real registry.

    `NEBULA_REGISTRY` was read by `get_registry` but not by `Registry`'s own
    constructor, so any code path building one directly reached past the
    override and wrote to ~/.nebula/registry.yaml.
    """
    from nebula import registry as registry_mod

    target = tmp_path / "elsewhere.yaml"
    monkeypatch.setenv("NEBULA_REGISTRY", str(target))
    assert registry_mod.Registry().path == target
    assert registry_mod.default_registry_path() == target
    assert registry_mod.get_registry().path == target


def test_the_override_is_expanded(tmp_path, monkeypatch):
    from nebula import registry as registry_mod

    monkeypatch.setenv("NEBULA_REGISTRY", "~/somewhere/registry.yaml")
    assert "~" not in str(registry_mod.Registry().path)


def test_an_empty_override_falls_back_to_the_default(tmp_path, monkeypatch):
    """An env var set to nothing is not an instruction to write to ''."""
    from nebula import registry as registry_mod

    monkeypatch.setenv("NEBULA_REGISTRY", "   ")
    monkeypatch.setattr(registry_mod, "DEFAULT_REGISTRY_PATH",
                        tmp_path / "registry.yaml")
    monkeypatch.setattr(registry_mod, "LEGACY_REGISTRY_PATH",
                        tmp_path / "archives.yaml")
    assert registry_mod.Registry().path == tmp_path / "registry.yaml"


def test_registering_never_touches_the_real_registry(tmp_path):
    """What the suite-wide guard checks, asserted directly once so the
    intent is visible in the tests rather than only in conftest."""
    from nebula import registry as registry_mod
    from nebula import transfer

    root = tmp_path / "arc"
    transfer.init_archive(root, name="arc")
    _reg().register_archive(root)

    assert _reg().path != registry_mod.DEFAULT_REGISTRY_PATH
    assert "arc" in _reg().all()


def test_the_leak_guard_notices_a_change(tmp_path):
    """The guard itself, exercised without touching the real home.

    A check that can only fail by actually damaging the developer's files
    is a check nobody can test, so `_snapshot` is pointed at a scratch
    directory and shown to notice a file appearing, changing and going
    away.
    """
    import conftest

    watched = tmp_path / "config"
    watched.mkdir()
    absent = tmp_path / "absent"
    snap = lambda: conftest._snapshot(watched, absent)

    empty = snap()

    (watched / "registry.yaml").write_text("postdoc:\n  root: /x\n")
    created = snap()
    assert created != empty

    (watched / "registry.yaml").write_text("postdoc:\n  root: /y\n")
    edited = snap()
    assert edited != created

    (watched / "registry.yaml").unlink()
    assert snap() == empty


def test_the_leak_guard_ignores_an_identical_rewrite(tmp_path):
    """Content, not mtime: rewriting a file with the same bytes has changed
    nothing a user would notice, and flagging it would make the guard flap."""
    import conftest

    watched = tmp_path / "config"
    watched.mkdir()
    absent = tmp_path / "absent"

    (watched / "identity.yaml").write_text("user: g@x.edu\n")
    before = conftest._snapshot(watched, absent)
    (watched / "identity.yaml").write_text("user: g@x.edu\n")
    assert conftest._snapshot(watched, absent) == before
