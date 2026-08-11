"""
The value@authority identity convention.

See docs/identity-trust-roadmap.md. Nothing here verifies an identity --
these tests pin the *shape*, the offline typo checks, and above all the
promise that an archive written before the convention existed still works.
"""

import pytest

from nebula import identity


# --- parsing ---------------------------------------------------------------

@pytest.mark.parametrize("raw,value,authority,explicit", [
    ("grant", "grant", "local", False),
    ("grant@local", "grant", "local", True),
    ("grant@ncsu.edu", "grant", "ncsu.edu", True),
    ("0000-0003-2885-4801@orcid.org", "0000-0003-2885-4801", "orcid.org", True),
    ("Grant-Giesbrecht@github.com", "Grant-Giesbrecht", "github.com", True),
])
def test_parse_splits_on_the_last_at(raw, value, authority, explicit):
    got = identity.parse_identity(raw)
    assert (got.value, got.authority, got.explicit) == (value, authority, explicit)


def test_an_email_needs_no_special_case():
    """The whole argument for this shape: an email already is value@authority."""
    got = identity.parse_identity("grant@ncsu.edu")
    assert got.authority == "ncsu.edu"
    assert got.qualified == "grant@ncsu.edu"


def test_value_may_contain_an_at_because_we_split_on_the_last_one():
    got = identity.parse_identity("odd@name@ncsu.edu")
    assert got.value == "odd@name"
    assert got.authority == "ncsu.edu"


def test_authority_case_is_normalised_but_value_is_not():
    got = identity.parse_identity("Grant-Giesbrecht@GitHub.COM")
    assert got.authority == "github.com"
    assert got.value == "Grant-Giesbrecht"


def test_parse_never_raises_on_junk():
    """Reading is permissive: an odd owner must not make an archive unopenable."""
    for junk in ["", "@", "!!!", "a@b@c@d", "@@@"]:
        identity.parse_identity(junk)


def test_raw_is_preserved_so_uris_do_not_gain_a_second_spelling():
    got = identity.parse_identity("grant")
    assert got.raw == "grant"          # what refs use
    assert got.qualified == "grant@local"   # what humans are shown


# --- ORCID check digit -----------------------------------------------------

@pytest.mark.parametrize("orcid", [
    "0000-0002-1825-0097",   # the canonical example from the ORCID spec
    "0000-0003-2885-4801",
])
def test_real_orcids_pass(orcid):
    assert identity.orcid_check_digit(orcid.replace("-", "")[:15]) == orcid[-1]


@pytest.mark.parametrize("orcid", [
    "0000-0002-1825-0098",   # wrong check digit
    "0000-0002-1852-0097",   # transposed digits
])
def test_mistyped_orcids_are_refused_offline(orcid):
    with pytest.raises(identity.IdentityError) as e:
        identity.validate_identity(f"{orcid}@orcid.org")
    assert "check digit" in str(e.value)


def test_orcid_with_x_check_digit():
    assert identity.orcid_check_digit("000000021694233") == "X"


def test_orcid_of_the_wrong_shape_says_what_the_shape_is():
    with pytest.raises(identity.IdentityError) as e:
        identity.validate_identity("12345@orcid.org")
    assert "0000-0003-2885-4801" in str(e.value)


# --- validation: errors are certainties, warnings are opinions -------------

@pytest.mark.parametrize("bad", [
    "0000-0003-2885-4800@orcid.org",   # check digit
    "-nope-@github.com",               # leading/trailing hyphen
    "no--double@github.com",           # double hyphen
    "@ncsu.edu",                       # nothing before the @
    "grant@",                          # nothing after the @
    "has space",
    "has/slash",
])
def test_certain_mistakes_are_refused(bad):
    with pytest.raises(identity.IdentityError):
        identity.validate_identity(bad)


@pytest.mark.parametrize("ok", [
    "grant@ncsu.edu",
    "0000-0003-2885-4801@orcid.org",
    "Grant-Giesbrecht@github.com",
    "grant@nebulahub.org",
])
def test_well_formed_identities_pass_without_comment(ok):
    assert identity.validate_identity(ok) == []


def test_unknown_authority_is_warned_not_refused():
    """Refusing would mean nebula keeping a whitelist of who may exist."""
    warnings = identity.validate_identity("grant@mylab")
    assert warnings and "not a domain" in warnings[0]


def test_bare_name_warns_that_it_is_only_locally_unique():
    warnings = identity.validate_identity("grant")
    assert warnings
    assert "grant@local" in warnings[0]
    assert "orcid.org" in warnings[0]


def test_explicit_local_still_warns():
    """`local` is a known authority, but it is the one that guarantees
    non-uniqueness -- it must not report a clean bill of health."""
    assert identity.validate_identity("grant@local")


def test_key_authority_is_reserved_not_usable():
    with pytest.raises(identity.IdentityError) as e:
        identity.validate_identity("k7f3a@key")
    assert "not yet" in str(e.value) or "reserved" in str(e.value)


# --- honest labelling ------------------------------------------------------

def test_nothing_is_ever_reported_as_verified_yet():
    for name in ["grant", "grant@ncsu.edu", "0000-0003-2885-4801@orcid.org"]:
        info = identity.describe_identity(name)
        assert info["verified"] is False
        assert info["status"] == "unverified"


def test_describe_exposes_what_a_badge_needs():
    info = identity.describe_identity("0000-0003-2885-4801@orcid.org")
    assert info["authority_label"] == "ORCID"
    assert info["known"] is True
    assert info["local"] is False
    assert info["explicit"] is True


def test_describe_marks_a_bare_name_as_local():
    info = identity.describe_identity("grant")
    assert info["local"] is True
    assert info["explicit"] is False
    assert info["qualified"] == "grant@local"


# --- backwards compatibility ----------------------------------------------

def test_pre_convention_names_still_round_trip(tmp_path, monkeypatch):
    """Archives predate all of this. A bare name must keep working."""
    monkeypatch.setenv("NEBULA_IDENTITY", str(tmp_path / "identity.yaml"))
    monkeypatch.delenv(identity.USER_ENV, raising=False)
    identity.set_user("grant")
    assert identity.get_user() == "grant"


def test_setting_a_name_does_not_rewrite_it(tmp_path, monkeypatch):
    """Adding @local on disk would change every URI pointing at the archive."""
    monkeypatch.setenv("NEBULA_IDENTITY", str(tmp_path / "identity.yaml"))
    monkeypatch.delenv(identity.USER_ENV, raising=False)
    identity.set_user("grant")
    assert identity.get_user() == "grant"
    assert "@" not in identity.get_user()


def test_set_user_refuses_a_certain_typo(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBULA_IDENTITY", str(tmp_path / "identity.yaml"))
    monkeypatch.delenv(identity.USER_ENV, raising=False)
    with pytest.raises(identity.IdentityError):
        identity.set_user("0000-0003-2885-4800@orcid.org")
    assert identity.get_user() is None


def test_set_user_accepts_a_merely_warned_name(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBULA_IDENTITY", str(tmp_path / "identity.yaml"))
    monkeypatch.delenv(identity.USER_ENV, raising=False)
    identity.set_user("grant@mylab")
    assert identity.get_user() == "grant@mylab"


# --- fixability ------------------------------------------------------------

def test_a_pasted_profile_url_suggests_the_right_form():
    with pytest.raises(identity.IdentityError) as e:
        identity.clean_user("https://orcid.org/0000-0003-2885-4801")
    assert "0000-0003-2885-4801@orcid.org" in str(e.value)


def test_a_pasted_github_url_suggests_the_right_form():
    with pytest.raises(identity.IdentityError) as e:
        identity.clean_user("https://github.com/Grant-Giesbrecht")
    assert "Grant-Giesbrecht@github.com" in str(e.value)


# --- check reports an owner that is not globally unique --------------------

def _arc(tmp_path, user):
    from nebula.config import ArchiveSettings, write_settings
    root = tmp_path / "arc"
    root.mkdir(parents=True)
    write_settings(root, ArchiveSettings(name="arc", user=user))
    return root


def _kinds(root):
    from nebula import check as check_mod
    return {i.kind: i for i in check_mod.check(root, verify_checksums=False)}


def test_check_flags_an_owner_with_no_authority(tmp_path):
    got = _kinds(_arc(tmp_path, "grant"))
    assert "unqualified_owner" in got
    issue = got["unqualified_owner"]
    assert issue.severity == "info"
    assert "grant@local" in issue.detail
    assert "whoami" in issue.fix


def test_check_is_quiet_about_a_qualified_owner(tmp_path):
    assert "unqualified_owner" not in _kinds(_arc(tmp_path, "grant@ncsu.edu"))
    assert "unqualified_owner" not in _kinds(
        _arc(tmp_path / "b", "0000-0003-2885-4801@orcid.org"))


def test_check_flags_an_explicit_local_owner_too(tmp_path):
    """@local is written down but still means 'this machine only'."""
    assert "unqualified_owner" in _kinds(_arc(tmp_path, "grant@local"))


def test_unqualified_owner_is_never_an_error(tmp_path):
    """A bare name is a valid way to work alone. It must not make an
    otherwise healthy archive look damaged."""
    from nebula import check as check_mod
    issues = check_mod.check(_arc(tmp_path, "grant"), verify_checksums=False)
    assert [i for i in issues if i.severity == "error"] == []


def test_check_says_nothing_about_verification(tmp_path):
    """Reporting only unqualified owners would imply the rest were checked.
    Nothing is verified, so the wording must not suggest a clean bill."""
    issue = _kinds(_arc(tmp_path, "grant"))["unqualified_owner"]
    assert "verif" not in (issue.detail + (issue.fix or "")).lower()


def test_check_flags_an_archive_that_declares_no_owner(tmp_path, monkeypatch):
    from nebula.config import ArchiveSettings, write_settings
    monkeypatch.delenv(identity.USER_ENV, raising=False)
    monkeypatch.setenv("NEBULA_IDENTITY", str(tmp_path / "none.yaml"))
    root = tmp_path / "arc"
    root.mkdir()
    write_settings(root, ArchiveSettings(name="arc", user=""))
    got = _kinds(root)
    assert "no_owner" in got
    assert got["no_owner"].severity == "info"


# --- a fragment's owner is a claim, not a fact -----------------------------

def test_my_own_owner_is_not_a_claim():
    got = identity.describe_owner("grant@ncsu.edu", local_user="grant@ncsu.edu")
    assert got["claimed"] is False
    assert got["display"] == "grant@ncsu.edu"
    assert got["note"] == ""


def test_someone_elses_owner_is_labelled():
    got = identity.describe_owner("jsmith@ncsu.edu", local_user="grant@ncsu.edu")
    assert got["claimed"] is True
    assert "(claimed)" in got["display"]
    assert "does not check" in got["note"]


def test_an_undeclared_owner_is_nobodys_claim():
    """The local identity standing in for a missing one is not an assertion
    by whoever wrote the archive."""
    got = identity.describe_owner("grant@ncsu.edu", local_user="grant@ncsu.edu",
                                  declared=False)
    assert got["claimed"] is False
    assert got["declared"] is False


def test_archive_identity_marks_a_foreign_owner(tmp_path, monkeypatch):
    from nebula.config import archive_identity
    monkeypatch.setenv(identity.USER_ENV, "grant@ncsu.edu")
    root = _arc(tmp_path, "jsmith@mit.edu")
    ident = archive_identity(root)
    assert ident["user_claimed"] is True
    assert ident["user_display"] == "jsmith@mit.edu (claimed)"
    assert ident["user_note"]


def test_archive_identity_does_not_label_my_own(tmp_path, monkeypatch):
    from nebula.config import archive_identity
    monkeypatch.setenv(identity.USER_ENV, "grant@ncsu.edu")
    ident = archive_identity(_arc(tmp_path, "grant@ncsu.edu"))
    assert ident["user_claimed"] is False
    assert ident["user_note"] == ""


def test_adopt_plan_carries_the_claim(tmp_path, monkeypatch):
    """The moment you decide to take someone's data in is the moment to
    say that the name attached to it is unverified."""
    import nebula
    from nebula import transfer
    monkeypatch.setenv(identity.USER_ENV, "grant@ncsu.edu")

    src = tmp_path / "src"
    transfer.init_archive(src, kind="standard", name="theirs",
                          user="jsmith@mit.edu")
    s = nebula.new(src, description="d")
    with s.artifact("raw.csv") as fn:
        fn.write_text("a")
    s.close()

    frag = tmp_path / "frag"
    transfer.export(src, frag, sessions=[s.id])

    dst = tmp_path / "mine"
    transfer.init_archive(dst, kind="standard", name="mine",
                          user="grant@ncsu.edu")

    plan = transfer.plan_adopt(frag, dst)
    owner = plan.to_dict()["source_owner"]
    assert owner["claimed"] is True
    assert owner["user"] == "jsmith@mit.edu"
    assert "does not check" in owner["note"]


def test_merge_plan_from_my_own_intake_is_not_a_claim(tmp_path, monkeypatch):
    import nebula
    from nebula import transfer
    monkeypatch.setenv(identity.USER_ENV, "grant@ncsu.edu")

    src = tmp_path / "intake"
    transfer.init_archive(src, kind="intake", name="intake",
                          user="grant@ncsu.edu")
    s = nebula.new(src, description="d")
    with s.artifact("raw.csv") as fn:
        fn.write_text("a")
    s.close()
    dst = tmp_path / "mine"
    transfer.init_archive(dst, kind="standard", name="mine",
                          user="grant@ncsu.edu")

    plan = transfer.plan_merge(src, dst)
    assert plan.to_dict()["source_owner"]["claimed"] is False
