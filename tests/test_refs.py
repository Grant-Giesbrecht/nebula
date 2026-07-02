import pytest

from nebula.refs import Ref, parse_ref, format_ref


def test_bare_filename():
    ref = parse_ref("diode.graf")
    assert ref == Ref(file="diode.graf", session=None, archive=None)
    assert format_ref(ref) == "diode.graf"


def test_bare_session():
    ref = parse_ref("S-0152")
    assert ref == Ref(file=None, session="S-0152", archive=None)
    assert format_ref(ref) == "S-0152"


def test_session_and_file():
    ref = parse_ref("S-0152/diode.graf")
    assert ref == Ref(file="diode.graf", session="S-0152", archive=None)
    assert format_ref(ref) == "S-0152/diode.graf"


def test_cross_archive():
    ref = parse_ref("postdoc|S-0152/diode.graf")
    assert ref == Ref(file="diode.graf", session="S-0152", archive="postdoc")
    assert format_ref(ref) == "postdoc|S-0152/diode.graf"
    assert ref.is_cross_archive()


def test_cross_archive_whole_session():
    ref = parse_ref("postdoc|S-0152")
    assert ref == Ref(file=None, session="S-0152", archive="postdoc")
    assert format_ref(ref) == "postdoc|S-0152"


def test_round_trip_many():
    cases = [
        "diode.graf",
        "S-0152",
        "S-0152/diode.graf",
        "postdoc|S-0152/diode.graf",
        "postdoc|S-0152",
        "audio|S-0001/scope_trace_raw.csv",
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
        parse_ref("|S-0152")


def test_malformed_empty_after_slash():
    with pytest.raises(ValueError):
        parse_ref("S-0152/")


def test_is_same_session():
    assert parse_ref("diode.graf").is_same_session()
    assert not parse_ref("S-0152/diode.graf").is_same_session()


def test_resolved_fills_in_context():
    ref = parse_ref("diode.graf")
    resolved = ref.resolved(archive="postdoc", session="S-0300")
    assert resolved == Ref(file="diode.graf", session="S-0300", archive="postdoc")

    # already-explicit fields are left alone
    ref2 = parse_ref("postdoc|S-0152/diode.graf")
    resolved2 = ref2.resolved(archive="audio", session="S-0999")
    assert resolved2 == ref2
