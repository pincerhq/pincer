"""The rate-limit algorithms against a hand-driven clock (pincer.core.ratelimit.strategies)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pincer.core.ratelimit import Rate, SlidingWindowStrategy, Strategy, TokenBucketStrategy

if TYPE_CHECKING:
    from pincer.core.ratelimit.types import RateLimitResult


def _run(strategy: Strategy, rate: Rate, times: list[float]) -> list[RateLimitResult]:
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
    results = _run(SlidingWindowStrategy(), Rate(3, 60), [600.0, 601.0, 602.0, 603.0])
    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0]
    denied = results[-1]
    # Only the next window makes room, where these 3 hits still weigh 3 * (1 - x):
    # 3 * (1 - x) + 1 <= 3  =>  x >= 1/3  =>  57 s + 20 s.
    assert denied.retry_after == pytest.approx(77.0)
    assert denied.reset_after == pytest.approx(57.0)


def test_sliding_window_retry_after_is_honest_when_the_window_is_full() -> None:
    """Waiting exactly `retry_after` after a full window is allowed, not refused again."""
    strategy, rate = SlidingWindowStrategy(), Rate(10, 60)
    state = None
    for _ in range(10):
        state, _ = strategy.apply(state, 630.0, rate)
    state, denied = strategy.apply(state, 631.0, rate)
    assert not denied.allowed
    assert denied.retry_after == pytest.approx(29.0 + 6.0)  # 10 * (1 - x) + 1 <= 10  =>  x >= 0.1
    _, too_early = strategy.apply(state, 631.0 + denied.retry_after - 0.5, rate)
    assert not too_early.allowed
    _, retried = strategy.apply(state, 631.0 + denied.retry_after, rate)
    assert retried.allowed


def test_sliding_window_weighs_the_previous_window() -> None:
    strategy, rate = SlidingWindowStrategy(), Rate(4, 60)
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
    strategy, rate = SlidingWindowStrategy(), Rate(2, 60)
    state = None
    for now in (600.0, 601.0):
        state, _ = strategy.apply(state, now, rate)
    _, result = strategy.apply(state, 800.0, rate)  # three windows later
    assert result.allowed
    assert result.remaining == 1


def test_token_bucket_bursts_then_refills() -> None:
    results = _run(TokenBucketStrategy(), Rate(2, 60), [0.0, 0.0, 0.0, 30.0, 30.0])
    assert [r.allowed for r in results] == [True, True, False, True, False]
    assert results[2].retry_after == pytest.approx(30.0)  # one token per 30 s
    assert results[1].reset_after == pytest.approx(60.0)


def test_token_bucket_never_exceeds_its_capacity() -> None:
    results = _run(TokenBucketStrategy(), Rate(2, 60), [0.0, 10_000.0, 10_000.0, 10_000.0])
    assert [r.allowed for r in results] == [True, True, True, False]


def test_strategies_share_the_base_class_and_registry() -> None:
    from pincer.core.ratelimit.strategies import STRATEGIES

    assert {"sliding_window": SlidingWindowStrategy, "token_bucket": TokenBucketStrategy} == STRATEGIES
    assert all(issubclass(cls, Strategy) for cls in STRATEGIES.values())
    assert SlidingWindowStrategy().ttl(Rate(1, 30)) == 60


def test_a_strategy_must_define_name_and_script() -> None:
    with pytest.raises(TypeError, match="must define a non-empty `script`"):

        class NoScript(Strategy):
            name = "no_script"

            def apply(self, state, now, rate, cost=1):  # type: ignore[no-untyped-def]
                raise NotImplementedError

    with pytest.raises(TypeError, match="abstract"):

        class Unfinished(Strategy):
            name = "x"
            script = "return 1"

        Unfinished()  # type: ignore[abstract]


@pytest.mark.parametrize(
    ("text", "rate"),
    [("20/minute", Rate(20, 60)), ("500/day", Rate(500, 86400)), ("1/Hours", Rate(1, 3600)), ("3/90s", Rate(3, 90))],
)
def test_rate_parse(text: str, rate: Rate) -> None:
    assert Rate.parse(text) == rate
    assert Rate.parse(rate) is rate


@pytest.mark.parametrize("text", ["", "20", "20/fortnight", "x/minute", "0/minute"])
def test_rate_parse_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        Rate.parse(text)
