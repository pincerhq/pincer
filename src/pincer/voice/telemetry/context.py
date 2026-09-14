"""
Correlation context for telephony telemetry.

Every record this subsystem writes carries the same identifier set, because an
incident starts from whichever one the reporter happened to have: a Twilio
CallSid from the provider console, a trace id from a log line, or a turn the
dashboard flagged as slow.

Identifiers
-----------
``call_id``           internal, ours, stable for the life of the call. Assigned
                      before the provider has told us anything, which is what
                      lets an outbound call that never gets a CallSid still be
                      diagnosable.
``provider_call_id``  Twilio's CallSid. Empty until the dial returns.
``trace_id``          32 hex chars, OTel-compatible, one per call.
``span_id``           16 hex chars, OTel-compatible.
``conversation_id``   the agent session the call is bound to.
``turn_id``           one caller utterance → agent response cycle.
``tenant_id``         workspace/organisation, when the deployment has them.

Propagation is by :mod:`contextvars`, so ``asyncio.create_task`` carries it for
free — which matters because the turn runs in its own task
(``voice-turn-{sid}``) and the TTS stream runs inside that. WebSocket handlers
and webhook handlers are separate tasks with no inherited context, so they
re-attach from the registry by ``provider_call_id``.
"""

from __future__ import annotations

import contextvars
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

# How long a finished call's context stays resolvable by CallSid. Twilio
# retries a status callback for minutes after teardown, and those late events
# are exactly the ones that explain an abrupt disconnect — dropping the
# context immediately would orphan them.
_ENDED_CONTEXT_TTL_S = 900.0
_MAX_TRACKED_CALLS = 2000


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def new_call_id() -> str:
    return secrets.token_hex(16)


@dataclass(frozen=True, slots=True)
class CallContext:
    """Identifiers and low-cardinality dimensions shared by a whole call."""

    call_id: str
    trace_id: str
    direction: str = ""
    provider_call_id: str = ""
    provider: str = "twilio"
    engine: str = ""
    transport: str = ""
    codec: str = ""
    sample_rate: int = 0
    model: str = ""
    language: str = ""
    conversation_id: str = ""
    tenant_id: str = ""
    environment: str = ""
    app_version: str = ""
    sampled: bool = True
    sample_rate_used: float = 1.0

    def dims(self) -> dict[str, str]:
        """The bounded label set. Deliberately excludes every id — high
        cardinality belongs in traces and logs, never on a metric."""
        return {
            "direction": self.direction,
            "provider": self.provider,
            "engine": self.engine,
            "model": self.model,
            "language": self.language,
            "environment": self.environment,
            "app_version": self.app_version,
        }


@dataclass(frozen=True, slots=True)
class TurnContext:
    """One caller-utterance → agent-response cycle."""

    turn_id: str
    turn_no: int
    span_id: str
    trigger: str = "caller_speech"


@dataclass(slots=True)
class _Tracked:
    context: CallContext
    seq: int = 0
    ended_at: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_call_var: contextvars.ContextVar[CallContext | None] = contextvars.ContextVar("pincer_call_ctx", default=None)
_turn_var: contextvars.ContextVar[TurnContext | None] = contextvars.ContextVar("pincer_turn_ctx", default=None)
_span_var: contextvars.ContextVar[str] = contextvars.ContextVar("pincer_span_id", default="")

_by_provider_id: dict[str, _Tracked] = {}
_by_call_id: dict[str, _Tracked] = {}


def _environment() -> str:
    return os.environ.get("PINCER_ENVIRONMENT", "") or "development"


def _app_version() -> str:
    try:
        from pincer import __version__

        return str(__version__)
    except Exception:
        return os.environ.get("PINCER_VERSION", "") or "unknown"


def register_call(
    *,
    provider_call_id: str = "",
    direction: str = "",
    engine: str = "",
    language: str = "",
    tenant_id: str = "",
    conversation_id: str = "",
    model: str = "",
    transport: str = "",
    codec: str = "",
    sample_rate: int = 0,
    sampled: bool = True,
    sample_rate_used: float = 1.0,
) -> CallContext:
    """Create (or return) the context for a call.

    Idempotent on ``provider_call_id``: the inbound webhook, the WebSocket
    ``setup`` and every status callback all land here and must agree on one
    ``call_id``.
    """
    existing = _by_provider_id.get(provider_call_id) if provider_call_id else None
    if existing is not None:
        return existing.context

    ctx = CallContext(
        call_id=new_call_id(),
        trace_id=new_trace_id(),
        direction=direction,
        provider_call_id=provider_call_id,
        engine=engine,
        language=language,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        model=model,
        transport=transport,
        codec=codec,
        sample_rate=sample_rate,
        environment=_environment(),
        app_version=_app_version(),
        sampled=sampled,
        sample_rate_used=sample_rate_used,
    )
    tracked = _Tracked(context=ctx)
    _by_call_id[ctx.call_id] = tracked
    if provider_call_id:
        _by_provider_id[provider_call_id] = tracked
    _prune()
    return ctx


def attach_provider_call_id(call_id: str, provider_call_id: str) -> CallContext | None:
    """Bind the CallSid that only exists once the dial returns.

    The outbound path registers the call *before* ``calls.create()``, so the
    pre-dial events (safety gates, briefing) are already correlated by the time
    Twilio names the call.
    """
    tracked = _by_call_id.get(call_id)
    if tracked is None or not provider_call_id:
        return None
    tracked.context = replace(tracked.context, provider_call_id=provider_call_id)
    _by_provider_id[provider_call_id] = tracked
    return tracked.context


def update_call(call_id: str, **fields: Any) -> CallContext | None:
    """Fill in dimensions learned after registration (engine, model, codec…)."""
    tracked = _by_call_id.get(call_id)
    if tracked is None:
        return None
    known = {k: v for k, v in fields.items() if hasattr(tracked.context, k) and v not in (None, "")}
    if known:
        tracked.context = replace(tracked.context, **known)
    return tracked.context


def context_for_provider_call(provider_call_id: str) -> CallContext | None:
    tracked = _by_provider_id.get(provider_call_id)
    return tracked.context if tracked else None


def context_for_call(call_id: str) -> CallContext | None:
    tracked = _by_call_id.get(call_id)
    return tracked.context if tracked else None


def next_seq(call_id: str) -> int:
    """Per-call monotonically increasing sequence number.

    Read-time ordering uses ``(ts_utc, seq)``; ``seq`` is what keeps two events
    stamped in the same millisecond in the order they actually happened.
    """
    tracked = _by_call_id.get(call_id)
    if tracked is None:
        return 0
    tracked.seq += 1
    return tracked.seq


def end_call(provider_call_id: str = "", call_id: str = "") -> None:
    """Mark a call finished; its context stays resolvable for late webhooks."""
    tracked = _by_provider_id.get(provider_call_id) or _by_call_id.get(call_id)
    if tracked is not None:
        tracked.ended_at = time.monotonic()
    _prune()


def _prune() -> None:
    now = time.monotonic()
    stale = [
        tid
        for tid, tracked in _by_call_id.items()
        if tracked.ended_at is not None and now - tracked.ended_at > _ENDED_CONTEXT_TTL_S
    ]
    # Hard cap as well: a deployment that never sees a clean teardown must not
    # grow this map without bound.
    if len(_by_call_id) > _MAX_TRACKED_CALLS:
        ended = sorted(
            (t for t in _by_call_id.values() if t.ended_at is not None),
            key=lambda t: t.ended_at or 0.0,
        )
        stale.extend(t.context.call_id for t in ended[: len(_by_call_id) - _MAX_TRACKED_CALLS])
    for tid in set(stale):
        tracked = _by_call_id.pop(tid, None)
        if tracked and tracked.context.provider_call_id:
            _by_provider_id.pop(tracked.context.provider_call_id, None)


# ── ambient binding ──────────────────────────────────────────────────


@contextmanager
def bind_call(ctx: CallContext | None) -> Iterator[CallContext | None]:
    token = _call_var.set(ctx)
    try:
        yield ctx
    finally:
        _call_var.reset(token)


@contextmanager
def bind_turn(turn: TurnContext | None) -> Iterator[TurnContext | None]:
    token = _turn_var.set(turn)
    span_token = _span_var.set(turn.span_id if turn else "")
    try:
        yield turn
    finally:
        _span_var.reset(span_token)
        _turn_var.reset(token)


@contextmanager
def bind_span(span_id: str) -> Iterator[str]:
    token = _span_var.set(span_id)
    try:
        yield span_id
    finally:
        _span_var.reset(token)


def current_call() -> CallContext | None:
    return _call_var.get()


def current_turn() -> TurnContext | None:
    return _turn_var.get()


def current_span_id() -> str:
    return _span_var.get()


def start_turn(turn_no: int, *, trigger: str = "caller_speech") -> TurnContext:
    return TurnContext(turn_id=new_span_id(), turn_no=turn_no, span_id=new_span_id(), trigger=trigger)


def reset_for_tests() -> None:
    _by_provider_id.clear()
    _by_call_id.clear()
    _call_var.set(None)
    _turn_var.set(None)
    _span_var.set("")
