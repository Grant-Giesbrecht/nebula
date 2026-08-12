"""
Local petnames and identity trails (nebula.contacts).

The human case this exists for: someone used an institutional email, then
GitHub, then an ORCID. Old archives carry the old ids and stay correct;
anything written now should use the newest.
"""
import pytest

from nebula import contacts as C
from nebula import identity


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setenv(C.CONTACTS_ENV, str(tmp_path / "contacts.yaml"))
    return C.Contacts(path=C.contacts_path())


def _grant(book):
    book.add_identity("grant", "grant@ncsu.edu", display="Grant Giesbrecht")
    book.add_identity("grant", "Grant-Giesbrecht@github.com")
    book.add_identity("grant", "0000-0003-2885-4801@orcid.org", since="2022-01")
    return book.get("grant")


# --- the trail -----------------------------------------------------------

def test_the_newest_identity_is_the_current_one(book):
    c = _grant(book)
    assert c.current == "0000-0003-2885-4801@orcid.org"
    assert c.former == ("grant@ncsu.edu", "Grant-Giesbrecht@github.com")


def test_an_old_id_resolves_to_the_current_one(book):
    """What makes 'write new refs with their current id' work when all you
    have is an old archive's owner string."""
    _grant(book)
    assert book.current_for("grant@ncsu.edu") == "0000-0003-2885-4801@orcid.org"


def test_all_the_ids_are_recognised_as_one_person(book):
    _grant(book)
    assert book.same_person("grant@ncsu.edu", "Grant-Giesbrecht@github.com")
    assert not book.same_person("grant@ncsu.edu", "someone@else.edu")


def test_an_unknown_identity_passes_through_unchanged(book):
    assert book.current_for("stranger@mit.edu") == "stranger@mit.edu"
    assert book.display("stranger@mit.edu") == "stranger@mit.edu"


# --- petnames ------------------------------------------------------------

def test_a_petname_resolves_to_the_current_identity(book):
    _grant(book)
    assert book.resolve("grant@localid") == "0000-0003-2885-4801@orcid.org"


def test_an_unknown_petname_is_refused(book):
    """Writing an unresolved alias into a ref would name someone who does
    not exist on any other machine."""
    with pytest.raises(C.ContactError):
        book.resolve("nobody@localid")


def test_a_real_identity_passes_through_resolve(book):
    assert book.resolve("x@mit.edu") == "x@mit.edu"


def test_localid_is_not_a_valid_identity_of_your_own():
    with pytest.raises(identity.IdentityError) as e:
        identity.validate_identity("grant@localid")
    assert "petname" in str(e.value)


def test_a_contact_identity_cannot_itself_be_a_petname(book):
    with pytest.raises(C.ContactError):
        book.add_identity("grant", "someone@localid")


def test_local_and_localid_are_different_authorities():
    assert identity.LOCAL_AUTHORITY != identity.LOCAL_ID_AUTHORITY
    assert identity.parse_identity("g@local").authority == "local"
    assert identity.parse_identity("g@localid").authority == "localid"


# --- display -------------------------------------------------------------

def test_display_uses_the_petname(book):
    _grant(book)
    assert book.display("grant@ncsu.edu") == "Grant Giesbrecht"


def test_describe_says_whether_an_id_is_current(book):
    _grant(book)
    old = book.describe("grant@ncsu.edu")
    assert old["known_contact"] is True
    assert old["is_current"] is False
    assert old["current"] == "0000-0003-2885-4801@orcid.org"
    new = book.describe("0000-0003-2885-4801@orcid.org")
    assert new["is_current"] is True


def test_describe_still_reports_nothing_as_verified(book):
    _grant(book)
    assert book.describe("0000-0003-2885-4801@orcid.org")["verified"] is False


# --- integrity -----------------------------------------------------------

def test_an_identity_cannot_belong_to_two_people(book, tmp_path):
    book.add_identity("grant", "shared@ncsu.edu")
    with pytest.raises(C.ContactError):
        book.add_identity("jamie", "shared@ncsu.edu")


def test_a_duplicated_identity_in_the_file_is_refused(tmp_path, monkeypatch):
    p = tmp_path / "contacts.yaml"
    p.write_text(
        "grant:\n  ids: [a@x.edu]\njamie:\n  ids: [a@x.edu]\n")
    monkeypatch.setenv(C.CONTACTS_ENV, str(p))
    with pytest.raises(C.ContactError):
        C.Contacts(path=p).all()


def test_a_mistyped_orcid_is_refused_before_it_becomes_current(book):
    with pytest.raises(identity.IdentityError):
        book.add_identity("grant", "0000-0003-2885-4800@orcid.org")


def test_adding_the_same_id_twice_is_refused(book):
    book.add_identity("grant", "grant@ncsu.edu")
    with pytest.raises(C.ContactError):
        book.add_identity("grant", "grant@ncsu.edu")


# --- persistence ---------------------------------------------------------

def test_the_trail_round_trips(book):
    _grant(book)
    again = C.Contacts(path=book.path)
    c = again.get("grant")
    assert c.ids == ("grant@ncsu.edu", "Grant-Giesbrecht@github.com",
                     "0000-0003-2885-4801@orcid.org")
    assert c.display == "Grant Giesbrecht"


def test_a_bare_string_entry_is_accepted(tmp_path, monkeypatch):
    """The simplest hand-written form: one petname, one id."""
    p = tmp_path / "contacts.yaml"
    p.write_text("jamie: jamie@mit.edu\n")
    monkeypatch.setenv(C.CONTACTS_ENV, str(p))
    assert C.Contacts(path=p).get("jamie").current == "jamie@mit.edu"


def test_since_is_optional_and_order_decides_currency(tmp_path, monkeypatch):
    p = tmp_path / "contacts.yaml"
    p.write_text("g:\n  ids:\n    - old@x.edu\n    - {id: new@y.edu, since: 2024-05}\n")
    monkeypatch.setenv(C.CONTACTS_ENV, str(p))
    c = C.Contacts(path=p).get("g")
    assert c.current == "new@y.edu"
    assert c.since == (None, "2024-05")
