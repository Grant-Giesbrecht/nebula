"""
Minting and resolving nebula:// URIs against real archives on disk.

`test_refs.py` covers the grammar with no filesystem in sight. This covers
the other half: which owner and which name a particular directory actually
claims, whether the URI it yields is honestly presented as unique, and
whether one written on another machine finds its way back to a path here.
"""

import pytest

import nebula
from nebula import transfer, uris
from nebula.refs import parse_uri
from nebula.registry import get_registry


def _archive(root, *, name, user=None):
    transfer.init_archive(root, kind="standard", name=name, user=user)
    return root


def _seg(root):
    """The archive segment a URI for `root` should carry: `label~id`.

    Read from the archive rather than hard-coded, because the id is minted
    at random -- which is the point of it.
    """
    from nebula.config import read_settings

    settings = read_settings(root, apply_env=False)
    return f"{settings.name}~{settings.id}" if settings.id else settings.name


def _strip_owner(root):
    """Blank an archive's declared owner.

    `init_archive` stamps this machine's identity, so the only way to get
    an archive that declares nobody -- the state of every archive created
    before owners were recorded -- is to remove it afterwards.
    """
    from nebula.config import read_settings, write_settings

    settings = read_settings(root, apply_env=False)
    settings.user = ""
    write_settings(root, settings)
    return root


def _session(root, description="run"):
    s = nebula.new(root, description=description, announce=False)
    with s.artifact("raw.csv") as fn:
        fn.write_text("x")
    s.close()
    return s


# ---------------------------------------------------------------------
# minting
# ---------------------------------------------------------------------

def test_uri_for_a_file_names_owner_archive_session_file(tmp_path):
    root = _archive(tmp_path / "postdoc", name="postdoc", user="g@ncsu.edu")
    s = _session(root)
    got = uris.uri_for(root, session=s.id, file="raw.csv")
    assert got == f"nebula://g@ncsu.edu/{_seg(root)}/{s.id}/raw.csv"
    # ...and it parses back into the same parts, id included.
    ref = parse_uri(got)
    assert (ref.user, ref.archive, ref.session, ref.file) == (
        "g@ncsu.edu", "postdoc", s.id, "raw.csv")
    assert ref.archive_id and _seg(root).endswith(ref.archive_id)


def test_the_archives_declared_name_wins_over_the_directory_name(tmp_path):
    """The name in a URI is the one its author's archive declares, because
    that is what travels with a copy. A directory renamed on this machine
    must not change what refs into it are called."""
    root = _archive(tmp_path / "some-folder", name="postdoc", user="g@ncsu.edu")
    assert uris.uri_for(root) == f"nebula://g@ncsu.edu/{_seg(root)}"
    assert uris.uri_for(root).startswith("nebula://g@ncsu.edu/postdoc~")


def test_the_archives_declared_owner_wins_over_the_local_identity(tmp_path,
                                                                  monkeypatch):
    """A colleague's archive mounted here keeps minting URIs under *their*
    name -- otherwise a citation written here would point at an archive
    that does not exist."""
    monkeypatch.setenv("NEBULA_USER", "me@here.edu")
    root = _archive(tmp_path / "theirs", name="lab", user="jane@lab.edu")
    assert uris.uri_for(root) == f"nebula://jane@lab.edu/{_seg(root)}"


def test_an_archive_with_no_owner_falls_back_to_this_machine(tmp_path,
                                                             monkeypatch):
    monkeypatch.setenv("NEBULA_USER", "me@here.edu")
    root = _strip_owner(_archive(tmp_path / "mine", name="mine"))
    info = uris.describe(root)
    assert info.uri == f"nebula://me@here.edu/{_seg(root)}"
    # ...and says so, because a copy sent elsewhere would mint a different one.
    assert not info.unique
    assert any("does not declare an owner" in w for w in info.warnings)


def test_no_owner_anywhere_is_an_error_not_a_guess(tmp_path, monkeypatch):
    monkeypatch.delenv("NEBULA_USER", raising=False)
    monkeypatch.setenv("NEBULA_IDENTITY", str(tmp_path / "absent.yaml"))
    root = _strip_owner(_archive(tmp_path / "mine", name="mine"))
    with pytest.raises(uris.UriError):
        uris.uri_for(root)


def test_allow_unknown_is_the_only_way_to_get_an_ownerless_uri(tmp_path,
                                                               monkeypatch):
    monkeypatch.delenv("NEBULA_USER", raising=False)
    monkeypatch.setenv("NEBULA_IDENTITY", str(tmp_path / "absent.yaml"))
    root = _strip_owner(_archive(tmp_path / "mine", name="mine"))
    got = uris.uri_for(root, session="S-26-0001", allow_unknown=True)
    assert got == f"nebula://{uris.UNKNOWN_USER}/mine/S-26-0001"
    # No id: minting writes archive.yaml, and this archive has no owner to
    # confirm it is ours, so nothing was minted into it.
    assert "~" not in got


def test_collection_and_asset_uris(tmp_path):
    from nebula import assets, collection

    root = _archive(tmp_path / "postdoc", name="postdoc", user="g@ncsu.edu")
    collection.create(root, "paper-2026")
    src = tmp_path / "cal.json"
    src.write_text("{}")
    meta = assets.import_asset(root, src)

    assert uris.uri_for(root, collection="paper-2026") == \
        f"nebula://g@ncsu.edu/{_seg(root)}/collections/paper-2026"
    assert uris.uri_for(root, asset=meta.id) == \
        f"nebula://g@ncsu.edu/{_seg(root)}/assets/{meta.id}"


def test_describe_reports_the_path_and_whether_it_is_there(tmp_path):
    root = _archive(tmp_path / "postdoc", name="postdoc", user="g@ncsu.edu")
    s = _session(root)

    here = uris.describe(root, session=s.id, file="raw.csv")
    assert here.exists and here.path.endswith("raw.csv")

    # A URI for something that is not on this machine right now is still the
    # correct name for it -- absence is reported, not refused.
    gone = uris.describe(root, session=s.id, file="never-written.csv")
    assert gone.uri.endswith("/never-written.csv")
    assert not gone.exists


def test_a_uri_names_one_thing(tmp_path):
    root = _archive(tmp_path / "postdoc", name="postdoc", user="g@ncsu.edu")
    with pytest.raises(uris.UriError):
        uris.uri_for(root, session="S-26-0001", collection="paper-2026")
    with pytest.raises(uris.UriError):
        uris.uri_for(root, file="raw.csv")          # no session


# ---------------------------------------------------------------------
# honesty about uniqueness
# ---------------------------------------------------------------------

def test_a_qualified_owner_carries_no_caveats(tmp_path):
    root = _archive(tmp_path / "postdoc", name="postdoc",
                    user="0000-0003-2885-4801@orcid.org")
    info = uris.describe(root)
    assert info.unique and info.warnings == []


@pytest.mark.parametrize("owner", ["grant", "grant@local"])
def test_a_local_owner_is_reported_as_not_unique(tmp_path, owner):
    """The whole point of the warnings: `grant@local` is a name two people
    can both have, so a URI built on it can collide and must say so."""
    root = _archive(tmp_path / "postdoc", name="postdoc", user=owner)
    info = uris.describe(root)
    assert not info.unique
    assert any("this machine and nowhere else" in w for w in info.warnings)


def test_a_petname_owner_is_reported_as_machine_local(tmp_path):
    root = _archive(tmp_path / "postdoc", name="postdoc", user="jane@localid")
    info = uris.describe(root)
    assert not info.unique
    assert any("petname" in w for w in info.warnings)


# ---------------------------------------------------------------------
# resolving
# ---------------------------------------------------------------------

def test_resolve_finds_a_registered_archive(tmp_path):
    root = _archive(tmp_path / "postdoc", name="postdoc", user="g@ncsu.edu")
    get_registry().register_archive(root)
    s = _session(root)

    got_root, ref = uris.resolve(
        f"nebula://g@ncsu.edu/postdoc/{s.id}/raw.csv")
    assert got_root == root
    assert ref.session == s.id and ref.file == "raw.csv"


def test_resolve_picks_the_right_owners_archive(tmp_path):
    """Two people, one archive name -- which is the reason the owner segment
    exists at all. Resolving by name alone would be a coin flip."""
    mine = _archive(tmp_path / "mine", name="postdoc", user="me@here.edu")
    theirs = _archive(tmp_path / "theirs", name="postdoc", user="jane@lab.edu")
    get_registry().register_archive(mine)
    get_registry().register_archive(theirs)

    assert uris.resolve(f"nebula://me@here.edu/{_seg(mine)}")[0] == mine
    assert uris.resolve(f"nebula://jane@lab.edu/{_seg(theirs)}")[0] == theirs
    # ...and by name alone, the legacy spelling, which still has to work.
    assert uris.resolve("nebula://me@here.edu/postdoc")[0] == mine
    assert uris.resolve("nebula://jane@lab.edu/postdoc")[0] == theirs


def test_resolve_says_unregistered_and_unmounted_differently(tmp_path):
    """'ask them to send it' and 'plug the drive in' are different jobs."""
    root = _archive(tmp_path / "postdoc", name="postdoc", user="g@ncsu.edu")
    get_registry().register_archive(root)

    with pytest.raises(uris.UriError, match="no archive .* is registered"):
        uris.resolve("nebula://nobody@nowhere.edu/postdoc")

    import shutil
    shutil.rmtree(root)
    with pytest.raises(uris.UriError, match="is not there"):
        uris.resolve("nebula://g@ncsu.edu/postdoc")


def test_resolve_refuses_a_compact_ref(tmp_path):
    with pytest.raises(uris.UriError):
        uris.resolve("postdoc|S-26-0001/raw.csv")


def test_resolve_any_takes_either_spelling(tmp_path):
    root = _archive(tmp_path / "postdoc", name="postdoc", user="g@ncsu.edu")
    get_registry().register_archive(root)
    assert uris.resolve_any("postdoc|S-26-0001/raw.csv")[0] == root
    assert uris.resolve_any("nebula://g@ncsu.edu/postdoc")[0] == root
    assert uris.resolve_any(f"nebula://{_seg(root)}/S-26-0001/raw.csv")[0] == root
    with pytest.raises(uris.UriError):
        uris.resolve_any("S-26-0001/raw.csv")       # names no archive


def test_resolve_follows_a_petname(tmp_path, monkeypatch):
    """`jane@localid` means nothing to anyone else, but on this machine it
    is how you refer to whoever the contacts file says it is."""
    from nebula import contacts

    monkeypatch.setenv("NEBULA_CONTACTS", str(tmp_path / "contacts.yaml"))
    monkeypatch.setattr(contacts, "_default", None)
    contacts.get_contacts().add_identity("jane", "jane@lab.edu")

    theirs = _archive(tmp_path / "theirs", name="lab", user="jane@lab.edu")
    get_registry().register_archive(theirs)

    assert uris.resolve("nebula://jane@localid/lab")[0] == theirs


def test_a_uri_survives_the_archive_moving(tmp_path):
    """The design test from the hosting-tiers section: moving an archive
    must not change a single URI already written."""
    root = _archive(tmp_path / "here", name="postdoc", user="g@ncsu.edu")
    s = _session(root)
    before = uris.uri_for(root, session=s.id, file="raw.csv")

    moved = tmp_path / "elsewhere"
    root.rename(moved)
    get_registry().register_archive(moved)

    after = uris.uri_for(moved, session=s.id, file="raw.csv")
    assert after == before
    assert uris.resolve(before)[0] == moved


# ---------------------------------------------------------------------
# What the id is for
# ---------------------------------------------------------------------

def _rename(root, new_name):
    from nebula.config import read_settings, write_settings

    settings = read_settings(root, apply_env=False)
    settings.name = new_name
    write_settings(root, settings)
    return root


def test_a_uri_survives_the_archive_being_renamed(tmp_path):
    """The whole point. A URI handed out today has to keep resolving after
    the archive it names is renamed -- otherwise it is not an identifier,
    it is a snapshot of a label."""
    root = _archive(tmp_path / "postdoc", name="postdoc", user="g@ncsu.edu")
    get_registry().register_archive(root)
    s = _session(root)
    cited = uris.uri_for(root, session=s.id, file="raw.csv")
    assert "postdoc~" in cited

    _rename(root, "thesis")
    get_registry().register_archive(root)

    got_root, ref = uris.resolve(cited)
    assert got_root == root
    assert ref.session == s.id and ref.file == "raw.csv"


def test_a_uri_survives_a_rename_and_a_move_together(tmp_path):
    root = _archive(tmp_path / "here", name="postdoc", user="g@ncsu.edu")
    get_registry().register_archive(root)
    s = _session(root)
    cited = uris.uri_for(root, session=s.id, file="raw.csv")

    _rename(root, "thesis")
    moved = tmp_path / "elsewhere"
    root.rename(moved)
    get_registry().register_archive(moved)

    assert uris.resolve(cited)[0] == moved


def test_re_minting_shows_the_current_label(tmp_path):
    """A stale label still resolves, and asking again gives today's name --
    the redirect behaviour a slug URL has."""
    root = _archive(tmp_path / "postdoc", name="postdoc", user="g@ncsu.edu")
    get_registry().register_archive(root)
    s = _session(root)
    old = uris.uri_for(root, session=s.id)

    _rename(root, "thesis")
    get_registry().register_archive(root)

    new = uris.uri_for(root, session=s.id)
    assert "thesis~" in new and "postdoc" not in new
    # ...and both name the same thing.
    assert uris.resolve(old)[0] == uris.resolve(new)[0]
    assert parse_uri(old).same_target(parse_uri(new))


def test_the_id_beats_the_name_when_they_disagree(tmp_path):
    """Two archives, and the ref's label now belongs to the wrong one. The
    id has to win, or a rename would silently redirect old refs."""
    a = _archive(tmp_path / "a", name="alpha", user="g@ncsu.edu")
    b = _archive(tmp_path / "b", name="beta", user="g@ncsu.edu")
    get_registry().register_archive(a)
    get_registry().register_archive(b)
    cited = uris.uri_for(a)                      # nebula://g@…/alpha~xxx

    # Swap the names round: `alpha` is now b's label.
    _rename(a, "gamma")
    _rename(b, "alpha")
    get_registry().register_archive(a)
    get_registry().register_archive(b)

    assert uris.resolve(cited)[0] == a


def test_an_archive_of_someone_elses_is_never_given_an_id(tmp_path,
                                                          monkeypatch):
    """Minting into a colleague's archive would rewrite their archive.yaml
    and give it an id under our numbering that they have never seen."""
    from nebula.config import ensure_archive_id, read_settings

    monkeypatch.setenv("NEBULA_USER", "me@here.edu")
    theirs = _archive(tmp_path / "theirs", name="lab", user="jane@lab.edu")
    # init_archive minted one because we created it; clear it to model a
    # fragment that arrived without one.
    settings = read_settings(theirs, apply_env=False)
    settings.id = ""
    from nebula.config import write_settings
    write_settings(theirs, settings)

    assert ensure_archive_id(theirs) is None
    assert read_settings(theirs, apply_env=False).id == ""
    # ...and it still gets a URI, resolving by name as it always did.
    assert uris.uri_for(theirs) == "nebula://jane@lab.edu/lab"


def test_my_own_id_less_archive_gets_one_on_first_use(tmp_path, monkeypatch):
    """No migration command to run: an id appears the first time anything
    creates, registers or asks for a URI."""
    from nebula.config import read_settings, write_settings

    # "Mine" means the archive's declared owner is this machine's identity;
    # that is the test ensure_archive_id applies before writing anything.
    monkeypatch.setenv("NEBULA_USER", "g@ncsu.edu")
    root = _archive(tmp_path / "mine", name="mine", user="g@ncsu.edu")
    settings = read_settings(root, apply_env=False)
    settings.id = ""
    write_settings(root, settings)

    got = uris.uri_for(root)
    assert "mine~" in got
    assert read_settings(root, apply_env=False).id


def test_the_collision_minting_cannot_prevent_is_reported(tmp_path):
    """Two archives created where neither knew about the other. Detected
    the moment both are registered here, because two archives sharing an id
    are indistinguishable to every ref that names either."""
    from nebula.config import read_settings, write_settings

    a = _archive(tmp_path / "a", name="alpha", user="g@ncsu.edu")
    b = _archive(tmp_path / "b", name="beta", user="g@ncsu.edu")
    for root in (a, b):
        settings = read_settings(root, apply_env=False)
        settings.id = "0fe"
        write_settings(root, settings)
        get_registry().register_archive(root)

    collisions = get_registry().find_id_collisions()
    assert collisions and collisions[0][0] == "0fe"
    assert len(collisions[0][1]) == 2


def test_no_collision_is_reported_for_two_spellings_of_one_archive(tmp_path):
    root = _archive(tmp_path / "a", name="alpha", user="g@ncsu.edu")
    get_registry().register_archive(root)
    get_registry().register_archive(root, key="alias")
    assert get_registry().find_id_collisions() == []
