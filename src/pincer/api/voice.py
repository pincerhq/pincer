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

import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Query, Request
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


@asynccontextmanager
async def _db() -> AsyncIterator[aiosqlite.Connection]:
    settings = get_settings_relaxed()
    async with aiosqlite.connect(settings.db_path) as conn:
        conn.row_factory = aiosqlite.Row
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


class ActiveCall(BaseModel):
    call_sid: str
    direction: str
    caller_number: str = ""
    target_number: str = ""
    target_name: str = ""
    purpose: str = ""
    language: str = ""
    engine: str = ""
    started_at: str = ""
    duration_seconds: int = 0


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
    )


@router.get("/active", response_model=list[ActiveCall])
async def active_calls() -> list[ActiveCall]:
    engine = _get_engine()
    if engine is None:
        return []
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
        )
        for sid, st in engine.get_active_calls().items()
    ]


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


@router.get("/calls", response_model=list[CallSummary])
async def list_calls(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    direction: str | None = Query(default=None, pattern="^(inbound|outbound)$"),
    status: str | None = Query(default=None, pattern="^(active|completed)$"),
) -> list[CallSummary]:
    sql = "SELECT call_sid, direction, from_number, to_number, started_at, ended_at, failure_code FROM voice_calls"
    where: list[str] = []
    args: list[Any] = []
    if direction:
        where.append("direction = ?")
        args.append(direction)
    if status:
        where.append("ended_at IS NOT NULL" if status == "completed" else "ended_at IS NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
    args += [limit, offset]

    async with _db() as conn:
        try:
            rows = await conn.execute_fetchall(sql, args)
        except aiosqlite.OperationalError:  # voice tables not created yet
            return []

    # One batched cost lookup for the page, not one per row.
    from pincer.observability.call_costs import get_call_costs

    costs = await get_call_costs(get_settings_relaxed(), [str(r["call_sid"]) for r in rows])
    return [_summary_from_row(r, costs.get(str(r["call_sid"]))) for r in rows]


@router.get("/calls/{call_sid}", response_model=CallDetail)
async def call_detail(call_sid: str) -> CallDetail:
    async with _db() as conn:
        try:
            cursor = await conn.execute(
                "SELECT call_sid, direction, from_number, to_number, started_at, ended_at, failure_code "
                "FROM voice_calls WHERE call_sid = ?",
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
    summary = _summary_from_row(row, float(cost["total_usd"]) if cost else None)
    return CallDetail(
        **summary.model_dump(),
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
]

_CALL_SID_RE = re.compile(r"Call SID: (\S+)")


@router.post("/calls", response_model=InitiateCallOut, status_code=201)
async def initiate_call(body: InitiateCallIn) -> InitiateCallOut:
    from pincer.voice.outbound import make_phone_call

    try:
        result = await make_phone_call(
            target_number=body.target_number,
            purpose=body.purpose,
            instructions=body.instructions,
            language=body.language,
            context={"user_id": "dashboard", "channel": "web"},
            target_name=body.target_name,
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
    agent = getattr(request.app.state, "agent", None)
    tools = getattr(agent, "_tools", None)
    if tools is None:
        raise HTTPException(status_code=503, detail="Agent tool registry not available")

    from pincer.voice.scheduling import schedule_appointment_call

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
