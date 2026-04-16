"""AsyncWebClient wrapper with rate-limit throttling and dual-token support.

Slack rate-limit tiers (requests per minute):
  Tier 1 —   1/min  (very sensitive admin operations)
  Tier 2 —  20/min  (standard read operations)
  Tier 3 —  50/min  (high-frequency reads)
  Tier 4 — 100/min  (low-cost lookups)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Max requests per 60-second window per tier
_TIER_LIMITS: dict[int, int] = {1: 1, 2: 20, 3: 50, 4: 100}


class SlackClient:
    """Wraps two AsyncWebClient instances (bot + user) with per-tier rate limiting."""

    def __init__(self, bot_token: str, user_token: str = "") -> None:
        try:
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError as exc:
            raise RuntimeError(
                "slack-sdk not installed. Run: uv pip install 'pincer-agent[slack]'"
            ) from exc

        if not bot_token:
            raise ValueError("bot_token must be non-empty")

        self._bot: Any = AsyncWebClient(token=bot_token)
        self._user: Any = AsyncWebClient(token=user_token) if user_token else None
        self._call_times: dict[int, list[float]] = {t: [] for t in _TIER_LIMITS}

    @property
    def bot(self) -> Any:
        """The bot-token AsyncWebClient."""
        return self._bot

    @property
    def user(self) -> Any:
        """The user-token AsyncWebClient, or bot client if no user token configured."""
        return self._user if self._user is not None else self._bot

    @property
    def has_user_token(self) -> bool:
        return self._user is not None

    async def _throttle(self, tier: int) -> None:
        """Block until a call at *tier* is within rate limits."""
        limit = _TIER_LIMITS.get(tier, 50)
        now = time.monotonic()
        window = 60.0
        calls = self._call_times.setdefault(tier, [])
        # Prune calls outside the window
        self._call_times[tier] = [t for t in calls if now - t < window]
        if len(self._call_times[tier]) >= limit:
            oldest = self._call_times[tier][0]
            wait = window - (now - oldest) + 0.05
            if wait > 0:
                logger.debug("Slack rate-limit tier %d — sleeping %.2fs", tier, wait)
                await asyncio.sleep(wait)
        self._call_times[tier].append(time.monotonic())

    async def call(self, method_fn: Any, tier: int = 3, **kwargs: Any) -> Any:
        """Throttle then call *method_fn(**kwargs)*; retry once on HTTP 429."""
        await self._throttle(tier)
        try:
            return await method_fn(**kwargs)
        except Exception as exc:
            if "ratelimited" in str(exc).lower() or "429" in str(exc):
                retry_after = 30
                logger.warning("Slack 429 received — backing off %ds", retry_after)
                await asyncio.sleep(retry_after)
                return await method_fn(**kwargs)
            raise
