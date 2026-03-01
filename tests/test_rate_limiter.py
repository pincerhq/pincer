"""Tests for the rate limiter."""

import asyncio

import pytest

from pincer.exceptions import RateLimitExceeded
from pincer.security.rate_limiter import RateLimiter, TokenBucket


@pytest.mark.asyncio
async def test_token_bucket_allows_within_limit():
    bucket = TokenBucket(capacity=5, refill_rate=5 / 60)
    for _ in range(5):
        allowed, _ = await bucket.consume()
        assert allowed is True
    allowed, wait = await bucket.consume()
    assert allowed is False
    assert wait > 0


@pytest.mark.asyncio
async def test_token_bucket_refills():
    bucket = TokenBucket(capacity=2, refill_rate=100)
    await bucket.consume()
    await bucket.consume()
    await asyncio.sleep(0.05)
    allowed, _ = await bucket.consume()
    assert allowed is True


@pytest.mark.asyncio
async def test_message_rate_limit():
    limiter = RateLimiter(messages_per_minute=3)
    for _ in range(3):
        await limiter.check_message("user1")
    with pytest.raises(RateLimitExceeded) as exc_info:
        await limiter.check_message("user1")
    assert "too fast" in exc_info.value.message.lower()


@pytest.mark.asyncio
async def test_tool_call_rate_limit():
    limiter = RateLimiter(tool_calls_per_minute=2)
    await limiter.check_tool_call("u1")
    await limiter.check_tool_call("u1")
    with pytest.raises(RateLimitExceeded) as exc_info:
        await limiter.check_tool_call("u1")
    assert "tool" in exc_info.value.message.lower()


@pytest.mark.asyncio
async def test_daily_spend_limit():
    limiter = RateLimiter(max_daily_spend_usd=1.0)
    await limiter.record_spend(0.90)
    await limiter.check_daily_spend(0.05)
    with pytest.raises(RateLimitExceeded):
        await limiter.check_daily_spend(0.20)


@pytest.mark.asyncio
async def test_concurrent_llm_semaphore():
    limiter = RateLimiter(max_concurrent_llm=2)
    acquired = 0
    async with limiter.llm_semaphore():
        acquired += 1
        async with limiter.llm_semaphore():
            acquired += 1
    assert acquired == 2


@pytest.mark.asyncio
async def test_user_status():
    limiter = RateLimiter(messages_per_minute=10, max_daily_spend_usd=5.0)
    await limiter.check_message("u1")
    await limiter.record_spend(1.50)
    status = await limiter.get_status("u1")
    assert status["messages_remaining"] == 9
    assert status["daily_spend_usd"] == 1.50
    assert status["daily_spend_pct"] == 30.0


@pytest.mark.asyncio
async def test_per_user_isolation():
    limiter = RateLimiter(messages_per_minute=2)
    await limiter.check_message("alice")
    await limiter.check_message("alice")
    # Alice is rate-limited
    with pytest.raises(RateLimitExceeded):
        await limiter.check_message("alice")
    # Bob is not affected
    await limiter.check_message("bob")


@pytest.mark.asyncio
async def test_cleanup_user():
    limiter = RateLimiter(messages_per_minute=1)
    await limiter.check_message("u1")
    with pytest.raises(RateLimitExceeded):
        await limiter.check_message("u1")
    limiter.cleanup_user("u1")
    # After cleanup, user gets fresh bucket
    await limiter.check_message("u1")


@pytest.mark.asyncio
async def test_update_daily_limit():
    limiter = RateLimiter(max_daily_spend_usd=1.0)
    await limiter.update_daily_limit(10.0)
    await limiter.record_spend(5.0)
    await limiter.check_daily_spend(4.0)  # Should pass now
