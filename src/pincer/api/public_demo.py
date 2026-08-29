"""The website's "try it now" call.

Unauthenticated by necessity — it sits on the marketing page, where nobody has
a token — which makes it the most abusable surface in this codebase: without
limits it is a way to make someone else's phone ring, on our carrier bill.

So it is off unless switched on, it takes a number only with an explicit
"this is my number" from the caller, and it is bounded four ways: a cooldown
per number, a daily cap per client, a daily cap overall, and every guardrail
`make_phone_call` already applies (do-not-call, quiet hours, the outbound
budget). The demo is worth having; it is not worth a harassment vector.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from pincer.config import get_settings_relaxed

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

# In-process counters. A restart forgives everything, which is the right
# trade-off for a demo: no schema, no cleanup job, and the global cap still
# bounds a running process. A multi-worker deployment gets per-worker limits —
# noted in the docs rather than pretended away.
_last_call_at: dict[str, float] = {}
_client_calls: dict[str, list[float]] = {}
_global_calls: list[float] = []

_DAY_S = 24 * 60 * 60


def _prune(stamps: list[float], now: float) -> list[float]:
    return [t for t in stamps if now - t < _DAY_S]


def _client_key(request: Request) -> str:
    """Best-effort caller identity: the proxy's forwarded address, else the peer."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_limits(number: str, client: str, now: float | None = None) -> None:
    """Raise 429 with a sentence the website can show as-is."""
    stamp = time.time() if now is None else now

    last = _last_call_at.get(number)
    if last is not None and stamp - last < NUMBER_COOLDOWN_S:
        wait_min = int((NUMBER_COOLDOWN_S - (stamp - last)) // 60) + 1
        raise HTTPException(
            status_code=429,
            detail=f"This number was just called. Try again in about {wait_min} minutes.",
        )

    mine = _prune(_client_calls.get(client, []), stamp)
    _client_calls[client] = mine
    if len(mine) >= PER_CLIENT_DAILY:
        raise HTTPException(
            status_code=429,
            detail="You have used today's demo calls. Book a demo and we will call you properly.",
        )

    everyone = _prune(_global_calls, stamp)
    _global_calls[:] = everyone
    if len(everyone) >= GLOBAL_DAILY:
        raise HTTPException(
            status_code=429,
            detail="The demo line is busy today. Book a demo and we will call you back.",
        )


def _record(number: str, client: str, now: float | None = None) -> None:
    stamp = time.time() if now is None else now
    _last_call_at[number] = stamp
    _client_calls.setdefault(client, []).append(stamp)
    _global_calls.append(stamp)


def reset_limits() -> None:
    """Test hook — the counters are process state, not a database."""
    _last_call_at.clear()
    _client_calls.clear()
    _global_calls.clear()


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

    client = _client_key(request)
    _check_limits(number, client)

    # Counted before the call, not after: two requests racing must not both
    # get through on "nobody has called yet".
    _record(number, client)

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
