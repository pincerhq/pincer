"""Google API errors: what the retry wrapper retries, and what counts as an outage (PR #316 review)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

from pincer.integrations.google.quota import is_google_outage, with_backoff


def _http_error(status: int, reason: str = "") -> HttpError:
    errors = [{"reason": reason, "message": reason}] if reason else []
    body = {"error": {"code": status, "message": f"HTTP {status}", "errors": errors}}
    return HttpError(SimpleNamespace(status=status, reason=f"HTTP {status}"), json.dumps(body).encode())


@pytest.mark.parametrize(
    ("status", "reason", "calls"),
    [
        (403, "rateLimitExceeded", 2),  # a per-minute limit: retried, the second attempt succeeds
        (500, "backendError", 2),
        (403, "quotaExceeded", 1),  # a daily quota: retrying for half a minute cannot help
        (403, "dailyLimitExceeded", 1),
        (403, "forbidden", 1),
    ],
)
@pytest.mark.usefixtures("instant_backoff")
async def test_only_short_lived_limits_are_retried(status, reason, calls):
    attempts: list[int] = []

    def _fn() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise _http_error(status, reason)
        return "ok"

    if calls == 1:
        with pytest.raises(HttpError):
            await with_backoff(_fn)
    else:
        assert await with_backoff(_fn) == "ok"
    assert len(attempts) == calls


@pytest.mark.parametrize(
    ("error", "outage"),
    [
        (_http_error(503), True),
        (_http_error(500), True),
        (_http_error(429), True),
        (_http_error(403, "rateLimitExceeded"), True),
        (_http_error(403, "userRateLimitExceeded"), True),
        (_http_error(403, "quotaExceeded"), True),
        (_http_error(403, "dailyLimitExceeded"), True),
        (_http_error(400, "backendError"), True),
        (_http_error(403, "forbidden"), False),
        (_http_error(400, "badRequest"), False),
        (_http_error(404, "notFound"), False),
        (ValueError("boom"), False),
    ],
)
def test_an_outage_is_google_down_or_limiting_us(error, outage):
    assert is_google_outage(error) is outage
