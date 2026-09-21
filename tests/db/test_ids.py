"""UUIDv7 row identifiers.

Two properties carry the weight of the whole phase, so they are asserted
directly rather than inferred:

* ids sort by time **as strings**, because SQLite compares these columns as
  TEXT and several reads order by the id to break a timestamp tie;
* a backfilled id is shaped exactly like a minted one, so nothing downstream
  can tell them apart.
"""

from __future__ import annotations

import time
import uuid

import pytest

from pincer.db.ids import Uuid7Sequence, new_id, uuid7_at


def _bits(value: str) -> tuple[int, int, int]:
    """(version, variant, timestamp ms) read straight off the integer."""
    as_int = uuid.UUID(value).int
    return (as_int >> 76) & 0xF, (as_int >> 62) & 0b11, as_int >> 80


# ── the minted id ────────────────────────────────────────────────────


def test_a_minted_id_is_a_v7_uuid_stamped_with_now():
    before = time.time_ns() // 1_000_000
    version, variant, ms = _bits(new_id())
    after = time.time_ns() // 1_000_000

    assert version == 7
    assert variant == 0b10  # RFC 4122/9562
    assert before <= ms <= after


def test_minted_ids_sort_in_the_order_they_were_minted():
    """As strings, not just as integers: SQLite compares these as TEXT.

    A tight loop puts many of them inside one millisecond, which is exactly
    the case an autoincrement tiebreaker used to cover.
    """
    ids = [new_id() for _ in range(10_000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)

    stamps = {uuid.UUID(value).int >> 80 for value in ids}
    assert len(stamps) < len(ids), "the run was too slow to prove same-millisecond ordering"


# ── the backfilled id ────────────────────────────────────────────────


def test_a_backfilled_id_carries_the_timestamp_it_was_given():
    stamp = 1_600_000_000_000
    version, variant, ms = _bits(uuid7_at(stamp))
    assert (version, variant, ms) == (7, 0b10, stamp)


def test_a_backfilled_id_is_shaped_like_a_minted_one():
    """Same version and variant bits, so no caller can tell them apart."""
    assert _bits(uuid7_at(1_600_000_000_000))[:2] == _bits(new_id())[:2]


def test_the_counter_orders_ids_inside_one_millisecond():
    stamp = 1_600_000_000_000
    ids = [uuid7_at(stamp, counter) for counter in range(500)]
    assert ids == sorted(ids)
    assert {uuid.UUID(value).int >> 80 for value in ids} == {stamp}


def test_a_counter_at_its_limits_stays_inside_its_own_bits():
    """The 42-bit counter is split either side of the variant.

    Small counters only exercise the low half, so a wrong shift or an
    over-wide mask would go unnoticed until a value crossed into the high
    half — where it would corrupt the version or variant nibble rather than
    merely mis-order.
    """
    stamp = 1_600_000_000_000
    for counter in (0, 2**30 - 1, 2**30, 2**42 - 1):
        version, variant, ms = _bits(uuid7_at(stamp, counter))
        assert (version, variant, ms) == (7, 0b10, stamp), counter

    # and the split is monotonic across the boundary it spans
    across = [uuid7_at(stamp, counter) for counter in (2**30 - 2, 2**30 - 1, 2**30, 2**30 + 1)]
    assert across == sorted(across)


def test_a_timestamp_or_counter_that_does_not_fit_is_refused():
    with pytest.raises(ValueError, match="48 bits"):
        uuid7_at(2**48)
    with pytest.raises(ValueError, match="42 bits"):
        uuid7_at(1_600_000_000_000, 2**42)


# ── the backfill sequence ────────────────────────────────────────────


def test_the_sequence_is_strictly_increasing_whatever_it_is_fed():
    """Ascending, tied, backwards and missing timestamps, in one stream.

    This is the backfill's whole correctness argument: rows fed in their
    original order come out in that order regardless of what their
    creation-time column says — including the four columns that are nullable.
    """
    stamps = [1_000, 2_000, 2_000, 2_000, 1_500, None, None, 3_000, None, 2_999]
    sequence = Uuid7Sequence()
    ids = [sequence.next(stamp) for stamp in stamps]

    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_the_sequence_keeps_a_real_timestamp_when_time_moves_forward():
    sequence = Uuid7Sequence()
    sequence.next(1_000)
    assert _bits(sequence.next(5_000))[2] == 5_000


def test_the_sequence_advances_the_clock_when_the_counter_runs_out():
    """Unreachable in practice — 4.4 trillion rows in one millisecond — but
    the branch exists so that the counter can never carry into the variant
    bits, and without it the sequence would repeat an id."""
    sequence = Uuid7Sequence()
    sequence._ms = 1_600_000_000_000  # noqa: SLF001 - the overflow is otherwise unreachable
    sequence._counter = 2**42 - 2  # noqa: SLF001
    before, at_limit, after = sequence.next(None), sequence.next(None), sequence.next(None)
    assert before < at_limit < after
    assert _bits(after)[2] == 1_600_000_000_001, "the clock did not advance past the overflow"


def test_the_sequence_starts_at_the_epoch_when_it_has_nothing_to_go_on():
    """A NULL creation time on the very first row sorts it first, which is
    where "we do not know when this was written" belongs."""
    assert _bits(Uuid7Sequence().next(None))[2] == 0
