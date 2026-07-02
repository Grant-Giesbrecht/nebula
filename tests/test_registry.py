from pathlib import Path

import pytest

from nebula.registry import Registry


def test_register_and_get(tmp_path):
    reg = Registry(path=tmp_path / "archives.yaml")
    reg.register("postdoc", tmp_path / "postdoc-data", git_org="grant-nist")

    cfg = reg.get("postdoc")
    assert cfg.name == "postdoc"
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
