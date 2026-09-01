"""
Archive ids: the arithmetic, and the property the whole design rests on.

The load-bearing claim is that an id is a *number*, not a token -- so `0fe`
and `00fe` are one id, normalising needs no registry, and the namespace
never has to be widened because it was never narrow.
"""

import random

import pytest

from nebula import archive_id


# ---------------------------------------------------------------------
# an id is a number
# ---------------------------------------------------------------------

@pytest.mark.parametrize("written", ["0fe", "00fe", "fe", "0FE", "000000fe",
                                     "  0fe  "])
def test_leading_zeros_and_case_do_not_change_an_id(written):
    assert archive_id.normalize(written) == "0fe"
    assert archive_id.value_of(written) == 0xFE


def test_two_spellings_of_one_id_compare_equal():
    assert archive_id.same_id("0fe", "00fe")
    assert archive_id.same_id("0FE", "fe")
    assert not archive_id.same_id("0fe", "0ff")


def test_an_absent_id_is_not_a_wildcard():
    """"No id" means "resolve me by name instead", not "matches anything" --
    a wildcard would make every id-less archive answer to every ref."""
    assert not archive_id.same_id(None, "0fe")
    assert not archive_id.same_id("0fe", None)
    assert not archive_id.same_id(None, None)


def test_rendering_pads_to_the_minimum_width_and_no_further():
    assert archive_id.render(0xFE) == "0fe"
    assert archive_id.render(0x1A2B3C) == "1a2b3c"


def test_a_short_id_is_not_a_prefix_of_a_long_one():
    """The property that makes "start short, grow later" safe: ids of
    different lengths are simply different numbers, so nothing written
    earlier becomes ambiguous when later ones get longer."""
    assert not archive_id.same_id("0fe", "0fe123")
    assert archive_id.value_of("0fe") != archive_id.value_of("0fe123")


@pytest.mark.parametrize("bad", ["", "  ", "0x1f", "ghi", "12-34", "0fe~"])
def test_malformed_ids_are_refused(bad):
    with pytest.raises(archive_id.ArchiveIdError):
        archive_id.value_of(bad)
    assert not archive_id.is_archive_id(bad)


def test_reading_is_permissive_where_parsing_a_record_is_at_stake():
    """A nonsense id in one ref must not stop the surrounding provenance
    record from being read; it just will not resolve."""
    assert archive_id.normalize_or_none(None) is None
    assert archive_id.normalize_or_none("") is None
    assert archive_id.normalize_or_none("nonsense") == "nonsense"
    assert archive_id.normalize_or_none("00FE") == "0fe"


# ---------------------------------------------------------------------
# minting
# ---------------------------------------------------------------------

def test_a_fresh_id_is_three_digits_and_in_range():
    got = archive_id.mint([])
    assert len(got) == archive_id.MIN_WIDTH
    lo, hi = archive_id.range_for(3)
    assert lo <= archive_id.value_of(got) <= hi


def test_minting_never_returns_a_taken_id():
    taken = {archive_id.render(v) for v in range(0x100, 0x180)}
    for _ in range(50):
        assert archive_id.mint(taken) not in taken


def test_minting_never_returns_the_reserved_zero():
    """`~0` means "no id", so it has to stay unmintable."""
    lo, _ = archive_id.range_for(archive_id.MIN_WIDTH)
    assert lo > archive_id.NO_ID
    for _ in range(50):
        assert archive_id.value_of(archive_id.mint([])) != archive_id.NO_ID


def test_a_spelling_of_a_taken_id_still_counts_as_taken():
    """Occupancy is on values, not strings -- otherwise `00fe` would look
    free while `0fe` was taken, and the two are one id."""
    taken = ["000000fe"]
    for _ in range(50):
        assert archive_id.value_of(archive_id.mint(taken)) != 0xFE


def test_a_junk_id_in_the_taken_set_does_not_stop_minting():
    """One archive.yaml with a corrupt id must not prevent another archive
    from being created."""
    assert archive_id.mint(["nonsense", None, "0fe"])


# ---------------------------------------------------------------------
# widening: computed, not discovered
# ---------------------------------------------------------------------

def test_an_empty_namespace_uses_the_narrowest_range():
    assert archive_id.next_free_width([]) == archive_id.MIN_WIDTH


def test_the_range_widens_once_it_passes_the_load_factor():
    lo, hi = archive_id.range_for(3)
    capacity = hi - lo + 1
    threshold = int(capacity * archive_id.LOAD_FACTOR)

    just_under = [archive_id.render(v) for v in range(lo, lo + threshold - 1)]
    assert archive_id.next_free_width(just_under) == 3

    over = [archive_id.render(v) for v in range(lo, lo + threshold + 1)]
    assert archive_id.next_free_width(over) == 4


def test_widening_is_decided_without_drawing_anything():
    """The point of counting occupancy: a *full* range must not be
    discovered by minting until it fails, which for a full range never
    terminates. A width is chosen up front, so a completely full 3-digit
    space still mints instantly."""
    lo, hi = archive_id.range_for(3)
    full = [archive_id.render(v) for v in range(lo, hi + 1)]

    assert archive_id.next_free_width(full) == 4
    got = archive_id.mint(full)
    assert len(got) == 4
    assert got not in full


def test_ids_already_wider_than_the_current_range_do_not_force_a_widen():
    """A single 6-digit id says nothing about how full the 3-digit range
    is, so it must not push everyone else into longer ids."""
    assert archive_id.next_free_width(["1a2b3c"]) == archive_id.MIN_WIDTH


def test_occupancy_reports_what_the_decision_is_made_on():
    lo, _ = archive_id.range_for(3)
    used, capacity = archive_id.occupancy(
        [archive_id.render(v) for v in range(lo, lo + 10)], 3)
    assert used == 10
    assert capacity == 3840          # 0x100..0xFFF


def test_minting_stays_cheap_as_the_namespace_fills():
    """With the load factor bounding occupancy below 50%, a draw succeeds
    with probability > 0.5, so this is a couple of draws each rather than
    an unbounded search."""
    rng = random.Random(1234)
    taken = set()
    for _ in range(400):
        got = archive_id.mint(taken, rng=rng)
        assert got not in taken
        taken.add(got)
    assert len(taken) == 400


def test_ranges_do_not_overlap():
    lo3, hi3 = archive_id.range_for(3)
    lo4, hi4 = archive_id.range_for(4)
    assert hi3 + 1 == lo4
    assert (hi3 - lo3 + 1, hi4 - lo4 + 1) == (0xF00, 0xF000)


def test_there_is_no_range_below_the_minimum_width():
    with pytest.raises(archive_id.ArchiveIdError):
        archive_id.range_for(2)
