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


# ---------------------------------------------------------------------
# Names a user can actually type
# ---------------------------------------------------------------------
# `nebula archives` prints the name each archive *declares*, because that
# is the portable one. Until this worked, no command accepted it: you read
# "intake_name" and got back "unknown archive 'intake_name'. Known
# archives: ['nebula_reg_name']" -- a machine telling somebody the word it
# had just printed is not a word.

def _registered(tmp_path, folder, *, name, user=None, key=None):
    from nebula import transfer

    root = tmp_path / folder
    transfer.init_archive(root, name=name, user=user)
    _reg().register_archive(root, key=key)
    return root


def test_an_archive_answers_to_its_declared_name(tmp_path):
    root = _registered(tmp_path, "folder", name="declared", key="a-nickname")
    reg = _reg()
    assert reg.get("declared").root == root
    assert reg.get("a-nickname").root == root
    assert reg.resolve_one("declared").nickname == "a-nickname"


def test_an_archive_answers_to_its_id(tmp_path):
    from nebula.config import read_settings

    root = _registered(tmp_path, "folder", name="declared", key="a-nickname")
    ident = read_settings(root, apply_env=False).id
    assert _reg().resolve_one(ident).root == root


def test_an_exact_nickname_wins_over_a_declared_name(tmp_path):
    """The nickname is the file's unique key, so typing one has to mean
    exactly that entry -- it is the escape hatch that makes two archives
    sharing a declared name separable at all."""
    # `a` *declares* "shared"; `b` is *filed under* "shared".
    a = _registered(tmp_path, "a", name="shared", user="me@here.edu",
                    key="mine")
    b = _registered(tmp_path, "b", name="other", user="me@here.edu",
                    key="shared")
    assert _reg().resolve_one("shared").root == b      # the key wins
    assert _reg().resolve_one("mine").root == a
    assert _reg().resolve_one("other").root == b


def test_a_name_naming_two_archives_is_refused_not_guessed(tmp_path):
    """Picking one silently is how somebody deletes the wrong entry."""
    _registered(tmp_path, "a", name="shared", user="me@here.edu",
                key="mine")
    _registered(tmp_path, "b", name="shared", user="jane@lab.edu",
                key="theirs")
    with pytest.raises(KeyError, match="different archives"):
        _reg().resolve_one("shared")


def test_several_nicknames_for_one_archive_are_not_ambiguous(tmp_path):
    """Aliases are several doors into one room, so the first is as good as
    any -- unlike two rooms."""
    root = _registered(tmp_path, "folder", name="declared", key="first")
    _reg().register_archive(root, key="second")
    assert _reg().resolve_one("declared").root == root


def test_an_unknown_name_lists_the_names_that_would_have_worked(tmp_path):
    _registered(tmp_path, "folder", name="declared", key="a-nickname")
    with pytest.raises(KeyError) as exc:
        _reg().resolve_one("nonsense")
    message = str(exc.value)
    assert "declared" in message and "a-nickname" in message


def test_unregister_takes_the_declared_name(tmp_path):
    """The reported bug: --remove only accepted the registry's own key."""
    _registered(tmp_path, "folder", name="declared", key="a-nickname")
    removed = _reg().unregister("declared")
    assert removed.nickname == "a-nickname"
    assert _reg().all() == {}


def test_unregistering_forgets_every_alias_for_that_archive(tmp_path):
    """Removing one door leaves the others open, so --remove appears to do
    nothing -- the archive is still listed afterwards."""
    root = _registered(tmp_path, "folder", name="declared", key="first")
    _reg().register_archive(root, key="second")
    assert len(_reg().all()) == 2

    gone = _reg().unregister_all("declared")
    assert sorted(c.nickname for c in gone) == ["first", "second"]
    assert _reg().all() == {}


def test_unregistering_leaves_a_different_archive_alone(tmp_path):
    a = _registered(tmp_path, "a", name="alpha", key="alpha")
    b = _registered(tmp_path, "b", name="beta", key="beta")
    _reg().unregister_all("alpha")
    assert list(_reg().all()) == ["beta"]
    assert b.is_dir() and a.is_dir()        # files are never touched


def test_try_get_accepts_exactly_what_get_does(tmp_path):
    """The two diverging is the shape of bug that let `nebula archives`
    print a name every other command rejected."""
    _registered(tmp_path, "folder", name="declared", key="a-nickname")
    reg = _reg()
    for name in ("declared", "a-nickname"):
        assert reg.try_get(name) is not None
        assert reg.get(name).nickname == "a-nickname"
    assert reg.try_get("nonsense") is None


def test_try_get_is_quiet_about_an_ambiguous_name(tmp_path):
    """A caller who wanted to be told would have used get()."""
    _registered(tmp_path, "a", name="shared", user="me@here.edu", key="mine")
    _registered(tmp_path, "b", name="shared", user="jane@lab.edu", key="theirs")
    assert _reg().try_get("shared") is None
    with pytest.raises(KeyError):
        _reg().get("shared")


# ---------------------------------------------------------------------
# default archive (A!)
# ---------------------------------------------------------------------

def test_default_nickname_starts_unset(tmp_path):
    reg = Registry(path=tmp_path / "archives.yaml")
    assert reg.default_nickname() is None


def test_set_default_and_read_it_back(tmp_path):
    reg = Registry(path=tmp_path / "archives.yaml")
    reg.register("postdoc", tmp_path / "postdoc-data")
    reg.set_default("postdoc")
    assert reg.default_nickname() == "postdoc"


def test_default_persists_across_instances(tmp_path):
    path = tmp_path / "archives.yaml"
    reg1 = Registry(path=path)
    reg1.register("postdoc", tmp_path / "postdoc-data")
    reg1.set_default("postdoc")

    reg2 = Registry(path=path)
    assert reg2.default_nickname() == "postdoc"


def test_default_survives_alongside_other_archives(tmp_path):
    """The reserved __default__ key must not be mistaken for an archive
    entry by _load(), and must not show up in all()/lookup()."""
    reg = Registry(path=tmp_path / "archives.yaml")
    reg.register("postdoc", tmp_path / "postdoc-data")
    reg.register("other", tmp_path / "other-data")
    reg.set_default("other")

    reg2 = Registry(path=tmp_path / "archives.yaml")
    assert set(reg2.all()) == {"postdoc", "other"}
    assert reg2.default_nickname() == "other"


def test_set_default_normalizes_to_the_real_nickname(tmp_path):
    """Passing a declared name (or anything resolve_one accepts) stores
    the nickname A! actually looks up by, not whatever text was typed."""
    reg = Registry(path=tmp_path / "archives.yaml")
    reg.register("nick", tmp_path / "data", declared_name="declared")
    reg.set_default("declared")
    assert reg.default_nickname() == "nick"


def test_set_default_rejects_unknown_archive(tmp_path):
    reg = Registry(path=tmp_path / "archives.yaml")
    with pytest.raises(KeyError):
        reg.set_default("nonexistent")
    assert reg.default_nickname() is None


def test_clear_default(tmp_path):
    reg = Registry(path=tmp_path / "archives.yaml")
    reg.register("postdoc", tmp_path / "postdoc-data")
    reg.set_default("postdoc")
    reg.set_default(None)
    assert reg.default_nickname() is None
