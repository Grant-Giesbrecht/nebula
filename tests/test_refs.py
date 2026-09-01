import pytest

from nebula.refs import Ref, format_ref, format_uri, parse_ref, parse_uri


def test_bare_filename():
    ref = parse_ref("diode.graf")
    assert ref == Ref(file="diode.graf", session=None, archive=None)
    assert format_ref(ref) == "diode.graf"


def test_bare_session():
    ref = parse_ref("S-26-0152")
    assert ref == Ref(file=None, session="S-26-0152", archive=None)
    assert format_ref(ref) == "S-26-0152"


def test_session_and_file():
    ref = parse_ref("S-26-0152/diode.graf")
    assert ref == Ref(file="diode.graf", session="S-26-0152", archive=None)
    assert format_ref(ref) == "S-26-0152/diode.graf"


def test_cross_archive():
    ref = parse_ref("postdoc|S-26-0152/diode.graf")
    assert ref == Ref(file="diode.graf", session="S-26-0152", archive="postdoc")
    assert format_ref(ref) == "postdoc|S-26-0152/diode.graf"
    assert ref.is_cross_archive()


def test_cross_archive_whole_session():
    ref = parse_ref("postdoc|S-26-0152")
    assert ref == Ref(file=None, session="S-26-0152", archive="postdoc")
    assert format_ref(ref) == "postdoc|S-26-0152"


def test_round_trip_many():
    cases = [
        "diode.graf",
        "S-26-0152",
        "S-26-0152/diode.graf",
        "postdoc|S-26-0152/diode.graf",
        "postdoc|S-26-0152",
        "audio|S-26-0001/scope_trace_raw.csv",
    ]
    for text in cases:
        assert format_ref(parse_ref(text)) == text


def test_empty_raises():
    with pytest.raises(ValueError):
        parse_ref("")
    with pytest.raises(ValueError):
        parse_ref("   ")


def test_malformed_multiple_pipes():
    with pytest.raises(ValueError):
        parse_ref("a|b|c")


def test_malformed_empty_archive():
    with pytest.raises(ValueError):
        parse_ref("|S-26-0152")


def test_malformed_empty_after_slash():
    with pytest.raises(ValueError):
        parse_ref("S-26-0152/")


def test_is_same_session():
    assert parse_ref("diode.graf").is_same_session()
    assert not parse_ref("S-26-0152/diode.graf").is_same_session()


def test_resolved_fills_in_context():
    ref = parse_ref("diode.graf")
    resolved = ref.resolved(archive="postdoc", session="S-26-0300")
    assert resolved == Ref(file="diode.graf", session="S-26-0300", archive="postdoc")

    # already-explicit fields are left alone
    ref2 = parse_ref("postdoc|S-26-0152/diode.graf")
    resolved2 = ref2.resolved(archive="audio", session="S-26-0999")
    assert resolved2 == ref2


# ---------------------------------------------------------------------
# Fully-qualified nebula:// URIs
# ---------------------------------------------------------------------
# The URI half of this module had no coverage at all until 2026-09-01,
# which is the half that has to be exactly right: a compact ref that
# mis-parses is wrong inside one archive, but a URI that mis-parses is
# wrong in someone else's citation.

def test_uri_archive_only():
    ref = parse_uri("nebula://grant@ncsu.edu/postdoc")
    assert ref == Ref(user="grant@ncsu.edu", archive="postdoc")
    assert ref.kind == "archive"
    assert ref.is_cross_user() and ref.is_cross_archive()


def test_uri_session():
    ref = parse_uri("nebula://grant@ncsu.edu/postdoc/S-26-0152")
    assert ref == Ref(user="grant@ncsu.edu", archive="postdoc",
                      session="S-26-0152")
    assert ref.kind == "session"


def test_uri_file():
    ref = parse_uri("nebula://grant@ncsu.edu/postdoc/S-26-0152/diode.graf")
    assert ref.user == "grant@ncsu.edu"
    assert ref.archive == "postdoc"
    assert ref.session == "S-26-0152"
    assert ref.file == "diode.graf"
    assert ref.kind == "file"


def test_uri_collection_and_asset():
    coll = parse_uri("nebula://grant@ncsu.edu/postdoc/collections/paper-2026")
    assert coll == Ref(user="grant@ncsu.edu", archive="postdoc",
                       collection="paper-2026")
    assert coll.kind == "collection"

    asset = parse_uri("nebula://grant@ncsu.edu/postdoc/assets/AF-26-0017")
    assert asset == Ref(user="grant@ncsu.edu", archive="postdoc",
                        asset="AF-26-0017")
    assert asset.kind == "asset"


def test_uri_round_trips():
    """Every shape a URI can take survives parse -> format unchanged.

    This is the property everything else rests on: a ref stored in one
    archive and read back in another has to name the same thing, and the
    only way it can is if there is exactly one spelling per target.
    """
    cases = [
        "nebula://grant@ncsu.edu/postdoc",
        "nebula://grant@ncsu.edu/postdoc/S-26-0152",
        "nebula://grant@ncsu.edu/postdoc/S-26-0152/diode.graf",
        "nebula://grant@ncsu.edu/postdoc/collections/paper-2026",
        "nebula://grant@ncsu.edu/postdoc/assets/AF-26-0017",
        # every identity tier from the roadmap's table
        "nebula://0000-0003-2885-4801@orcid.org/postdoc/S-26-0152",
        "nebula://Grant-Giesbrecht@github.com/lab/S-26-0001/raw.csv",
        "nebula://grant@local/scratch/S-26-0001",
        "nebula://grant/scratch/S-26-0001",
        # a filename with dots, which is why the separator is '/' not '.'
        "nebula://grant@ncsu.edu/postdoc/S-26-0152/raw.data.v2.csv",
    ]
    for text in cases:
        assert format_uri(parse_uri(text)) == text, text


def test_parse_ref_accepts_a_uri():
    """One parser: anything that takes a ref takes a URI."""
    ref = parse_ref("  nebula://grant@ncsu.edu/postdoc/S-26-0152/diode.graf  ")
    assert ref.user == "grant@ncsu.edu"
    assert ref.file == "diode.graf"


def test_parse_ref_uri_scheme_is_case_insensitive():
    assert parse_ref("NEBULA://g@x.edu/a/S-26-0001").user == "g@x.edu"


def test_format_ref_of_a_user_bearing_ref_is_the_full_uri():
    """There is no compact spelling that can carry an owner, so format_ref
    must not silently drop it -- that would turn a ref into someone else's
    archive into a ref into your own."""
    ref = parse_uri("nebula://jane@lab.edu/postdoc/S-26-0152/diode.graf")
    assert format_ref(ref) == "nebula://jane@lab.edu/postdoc/S-26-0152/diode.graf"


def test_format_uri_fills_in_implicit_parts():
    ref = parse_ref("S-26-0152/diode.graf")
    got = format_uri(ref, user="grant@ncsu.edu", archive="postdoc")
    assert got == "nebula://grant@ncsu.edu/postdoc/S-26-0152/diode.graf"


def test_format_uri_needs_a_user_and_an_archive():
    ref = parse_ref("S-26-0152/diode.graf")
    with pytest.raises(ValueError):
        format_uri(ref)
    with pytest.raises(ValueError):
        format_uri(ref, user="grant@ncsu.edu")     # no archive
    with pytest.raises(ValueError):
        format_uri(ref, archive="postdoc")         # no user


def test_format_uri_for_a_file_needs_its_session():
    with pytest.raises(ValueError):
        format_uri(Ref(user="g@x.edu", archive="postdoc", file="diode.graf"))


@pytest.mark.parametrize("text", [
    "nebula://",
    "nebula://grant@ncsu.edu",                       # no archive
    "nebula://grant@ncsu.edu/",                      # empty archive
    "nebula:///postdoc",                             # empty user
    "nebula://grant@ncsu.edu/postdoc/S-26-0152/a/b",  # too many segments
    "nebula://grant@ncsu.edu/postdoc/collections",   # no collection named
    "nebula://grant@ncsu.edu/postdoc/collections/a/b",
    "nebula://grant@ncsu.edu/postdoc/assets",        # no asset named
])
def test_malformed_uris_raise(text):
    """Loudly, never by guessing: a silently-wrong provenance link is worse
    than a failed parse (module docstring)."""
    with pytest.raises(ValueError):
        parse_ref(text)


def test_a_uri_is_one_pasteable_token():
    """No whitespace anywhere, which is what lets a ref be dropped into
    BibTeX, an issue, or a chat message and come back intact."""
    uri = format_uri(Ref(user="0000-0003-2885-4801@orcid.org",
                         archive="postdoc", session="S-26-0152",
                         file="diode.graf"))
    assert " " not in uri and "\t" not in uri and "\n" not in uri


def test_resolved_leaves_an_explicit_user_alone():
    ref = parse_uri("nebula://jane@lab.edu/shared/S-26-0002/cal.json")
    assert ref.resolved(archive="postdoc", session="S-26-0999",
                        user="grant@ncsu.edu") == ref


def test_resolved_fills_in_the_user_when_absent():
    ref = parse_ref("S-26-0152/diode.graf")
    got = ref.resolved(archive="postdoc", session="S-26-0300",
                       user="grant@ncsu.edu")
    assert got.user == "grant@ncsu.edu"
    assert format_ref(got) == "nebula://grant@ncsu.edu/postdoc/S-26-0152/diode.graf"


def test_sibling_namespaces_do_not_inherit_a_session():
    """A collection or an asset lives beside sessions, not inside one, so
    filling in context must not give it the ambient session id."""
    for text in ("collections/paper-2026", "assets/AF-26-0017"):
        got = parse_ref(text).resolved(archive="postdoc", session="S-26-0300")
        assert got.session is None


# ---------------------------------------------------------------------
# One grammar: prefix-droppable segments, and the archive's id
# ---------------------------------------------------------------------

def test_the_archive_segment_carries_a_label_and_an_id():
    ref = parse_ref("nebula://postdoc~0fe/S-26-0152/raw.csv")
    assert ref.archive == "postdoc"
    assert ref.archive_id == "0fe"
    assert ref.user is None                 # implicit: mine
    assert ref.archive_segment == "postdoc~0fe"


def test_the_id_is_normalised_on_the_way_in():
    """Two spellings of one id must not become two different refs."""
    a = parse_ref("nebula://postdoc~00FE/S-26-0152/raw.csv")
    b = parse_ref("nebula://postdoc~fe/S-26-0152/raw.csv")
    assert a.archive_id == b.archive_id == "0fe"
    assert a == b
    assert format_ref(a) == format_ref(b)


def test_the_label_is_never_what_identifies_an_archive():
    """The rule the whole design rests on: rename the archive and a ref
    still names the same thing."""
    before = parse_ref("nebula://postdoc~0fe/S-26-0152/raw.csv")
    after = parse_ref("nebula://thesis~0fe/S-26-0152/raw.csv")
    assert before.same_target(after)
    assert before.identity() == after.identity()
    # ...and two different archives are still two, however alike the labels.
    other = parse_ref("nebula://postdoc~1a2/S-26-0152/raw.csv")
    assert not before.same_target(other)


def test_an_archive_with_no_id_falls_back_to_its_label():
    a = parse_ref("nebula://grant@ncsu.edu/postdoc/S-26-0152")
    b = parse_ref("nebula://grant@ncsu.edu/postdoc/S-26-0152")
    assert a.archive_id is None
    assert a.same_target(b)
    assert not a.same_target(parse_ref("nebula://grant@ncsu.edu/thesis/S-26-0152"))


def test_dropping_a_prefix_of_the_segments_means_here():
    """The absolute/relative-path model: each spelling says how far up it
    starts, and the rest is 'here'."""
    assert parse_ref("raw.csv").is_same_session()
    assert parse_ref("S-26-0152/raw.csv").archive is None
    mine = parse_ref("nebula://postdoc~0fe/S-26-0152/raw.csv")
    assert mine.user is None and mine.archive_id == "0fe"
    theirs = parse_ref("nebula://jane@lab.edu/postdoc~0fe/S-26-0152/raw.csv")
    assert theirs.user == "jane@lab.edu"


def test_new_grammar_round_trips():
    cases = [
        "raw.csv",
        "S-26-0152",
        "S-26-0152/raw.csv",
        "collections/paper-2026",
        "assets/AF-26-0017",
        "nebula://postdoc~0fe",
        "nebula://postdoc~0fe/S-26-0152",
        "nebula://postdoc~0fe/S-26-0152/raw.csv",
        "nebula://postdoc~0fe/collections/paper-2026",
        "nebula://postdoc~0fe/assets/AF-26-0017",
        "nebula://postdoc~1a2b3c/S-26-0152/raw.csv",
        "nebula://grant@ncsu.edu/postdoc~0fe/S-26-0152/raw.csv",
        "nebula://0000-0003-2885-4801@orcid.org/postdoc~0fe/S-26-0152",
        # legacy: an archive segment with no id, which still has to read
        "nebula://grant@ncsu.edu/postdoc/S-26-0152/raw.csv",
        "nebula://grant/postdoc/S-26-0152",
    ]
    for text in cases:
        assert format_ref(parse_ref(text)) == text, text


def test_the_pipe_spelling_is_read_and_never_written():
    """Refs written before the grammar was unified keep resolving, and get
    rewritten the next time anything reformats them."""
    ref = parse_ref("postdoc~0fe|S-26-0152/raw.csv")
    assert (ref.archive, ref.archive_id) == ("postdoc", "0fe")
    assert format_ref(ref) == "nebula://postdoc~0fe/S-26-0152/raw.csv"
    assert "|" not in format_ref(ref)


def test_an_id_less_archive_keeps_the_pipe_spelling():
    """There is no URI form for it -- without an id, an archive segment
    cannot be told from a user segment when it is read back -- so the
    legacy spelling is emitted rather than an unparseable one."""
    ref = parse_ref("postdoc|S-26-0152/raw.csv")
    assert ref.archive_id is None
    assert format_ref(ref) == "postdoc|S-26-0152/raw.csv"
    assert format_ref(parse_ref(format_ref(ref))) == format_ref(ref)


def test_an_archive_named_without_a_user_must_carry_its_id():
    """Otherwise `nebula://postdoc/S-26-0152` could be read either way. The
    error says which, rather than guessing."""
    with pytest.raises(ValueError, match="needs its id"):
        parse_ref("nebula://postdoc/S-26-0152")
    with pytest.raises(ValueError, match="needs its id"):
        parse_ref("nebula://postdoc/collections/paper-2026")
    with pytest.raises(ValueError):
        format_uri(Ref(archive="postdoc", session="S-26-0152"))


def test_a_bare_user_is_still_read_as_a_user():
    """`identity` permits a name with no authority, and rewriting one would
    change every URI already pointing at that archive."""
    ref = parse_ref("nebula://grant/postdoc/S-26-0152")
    assert ref.user == "grant"
    assert ref.archive == "postdoc"


def test_a_bare_token_is_never_an_archive():
    """No scheme means "inside this archive", so `postdoc~0fe` on its own is
    a filename, not an archive."""
    assert parse_ref("postdoc~0fe").file == "postdoc~0fe"
    assert parse_ref("postdoc~0fe").archive is None


def test_format_uri_fills_in_an_implicit_archive_and_id():
    ref = parse_ref("S-26-0152/raw.csv")
    got = format_uri(ref, user="grant@ncsu.edu", archive="postdoc",
                     archive_id="0fe")
    assert got == "nebula://grant@ncsu.edu/postdoc~0fe/S-26-0152/raw.csv"


def test_resolved_does_not_lend_this_archives_id_to_another_one():
    """Filling in context must not claim that a ref into somebody else's
    archive is really into this one."""
    foreign = parse_ref("nebula://jane@lab.edu/shared/S-26-0002/cal.json")
    got = foreign.resolved(archive="postdoc", session="S-26-0999",
                           user="me@here.edu", archive_id="0fe")
    assert got.archive_id is None and got.archive == "shared"

    local = parse_ref("raw.csv")
    filled = local.resolved(archive="postdoc", session="S-26-0300",
                            user="me@here.edu", archive_id="0fe")
    assert filled.archive_id == "0fe"


@pytest.mark.parametrize("text", [
    "nebula://",
    "nebula://grant@ncsu.edu",
    "nebula://postdoc~0fe/S-26-0152/a/b",
    "nebula://postdoc~0fe/collections",
    "nebula://postdoc~0fe/assets",
])
def test_malformed_new_grammar_uris_raise(text):
    with pytest.raises(ValueError):
        parse_ref(text)
