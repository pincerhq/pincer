"""Google API errors: what the retry wrapper retries, and what counts as an outage (PR #316 review)."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from googleapiclient.errors import HttpError

from pincer.integrations.google import quota
from pincer.integrations.google.quota import error_reasons, is_google_outage, with_backoff

if TYPE_CHECKING:
    from collections.abc import Callable


def _http_error(status: int, reason: str = "") -> HttpError:
    errors = [{"reason": reason, "message": reason}] if reason else []
    body = {"error": {"code": status, "message": f"HTTP {status}", "errors": errors}}
    return HttpError(SimpleNamespace(status=status, reason=f"HTTP {status}"), json.dumps(body).encode())


def _raising(error: Exception, *, times: int) -> tuple[Callable[[], str], list[int]]:
    """A call that raises *error* on its first *times* attempts, then returns ``"ok"``; and its attempts."""
    attempts: list[int] = []

    def _fn() -> str:
        attempts.append(1)
        if len(attempts) <= times:
            raise error
        return "ok"

    return _fn, attempts


@pytest.mark.parametrize(
    ("status", "reason", "calls"),
    [
        (403, "rateLimitExceeded", 2),  # a per-minute limit: retried, the second attempt succeeds
        (500, "backendError", 2),
        (403, "quotaExceeded", 1),  # a daily quota: retrying for half a minute cannot help
        (403, "dailyLimitExceeded", 1),
        (403, "forbidden", 1),
        (429, "", 2),  # a bare 429 is a short-lived limit: retried
        (429, "quotaExceeded", 1),  # an exhausted quota is not retried on a 429 either
        (429, "dailyLimitExceeded", 1),
    ],
)
@pytest.mark.usefixtures("instant_backoff")
async def test_only_short_lived_limits_are_retried(status, reason, calls):
    _fn, attempts = _raising(_http_error(status, reason), times=1)

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


def _error_info_http_error(status: int, error_info_reason: str, *, legacy_reason: str = "") -> HttpError:
    """A body with a google.rpc ErrorInfo ``details`` array (UPPER_SNAKE reason), optionally beside the
    legacy ``errors`` array. googleapiclient (2.194.0, errors.py:64-73) fills ``error_details`` from the
    first of ``detail``, ``details``, ``errors``, ``message`` it finds, so ``details`` hides ``errors``."""
    error: dict[str, object] = {
        "code": status,
        "message": "Rate Limit Exceeded",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": error_info_reason,
                "domain": "googleapis.com",
            }
        ],
    }
    if legacy_reason:
        error["errors"] = [{"reason": legacy_reason, "message": "Rate Limit Exceeded", "domain": "usageLimits"}]
    return HttpError(SimpleNamespace(status=status, reason=f"HTTP {status}"), json.dumps({"error": error}).encode())


@pytest.mark.parametrize(
    "error",
    [
        _error_info_http_error(403, "RATE_LIMIT_EXCEEDED", legacy_reason="rateLimitExceeded"),
        _error_info_http_error(403, "RATE_LIMIT_EXCEEDED"),
        _error_info_http_error(403, "QUOTA_EXCEEDED"),
        _error_info_http_error(429, "QUOTA_EXCEEDED"),
    ],
    ids=["details-and-errors", "error-info-only", "error-info-quota", "error-info-quota-429"],
)
def test_a_limit_reported_in_error_info_details_is_an_outage(error):
    """Review 3 (Q1): a 403 whose body carries ``details`` was read as a bad request, so the reminder
    loop logged a traceback every tick instead of treating it as Google limiting us."""
    assert is_google_outage(error) is True


def test_error_reasons_reads_both_the_errors_array_and_error_info_details():
    error = _error_info_http_error(403, "USER_RATE_LIMIT_EXCEEDED", legacy_reason="rateLimitExceeded")

    assert error_reasons(error) == {"rateLimitExceeded", "userRateLimitExceeded"}


@pytest.mark.usefixtures("instant_backoff")
async def test_a_429_for_an_exhausted_quota_in_error_info_is_not_retried():
    """Issue #333: a 429 RESOURCE_EXHAUSTED whose ErrorInfo reason is QUOTA_EXCEEDED was retried five
    times on a quota that only resets daily."""
    error = _error_info_http_error(429, "QUOTA_EXCEEDED")
    _fn, attempts = _raising(error, times=5)

    with pytest.raises(HttpError):
        await with_backoff(_fn)
    assert len(attempts) == 1
    assert is_google_outage(error) is True


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.usefixtures("instant_backoff")
async def test_an_exhausted_quota_beside_a_short_lived_limit_is_not_retried(status):
    """PR #425 review: a body naming both a rate limit and an exhausted quota is not retried. The
    exhausted reason wins: the daily quota still blocks the call once the per-minute limit clears."""
    error = _error_info_http_error(status, "QUOTA_EXCEEDED", legacy_reason="rateLimitExceeded")
    _fn, attempts = _raising(error, times=5)

    assert error_reasons(error) == frozenset({"quotaExceeded", "rateLimitExceeded"})
    with pytest.raises(HttpError):
        await with_backoff(_fn)
    assert len(attempts) == 1
    assert is_google_outage(error) is True


@pytest.mark.usefixtures("instant_backoff")
async def test_the_retry_wrapper_and_the_outage_check_share_one_parse_of_the_body(monkeypatch):
    """Issue #333: the retry check and the outage check each re-parsed the body. A 403 makes both
    reach the reasons (a 429 is an outage on its status alone). Only quota's own ``json`` is counted;
    googleapiclient's parse in ``HttpError.__init__`` is outside this module."""
    error = _error_info_http_error(403, "QUOTA_EXCEEDED")
    parses: list[int] = []

    def _counting_loads(*args: Any, **kwargs: Any) -> Any:
        parses.append(1)
        return json.loads(*args, **kwargs)

    monkeypatch.setattr(quota, "json", SimpleNamespace(loads=_counting_loads))
    _fn, _ = _raising(error, times=1)

    with pytest.raises(HttpError):
        await with_backoff(_fn)
    assert is_google_outage(error) is True
    assert len(parses) == 1


@pytest.mark.usefixtures("instant_backoff")
async def test_a_non_json_body_has_no_reasons_and_falls_back_to_the_status():
    """Issue #333: a body that is not JSON (an HTML error page) is decided by its status code alone."""
    error = HttpError(SimpleNamespace(status=503, reason="HTTP 503"), b"<html>Service Unavailable</html>")
    _fn, attempts = _raising(error, times=1)

    assert error_reasons(error) == frozenset()
    assert is_google_outage(error) is True
    assert await with_backoff(_fn) == "ok"
    assert len(attempts) == 2


@pytest.mark.parametrize(
    "content",
    [b'{"error": "Rate Limit Exceeded"}', b'{"message": "oops"}', b"[]", b'"oops"', b"\xff\xfe"],
    ids=["error-is-a-string", "no-error-key", "empty-list", "json-string", "not-utf-8"],
)
@pytest.mark.usefixtures("instant_backoff")
async def test_a_body_not_shaped_like_a_google_error_has_no_reasons(content):
    """Issue #333: valid JSON that is not a Google error body (or bytes that are not UTF-8) gives no
    reasons, so a 429 is still retried on its status code alone."""
    error = HttpError(SimpleNamespace(status=429, reason="HTTP 429"), content)
    _fn, attempts = _raising(error, times=1)

    assert error_reasons(error) == frozenset()
    assert await with_backoff(_fn) == "ok"
    assert len(attempts) == 2


@pytest.mark.usefixtures("instant_backoff")
async def test_a_list_wrapped_error_body_is_read():
    """Some Google APIs wrap the error body in a list: its ``errors[]`` reasons still count."""
    body = [{"error": {"code": 429, "message": "Quota", "errors": [{"reason": "dailyLimitExceeded"}]}}]
    error = HttpError(SimpleNamespace(status=429, reason="HTTP 429"), json.dumps(body).encode())
    _fn, attempts = _raising(error, times=5)

    assert error_reasons(error) == frozenset({"dailyLimitExceeded"})
    with pytest.raises(HttpError):
        await with_backoff(_fn)
    assert len(attempts) == 1


@pytest.mark.usefixtures("instant_backoff")
async def test_anything_but_a_google_http_error_has_no_reasons():
    error = ValueError("boom")
    _fn, attempts = _raising(error, times=5)

    assert error_reasons(error) == frozenset()
    assert is_google_outage(error) is False
    with pytest.raises(ValueError, match="boom"):
        await with_backoff(_fn)
    assert len(attempts) == 1


@pytest.mark.usefixtures("instant_backoff")
async def test_a_limit_that_outlasts_every_retry_is_raised():
    error = _http_error(503)
    _fn, attempts = _raising(error, times=10)

    with pytest.raises(HttpError) as raised:
        await with_backoff(_fn, max_retries=2)
    assert raised.value is error
    assert len(attempts) == 3


def test_without_googleapiclient_nothing_is_a_google_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "googleapiclient.errors", None)  # makes the import raise ImportError

    assert is_google_outage(_http_error(503)) is False
    assert error_reasons(_http_error(403, "quotaExceeded")) == frozenset()
