"""
Safety gates — the two places a voice action can be stopped.

1. Confirmation gates: mandatory verbal confirmation before consequential
   actions. Every action that spends money, modifies schedules, sends
   messages, or shares personal data passes through one during a call.

2. The outbound-call gate (Sprint 8, T8.3): the single server-side chokepoint
   every dial attempt crosses — chat tool, dashboard API, appointment
   scheduler, and automatic retries all reach Twilio through
   `voice.outbound.make_phone_call`, which asks `check_outbound_allowed`
   first. It enforces, in order:

     - the shared do-not-call list (honoured across ALL initiating users),
     - quiet hours (default 20:00-08:00 local, §7 UWG cold-calling
       sensitivity),
     - a global daily call cap across every user and channel,
     - a per-target cooldown that bounds how often one number can be dialled.

   Because retries go through the same function, a retry storm cannot exist:
   redials consume the daily cap and count against the target cooldown.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pincer.config import Settings

logger = logging.getLogger(__name__)


class ActionCategory(StrEnum):
    SPENDING = "spending"
    SCHEDULING = "scheduling"
    MESSAGING = "messaging"
    DATA_SHARING = "data_sharing"
    CANCELLATION = "cancellation"
    CALLING = "calling"
    OTHER = "other"


class ConfirmationStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNCLEAR = "unclear"


@dataclass
class ConfirmationGate:
    category: ActionCategory
    description: str
    prompt: str
    status: ConfirmationStatus = ConfirmationStatus.PENDING


CONFIRMATION_PATTERNS: dict[ActionCategory, str] = {
    ActionCategory.SPENDING: "This will cost {details}. Should I go ahead?",
    ActionCategory.SCHEDULING: "I'll book {details}. Confirm?",
    ActionCategory.MESSAGING: "I'll send {details}. OK?",
    ActionCategory.DATA_SHARING: "They're asking for your {details}. Should I share it?",
    ActionCategory.CANCELLATION: "This will cancel {details}. Are you sure?",
    ActionCategory.CALLING: "I'll call {details}. Proceed?",
    ActionCategory.OTHER: "I'm going to {details}. Is that correct?",
}

# English + German (Sprint 2). German affirmatives use lookarounds so that
# negated forms ("passt nicht", "nicht richtig") don't double-match as both.
AFFIRMATIVE_PATTERNS = re.compile(
    r"\b(yes|yeah|yep|yup|sure|go ahead|do it|correct|confirmed|absolutely|"
    r"that's right|proceed|affirmative|ok|okay|sounds good|perfect|"
    r"go for it|please do|of course|"
    r"ja|jawohl|genau|klar|gerne|einverstanden|in ordnung|"
    r"stimmt(?!\s+nicht)|korrekt|"
    r"(?<!nicht )richtig|(?<!not )right|"
    r"passt(?!\s+(?:mir\s+|es\s+)?nicht)|"
    r"machen sie das|mach das|sehr gut)\b",
    re.IGNORECASE,
)

NEGATIVE_PATTERNS = re.compile(
    r"\b(no|nah|nope|don't|stop|wait|hold on|cancel|never mind|"
    r"not yet|hold off|scratch that|forget it|negative|wrong|"
    r"that's wrong|incorrect|actually no|"
    r"nein|nee|nö|lieber nicht|bloß nicht|auf keinen fall|keinesfalls|"
    r"stopp|warte|moment mal|abbrechen|vergessen sie es|vergiss es|"
    r"passt (?:mir |es )?nicht|stimmt nicht|nicht richtig|falsch|"
    r"das ist falsch|doch nicht|so nicht)\b",
    re.IGNORECASE,
)


def classify_action(tool_name: str, arguments: dict) -> ActionCategory:
    """Classify a tool call into an action category for confirmation."""
    spending_tools = {"make_payment", "purchase", "order", "buy"}
    scheduling_tools = {"calendar_create", "schedule", "book", "reschedule"}
    messaging_tools = {"email_send", "send_message", "sms_send"}
    calling_tools = {"make_phone_call"}
    cancel_tools = {"cancel", "delete", "remove", "unsubscribe"}

    name_lower = tool_name.lower()

    if name_lower in calling_tools or "call" in name_lower:
        return ActionCategory.CALLING
    if name_lower in spending_tools or "pay" in name_lower or "cost" in str(arguments):
        return ActionCategory.SPENDING
    if name_lower in scheduling_tools or "calendar" in name_lower:
        return ActionCategory.SCHEDULING
    if name_lower in messaging_tools or "email_send" in name_lower:
        return ActionCategory.MESSAGING
    if name_lower in cancel_tools or "cancel" in name_lower:
        return ActionCategory.CANCELLATION

    return ActionCategory.OTHER


def build_confirmation_prompt(category: ActionCategory, details: str) -> str:
    """Build a natural-language confirmation prompt for the given action."""
    template = CONFIRMATION_PATTERNS.get(category, CONFIRMATION_PATTERNS[ActionCategory.OTHER])
    return template.format(details=details)


def parse_confirmation(utterance: str) -> ConfirmationStatus:
    """Parse a user's verbal response as confirmation, rejection, or unclear."""
    text = utterance.strip()
    if not text:
        return ConfirmationStatus.UNCLEAR

    has_affirmative = bool(AFFIRMATIVE_PATTERNS.search(text))
    has_negative = bool(NEGATIVE_PATTERNS.search(text))

    if has_affirmative and not has_negative:
        return ConfirmationStatus.CONFIRMED
    if has_negative and not has_affirmative:
        return ConfirmationStatus.REJECTED
    if has_affirmative and has_negative:
        return ConfirmationStatus.UNCLEAR

    return ConfirmationStatus.UNCLEAR


def create_gate(tool_name: str, arguments: dict, description: str = "") -> ConfirmationGate:
    """Create a confirmation gate for a tool call."""
    category = classify_action(tool_name, arguments)
    if not description:
        description = f"{tool_name} with {arguments}"

    prompt = build_confirmation_prompt(category, description)

    return ConfirmationGate(
        category=category,
        description=description,
        prompt=prompt,
    )


def requires_confirmation(tool_name: str) -> bool:
    """Check if a tool call requires verbal confirmation during a voice call."""
    no_confirm_tools = {
        "calendar_today",
        "calendar_week",
        "email_check",
        "email_read",
        "email_search",
        "email_list_folders",
        "file_read",
        "file_list",
        "load_skill",
        "load_skill_reference",
    }
    return tool_name not in no_confirm_tools


# ══════════════════════════════════════════════════════════════════════
# Outbound call gate (Sprint 8, T8.3)
# ══════════════════════════════════════════════════════════════════════

OUTBOUND_GUARD_SQL = """
CREATE TABLE IF NOT EXISTS do_not_call (
    phone_number TEXT PRIMARY KEY,
    reason TEXT DEFAULT '',
    source TEXT DEFAULT '',
    call_sid TEXT DEFAULT '',
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbound_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_number TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    channel TEXT DEFAULT '',
    call_sid TEXT DEFAULT '',
    placed_at TEXT NOT NULL,
    local_day TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbound_log_day ON outbound_call_log(local_day);
CREATE INDEX IF NOT EXISTS idx_outbound_log_number ON outbound_call_log(phone_number, placed_at);
"""


class BlockReason(StrEnum):
    """Why a dial attempt was refused. Stable identifiers — the API maps these
    to HTTP status codes and tests assert on them."""

    DO_NOT_CALL = "do_not_call"
    QUIET_HOURS = "quiet_hours"
    DAILY_LIMIT = "daily_limit"
    TARGET_COOLDOWN = "target_cooldown"


@dataclass
class OutboundDecision:
    """Verdict of the outbound gate. `message` is user-facing, `reason` is code-facing."""

    allowed: bool
    reason: BlockReason | None = None
    message: str = ""
    retry_after_min: int = 0

    def __bool__(self) -> bool:
        return self.allowed


_BLOCK_MESSAGES: dict[str, dict[BlockReason, str]] = {
    "en": {
        BlockReason.DO_NOT_CALL: (
            "{number} is on the do-not-call list (they asked not to be called again), so I won't dial it."
        ),
        BlockReason.QUIET_HOURS: (
            "It's quiet hours ({window} {tz}) — I won't call {number} now. I can place the call after {resume}."
        ),
        BlockReason.DAILY_LIMIT: "Daily outbound call limit reached ({limit} calls today). Try again tomorrow.",
        BlockReason.TARGET_COOLDOWN: (
            "{number} has already been called {attempts} time(s) in the last {window} minutes — "
            "waiting {retry_after} more minute(s) before dialling again."
        ),
    },
    "de": {
        BlockReason.DO_NOT_CALL: (
            "{number} steht auf der Sperrliste (kein weiterer Anruf gewünscht) — ich rufe dort nicht an."
        ),
        BlockReason.QUIET_HOURS: (
            "Ruhezeit ({window} {tz}) — ich rufe {number} jetzt nicht an. Ab {resume} ist der Anruf möglich."
        ),
        BlockReason.DAILY_LIMIT: (
            "Tageslimit für ausgehende Anrufe erreicht ({limit} Anrufe heute). Bitte morgen erneut versuchen."
        ),
        BlockReason.TARGET_COOLDOWN: (
            "{number} wurde in den letzten {window} Minuten bereits {attempts}-mal angerufen — "
            "noch {retry_after} Minute(n) Wartezeit."
        ),
    },
}


def _messages(language: str) -> dict[BlockReason, str]:
    return _BLOCK_MESSAGES.get((language or "en").strip().lower()[:2], _BLOCK_MESSAGES["en"])


# ── Storage ──────────────────────────────────────────────────────────


async def ensure_outbound_tables(conn: aiosqlite.Connection) -> None:
    await conn.executescript(OUTBOUND_GUARD_SQL)
    await conn.commit()


@asynccontextmanager
async def _db(settings: Settings | Any) -> AsyncIterator[aiosqlite.Connection]:
    """Open the guard tables, always closing the connection.

    `aiosqlite.Connection.__aenter__` awaits the object again, so the
    connection must be created *inside* the context manager rather than
    awaited first and then entered — doing both starts its worker thread twice.
    """
    async with aiosqlite.connect(str(settings.db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await ensure_outbound_tables(conn)
        yield conn


def normalize_number(number: str) -> str:
    """Comparison key for a phone number: digits and a leading +, nothing else."""
    cleaned = re.sub(r"[^\d+]", "", (number or "").strip())
    if cleaned and not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


# ── Do-not-call list ─────────────────────────────────────────────────


async def add_do_not_call(
    settings: Settings | Any,
    number: str,
    reason: str = "",
    source: str = "manual",
    call_sid: str = "",
) -> bool:
    """Add a number to the shared do-not-call list. Idempotent; returns True
    when the number was not already listed."""
    key = normalize_number(number)
    if not key:
        return False
    async with _db(settings) as conn:
        cursor = await conn.execute("SELECT 1 FROM do_not_call WHERE phone_number = ?", (key,))
        already = await cursor.fetchone() is not None
        await conn.execute(
            "INSERT INTO do_not_call (phone_number, reason, source, call_sid, added_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(phone_number) DO UPDATE SET reason=excluded.reason, source=excluded.source",
            (key, reason, source, call_sid, datetime.now(UTC).isoformat()),
        )
        await conn.commit()
    if not already:
        from pincer.voice.pii_guard import mask_phone_number

        # Masked at the call site as well as by the root filter: this line is
        # emitted from CLI paths that never run `_setup_logging`.
        logger.warning("Added to do-not-call list: %s (source=%s, reason=%s)", mask_phone_number(key), source, reason)
    return not already


async def remove_do_not_call(settings: Settings | Any, number: str) -> bool:
    """Remove a number from the do-not-call list (explicit user override)."""
    key = normalize_number(number)
    async with _db(settings) as conn:
        cursor = await conn.execute("DELETE FROM do_not_call WHERE phone_number = ?", (key,))
        await conn.commit()
        return bool(cursor.rowcount)


async def is_do_not_call(settings: Settings | Any, number: str) -> bool:
    key = normalize_number(number)
    if not key:
        return False
    async with _db(settings) as conn:
        cursor = await conn.execute("SELECT 1 FROM do_not_call WHERE phone_number = ?", (key,))
        return await cursor.fetchone() is not None


async def list_do_not_call(settings: Settings | Any) -> list[dict[str, str]]:
    async with _db(settings) as conn:
        rows = await conn.execute_fetchall(
            "SELECT phone_number, reason, source, call_sid, added_at FROM do_not_call ORDER BY added_at DESC"
        )
    return [dict(row) for row in rows]


# Callee opt-out intent, EN + DE + UK. Deliberately narrow: only unambiguous
# "never call me again" phrasings, so an annoyed "stop" mid-sentence does not
# silently blacklist a number the user needs.
OPT_OUT_PATTERNS = re.compile(
    r"("
    r"(?:do\s*n[o']?t|don't|never|stop)\s+(?:ever\s+)?(?:call|calling|phone|ring)(?:\s+me)?(?:\s+again|\s+anymore|\s+any\s+more)?"
    r"|take\s+(?:me|this\s+number|my\s+number)\s+off\s+(?:your\s+)?(?:the\s+)?list"
    r"|remove\s+(?:me|this\s+number|my\s+number)\s+from\s+(?:your\s+)?(?:the\s+)?(?:list|database|records)"
    r"|remove\s+this\s+number"
    r"|(?:put\s+me|add\s+me)\s+on\s+(?:your\s+)?do[\s-]*not[\s-]*call"
    # "Rufen Sie mich bitte nicht mehr an", "Ruf hier nie wieder an", …
    r"|ruf(?:e|en)?\s+(?:sie\s+)?(?:\w+\s+){0,3}?ni(?:cht|e)\s+(?:mehr|wieder)\s+an"
    r"|nie\s+wieder\s+an(?:rufen)?"
    r"|(?:keine|nie\s+wieder)\s+(?:weiteren\s+)?anrufe(?:\s+mehr)?"
    r"|(?:meine\s+)?nummer\s+(?:aus\s+\w+\s+)?(?:liste\s+)?(?:löschen|streichen|entfernen)"
    r"|löschen\s+sie\s+(?:meine|diese)\s+nummer"
    r"|setzen\s+sie\s+mich\s+auf\s+die\s+sperrliste"
    r"|більше\s+не\s+(?:дзвоніть|телефонуйте)"
    r"|не\s+дзвоніть\s+(?:мені\s+)?більше"
    r")",
    re.IGNORECASE,
)


def detect_opt_out(text: str) -> bool:
    """True when the callee unambiguously asked never to be called again."""
    return bool(text) and bool(OPT_OUT_PATTERNS.search(text))


async def honor_opt_out(
    settings: Settings | Any,
    number: str,
    transcript_text: str,
    call_sid: str = "",
) -> bool:
    """Auto-add the callee to the do-not-call list when the call transcript
    contains an opt-out request. Called by the post-call pipeline."""
    if not detect_opt_out(transcript_text):
        return False
    await add_do_not_call(settings, number, reason="callee opt-out during call", source="callee", call_sid=call_sid)
    return True


# ── Call log (daily cap + target cooldown) ───────────────────────────


async def record_outbound_call(
    settings: Settings | Any,
    number: str,
    user_id: str = "",
    channel: str = "",
    call_sid: str = "",
) -> None:
    """Record a placed call. Feeds the daily cap and the target cooldown."""
    from pincer.voice.localtime import voice_today_str

    async with _db(settings) as conn:
        await conn.execute(
            "INSERT INTO outbound_call_log (phone_number, user_id, channel, call_sid, placed_at, local_day) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                normalize_number(number),
                user_id,
                channel,
                call_sid,
                datetime.now(UTC).isoformat(),
                voice_today_str(settings),
            ),
        )
        await conn.commit()


async def calls_today(settings: Settings | Any) -> int:
    """Outbound calls placed today (voice-local day) across all users/channels."""
    from pincer.voice.localtime import voice_today_str

    async with _db(settings) as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM outbound_call_log WHERE local_day = ?", (voice_today_str(settings),)
        )
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def _recent_target_calls(settings: Settings | Any, number: str, window_min: int) -> list[datetime]:
    cutoff = datetime.now(UTC) - timedelta(minutes=window_min)
    async with _db(settings) as conn:
        rows = await conn.execute_fetchall(
            "SELECT placed_at FROM outbound_call_log WHERE phone_number = ? AND placed_at >= ? ORDER BY placed_at ASC",
            (normalize_number(number), cutoff.isoformat()),
        )
    stamps: list[datetime] = []
    for row in rows:
        try:
            parsed = datetime.fromisoformat(str(row[0]))
        except (TypeError, ValueError):
            continue
        stamps.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC))
    return stamps


def max_attempts_per_target(settings: Settings | Any) -> int:
    """Calls allowed to one number inside the cooldown window.

    The retry policy (`voice_retry_attempts`) may legitimately redial after a
    voicemail or no-answer, so the cooldown budgets exactly the first call plus
    its configured retries. Anything past that is hammering, and is blocked.
    """
    return 1 + max(0, int(getattr(settings, "voice_retry_attempts", 0) or 0))


# ── Quiet hours ──────────────────────────────────────────────────────


def parse_quiet_hours(value: str) -> tuple[int, int] | None:
    """Parse "HH:MM-HH:MM" into (start_minutes, end_minutes), or None if unset/invalid."""
    raw = (value or "").strip()
    if not raw:
        return None
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", raw)
    if not match:
        logger.warning("Ignoring malformed PINCER_VOICE_QUIET_HOURS=%r (expected HH:MM-HH:MM)", value)
        return None
    sh, sm, eh, em = (int(g) for g in match.groups())
    if not (0 <= sh < 24 and 0 <= sm < 60 and 0 <= eh < 24 and 0 <= em < 60):
        logger.warning("Ignoring out-of-range PINCER_VOICE_QUIET_HOURS=%r", value)
        return None
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return None  # zero-length window = quiet hours off
    return start, end


def in_quiet_hours(settings: Settings | Any, now: datetime | None = None) -> bool:
    """True when local time falls inside the configured quiet-hours window
    (which normally wraps midnight, e.g. 20:00-08:00)."""
    window = parse_quiet_hours(str(getattr(settings, "voice_quiet_hours", "") or ""))
    if window is None:
        return False
    from pincer.voice.localtime import voice_now

    local = now or voice_now(settings)
    minutes = local.hour * 60 + local.minute
    start, end = window
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end  # wraps midnight


def quiet_hours_resume(settings: Settings | Any, now: datetime | None = None) -> str:
    """Local clock time at which quiet hours end ("08:00"), for the message."""
    window = parse_quiet_hours(str(getattr(settings, "voice_quiet_hours", "") or ""))
    if window is None:
        return ""
    return f"{window[1] // 60:02d}:{window[1] % 60:02d}"


def _quiet_hours_exempt(settings: Settings | Any, user_id: str) -> bool:
    raw = str(getattr(settings, "voice_quiet_hours_override_users", "") or "")
    allowed = {u.strip() for u in raw.split(",") if u.strip()}
    return bool(user_id) and user_id in allowed


# ── The gate ─────────────────────────────────────────────────────────


async def check_outbound_allowed(
    settings: Settings | Any,
    number: str,
    user_id: str = "",
    language: str = "en",
) -> OutboundDecision:
    """The single server-side decision on whether this call may be dialled.

    Enforced identically for every channel: the chat tool, the dashboard API,
    the appointment scheduler, and automatic retries all pass through here via
    `voice.outbound.make_phone_call`.
    """
    messages = _messages(language)
    key = normalize_number(number)

    if await is_do_not_call(settings, key):
        return OutboundDecision(
            allowed=False,
            reason=BlockReason.DO_NOT_CALL,
            message=messages[BlockReason.DO_NOT_CALL].format(number=key),
        )

    if in_quiet_hours(settings) and not _quiet_hours_exempt(settings, user_id):
        from pincer.voice.localtime import get_voice_timezone

        resume = quiet_hours_resume(settings)
        return OutboundDecision(
            allowed=False,
            reason=BlockReason.QUIET_HOURS,
            message=messages[BlockReason.QUIET_HOURS].format(
                number=key,
                window=str(getattr(settings, "voice_quiet_hours", "")),
                tz=str(get_voice_timezone(settings)),
                resume=resume,
            ),
        )

    daily_limit = int(getattr(settings, "voice_daily_call_limit", 0) or 0)
    if daily_limit > 0 and await calls_today(settings) >= daily_limit:
        return OutboundDecision(
            allowed=False,
            reason=BlockReason.DAILY_LIMIT,
            message=messages[BlockReason.DAILY_LIMIT].format(limit=daily_limit),
        )

    cooldown_min = int(getattr(settings, "voice_target_cooldown_min", 0) or 0)
    if cooldown_min > 0:
        recent = await _recent_target_calls(settings, key, cooldown_min)
        allowance = max_attempts_per_target(settings)
        if len(recent) >= allowance:
            elapsed = (datetime.now(UTC) - recent[0]).total_seconds() / 60.0
            retry_after = max(1, int(cooldown_min - elapsed) + 1)
            return OutboundDecision(
                allowed=False,
                reason=BlockReason.TARGET_COOLDOWN,
                message=messages[BlockReason.TARGET_COOLDOWN].format(
                    number=key, attempts=len(recent), window=cooldown_min, retry_after=retry_after
                ),
                retry_after_min=retry_after,
            )

    return OutboundDecision(allowed=True)
