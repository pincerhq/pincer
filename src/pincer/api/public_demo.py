"""The website's "try it now" call.

Unauthenticated by necessity — it sits on the marketing page, where nobody has
a token — which makes it the most abusable surface in this codebase: without
limits it is a way to make someone else's phone ring, on our carrier bill.

So it is off unless switched on, it takes a number only with an explicit
"this is my number" from the caller, and it is bounded four ways: a cooldown
per number, a daily cap per client, a daily cap overall, and every guardrail
`make_phone_call` already applies (do-not-call, quiet hours, the outbound
budget). The demo is worth having; it is not worth a harassment vector.

The three counters are `pincer.core.ratelimit` limiters on the cache backend
(`PINCER_CACHE_BACKEND_URL`), so with Redis they hold across workers and
restarts. They are not route decorators: the number comes from the body, and a
request refused for another reason (no consent, a bad number, another cap) must
not spend a slot, so all three are checked first and charged only together.
They fail closed: when the backend can't be asked, nobody is called.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from pincer.api.auth_guard import client_ip
from pincer.config import get_settings_relaxed
from pincer.core.ratelimit import (
    AsyncLimiter,
    AsyncLimiterFactory,
    Charge,
    Rate,
    RateLimitResult,
    TokenBucketStrategy,
    acharge_all,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/public", tags=["public"])

#: One demo call per number per this many seconds.
NUMBER_COOLDOWN_S = 30 * 60
#: Per client address, per rolling day.
PER_CLIENT_DAILY = 3
#: Across everyone, per rolling day — the bill's backstop.
GLOBAL_DAILY = 25

#: What the agent is told to do on a demo call. Fixed on the server: a purpose
#: taken from the request would make this a free text-to-speech gateway.
DEMO_TASK = (
    "This is a demo call the person requested from the Pincer website. "
    "Introduce yourself as Pincer's AI assistant, say the call is a demo, "
    "ask what they would want an assistant to handle for them, answer briefly, "
    "and end the call politely within two minutes."
)

_DAY_S = 24 * 60 * 60

#: Why each limiter refuses, in words the website shows as-is.
_UNAVAILABLE = "The demo line is not available right now. Please try again later."


def _cooldown_message(result: RateLimitResult) -> str:
    wait_min = int(result.retry_after // 60) + 1
    return f"This number was just called. Try again in about {wait_min} minutes."


_CLIENT_MESSAGE = "You have used today's demo calls. Book a demo and we will call you properly."
_GLOBAL_MESSAGE = "The demo line is busy today. Book a demo and we will call you back."


@dataclass(frozen=True)
class _DemoLimits:
    """The three caps, under `<prefix>:kv:ratelimit:demo_*`. They fail closed."""

    number: AsyncLimiter
    client: AsyncLimiter
    everyone: AsyncLimiter

    @classmethod
    def from_settings(cls, settings: Any) -> _DemoLimits:
        factory = AsyncLimiterFactory.from_settings(settings)
        return cls(
            # A token bucket of one: exactly NUMBER_COOLDOWN_S between calls to a number.
            number=factory.limiter(Rate(1, NUMBER_COOLDOWN_S), "demo_number", TokenBucketStrategy(), fail_open=False),
            client=factory.limiter(Rate(PER_CLIENT_DAILY, _DAY_S), "demo_client", fail_open=False),
            everyone=factory.limiter(Rate(GLOBAL_DAILY, _DAY_S), "demo_global", fail_open=False),
        )

    def charges(self, number: str, client: str) -> list[Charge[AsyncLimiter]]:
        """Each cap with its identifier and refusal message, in the order a refusal is reported."""
        return [
            Charge(self.number, number, detail=_cooldown_message),
            Charge(self.client, client, detail=_CLIENT_MESSAGE),
            Charge(self.everyone, "all", detail=_GLOBAL_MESSAGE),
        ]


_limits: _DemoLimits | None = None


def _demo_limits() -> _DemoLimits:
    global _limits
    if _limits is None:
        _limits = _DemoLimits.from_settings(get_settings_relaxed())
    return _limits


async def _charge_limits(number: str, client: str) -> None:
    """Raise 429 (or 503) unless all three caps allow a call; then count it against all three.

    `acharge_all` checks first and charges only together: a request any cap
    refuses spends nothing on the others.
    """
    charge, result = await acharge_all(_demo_limits().charges(number, client))
    if result.allowed:
        return
    if result.failed:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE)
    raise HTTPException(status_code=429, detail=charge.message(result))


def reset_limits() -> None:
    """Test hook: the next request builds fresh limiters (a fresh in-memory backend)."""
    global _limits
    _limits = None


class DemoCallIn(BaseModel):
    phone_number: str = Field(min_length=4, max_length=20)
    # Not a checkbox for legal theatre: this endpoint dials whoever is named,
    # so the person asking has to say the number is theirs.
    consent: bool = False
    language: str = Field(default="", max_length=8)
    name: str = Field(default="", max_length=120)


class DemoCallOut(BaseModel):
    status: str = "calling"
    message: str = ""


@router.post("/demo-call", response_model=DemoCallOut, status_code=202)
async def demo_call(body: DemoCallIn, request: Request) -> DemoCallOut:
    from pincer.voice.outbound import make_phone_call, validate_e164

    settings: Any = get_settings_relaxed()
    if not getattr(settings, "demo_call_enabled", False):
        raise HTTPException(status_code=404, detail="The demo line is not available.")

    if not body.consent:
        raise HTTPException(
            status_code=422,
            detail="Please confirm the number is yours and that you want us to call it.",
        )

    number = validate_e164(body.phone_number)
    if not number:
        raise HTTPException(
            status_code=422,
            detail=f"That does not look like a phone number: {body.phone_number}",
        )

    client = client_ip(request)
    # Counted before the call, not after: two requests racing must not both
    # get through on "nobody has called yet".
    await _charge_limits(number, client)

    logger.info("Demo call requested: to=%s client=%s", number, client)
    result = await make_phone_call(
        target_number=number,
        purpose=DEMO_TASK,
        language=body.language,
        context={"user_id": "website-demo", "channel": "web"},
        target_name=body.name,
        source="demo",
    )
    if result.startswith("Error"):
        # The guardrail that fired is the answer — quiet hours and the
        # do-not-call list both reach the website verbatim.
        raise HTTPException(status_code=409, detail=result.removeprefix("Error:").strip())

    return DemoCallOut(status="calling", message="Your phone should ring in a few seconds.")


__all__ = ["GLOBAL_DAILY", "NUMBER_COOLDOWN_S", "PER_CLIENT_DAILY", "reset_limits", "router"]
