"""The rate-limit algorithms against a hand-driven clock (pincer.core.ratelimit.strategies)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pincer.core.ratelimit import Rate, SlidingWindowCounter, TokenBucket

if TYPE_CHECKING:
    from pincer.core.ratelimit.types import RateLimitResult


def _run(strategy: SlidingWindowCounter | TokenBucket, rate: Rate, times: list[float]) -> list[RateLimitResult]:
    state = None
    results = []
    for now in times:
        state, result = strategy.apply(state, now, rate)
        results.append(result)
    return results


def test_rate_validates() -> None:
    with pytest.raises(ValueError):
        Rate(0)
    with pytest.raises(ValueError):
        Rate(1, 0)
    assert Rate.per_minute(5) == Rate(5, 60.0)


def test_sliding_window_allows_up_to_the_limit_then_denies() -> None:
    results = _run(SlidingWindowCounter(), Rate(3, 60), [600.0, 601.0, 602.0, 603.0])
    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0]
    denied = results[-1]
    assert denied.retry_after == pytest.approx(57.0)  # the rest of this window: only the next one makes room
    assert denied.reset_after == pytest.approx(57.0)


def test_sliding_window_weighs_the_previous_window() -> None:
    strategy, rate = SlidingWindowCounter(), Rate(4, 60)
    state = None
    for now in (650.0, 651.0, 652.0, 653.0):  # 4 hits late in window 10
        state, _ = strategy.apply(state, now, rate)
    # 15 s into window 11, the previous window still weighs 4 * 0.75 = 3: one more fits.
    state, first = strategy.apply(state, 675.0, rate)
    state, second = strategy.apply(state, 675.0, rate)
    assert first.allowed
    assert not second.allowed
    # previous * (1 - x) + current + 1 <= 4 with previous=4, current=1  =>  x >= 0.5  =>  at 690 s
    assert second.retry_after == pytest.approx(15.0)
    # By then the old window has faded enough.
    _, later = strategy.apply(state, 690.0, rate)
    assert later.allowed


def test_sliding_window_forgets_windows_older_than_the_previous_one() -> None:
    strategy, rate = SlidingWindowCounter(), Rate(2, 60)
    state = None
    for now in (600.0, 601.0):
        state, _ = strategy.apply(state, now, rate)
    _, result = strategy.apply(state, 800.0, rate)  # three windows later
    assert result.allowed
    assert result.remaining == 1


def test_token_bucket_bursts_then_refills() -> None:
    results = _run(TokenBucket(), Rate(2, 60), [0.0, 0.0, 0.0, 30.0, 30.0])
    assert [r.allowed for r in results] == [True, True, False, True, False]
    assert results[2].retry_after == pytest.approx(30.0)  # one token per 30 s
    assert results[1].reset_after == pytest.approx(60.0)


def test_token_bucket_never_exceeds_its_capacity() -> None:
    results = _run(TokenBucket(), Rate(2, 60), [0.0, 10_000.0, 10_000.0, 10_000.0])
    assert [r.allowed for r in results] == [True, True, True, False]
