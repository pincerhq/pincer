"""Voice REST API — dashboard/app-facing endpoints for the Voice page.

All routes live under /api/voice/* and are therefore gated by the bearer
`auth_middleware` in `pincer.api.server` (only /api/health, /api/docs and
/api/openapi.json are public).

Data sources:
  - Live call state:  the engine wired by `init_voice_routes` at startup
    (`pincer.voice.twiml_server.get_engine`) — empty when voice is off.
  - History:          voice_calls / call_transcripts / call_actions in the
    main SQLite DB, as created by `pincer.voice.retention.ensure_voice_tables`
    and written by the post-call processor. `status` and `duration_seconds`
    are derived from started_at/ended_at — the runtime schema stores neither.
  - Contacts:         phone_contacts (may not exist yet; served as empty).
  - Outbound:         `pincer.voice.outbound.make_phone_call`, the same
    validated path as the chat tool (E.164, outbound flag, daily limit).
    The dashboard-token auth on this endpoint stands in for the in-chat
    approval gate: the click IS the explicit user approval.

Transcript text is served through `mask_pii` (defense-in-depth on top of
masking at storage time). Raw audio is intentionally not exposed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel, Field

from pincer.config import get_settings_relaxed
from pincer.voice.pii_guard import mask_pii

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pincer.voice.engine import VoiceEngine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice", tags=["voice"])


def _get_engine() -> VoiceEngine | None:
    """Live engine, or None when voice isn't running (API-only, voice off)."""
    try:
        from pincer.voice.twiml_server import get_engine

        return get_engine()
    except ImportError:  # pragma: no cover — voice extra not installed
        return None


# The voice schema is ensured once per process, on the first query. Until
# Sprint 13 the API was a pure reader and could assume the writer had migrated
# first; now its queries reference `voice_calls.thread_id` and `call_threads`,
# so an API that starts against a database whose last write predates Sprint 13
# would raise OperationalError on EVERY call query — and the `except
# OperationalError: return []` guards below would report that as "no calls",
# silently emptying the user's whole call history until the next call ended.
# Keyed by database path rather than a bare flag, so pointing at a different
# database (tests, a restored backup) re-checks instead of trusting a stale yes.
_schema_ready_for: str = ""


@asynccontextmanager
async def _db() -> AsyncIterator[aiosqlite.Connection]:
    global _schema_ready_for  # noqa: PLW0603
    settings = get_settings_relaxed()
    db_path = str(settings.db_path)
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        if _schema_ready_for != db_path:
            from pincer.voice.retention import ensure_voice_tables

            await ensure_voice_tables(conn)
            _schema_ready_for = db_path
        yield conn


def _duration_seconds(started_at: str, ended_at: str | None) -> int:
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at) if ended_at else datetime.now(UTC)
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        return max(0, int((end - start).total_seconds()))
    except (ValueError, TypeError):
        return 0


# ── Schemas ──────────────────────────────────────────────────────────


class VoiceStatus(BaseModel):
    enabled: bool = False
    engine: str = ""
    language: str = ""
    consent_mode: str = ""
    outbound_enabled: bool = False
    voice_configured: bool = False  # ElevenLabs voice ID present
    webhook_base_configured: bool = False
    active_call_count: int = 0
    listen_in_enabled: bool = False  # Sprint 15: live listen-in media fork on


class AnalyticsOut(BaseModel):
    """§1 fields. Speech values are null when no conversation happened —
    the UI must render that as "not assessed", never as a zeroed bar."""

    agent_speech_ms: int | None = None
    caller_speech_ms: int | None = None
    silence_ms: int | None = None
    overlap_ms: int | None = None
    interruptions: int = 0
    talk_ratio: float | None = None
    # exact (Media Streams: byte counts + word timings) | estimated
    # (ConversationRelay: character counts × a per-language speaking rate).
    # The UI MUST label estimated numbers as such.
    method: str = "estimated"
    sentiment: str | None = None
    sentiment_trajectory: str | None = None
    sentiment_rationale: str | None = None
    # Why sentiment is absent: too_short | not_conversed | extraction_failed.
    # '' when sentiment is present.
    sentiment_reason: str = ""


class BriefingOut(BaseModel):
    task: str = ""
    source: str = ""
    target_name: str = ""


class ActiveCall(BaseModel):
    call_sid: str
    direction: str
    caller_number: str = ""
    target_number: str = ""
    target_name: str = ""
    purpose: str = ""
    # First 120 chars of the task the agent is running under ('' = inbound)
    briefing_task_preview: str = ""
    language: str = ""
    engine: str = ""
    started_at: str = ""
    duration_seconds: int = 0
    # Sprint 15: live listen-in. `listen_available` = feature on AND the
    # Twilio monitor fork for this call is attached; the dashboard shows the
    # 🎧 button only then.
    listen_available: bool = False
    listener_count: int = 0
    listener_capacity: int = 0  # PINCER_LISTEN_IN_MAX_LISTENERS; the UI disables 🎧 at the cap


class CallSummary(BaseModel):
    call_sid: str
    direction: str
    status: str  # derived: 'active' until ended_at is set, then 'completed'
    from_number: str = ""
    to_number: str = ""
    started_at: str = ""
    ended_at: str | None = None
    duration_seconds: int = 0
    # Sprint 9 (T9.3/T9.1): why it failed and what it cost. `failure_code` is
    # the stable taxonomy from pincer.observability.failure_codes ('none' =
    # completed, '' = the call predates the taxonomy).
    failure_code: str = ""
    failure_description: str = ""
    cost_usd: float | None = None
    # Sprint 12: receptionist intent ('' = not a receptionist call)
    inbound_intent: str = ""
    # Sprint 13: the matter this call belongs to ('' = threadless, e.g. every
    # call that predates the feature — v1 never groups history retroactively)
    thread_id: str = ""
    thread_subject: str = ""
    thread_attach_kind: str = ""
    # Compact analytics for list chips; all null-safe (analytics start at
    # deploy — every earlier call legitimately has none).
    sentiment: str | None = None
    talk_ratio: float | None = None
    method: str | None = None


class TranscriptLine(BaseModel):
    speaker: str
    text: str
    confidence: float = 1.0
    state: str = ""
    timestamp: str = ""


class CallActionOut(BaseModel):
    action_type: str
    tool_name: str = ""
    input_summary: str = ""
    output_summary: str = ""
    user_confirmed: bool | None = None
    timestamp: str = ""
    # Sprint 11: in-call tool policy columns ('' on rows predating the migration)
    tier: str = ""
    approval_mode: str = ""
    deny_reason: str = ""


class CallDetail(CallSummary):
    # Exactly what the agent was told to do, as stored at dial time.
    # None for inbound calls and for calls placed before briefings existed.
    briefing: BriefingOut | None = None
    # Talk time + sentiment. None for calls that predate the feature.
    analytics: AnalyticsOut | None = None
    transcript: list[TranscriptLine] = Field(default_factory=list)
    actions: list[CallActionOut] = Field(default_factory=list)
    # Full per-component breakdown (twilio / stt / tts / llm), Sprint 9 T9.1
    cost: dict[str, Any] | None = None


class Contact(BaseModel):
    name: str
    phone_number: str
    category: str = ""
    notes: str = ""


class InitiateCallIn(BaseModel):
    target_number: str = Field(min_length=4, max_length=20)
    purpose: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(default="", max_length=4000)
    language: str = Field(default="", max_length=8)  # '' = default language
    target_name: str = Field(default="", max_length=120)  # who is being called (optional)
    # Sprint 13 §4.2: continue an existing matter (validated: exists, not closed)
    thread_id: str = Field(default="", max_length=32)


class InitiateCallOut(BaseModel):
    call_sid: str
    status: str
    message: str = ""


class ScheduleAppointmentIn(BaseModel):
    target_number: str = Field(min_length=4, max_length=20)
    contact_name: str = Field(min_length=1, max_length=200)
    topic: str = Field(min_length=1, max_length=2000)
    timeframe: str = Field(default="", max_length=64)
    duration_minutes: int = Field(default=30, ge=5, le=480)
    language: str = Field(default="", max_length=8)
    attendees: str = Field(default="", max_length=2000)
    location_or_meet: str = Field(default="", max_length=500)
    thread_id: str = Field(default="", max_length=32)


class ScheduleAppointmentOut(BaseModel):
    status: str
    message: str


class DoNotCallEntry(BaseModel):
    phone_number: str
    reason: str = ""
    source: str = ""
    call_sid: str = ""
    added_at: str = ""


class DoNotCallIn(BaseModel):
    phone_number: str = Field(min_length=4, max_length=20)
    reason: str = Field(default="", max_length=500)


class TurnModelChoice(BaseModel):
    value: str  # "" | "<model>" | "<provider>:<model>"
    label: str


class VoiceConfig(BaseModel):
    voice_turn_model: str
    default_model: str
    choices: list[TurnModelChoice]


class VoiceConfigUpdate(BaseModel):
    voice_turn_model: str = Field(max_length=120)


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("/status", response_model=VoiceStatus)
async def voice_status() -> VoiceStatus:
    s = get_settings_relaxed()
    engine = _get_engine()
    return VoiceStatus(
        enabled=s.voice_enabled,
        engine=s.voice_engine,
        language=s.voice_default_language,
        consent_mode=s.voice_consent_mode,
        outbound_enabled=s.voice_outbound_enabled,
        voice_configured=bool(s.elevenlabs_voice_id or s.elevenlabs_voice_id_en),
        webhook_base_configured=s.voice_webhook_base_url.strip().startswith("http"),
        active_call_count=len(engine.get_active_calls()) if engine else 0,
        listen_in_enabled=getattr(s, "listen_in_enabled", False) is True,
    )


@router.get("/active", response_model=list[ActiveCall])
async def active_calls() -> list[ActiveCall]:
    engine = _get_engine()
    if engine is None:
        return []
    from pincer.voice.monitor import get_monitor_hub, listen_in_enabled

    settings = get_settings_relaxed()
    hub = get_monitor_hub()
    listen_on = listen_in_enabled(settings)
    if listen_on:
        hub.configure(settings)
    return [
        ActiveCall(
            call_sid=sid,
            direction=st.direction.value,
            caller_number=st.caller_number,
            target_number=st.target_number,
            target_name=st.target_name,
            purpose=st.purpose,
            language=st.language,
            engine=st.engine_type,
            started_at=st.started_at.isoformat(),
            duration_seconds=st.duration_seconds,
            briefing_task_preview=_briefing_preview(st),
            listen_available=listen_on and hub.source_attached(sid),
            listener_count=hub.listener_count(sid),
            listener_capacity=hub.max_listeners if listen_on else 0,
        )
        for sid, st in engine.get_active_calls().items()
    ]


# ── Live listen-in egress (Sprint 15) ────────────────────────────────
#
# WSS /api/voice/listen/{call_sid}: the dashboard's listen-only feed of the
# Twilio monitor fork (see `pincer.voice.monitor`). Starlette's HTTP
# middleware does not run on WebSocket upgrades, so the bearer check is done
# here, BEFORE accept(): an unauthenticated upgrade is denied with 401
# (429 while the T8.2 brute-force guard has the IP locked) and never sees a
# frame. The browser cannot set an Authorization header on a WebSocket, so
# the token may also arrive as `?token=` — the same secret, same comparison.
#
# Wire protocol v1 (JSON text frames, server → client only):
#   {"type":"start","call_sid":…,"tracks":["inbound","outbound"],
#    "codec":"mulaw","sample_rate":8000,"listener_count":n}
#   {"type":"media","track":"inbound"|"outbound","payload":"<b64 μ-law>","ts":…}
#   {"type":"end","reason":"call_ended"|"capacity"|"unavailable"|"error"}
# Close codes: 1000 normal, 4001 capacity, 4004 unavailable.
# Nothing the client sends is interpreted — the feed is rx-only by design.

_DASHBOARD_USER = "dashboard"
_LISTENER_USER = "dashboard"  # the dashboard bearer is a shared secret; no finer identity exists


async def _deny_ws(websocket: WebSocket, status_code: int, body: str) -> None:
    """Refuse a WS upgrade with a real HTTP status (falls back to a 403 close
    when the ASGI server lacks the denial-response extension)."""
    try:
        await websocket.send_denial_response(Response(content=body, status_code=status_code, media_type="text/plain"))
    except Exception:
        with contextlib.suppress(Exception):
            await websocket.close(code=1008, reason=body)


async def _authorize_listener(websocket: WebSocket) -> str | None:
    """Bearer check for the listen socket, pre-accept. Returns the audit user
    label, or None after the upgrade has been denied."""
    from pincer.api.auth_guard import audit_auth_failure, client_ip

    s = get_settings_relaxed()
    allowed: set[str] = set()
    for attr in ("dashboard_token", "web_chat_token"):
        raw: Any = getattr(s, attr, None)
        value = str(raw.get_secret_value() or "") if hasattr(raw, "get_secret_value") else str(raw or "")
        if value:
            allowed.add(value)
    if not allowed:
        # No token configured: allow, exactly like the HTTP middleware
        # (`pincer doctor --production` reports this state CRITICAL).
        return _LISTENER_USER

    ip = client_ip(websocket)
    path = websocket.url.path
    guard = getattr(getattr(websocket, "app", None), "state", None)
    guard = getattr(guard, "auth_guard", None)
    if guard is not None:
        wait = guard.retry_after(ip)
        if wait:
            await audit_auth_failure(ip, path, "locked_out", locked_for=wait)
            await _deny_ws(websocket, 429, "Too many failed authentication attempts")
            return None

    header = websocket.headers.get("authorization", "")
    supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not supplied:
        supplied = str(websocket.query_params.get("token", "") or "")
    if supplied and any(secrets.compare_digest(supplied, candidate) for candidate in allowed):
        if guard is not None:
            guard.record_success(ip)
        return _LISTENER_USER

    locked_for = guard.record_failure(ip) if guard is not None else 0
    await audit_auth_failure(ip, path, "invalid_token", locked_for=locked_for)
    await _deny_ws(websocket, 401, "Invalid token")
    return None


async def _end_and_close(websocket: WebSocket, reason: str, code: int) -> None:
    with contextlib.suppress(Exception):
        await websocket.send_json({"type": "end", "reason": reason})
    with contextlib.suppress(Exception):
        await websocket.close(code=code, reason=reason)


async def _audit_listen_session(
    *,
    user: str,
    call_sid: str,
    ip: str,
    started_at: datetime,
    ended_at: datetime,
    reason: str,
    frames: int,
    dropped: int,
) -> None:
    """§2.2: every listen session leaves one audit row. Best effort."""
    duration_s = max(0.0, (ended_at - started_at).total_seconds())
    try:
        from pincer.security.audit import AuditAction, AuditEntry, get_audit_logger

        audit = await get_audit_logger()
        await audit.log(
            AuditEntry(
                user_id=user,
                action=AuditAction.LISTEN_IN_SESSION,
                tool="listen_in",
                input_summary=call_sid,
                output_summary=f"{reason} after {duration_s:.1f}s",
                approved=True,
                duration_ms=int(duration_s * 1000),
                ip_address=ip,
                channel="voice",
                metadata={
                    "user": user,
                    "call_sid": call_sid,
                    "started_at": started_at.isoformat(),
                    "ended_at": ended_at.isoformat(),
                    "duration_s": round(duration_s, 1),
                    "reason": reason,
                    "frames": frames,
                    "frames_dropped": dropped,
                },
            )
        )
    except Exception:  # pragma: no cover — auditing must not break the socket teardown
        logger.debug("Audit logging of listen-in session failed", exc_info=True)
    with contextlib.suppress(Exception):
        from pincer.observability.metrics import record_listen_session

        record_listen_session(reason=reason, duration_s=duration_s)


@router.websocket("/listen/{call_sid}")
async def listen_ws(websocket: WebSocket, call_sid: str) -> None:
    """Listen-only live feed of an active call (Sprint 15)."""
    from pincer.api.auth_guard import client_ip
    from pincer.voice.monitor import (
        CLOSE_CAPACITY,
        CLOSE_UNAVAILABLE,
        END_CALL_ENDED,
        END_CAPACITY,
        END_ERROR,
        END_STOPPED,
        END_UNAVAILABLE,
        TRACKS,
        ListenerCapacityError,
        MonitorUnavailableError,
        get_monitor_hub,
        listen_in_enabled,
    )

    user = await _authorize_listener(websocket)
    if user is None:
        return
    ip = client_ip(websocket)
    settings = get_settings_relaxed()
    hub = get_monitor_hub()
    hub.configure(settings)

    await websocket.accept()
    if not listen_in_enabled(settings):
        await _end_and_close(websocket, END_UNAVAILABLE, CLOSE_UNAVAILABLE)
        return
    try:
        sub = await hub.subscribe(call_sid, user)
    except ListenerCapacityError:
        await _end_and_close(websocket, END_CAPACITY, CLOSE_CAPACITY)
        return
    except MonitorUnavailableError:
        await _end_and_close(websocket, END_UNAVAILABLE, CLOSE_UNAVAILABLE)
        return

    started_at = datetime.now(UTC)
    reason = END_STOPPED
    try:
        await websocket.send_json(
            {
                "type": "start",
                "call_sid": call_sid,
                "tracks": list(TRACKS),
                "codec": "mulaw",
                "sample_rate": 8000,
                "listener_count": hub.listener_count(call_sid),
            }
        )

        async def _pump() -> str:
            while True:
                item = await sub.queue.get()
                if item.get("type") == "end":
                    return str(item.get("reason") or END_CALL_ENDED)
                await websocket.send_json(item)

        async def _watch_client() -> str:
            # The feed is rx-only: client frames are read only to notice the
            # disconnect promptly; their content is ignored.
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return END_STOPPED

        pump = asyncio.ensure_future(_pump())
        watch = asyncio.ensure_future(_watch_client())
        done, pending = await asyncio.wait({pump, watch}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        if pump in done:
            exc = pump.exception()
            if exc is not None:
                reason = END_ERROR
                raise exc
            reason = pump.result()
            await _end_and_close(websocket, reason, 1000)
        else:
            exc = watch.exception()
            reason = END_STOPPED if exc is None else END_ERROR
    except Exception:
        if reason == END_STOPPED:
            reason = END_ERROR
        logger.debug("Listen-in socket ended with error [%s]", call_sid, exc_info=True)
        with contextlib.suppress(Exception):
            await websocket.close(code=1011, reason=END_ERROR)
    finally:
        hub.unsubscribe(sub, reason)
        await _audit_listen_session(
            user=user,
            call_sid=call_sid,
            ip=ip,
            started_at=started_at,
            ended_at=datetime.now(UTC),
            reason=reason,
            frames=sub.frames,
            dropped=sub.dropped,
        )


def _analytics_out(record: Any) -> AnalyticsOut | None:
    if record is None:
        return None
    return AnalyticsOut(
        agent_speech_ms=record.agent_speech_ms,
        caller_speech_ms=record.caller_speech_ms,
        silence_ms=record.silence_ms,
        overlap_ms=record.overlap_ms,
        interruptions=record.interruptions,
        talk_ratio=record.talk_ratio,
        method=record.method,
        sentiment=record.sentiment,
        sentiment_trajectory=record.sentiment_trajectory,
        # The rationale quotes the call, so it is masked like every other
        # read surface here — and it is NULL outright once the transcript it
        # came from has been purged.
        sentiment_rationale=mask_pii(record.sentiment_rationale) if record.sentiment_rationale else None,
        sentiment_reason=record.sentiment_reason,
    )


def _briefing_preview(state: Any) -> str:
    from pincer.voice.briefing import ACTIVE_PREVIEW_CHARS, briefing_from_state

    briefing = briefing_from_state(state)
    return mask_pii(briefing.preview(ACTIVE_PREVIEW_CHARS)) if briefing is not None else ""


def _row_value(row: aiosqlite.Row, key: str) -> Any:
    """Column value, or None when the DB predates the Sprint 9 migration."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _summary_from_row(row: aiosqlite.Row, cost_usd: float | None = None) -> CallSummary:
    from pincer.observability.failure_codes import describe

    ended_at = row["ended_at"]
    failure_code = str(_row_value(row, "failure_code") or "")
    return CallSummary(
        call_sid=row["call_sid"],
        direction=row["direction"] or "",
        status="completed" if ended_at else "active",
        from_number=row["from_number"] or "",
        to_number=row["to_number"] or "",
        started_at=row["started_at"] or "",
        ended_at=ended_at,
        duration_seconds=_duration_seconds(row["started_at"] or "", ended_at),
        failure_code=failure_code,
        failure_description=describe(failure_code) if failure_code else "",
        cost_usd=cost_usd,
        inbound_intent=str(_row_value(row, "inbound_intent") or ""),
        thread_id=str(_row_value(row, "thread_id") or ""),
        thread_subject=mask_pii(str(_row_value(row, "thread_subject") or "")),
        thread_attach_kind=str(_row_value(row, "thread_attach_kind") or ""),
    )


# ── Sprint 12: receptionist messages ────────────────────────────────


class InboundMessageOut(BaseModel):
    id: int
    call_sid: str
    caller_name: str = ""
    caller_name_unverified: bool = False
    callback_number: str = ""
    callback_unverified: bool = False
    matter: str = ""
    urgent: bool = False
    created_at: str = ""
    delivered_to_owner_at: str | None = None


@router.get("/messages", response_model=list[InboundMessageOut])
async def inbound_messages(limit: int = Query(default=50, ge=1, le=500)) -> list[InboundMessageOut]:
    """Messages taken by the receptionist, newest first (PII-masked like every read surface)."""
    async with _db() as conn:
        try:
            rows = await conn.execute_fetchall(
                "SELECT * FROM inbound_messages ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            )
        except aiosqlite.OperationalError:
            return []
    return [
        InboundMessageOut(
            id=int(r["id"]),
            call_sid=r["call_sid"] or "",
            caller_name=mask_pii(r["caller_name"] or ""),
            caller_name_unverified=bool(r["caller_name_unverified"]),
            callback_number=mask_pii(r["callback_number"] or ""),
            callback_unverified=bool(r["callback_unverified"]),
            matter=mask_pii(r["matter"] or ""),
            urgent=bool(r["urgent"]),
            created_at=r["created_at"] or "",
            delivered_to_owner_at=r["delivered_to_owner_at"],
        )
        for r in rows
    ]


class SentimentDistribution(BaseModel):
    positive: int = 0
    neutral: int = 0
    negative: int = 0
    mixed: int = 0
    assessed: int = 0  # calls with a reading; the rest were too short / never conversed


class ReceptionistStats(BaseModel):
    window_days: int = 7
    sentiment_distribution: SentimentDistribution = Field(default_factory=SentimentDistribution)


@router.get("/receptionist/stats", response_model=ReceptionistStats)
async def receptionist_stats(days: int = Query(default=7, ge=1, le=90)) -> ReceptionistStats:
    """Inbound-line stats for the receptionist strip.

    Sentiment counts only calls that actually got a reading — a call too short
    to assess is not a neutral one, and pooling them would quietly flatter the
    distribution.
    """
    from pincer.voice.analytics import sentiment_distribution

    counts = await sentiment_distribution(get_settings_relaxed().db_path, days=days, direction="inbound")
    return ReceptionistStats(
        window_days=days,
        sentiment_distribution=SentimentDistribution(**counts, assessed=sum(counts.values())),
    )


@router.get("/calls", response_model=list[CallSummary])
async def list_calls(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    direction: str | None = Query(default=None, pattern="^(inbound|outbound)$"),
    status: str | None = Query(default=None, pattern="^(active|completed)$"),
    thread_id: str | None = Query(default=None, max_length=32),
) -> list[CallSummary]:
    sql = (
        "SELECT c.call_sid, c.direction, c.from_number, c.to_number, c.started_at, c.ended_at, c.failure_code, "
        "c.thread_id, c.thread_attach_kind, t.subject AS thread_subject "
        "FROM voice_calls c LEFT JOIN call_threads t ON t.thread_id = c.thread_id"
    )
    where: list[str] = []
    args: list[Any] = []
    if direction:
        where.append("c.direction = ?")
        args.append(direction)
    if status:
        where.append("c.ended_at IS NOT NULL" if status == "completed" else "c.ended_at IS NULL")
    if thread_id:
        where.append("c.thread_id = ?")
        args.append(thread_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY c.started_at DESC LIMIT ? OFFSET ?"
    args += [limit, offset]

    async with _db() as conn:
        try:
            rows = await conn.execute_fetchall(sql, args)
        except aiosqlite.OperationalError:  # voice tables not created yet
            return []

    # One batched cost lookup for the page, not one per row.
    from pincer.observability.call_costs import get_call_costs
    from pincer.voice.analytics import load_many

    settings = get_settings_relaxed()
    sids = [str(r["call_sid"]) for r in rows]
    costs = await get_call_costs(settings, sids)
    # One batched analytics lookup for the page, not one query per row.
    records = await load_many(settings.db_path, sids)

    summaries = []
    for row in rows:
        sid = str(row["call_sid"])
        summary = _summary_from_row(row, costs.get(sid))
        record = records.get(sid)
        if record is not None:
            summary.sentiment = record.sentiment
            summary.talk_ratio = record.talk_ratio
            summary.method = record.method
        summaries.append(summary)
    return summaries


# ── Scheduled calls ──────────────────────────────────────────────────
#
# A call the owner wants placed later. Stored as a one-off cron schedule whose
# action dispatches to `pincer.voice.scheduled_calls`, so when it fires it goes
# down the same path as any other outbound call and meets the same guardrails.
#
# Declared above `/calls/{call_sid}`: a path parameter would otherwise swallow
# "scheduled" and answer with a call-not-found.


class ScheduledCallIn(BaseModel):
    target_number: str = Field(min_length=4, max_length=20)
    purpose: str = Field(min_length=1, max_length=2000)
    # Exactly one of these: minutes from now, or a concrete local time.
    run_in_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 90)
    at: str = Field(default="", max_length=40)
    target_name: str = Field(default="", max_length=120)
    language: str = Field(default="", max_length=8)
    instructions: str = Field(default="", max_length=4000)
    thread_id: str = Field(default="", max_length=32)
    timezone: str = Field(default="", max_length=64)


class ScheduledCallOut(BaseModel):
    id: int
    target_number: str
    target_name: str = ""
    purpose: str
    language: str = ""
    thread_id: str = ""
    #: When it fires, as UTC ISO.
    next_run_at: str = ""
    timezone: str = ""
    created_at: str = ""


def _scheduled_call_out(row: dict[str, Any]) -> ScheduledCallOut | None:
    """One schedule row as a scheduled call, or None if it is not one."""
    from pincer.voice.scheduled_calls import ACTION_TYPE

    action = row.get("action")
    if isinstance(action, str):
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            action = json.loads(action)
    if not isinstance(action, dict) or action.get("type") != ACTION_TYPE:
        return None
    return ScheduledCallOut(
        id=int(row["id"]),
        target_number=str(action.get("target_number", "")),
        target_name=str(action.get("target_name", "")),
        purpose=str(action.get("purpose", "")),
        language=str(action.get("language", "")),
        thread_id=str(action.get("thread_id", "")),
        next_run_at=str(row.get("next_run_at") or ""),
        timezone=str(row.get("timezone") or ""),
        created_at=str(row.get("created_at") or ""),
    )


@router.get("/calls/scheduled", response_model=list[ScheduledCallOut])
async def list_scheduled_calls() -> list[ScheduledCallOut]:
    """Calls waiting to go out, soonest first."""
    from pincer.scheduler.cron import CronScheduler

    settings = get_settings_relaxed()
    scheduler = CronScheduler(settings.db_path)
    try:
        await scheduler.ensure_table()
        rows = await scheduler.list_schedules(_DASHBOARD_USER)
    except Exception:
        logger.exception("Scheduled call listing failed")
        return []

    out = [c for c in (_scheduled_call_out(r) for r in rows) if c is not None]
    out.sort(key=lambda c: c.next_run_at or "")
    return out


@router.post("/calls/scheduled", response_model=ScheduledCallOut, status_code=201)
async def schedule_call(body: ScheduledCallIn) -> ScheduledCallOut:
    """Place this call later. The briefing is validated now, not at fire time."""
    from pincer.scheduler.cron import CronScheduler
    from pincer.voice.briefing import BriefingError, validate_task
    from pincer.voice.outbound import validate_e164
    from pincer.voice.scheduled_calls import (
        ScheduledCallError,
        build_action,
        one_off_cron,
        resolve_when,
    )

    settings = get_settings_relaxed()

    # Both checks happen here rather than when the call fires: a number or a
    # purpose the backend would refuse must fail while someone is looking at
    # the form, not silently at 23:10.
    number = validate_e164(body.target_number)
    if not number:
        raise HTTPException(status_code=422, detail=f"Invalid phone number format: {body.target_number}")
    try:
        purpose = validate_task(body.purpose)
    except BriefingError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    tz = body.timezone or settings.timezone or "UTC"
    try:
        when = resolve_when(tz=tz, run_in_minutes=body.run_in_minutes, at=body.at)
    except ScheduledCallError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    scheduler = CronScheduler(settings.db_path)
    await scheduler.ensure_table()

    action = build_action(
        target_number=number,
        purpose=purpose,
        target_name=body.target_name,
        language=body.language,
        instructions=body.instructions,
        thread_id=body.thread_id,
        scheduled_for=when.isoformat(),
    )
    # The name is what the schedules list shows, and it has to be unique per
    # user — the moment makes it so without a counter.
    name = f"Call {number} at {when.strftime('%Y-%m-%d %H:%M')}"
    try:
        sid = await scheduler.add(
            name=name,
            cron_expr=one_off_cron(when),
            action=action,
            pincer_user_id=_DASHBOARD_USER,
            tz=tz,
            channel="web",
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    row = await scheduler.get(sid)
    return ScheduledCallOut(
        id=sid,
        target_number=number,
        target_name=body.target_name,
        purpose=purpose,
        language=body.language,
        thread_id=body.thread_id,
        next_run_at=(row.next_run_at if row else "") or "",
        timezone=tz,
    )


@router.delete("/calls/scheduled/{schedule_id}", status_code=204)
async def cancel_scheduled_call(schedule_id: int) -> None:
    """Call it off. Only removes schedules this feature created."""
    from pincer.scheduler.cron import CronScheduler

    settings = get_settings_relaxed()
    scheduler = CronScheduler(settings.db_path)
    await scheduler.ensure_table()

    row = await scheduler.get(schedule_id)
    if row is None or _scheduled_call_out({"id": schedule_id, "action": row.action}) is None:
        raise HTTPException(status_code=404, detail="No such scheduled call")
    if not await scheduler.remove(schedule_id, _DASHBOARD_USER):
        raise HTTPException(status_code=404, detail="No such scheduled call")


@router.get("/calls/{call_sid}", response_model=CallDetail)
async def call_detail(call_sid: str) -> CallDetail:
    async with _db() as conn:
        try:
            cursor = await conn.execute(
                "SELECT c.call_sid, c.direction, c.from_number, c.to_number, c.started_at, c.ended_at, "
                "c.failure_code, c.thread_id, c.thread_attach_kind, c.briefing_json, "
                "t.subject AS thread_subject "
                "FROM voice_calls c LEFT JOIN call_threads t ON t.thread_id = c.thread_id "
                "WHERE c.call_sid = ?",
                (call_sid,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Call not found")

            t_rows = await conn.execute_fetchall(
                "SELECT speaker, text, confidence, state, timestamp "
                "FROM call_transcripts WHERE call_id = ? AND is_final = 1 ORDER BY timestamp ASC, id ASC",
                (call_sid,),
            )
            a_rows = await conn.execute_fetchall(
                "SELECT * FROM call_actions WHERE call_id = ? ORDER BY timestamp ASC, id ASC",
                (call_sid,),
            )
        except aiosqlite.OperationalError as e:
            raise HTTPException(status_code=404, detail="Call not found") from e

    from pincer.observability.call_costs import get_call_cost

    cost = await get_call_cost(get_settings_relaxed(), call_sid)
    from pincer.voice.analytics import load_analytics
    from pincer.voice.briefing import CallBriefing

    record = await load_analytics(get_settings_relaxed().db_path, call_sid)
    stored = CallBriefing.from_json(str(_row_value(row, "briefing_json") or ""))
    summary = _summary_from_row(row, float(cost["total_usd"]) if cost else None)
    return CallDetail(
        **summary.model_dump(),
        briefing=(
            BriefingOut(
                task=mask_pii(stored.task),
                source=stored.source,
                target_name=mask_pii(stored.target_name),
            )
            if stored is not None
            else None
        ),
        analytics=_analytics_out(record),
        cost=cost,
        transcript=[
            TranscriptLine(
                speaker=t["speaker"] or "",
                text=mask_pii(t["text"] or ""),
                confidence=t["confidence"] if t["confidence"] is not None else 1.0,
                state=t["state"] or "",
                timestamp=t["timestamp"] or "",
            )
            for t in t_rows
        ],
        actions=[
            CallActionOut(
                action_type=a["action_type"] or "",
                tool_name=a["tool_name"] or "",
                input_summary=mask_pii(a["input_summary"] or ""),
                output_summary=mask_pii(a["output_summary"] or ""),
                user_confirmed=None if a["user_confirmed"] is None else bool(a["user_confirmed"]),
                timestamp=a["timestamp"] or "",
                tier=str(_row_value(a, "tier") or ""),
                approval_mode=str(_row_value(a, "approval_mode") or ""),
                deny_reason=str(_row_value(a, "deny_reason") or ""),
            )
            for a in a_rows
        ],
    )


# ── Sprint 13: call threads (§9) ────────────────────────────────────
#
# Read surfaces mask PII like every other read surface here. The subject and
# rolling summary are user-authored/LLM-derived text about a third party, so
# they go through mask_pii too — the dashboard is not a way around masking.


class CommitmentOut(BaseModel):
    who: str = ""
    what: str = ""
    due: str | None = None
    status: str = "open"
    source_call_sid: str = ""


class ThreadOut(BaseModel):
    thread_id: str
    subject: str = ""
    status: str = "open"
    origin: str = ""
    primary_number: str = ""
    contact_name: str = ""
    language: str = ""
    rolling_summary: str = ""
    open_commitments: list[CommitmentOut] = Field(default_factory=list)
    call_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    resolved_at: str | None = None
    closed_at: str | None = None


class ThreadCallOut(BaseModel):
    call_sid: str
    thread_attach_kind: str = ""
    # Per-call sentiment for the thread timeline. v1 deliberately does NOT
    # synthesize a thread-level sentiment: a thread's story is the rolling
    # summary's job, and averaging readings across weeks would invent a trend.
    sentiment: str | None = None
    direction: str = ""
    started_at: str = ""
    ended_at: str | None = None
    outcome: str = ""
    task_result: str = ""
    failure_code: str = ""
    # True when the call row/transcript is gone to retention but the call is
    # still part of the matter's history (§5).
    purged: bool = False


class ThreadDetail(ThreadOut):
    calls: list[ThreadCallOut] = Field(default_factory=list)


class ThreadCreateIn(BaseModel):
    subject: str = Field(min_length=1, max_length=120)
    primary_number: str = Field(default="", max_length=20)
    contact_name: str = Field(default="", max_length=200)
    language: str = Field(default="", max_length=8)


class ThreadPatchIn(BaseModel):
    subject: str | None = Field(default=None, min_length=1, max_length=120)
    status: str | None = Field(default=None, pattern="^(open|resolved|closed)$")


class ThreadAssignIn(BaseModel):
    call_sid: str = Field(min_length=1, max_length=64)


class ThreadMergeIn(BaseModel):
    source_thread_id: str = Field(min_length=1, max_length=32)


def _thread_manager() -> Any:
    from pincer.voice.threads import get_thread_manager

    return get_thread_manager(get_settings_relaxed())


def _thread_out(thread: Any, call_count: int = 0) -> ThreadOut:
    return ThreadOut(
        thread_id=thread.thread_id,
        subject=mask_pii(thread.subject),
        status=thread.status,
        origin=thread.origin,
        primary_number=mask_pii(thread.primary_number),
        contact_name=mask_pii(thread.contact_name),
        language=thread.language,
        rolling_summary=mask_pii(thread.rolling_summary),
        open_commitments=[
            CommitmentOut(
                who=str(c.get("who", "")),
                what=mask_pii(str(c.get("what", ""))),
                due=c.get("due"),
                status=str(c.get("status", "open")),
                source_call_sid=str(c.get("source_call_sid", "")),
            )
            for c in thread.open_commitments
        ],
        call_count=call_count,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
        resolved_at=thread.resolved_at,
        closed_at=thread.closed_at,
    )


@asynccontextmanager
async def _thread_errors() -> AsyncIterator[None]:
    """ThreadError is the "you asked for something not allowed" signal (unknown
    thread, closed thread, invalid transition, single-thread rule). Map it once
    here instead of at every call site."""
    from pincer.voice.threads import ThreadError

    try:
        yield
    except ThreadError as e:
        message = str(e)
        status = 404 if "does not exist" in message else 409
        raise HTTPException(status_code=status, detail=message) from e


def _parse_statuses(values: list[str] | None) -> list[str]:
    """Normalize the `status` filter into a list of statuses to include.

    A dashboard offers combined views ("open + resolved") and an "all" chip, so
    this accepts every reasonable encoding of that rather than making the UI
    guess ours: repeated params (`status=open&status=resolved`), a comma list,
    or a space-separated one (which is what `status=open+resolved` decodes to).
    Empty, or "all", means no filter. An unknown status is a 422, never a
    silently ignored filter that would show the user the wrong set of threads.
    """
    from pincer.voice.threads import STATUSES

    wanted: list[str] = []
    for value in values or []:
        for part in str(value).replace(",", " ").split():
            part = part.strip().lower()
            if not part or part == "all":
                continue
            if part not in STATUSES:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown thread status {part!r} (expected any of {', '.join(STATUSES)}, all)",
                )
            if part not in wanted:
                wanted.append(part)
    return wanted


@router.get("/threads", response_model=list[ThreadOut])
async def list_threads(
    status: Annotated[list[str] | None, Query()] = None,
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    has_expired_commitments: bool = Query(default=False),
) -> list[ThreadOut]:
    """Threads, newest activity first. `q` matches subject, contact, or number."""
    statuses = _parse_statuses(status)
    manager = _thread_manager()
    try:
        found = await manager.list_threads(
            status=statuses,
            query=q,
            limit=limit,
            offset=offset,
            has_expired_commitments=has_expired_commitments,
        )
    except aiosqlite.OperationalError:  # voice tables not created yet
        return []
    out: list[ThreadOut] = []
    for thread in found:
        calls = await manager.calls(thread.thread_id)
        out.append(_thread_out(thread, call_count=len(calls)))
    return out


@router.post("/threads", response_model=ThreadOut, status_code=201)
async def create_thread(body: ThreadCreateIn) -> ThreadOut:
    """Manually open an empty thread (§4.4) — calls are assigned to it after."""
    from pincer.voice.threads import ORIGIN_USER_TASK

    async with _thread_errors():
        thread = await _thread_manager().create(
            subject=body.subject,
            origin=ORIGIN_USER_TASK,
            primary_number=body.primary_number,
            contact_name=body.contact_name,
            language=body.language,
        )
    return _thread_out(thread)


@router.get("/threads/{thread_id}", response_model=ThreadDetail)
async def thread_detail(thread_id: str) -> ThreadDetail:
    """Thread + its calls in order. Purged calls are listed as stubs (§5)."""
    manager = _thread_manager()
    async with _thread_errors():
        thread = await manager.require(thread_id)
    calls = await manager.calls(thread_id)
    from pincer.voice.analytics import load_many

    records = await load_many(get_settings_relaxed().db_path, [c.call_sid for c in calls])
    return ThreadDetail(
        **_thread_out(thread, call_count=len(calls)).model_dump(),
        calls=[
            ThreadCallOut(
                call_sid=c.call_sid,
                thread_attach_kind=c.attach_kind,
                sentiment=records[c.call_sid].sentiment if c.call_sid in records else None,
                direction=c.direction,
                started_at=c.started_at,
                ended_at=c.ended_at,
                outcome=c.outcome_code,
                task_result=mask_pii(c.task_result),
                failure_code=c.failure_code,
                purged=c.purged,
            )
            for c in calls
        ],
    )


@router.patch("/threads/{thread_id}", response_model=ThreadOut)
async def patch_thread(thread_id: str, body: ThreadPatchIn) -> ThreadOut:
    """Rename a thread and/or move its status. Transitions are validated by
    the §5 table — in particular, closed is final."""
    manager = _thread_manager()
    async with _thread_errors():
        thread = await manager.require(thread_id)
        if body.subject is not None:
            thread = await manager.update_subject(thread_id, body.subject)
        if body.status is not None:
            thread = await manager.set_status(thread_id, body.status, reason="dashboard")
    calls = await manager.calls(thread_id)
    return _thread_out(thread, call_count=len(calls))


@router.post("/threads/{thread_id}/assign", response_model=ThreadDetail)
async def assign_call_to_thread(thread_id: str, body: ThreadAssignIn) -> ThreadDetail:
    """Reassign one call to this thread (§4.4). Removes it from its previous
    thread — the only way a call ever leaves one."""
    async with _thread_errors():
        from pincer.voice.threads import KIND_MANUAL

        await _thread_manager().attach(body.call_sid, thread_id, KIND_MANUAL)
    return await thread_detail(thread_id)


@router.post("/threads/{thread_id}/merge", response_model=ThreadDetail)
async def merge_threads(thread_id: str, body: ThreadMergeIn) -> ThreadDetail:
    """Merge the source thread into this one: its calls re-attach as `manual`
    and the source is closed with a `merged_into` note in the audit log."""
    async with _thread_errors():
        await _thread_manager().merge(body.source_thread_id, thread_id)
    return await thread_detail(thread_id)


# ── Sprint 11: in-call approvals (`user` mode) ──────────────────────


class VoiceApprovalOut(BaseModel):
    approval_id: str
    call_sid: str
    tool_name: str
    summary: str
    summary_spoken_language: str = ""
    args_preview: dict[str, Any] = Field(default_factory=dict)
    expires_at: str = ""
    created_at: str = ""
    final_state: str = ""


class VoiceApprovalDecision(BaseModel):
    approved: bool


@router.get("/approvals", response_model=list[VoiceApprovalOut])
async def voice_approvals_pending(call_sid: str | None = None) -> list[VoiceApprovalOut]:
    """Open in-call approval requests (the dashboard's approval card source)."""
    from pincer.voice import approvals as voice_approvals

    return [VoiceApprovalOut(**req.payload()) for req in voice_approvals.pending(call_sid)]


@router.post("/approvals/{approval_id}", response_model=VoiceApprovalOut)
async def voice_approval_decide(approval_id: str, body: VoiceApprovalDecision) -> VoiceApprovalOut:
    """Answer an open in-call approval. 404 when unknown, already answered, or expired."""
    from pincer.voice import approvals as voice_approvals

    req = voice_approvals.get(approval_id)
    if req is None or not voice_approvals.resolve(approval_id, body.approved):
        raise HTTPException(status_code=404, detail="Approval not open")
    return VoiceApprovalOut(**req.payload())


@router.get("/contacts", response_model=list[Contact])
async def contacts() -> list[Contact]:
    async with _db() as conn:
        try:
            rows = await conn.execute_fetchall(
                "SELECT name, phone_number, category, notes FROM phone_contacts ORDER BY name COLLATE NOCASE ASC"
            )
        except aiosqlite.OperationalError:  # table only exists once contacts are used
            return []
    return [
        Contact(
            name=r["name"] or "",
            phone_number=r["phone_number"] or "",
            category=r["category"] or "",
            notes=r["notes"] or "",
        )
        for r in rows
    ]


# make_phone_call reports failures as "Error: ..." strings (tool contract);
# map the known ones to meaningful HTTP status codes for the app.
_ERROR_STATUS: list[tuple[str, int]] = [
    ("not enabled", 403),
    ("disabled", 403),
    ("Invalid phone number", 422),
    ("Daily outbound call limit", 429),
    # T8.3 outbound abuse gate — same limits, same wording, whatever the channel
    ("do-not-call list", 403),
    ("Sperrliste", 403),
    ("quiet hours", 403),
    ("Ruhezeit", 403),
    ("Tageslimit für ausgehende Anrufe", 429),
    ("before dialling again", 429),
    ("Wartezeit", 429),
    ("WEBHOOK_BASE_URL", 503),
    ("not installed", 503),
    ("not connected", 503),  # Google Calendar missing (schedule endpoint)
    ("no free slot", 409),
    ("kein freier Slot", 409),
    # Sprint 13 §4.2: a supplied thread that cannot take the call
    ("does not exist", 404),
    ("is closed and cannot", 409),
    # A call the agent cannot open with a task is a client error, not a 502
    ("Purpose too short", 422),
    ("Purpose too long", 422),
]

_CALL_SID_RE = re.compile(r"Call SID: (\S+)")


@router.post("/calls", response_model=InitiateCallOut, status_code=201)
async def initiate_call(body: InitiateCallIn) -> InitiateCallOut:
    from pincer.voice.briefing import SOURCE_DASHBOARD, BriefingError, validate_task
    from pincer.voice.outbound import make_phone_call

    # Validated here as well as inside make_phone_call so the dashboard gets a
    # 422 with the actionable message before anything is attempted.
    try:
        validate_task(body.purpose)
    except BriefingError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    try:
        result = await make_phone_call(
            target_number=body.target_number,
            purpose=body.purpose,
            instructions=body.instructions,
            language=body.language,
            context={"user_id": "dashboard", "channel": "web"},
            target_name=body.target_name,
            thread_id=body.thread_id,
            source=SOURCE_DASHBOARD,
        )
    except Exception as e:  # make_phone_call catches its own errors; belt and braces
        logger.exception("Dashboard-initiated call failed")
        raise HTTPException(status_code=502, detail=f"Call initiation failed: {e}") from e

    if result.startswith("Error"):  # "Error: ..." or "Error placing call: ..."
        detail = result.removeprefix("Error:").strip()
        code = next((c for marker, c in _ERROR_STATUS if marker in detail), 502)
        raise HTTPException(status_code=code, detail=detail)

    match = _CALL_SID_RE.search(result)
    return InitiateCallOut(
        call_sid=match.group(1) if match else "",
        status="initiated",
        message=result,
    )


def _turn_model_choices(settings: Any) -> list[TurnModelChoice]:
    """Model options offered in the dashboard, gated on configured API keys."""
    choices = [TurnModelChoice(value="", label=f"Default ({settings.default_model})")]
    try:
        has_anthropic = bool(settings.anthropic_api_key.get_secret_value())
    except AttributeError:
        has_anthropic = False
    try:
        has_openai = bool(settings.openai_api_key.get_secret_value())
    except AttributeError:
        has_openai = False
    if has_anthropic:
        choices.append(TurnModelChoice(value="claude-haiku-4-5-20251001", label="Claude Haiku 4.5 (fast)"))
    if has_openai:
        choices.append(TurnModelChoice(value="openai:gpt-5-mini", label="GPT-5 mini (OpenAI, reasoning)"))
        choices.append(TurnModelChoice(value="openai:gpt-4o-mini", label="GPT-4o mini (OpenAI, fast)"))
    return choices


def _live_settings_objects(request: Request) -> list[Any]:
    """Every Settings instance that must see a runtime change: the API's
    relaxed singleton AND the running agent's own instance (they can differ —
    the voice pipeline reads the agent's)."""
    objects: list[Any] = [get_settings_relaxed()]
    agent = getattr(request.app.state, "agent", None)
    agent_settings = getattr(agent, "_settings", None)
    if agent_settings is not None and agent_settings is not objects[0]:
        objects.append(agent_settings)
    return objects


@router.get("/config", response_model=VoiceConfig)
async def voice_config(request: Request) -> VoiceConfig:
    """Current voice turn model + the options the dashboard can offer."""
    settings_objects = _live_settings_objects(request)
    current = str(getattr(settings_objects[-1], "voice_turn_model", "") or "")
    base = get_settings_relaxed()
    return VoiceConfig(
        voice_turn_model=current,
        default_model=str(getattr(base, "default_model", "") or ""),
        choices=_turn_model_choices(base),
    )


@router.put("/config", response_model=VoiceConfig)
async def update_voice_config(body: VoiceConfigUpdate, request: Request) -> VoiceConfig:
    """Switch the voice turn model at runtime (Sprint 5 T5.4 model tiering).

    Applied to the live settings immediately — the very next call turn uses
    it — and persisted to data_dir/voice_runtime.json across restarts."""
    from pincer.voice.runtime_config import TURN_MODEL_RE, save_turn_model

    model = body.voice_turn_model.strip()
    if not TURN_MODEL_RE.match(model):
        raise HTTPException(status_code=422, detail=f"Invalid turn model format: {model!r}")

    settings_objects = _live_settings_objects(request)
    for settings_obj in settings_objects:
        settings_obj.voice_turn_model = model
    try:
        save_turn_model(settings_objects[0], model)
    except Exception:
        logger.exception("voice_runtime.json write failed — change is active but not persisted")

    logger.info("Voice turn model set via API: %r", model or "(default)")
    base = get_settings_relaxed()
    return VoiceConfig(
        voice_turn_model=model,
        default_model=str(getattr(base, "default_model", "") or ""),
        choices=_turn_model_choices(base),
    )


@router.post("/schedule", response_model=ScheduleAppointmentOut, status_code=201)
async def schedule_appointment(body: ScheduleAppointmentIn, request: Request) -> ScheduleAppointmentOut:
    """Sprint 6: appointment scheduling from the dashboard — same workflow as
    the schedule_appointment_call chat tool (free/busy → candidates → call →
    calendar event). Dashboard-token auth stands in for the approval gate."""
    from pincer.voice.briefing import SOURCE_DASHBOARD, BriefingError, validate_task, validate_topic
    from pincer.voice.scheduling import schedule_appointment_call

    # Validated before anything else: a bad request is the caller's problem to
    # fix, and reporting it as "service unavailable" sends them the wrong way.
    try:
        validate_topic(body.topic)
        validate_task(f"Schedule appointment with {body.contact_name}: {body.topic}")
    except BriefingError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    agent = getattr(request.app.state, "agent", None)
    tools = getattr(agent, "_tools", None)
    if tools is None:
        raise HTTPException(status_code=503, detail="Agent tool registry not available")

    settings = get_settings_relaxed()
    result = await schedule_appointment_call(
        tools,
        settings,
        target_number=body.target_number,
        contact_name=body.contact_name,
        topic=body.topic,
        timeframe=body.timeframe,
        duration_minutes=body.duration_minutes,
        language=body.language,
        attendees=body.attendees,
        location_or_meet=body.location_or_meet,
        user_id="dashboard",
        channel="web",
        thread_id=body.thread_id,
        source=SOURCE_DASHBOARD,
    )
    if result.startswith("Error"):
        detail = result.removeprefix("Error:").strip()
        code = next((c for marker, c in _ERROR_STATUS if marker in detail), 502)
        raise HTTPException(status_code=code, detail=detail)
    return ScheduleAppointmentOut(status="initiated", message=result)


# ── Do-not-call list (Sprint 8, T8.3) ────────────────────────────────
#
# The list is shared: an entry blocks the number for every user and every
# channel. Numbers are returned unmasked here because this endpoint exists to
# let an operator audit and correct the list — a masked entry cannot be removed.


@router.get("/do-not-call", response_model=list[DoNotCallEntry])
async def do_not_call_list() -> list[DoNotCallEntry]:
    from pincer.voice.safety_gates import list_do_not_call

    entries = await list_do_not_call(get_settings_relaxed())
    return [
        DoNotCallEntry(
            phone_number=e.get("phone_number", ""),
            reason=e.get("reason", "") or "",
            source=e.get("source", "") or "",
            call_sid=e.get("call_sid", "") or "",
            added_at=e.get("added_at", "") or "",
        )
        for e in entries
    ]


@router.post("/do-not-call", response_model=DoNotCallEntry, status_code=201)
async def do_not_call_add(body: DoNotCallIn) -> DoNotCallEntry:
    from pincer.voice.outbound import validate_e164
    from pincer.voice.safety_gates import add_do_not_call

    validated = validate_e164(body.phone_number)
    if not validated:
        raise HTTPException(status_code=422, detail=f"Invalid phone number format: {body.phone_number}")

    settings = get_settings_relaxed()
    await add_do_not_call(settings, validated, reason=body.reason, source="dashboard")
    logger.info("Number added to do-not-call list via dashboard")
    return DoNotCallEntry(phone_number=validated, reason=body.reason, source="dashboard")


@router.delete("/do-not-call/{phone_number}", status_code=204)
async def do_not_call_remove(phone_number: str) -> None:
    """Remove a number. Only an explicit human action can undo an opt-out."""
    from pincer.voice.safety_gates import remove_do_not_call

    removed = await remove_do_not_call(get_settings_relaxed(), phone_number)
    if not removed:
        raise HTTPException(status_code=404, detail="Number not on the do-not-call list")
    logger.warning("Number removed from do-not-call list via dashboard")
