"""
Immutable archive ids: the durable half of a nebula URI's archive segment.

    nebula://grant@github.com/postdoc~0fe/S-26-1234/artifact.tome
                              ^^^^^^^ ^^^
                              label   id

An archive's *name* is a label people change. Its *id* is minted once and
never changes, so a ref survives a rename. See ``docs/uri-grammar.md`` for
the argument; this module is the arithmetic.

**An id is a number, not a token.** ``0fe``, ``00fe``, ``fe`` and ``0FE`` are
all the same id, exactly as 7 and 0007 are one number. Two consequences, and
they are the whole reason the design works:

* Normalising is *lexical* -- parse as hex, render canonically. No registry,
  no resolution, no ambiguity, so ``refs`` can compare two refs without
  asking the filesystem anything. (The alternative, a fixed-width token with
  git-style prefix matching, cannot: a prefix is ambiguous until something
  resolves it.)
* The namespace has no width, so it never has to be *widened*. ``0fe`` and
  ``1a2b3c`` were always in one space; you had merely not reached the large
  numbers yet. No id ever has to change.

Canonical form is lowercase hex zero-padded to :data:`MIN_WIDTH`, which is
cosmetic only -- it keeps archive segments looking uniform.
"""

from __future__ import annotations

import random
import re
from typing import Iterable, Optional, Set

#: Rendered ids are padded to at least this many hex digits. Purely how it
#: looks: 0xfe is 0xfe whether it is written `fe` or `0fe`.
MIN_WIDTH = 3

#: Reserved. Never minted, so it can mean "this archive has no id" in any
#: context where a value is required.
NO_ID = 0

#: Widen the minting range once a range is this full. Below it, a random
#: draw lands on a free id with probability > 1 - LOAD_FACTOR, so minting
#: costs a bounded number of draws no matter how many archives exist. This
#: is the number that makes "when do we bump the range?" a calculation
#: rather than something discovered by failing repeatedly -- see
#: `next_free_width`.
LOAD_FACTOR = 0.5

#: Give up on a range and widen instead. Only reachable if `random` is
#: behaving pathologically or a caller passed a stale `taken` set, since
#: the load factor already bounds the expected count at ~2.
_MAX_DRAWS = 64

_ID_RE = re.compile(r"^[0-9a-fA-F]+$")


class ArchiveIdError(ValueError):
    """A string that cannot be an archive id."""


def is_archive_id(text: str) -> bool:
    """Whether `text` is shaped like an id. Cheap, and total: used to tell
    an archive segment from a user segment, so it must never raise."""
    return bool(text) and bool(_ID_RE.match(text.strip()))


def value_of(text: str) -> int:
    """The number an id string denotes. Raises on anything else."""
    raw = (text or "").strip()
    if not _ID_RE.match(raw):
        raise ArchiveIdError(
            f"an archive id is hexadecimal, like 0fe: {text!r}")
    return int(raw, 16)


def render(value: int) -> str:
    """The canonical spelling of an id value."""
    if value < 0:
        raise ArchiveIdError(f"an archive id cannot be negative: {value}")
    return f"{value:0{MIN_WIDTH}x}"


def normalize(text: str) -> str:
    """Canonicalise an id as written. `00FE` -> `0fe`.

    Every stored ref goes through this, which is what makes two spellings of
    one id compare equal without anything having to resolve them.
    """
    return render(value_of(text))


def normalize_or_none(text: Optional[str]) -> Optional[str]:
    """`normalize`, but None passes through and a malformed id does too.

    Reading is permissive throughout nebula: a ref carrying a nonsense id is
    a ref that will not resolve, which the caller reports -- it is not a
    reason to refuse to *parse* the surrounding provenance record. Callers
    that need certainty use `value_of`.
    """
    if text is None:
        return None
    raw = text.strip()
    if not raw:
        return None
    try:
        return normalize(raw)
    except ArchiveIdError:
        return raw


def same_id(a: Optional[str], b: Optional[str]) -> bool:
    """Whether two id strings denote the same id. Absent on either side is
    not a match: "no id" is not a wildcard, it is a different archive
    segment shape that has to be resolved by name instead."""
    if a is None or b is None:
        return False
    try:
        return value_of(a) == value_of(b)
    except ArchiveIdError:
        return a.strip().lower() == b.strip().lower()


# ---------------------------------------------------------------------
# minting
# ---------------------------------------------------------------------

def range_for(width: int) -> "tuple[int, int]":
    """The inclusive span `[lo, hi]` of ids `width` hex digits wide.

    Width 3 starts at 0x100 rather than 0: leading zeros are not part of the
    value, so an id below 0x100 would render shorter than MIN_WIDTH and the
    uniform look would be lost for no gain. It also keeps 0 free as
    :data:`NO_ID`.
    """
    if width < MIN_WIDTH:
        raise ArchiveIdError(f"ids are at least {MIN_WIDTH} hex digits wide")
    return 16 ** (width - 1), 16 ** width - 1


def occupancy(taken: Iterable[str], width: int) -> "tuple[int, int]":
    """`(used, capacity)` for the range of the given width.

    One pass over the ids this machine knows about -- tens of them, not
    millions -- which is why the widening decision can be *computed* instead
    of discovered by minting until it fails.
    """
    lo, hi = range_for(width)
    values = _values(taken)
    used = sum(1 for v in values if lo <= v <= hi)
    return used, hi - lo + 1


def next_free_width(taken: Iterable[str], *, start: int = MIN_WIDTH) -> int:
    """The narrowest range still loosely enough packed to draw from.

    Probing until a draw happens to fail is the obvious implementation and
    the wrong one: as a range fills, the expected number of draws grows
    without bound, and a *full* range never terminates. Counting occupancy
    up front turns that into arithmetic, and the answer stays honest as
    archives are added and removed.
    """
    return _width_for(_values(taken), start)


def _width_for(values: Set[int], start: int = MIN_WIDTH) -> int:
    width = max(start, MIN_WIDTH)
    while True:
        lo, hi = range_for(width)
        used = sum(1 for v in values if lo <= v <= hi)
        if used < (hi - lo + 1) * LOAD_FACTOR:
            return width
        width += 1


def mint(taken: Iterable[str], *, rng: Optional[random.Random] = None) -> str:
    """A fresh id that collides with nothing in `taken`.

    Drawn at random rather than allocated sequentially: a counter would hand
    the same id to two archives created on two machines, and surviving the
    trip off this machine is the entire point of having an id.

    `taken` is every id this machine knows about -- normally every
    registered archive. It is not, and cannot be, every id in the world; see
    ``docs/uri-grammar.md`` on the collision that check cannot cover.
    """
    rng = rng or random.SystemRandom()
    values = _values(taken)
    width = _width_for(values)
    while True:
        lo, hi = range_for(width)
        for _ in range(_MAX_DRAWS):
            candidate = rng.randint(lo, hi)
            if candidate not in values:
                return render(candidate)
        width += 1


def _values(taken: Iterable[str]) -> Set[int]:
    """The ids we can make sense of, as numbers. Anything malformed is
    dropped rather than raising: a junk id in one archive.yaml must not stop
    another archive from being created."""
    out: Set[int] = set()
    for one in taken or ():
        if isinstance(one, int):
            out.add(one)
            continue
        try:
            out.add(value_of(one))
        except (ArchiveIdError, AttributeError, TypeError):
            continue
    return out
