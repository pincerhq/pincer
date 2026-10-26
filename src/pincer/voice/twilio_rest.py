"""Async access to the Twilio REST API for the voice stack.

Every REST round-trip used to be a blocking `twilio.rest.Client` call made from
a coroutine — 150–800 ms during which no other call's audio moved, and a cold
TLS handshake each time because every site built a new Client. This module is
the only place that may import `twilio.rest` (ruff's banned-api rule, TID251,
enforces it); the voice code awaits it.

One instance per engine: the Client is built on first use with
`AsyncTwilioHttpClient`, whose aiohttp session is bound to the running loop
and reused for every later request until `aclose()`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# A hung Twilio API must not hang `end_call`: `_hangup_after` and the shutdown
# drain wait on it. Enforced here because AsyncTwilioHttpClient accepts a
# `timeout` argument but never applies it (twilio 9.x passes None to aiohttp,
# whose own default is 300 s).
REQUEST_TIMEOUT_S = 10.0


class DialOutcomeUnknown(Exception):
    """`create_call` failed after the POST may have reached Twilio: a timeout,
    or the connection dropped while the response was on its way. The call may
    have been placed. A Twilio error response (`TwilioRestException`) or a
    connection that was never made is a definite "not placed" and is raised
    as itself."""


def _outcome_unknown(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    try:
        import aiohttp
    except ImportError:  # pragma: no cover — aiohttp ships with the voice extra
        return isinstance(exc, ConnectionError)
    if isinstance(exc, aiohttp.ClientConnectorError):
        return False  # never connected: nothing was sent
    return isinstance(exc, (aiohttp.ClientError, ConnectionError))


def _setting(settings: Any, name: str) -> str | None:
    value = getattr(settings, name, "")
    return (value.strip() or None) if isinstance(value, str) else None


class TwilioRest:
    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def _get(self) -> Any:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    try:
                        from twilio.http.async_http_client import AsyncTwilioHttpClient
                        from twilio.rest import Client  # noqa: TID251 — the one sanctioned import
                    except ImportError as e:
                        raise RuntimeError("twilio extra not installed — uv pip install 'pincer-agent[voice]'") from e

                    self._client = Client(
                        self._settings.twilio_account_sid,
                        self._settings.twilio_auth_token.get_secret_value(),
                        http_client=AsyncTwilioHttpClient(),
                        edge=_setting(self._settings, "twilio_edge"),
                        region=_setting(self._settings, "twilio_region"),
                    )
        return self._client

    async def update_call(self, call_sid: str, *, twiml: str | None = None, status: str | None = None) -> Any:
        client = await self._get()
        kwargs: dict[str, Any] = {}
        if twiml is not None:
            kwargs["twiml"] = twiml
        if status is not None:
            kwargs["status"] = status
        return await asyncio.wait_for(client.calls(call_sid).update_async(**kwargs), REQUEST_TIMEOUT_S)

    async def create_call(self, **kwargs: Any) -> Any:
        """Raises `DialOutcomeUnknown` when Twilio may have placed the call anyway."""
        client = await self._get()
        try:
            return await asyncio.wait_for(client.calls.create_async(**kwargs), REQUEST_TIMEOUT_S)
        except Exception as exc:
            if _outcome_unknown(exc):
                raise DialOutcomeUnknown(f"{type(exc).__name__}: dial outcome unknown") from exc
            raise

    async def find_call(self, *, to: str, from_: str, since: datetime) -> Any | None:
        """The newest call from `from_` to `to` created at or after `since`, or None.

        For a `create_call` that timed out: the POST may still have been
        accepted. Filtered on `date_created`, not Twilio's `StartTime` filter —
        that one has day granularity and a queued call has no start time yet.
        """
        client = await self._get()
        calls = await asyncio.wait_for(client.calls.list_async(to=to, from_=from_, limit=20), REQUEST_TIMEOUT_S)
        # Twilio stamps date_created to the second, on its own clock.
        cutoff = since - timedelta(seconds=5)
        for call in calls:
            created = getattr(call, "date_created", None)
            if created is not None and created >= cutoff:
                return call
        return None

    async def aclose(self) -> None:
        client, self._client = self._client, None
        http = getattr(client, "http_client", None)
        session = getattr(http, "session", None)
        if session is not None and hasattr(session, "close"):
            await session.close()
