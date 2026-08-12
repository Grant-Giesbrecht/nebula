"""
Multiple locations per archive, in priority order.

An archive can be in more than one place -- a laptop working copy, a NAS
backup, a lab server. The registry lists them most-preferred first and
resolution takes the first that is actually there.
"""
import pytest

from nebula import registry as R


@pytest.fixture
def reg(tmp_path):
    return R.Registry(path=tmp_path / "registry.yaml")


def test_a_single_path_still_registers_and_reads(reg, tmp_path):
    root = tmp_path / "arc"
    root.mkdir()
    reg.register("postdoc", root)
    assert reg.get("postdoc").root == root


def test_resolution_prefers_the_first_available(reg, tmp_path):
    gone = tmp_path / "unplugged"          # deliberately absent
    here = tmp_path / "here"
    here.mkdir()
    reg.register("postdoc", locations=[
        R.Location("path", str(gone), "external drive"),
        R.Location("path", str(here), "laptop"),
    ])
    assert reg.get("postdoc").root == here


def test_an_entirely_unavailable_archive_still_names_a_path(reg, tmp_path):
    """So an error can say *where* it looked instead of returning None."""
    gone = tmp_path / "unplugged"
    reg.register("postdoc", locations=[R.Location("path", str(gone))])
    cfg = reg.get("postdoc")
    assert cfg.available is False
    assert cfg.root == gone


def test_a_remote_location_is_recorded_but_never_available(reg, tmp_path):
    """Nebula has no client yet. It must degrade to 'not reachable', not
    pretend the archive is there."""
    here = tmp_path / "here"
    here.mkdir()
    reg.register("postdoc", locations=[
        R.Location("url", "nebula+https://lab.example.edu/postdoc", "server"),
        R.Location("path", str(here)),
    ])
    cfg = reg.get("postdoc")
    assert cfg.locations[0].available is False
    assert cfg.root == here                  # falls through to the local copy


def test_remote_only_is_flagged(reg):
    reg.register("far", locations=[R.Location("url", "nebula+https://x/y")])
    assert reg.get("far").remote_only is True


# --- editing -------------------------------------------------------------

def test_added_locations_do_not_displace_the_working_copy(reg, tmp_path):
    here = tmp_path / "here"
    here.mkdir()
    other = tmp_path / "nas"
    other.mkdir()
    reg.register("postdoc", here)
    cfg = reg.add_location("postdoc", R.Location("path", str(other), "NAS"))
    assert [l.value for l in cfg.locations] == [str(here), str(other)]
    assert cfg.root == here


def test_prefer_puts_it_first(reg, tmp_path):
    here = tmp_path / "here"
    here.mkdir()
    other = tmp_path / "nas"
    other.mkdir()
    reg.register("postdoc", here)
    cfg = reg.add_location("postdoc", R.Location("path", str(other)), first=True)
    assert cfg.root == other


def test_adding_the_same_location_twice_does_not_duplicate_it(reg, tmp_path):
    here = tmp_path / "here"
    here.mkdir()
    reg.register("postdoc", here)
    cfg = reg.add_location("postdoc", R.Location("path", str(here), "renamed"))
    assert len(cfg.locations) == 1
    assert cfg.locations[0].label == "renamed"


def test_the_last_location_cannot_be_removed(reg, tmp_path):
    here = tmp_path / "here"
    here.mkdir()
    reg.register("postdoc", here)
    with pytest.raises(ValueError):
        reg.remove_location("postdoc", str(here))


def test_removing_an_unknown_location_raises(reg, tmp_path):
    here = tmp_path / "here"
    here.mkdir()
    reg.register("postdoc", here)
    with pytest.raises(KeyError):
        reg.remove_location("postdoc", "/nowhere")


# --- file format ---------------------------------------------------------

def test_one_plain_path_round_trips_as_root(reg, tmp_path):
    """A hand-edited file should not grow a list-of-dicts for what was a
    single line."""
    here = tmp_path / "here"
    here.mkdir()
    reg.register("postdoc", here)
    import yaml
    raw = yaml.safe_load(reg.path.read_text())
    assert raw["postdoc"]["root"] == str(here)
    assert "locations" not in raw["postdoc"]


def test_several_locations_round_trip(reg, tmp_path):
    here = tmp_path / "here"
    here.mkdir()
    reg.register("postdoc", locations=[
        R.Location("path", str(here), "laptop"),
        R.Location("url", "nebula+https://lab/x", "server"),
    ])
    again = R.Registry(path=reg.path).get("postdoc")
    assert [(l.kind, l.value, l.label) for l in again.locations] == [
        ("path", str(here), "laptop"),
        ("url", "nebula+https://lab/x", "server"),
    ]


def test_a_legacy_root_entry_loads(tmp_path):
    """Registries written before locations existed must keep working."""
    p = tmp_path / "registry.yaml"
    p.write_text(f"postdoc:\n  root: {tmp_path}\n  user: g@ncsu.edu\n")
    cfg = R.Registry(path=p).get("postdoc")
    assert cfg.root == tmp_path
    assert cfg.user == "g@ncsu.edu"
    assert len(cfg.locations) == 1


def test_a_bare_string_location_is_accepted(tmp_path):
    p = tmp_path / "registry.yaml"
    p.write_text(f"postdoc:\n  locations:\n    - {tmp_path}\n")
    assert R.Registry(path=p).get("postdoc").root == tmp_path


def test_an_entry_with_nowhere_to_look_is_refused(tmp_path):
    p = tmp_path / "registry.yaml"
    p.write_text("postdoc:\n  user: g@ncsu.edu\n")
    with pytest.raises(ValueError):
        R.Registry(path=p).all()


# --- the rename ----------------------------------------------------------

def test_the_legacy_filename_is_read_when_the_new_one_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "DEFAULT_REGISTRY_PATH", tmp_path / "registry.yaml")
    monkeypatch.setattr(R, "LEGACY_REGISTRY_PATH", tmp_path / "archives.yaml")
    monkeypatch.delenv("NEBULA_REGISTRY", raising=False)
    (tmp_path / "archives.yaml").write_text(f"postdoc:\n  root: {tmp_path}\n")
    assert R.Registry().get("postdoc").root == tmp_path


def test_migrate_renames_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "DEFAULT_REGISTRY_PATH", tmp_path / "registry.yaml")
    monkeypatch.setattr(R, "LEGACY_REGISTRY_PATH", tmp_path / "archives.yaml")
    monkeypatch.delenv("NEBULA_REGISTRY", raising=False)
    (tmp_path / "archives.yaml").write_text(f"postdoc:\n  root: {tmp_path}\n")
    got = R.Registry()
    assert got.migrate() == tmp_path / "registry.yaml"
    assert not (tmp_path / "archives.yaml").exists()
    assert R.Registry().get("postdoc").root == tmp_path


def test_migrate_never_clobbers_an_existing_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "DEFAULT_REGISTRY_PATH", tmp_path / "registry.yaml")
    monkeypatch.setattr(R, "LEGACY_REGISTRY_PATH", tmp_path / "archives.yaml")
    monkeypatch.delenv("NEBULA_REGISTRY", raising=False)
    (tmp_path / "archives.yaml").write_text(f"old:\n  root: {tmp_path}\n")
    (tmp_path / "registry.yaml").write_text(f"new:\n  root: {tmp_path}\n")
    assert R.Registry().migrate() is None
    assert (tmp_path / "archives.yaml").exists()
