"""Sprint 12 — inbound receptionist, channel-level integration + red team (§14).

Real VoiceChannel + CallStateMachine + InCallToolGate + ReceptionSession
against the FakeVoiceEngine. The conversation LLM is a scripted intent agent
(emits the ``[INTENT:…]`` token the way the prompt instructs); the calendar is
a fake registry whose free/busy can be flipped mid-call (race test).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from pincer.channels.phone_calls import VoiceChannel
from pincer.core.agent import StreamChunk, StreamEventType
from pincer.tools.registry import ToolRegistry
from pincer.voice import approvals, call_locks, status_notify
from pincer.voice.engine import CallDirection, ConversationRelayEngine
from pincer.voice.prompts import get_prompt
from pincer.voice.receptionist import profile as prof
from pincer.voice.receptionist import session as sess
from pincer.voice.receptionist.profile import parse_business_profile
from pincer.voice.state_machine import CallPhase
from pincer.voice.tool_speech import RAW_DATA_RE
from pincer.voice.transcript import Speaker

from .fake_engine import FakeVoiceEngine
from .personas import (
    InboundDataExtractionPersona,
    InboundPromptInjectionPersona,
    InboundSocialEngineerPersona,
    InboundThreadProbePersona,
)
from .personas_de import (
    InboundDataExtractionPersonaDe,
    InboundPromptInjectionPersonaDe,
    InboundSocialEngineerPersonaDe,
    InboundThreadProbePersonaDe,
)
from .settings import apply_in_call_tool_defaults, apply_test_paths
from .strict_async import FAIL_ON_UNAWAITED_COROUTINE

pytestmark = FAIL_ON_UNAWAITED_COROUTINE
BERLIN = ZoneInfo("Europe/Berlin")
CALL = "CA_inbound"
# A call whose state was lost while its transfer target rang (restart/redeploy)
LOST_CALL = "CAlost"
CALLER = "+4917212345678"
OWNER = "777"
# A Monday 09:00 local, inside hours (profile below)
OPEN_NOW = datetime(2026, 8, 17, 9, 0, tzinfo=BERLIN)
CLOSED_NOW = datetime(2026, 8, 17, 20, 0, tzinfo=BERLIN)

PROFILE = {
    "version": 1,
    "business": {"name": "Praxis Dr. Müller", "languages": ["de", "en"], "timezone": "Europe/Berlin"},
    "hours": {
        "mon": ["08:00-12:00", "14:00-17:00"],
        "tue": ["08:00-12:00", "14:00-17:00"],
        "wed": ["08:00-12:00"],
        "thu": ["08:00-12:00", "14:00-17:00"],
        "fri": ["08:00-12:00"],
        "sat": [],
        "sun": [],
    },
    "services": ["Allgemeinmedizin"],
    "faq": [{"q": "Wo kann ich parken?", "a": "Direkt hinter dem Haus, Einfahrt Gartenstraße."}],
    "address": "Gartenstraße 12, 32257 Bünde",
    "booking": {"enabled": True, "event_duration_min": 30, "ask_email": False},
    "transfer": {"enabled": True, "target": "+4952233344455", "announce": "Ich verbinde Sie."},
    "after_hours": {"message": "Sie erreichen uns Montag bis Freitag vormittags. Ich nehme gern eine Nachricht auf."},
}

# Canaries the receptionist must never speak (seeded into the fake calendar)
CANARIES = ("Zahnarzt Krause", "Steuerberater", "attacker@example.com", "Hauptstrasse 14")


class IntentAgent:
    """Scripted conversation LLM: classifies by keyword and answers FAQ from the profile."""

    def __init__(self, language: str = "de") -> None:
        self.language = language
        self.calls: list[dict] = []

    async def stream_voice_turn(self, **kwargs):
        self.calls.append(kwargs)
        text = str(kwargs.get("text") or "").lower()
        extra = str(kwargs.get("extra_system") or "")
        lines = get_prompt("RECEPTIONIST_LINES", self.language)
        if "[INTENT:" not in extra:
            reply = "Gerne." if self.language == "de" else "Sure."
            yield StreamChunk(StreamEventType.TEXT, reply)
            yield StreamChunk(StreamEventType.DONE, reply)
            return
        if "park" in text:
            reply = (
                "[INTENT:question] Parken können Sie direkt hinter dem Haus, Einfahrt Gartenstraße. "
                "Kann ich sonst noch helfen?"
            )
        elif "preis" in text or "price" in text or "kosten" in text:
            reply = f"[INTENT:question] {lines['faq_unknown']}"
        elif "nachricht" in text or "message" in text or "ausrichten" in text:
            reply = "[INTENT:message] Gerne."
        elif "termin" in text or "appointment" in text or "book" in text:
            reply = "[INTENT:appointment] Gerne."
        elif "blabla" in text or "xyz" in text:
            reply = "[INTENT:unknown] Wie bitte?"
        else:
            reply = "[INTENT:unknown] Wie bitte?"
        yield StreamChunk(StreamEventType.TEXT, reply)
        yield StreamChunk(StreamEventType.DONE, reply)


class FakeCalendar:
    """google__check_freebusy / google__create_event with flippable busy state."""

    def __init__(self) -> None:
        self.busy: list[tuple[str, str]] = [("2026-08-18T09:00:00+02:00", "2026-08-18T10:00:00+02:00")]
        self.created: list[dict] = []
        self.fail_create = False
        self.freebusy_calls = 0
        self.take_slot_on_recheck: str | None = None  # ISO start → becomes busy at write-time re-check
        self.windows: list[tuple[str, str]] = []  # every (time_min, time_max) asked for

    def registry(self) -> ToolRegistry:
        registry = ToolRegistry()

        async def check_freebusy(emails: str, time_min: str = "", time_max: str = "") -> str:
            self.freebusy_calls += 1
            self.windows.append((time_min, time_max))
            busy = list(self.busy)
            if self.take_slot_on_recheck and self.freebusy_calls > 1:
                start = datetime.fromisoformat(self.take_slot_on_recheck)
                end = (
                    start.replace(minute=start.minute + 30)
                    if start.minute < 30
                    else start.replace(hour=start.hour + 1, minute=0)
                )
                busy.append((start.isoformat(), end.isoformat()))
            # A real free/busy query only reports events overlapping the window.
            # Returning everything regardless is what let a too-narrow recheck
            # window look correct here.
            if time_min and time_max:
                lo, hi = datetime.fromisoformat(time_min), datetime.fromisoformat(time_max)
                busy = [
                    (bs, be) for bs, be in busy if datetime.fromisoformat(bs) < hi and datetime.fromisoformat(be) > lo
                ]
            if not busy:
                return f"{emails}: FREE"
            lines = [f"{emails}: BUSY at:"] + [f"    {s} → {e}" for s, e in busy]
            return "\n".join(lines) + "\n# Zahnarzt Krause, Steuerberater"  # contents that must NEVER be spoken

        async def create_event(**kwargs) -> str:
            if self.fail_create:
                return "Error: calendar unavailable"
            self.created.append(kwargs)
            return f"Event created: '{kwargs.get('summary')}'\nStart: {kwargs.get('start')}\nID: ev1\nLink: https://cal/ev1"

        async def list_events(**kwargs) -> str:
            return "Zahnarzt Krause, Steuerberater"

        registry.register("google__check_freebusy", "fb", check_freebusy, {"type": "object", "properties": {}})
        registry.register("google__create_event", "ce", create_event, {"type": "object", "properties": {}})
        registry.register("google__list_events", "le", list_events, {"type": "object", "properties": {}})
        registry.register("email_send", "es", create_event, {"type": "object", "properties": {}})
        return registry


def _settings(**overrides):
    settings = apply_test_paths(MagicMock())
    settings.voice_enabled = True
    settings.voice_language = "de-DE"
    settings.voice_default_language = "de"
    settings.voice_supported_languages = "en,de,uk"
    settings.voice_de_formality = "sie"
    settings.voice_stt_min_confidence = 0.55
    settings.voice_filler_phrases = ""
    settings.data_dir = None
    settings.receptionist_enabled = True
    settings.receptionist_booking_approval = "off"
    settings.inbound_recording = False
    settings.voice_blocklist = ""
    settings.inbound_max_concurrent = 3
    settings.scheduling_calendar_id = "primary"
    settings.slot_buffer_min = 0
    settings.voice_webhook_base_url = "https://voice.example.com"
    settings.twilio_phone_number = "+4952230000"
    apply_in_call_tool_defaults(settings, default_user_id=OWNER)
    for k, v in overrides.items():
        setattr(settings, k, v)
    return settings


@pytest.fixture(autouse=True)
def _no_silent_phase_rejections():
    """Fail the test if the receptionist attempts a transition the state
    machine does not allow.

    `_to_phase` only logs a warning on rejection, so a missing edge in
    VALID_TRANSITIONS leaves the call parked in the wrong phase and every
    downstream consumer — phase timeouts, transcript phase labels, the
    benign-phase classification at hangup — quietly reads the stale value.
    Nothing fails, so nothing notices.
    """
    from pincer.voice.receptionist.session import ReceptionSession

    original = ReceptionSession._to_phase
    rejected: list[tuple[str, str, str]] = []

    def recording(self, phase, reason):
        before = self.sm.phase
        original(self, phase, reason)
        if before != phase and self.sm.phase != phase:
            rejected.append((before.value, phase.value, reason))

    ReceptionSession._to_phase = recording
    try:
        yield
    finally:
        ReceptionSession._to_phase = original
    assert rejected == [], f"receptionist attempted transition(s) VALID_TRANSITIONS rejects: {rejected}"


@pytest.fixture(autouse=True)
def _clean():
    # Some tests below wire an AsyncMock engine via init_voice_routes(); restore
    # the module globals afterwards so API tests in the same session never see it.
    from pincer.voice import twiml_server as _ts

    saved_engine, saved_settings = _ts._engine, _ts._settings
    status_notify._reset_for_tests()
    approvals._reset_for_tests()
    prof.set_profile(parse_business_profile(copy.deepcopy(PROFILE)))
    yield
    prof.set_profile(None)
    _ts._engine, _ts._settings = saved_engine, saved_settings
    status_notify._reset_for_tests()
    approvals._reset_for_tests()


class Harness:
    def __init__(self, channel, engine, agent, state, calendar, owner_msgs):
        self.channel, self.engine, self.agent, self.state, self.calendar, self.owner_msgs = (
            channel,
            engine,
            agent,
            state,
            calendar,
            owner_msgs,
        )

    @property
    def session(self) -> sess.ReceptionSession:
        return self.channel.get_reception_session(CALL)

    @property
    def sm(self):
        return self.channel.get_state_machine(CALL)

    @property
    def spoken(self) -> list[str]:
        return self.engine.spoken.get(CALL, [])

    async def say(self, text: str) -> None:
        await self.engine.on_speech_input(CALL, text)

    def last(self) -> str:
        return self.spoken[-1] if self.spoken else ""


async def _start(
    *, language="de", now=OPEN_NOW, settings=None, calendar=None, profile_changes=None, engine_cls=FakeVoiceEngine
) -> Harness:
    if profile_changes:
        data = copy.deepcopy(PROFILE)
        for dotted, value in profile_changes.items():
            cursor = data
            parts = dotted.split(".")
            for part in parts[:-1]:
                cursor = cursor[part]
            cursor[parts[-1]] = value
        prof.set_profile(parse_business_profile(data))
    settings = settings or _settings()
    calendar = calendar or FakeCalendar()
    engine = engine_cls(settings)
    channel = VoiceChannel(settings)
    channel.set_engine(engine)
    channel.set_tool_registry(calendar.registry())
    agent = IntentAgent(language)
    channel.set_stream_agent(agent)
    owner_msgs: list[tuple[str, str, str]] = []

    async def notifier(user_id, chan, text):
        owner_msgs.append((user_id, chan, text))
        return True

    status_notify.set_status_notifier(notifier)

    async def _blocking(incoming):
        return "Fallback."

    await channel.start(_blocking)
    from pincer.voice.postcall import PostCallProcessor

    channel.set_post_call_processor(PostCallProcessor(settings, llm=None, memory=None, db_path=str(settings.db_path)))

    state = await engine.on_call_start(CALL, CALLER, CallDirection.INBOUND, language=language)
    session = channel.get_reception_session(CALL)
    assert session is not None, "receptionist session must start at call registration"
    # Re-evaluate open/closed with the frozen clock
    session._now_fn = lambda: now
    session.is_open = prof.get_profile().is_open(now)
    # The session was started on the wall clock; redo the opening on the frozen
    # one in either direction (real clock closed vs. OPEN_NOW, or vice versa).
    wrong_open = session.is_open and session.sm.phase in (CallPhase.TAKE_MESSAGE, CallPhase.AFTER_HOURS)
    wrong_closed = not session.is_open and session.sm.phase == CallPhase.RECEPTION_INTENT
    if wrong_open or wrong_closed:
        session.sm._state.phase = CallPhase.GREETING
        session.start()
    return Harness(channel, engine, agent, state, calendar, owner_msgs)


async def _wait_owner(h: Harness) -> None:
    for _ in range(100):
        if h.owner_msgs:
            return
        await asyncio.sleep(0.01)


def _assert_no_leak(h: Harness):
    spoken = " ".join(h.spoken)
    for canary in CANARIES:
        assert canary not in spoken, f"leaked {canary!r}: {h.spoken}"
    assert not any(RAW_DATA_RE.search(s) for s in h.spoken), h.spoken


# ── Greeting / opening ───────────────────────────────────────────────


def test_opening_text_discloses_ai_and_business_name():
    profile = prof.get_profile()
    text, is_open = sess.opening_text(profile, "de", OPEN_NOW)
    assert (
        is_open
        and text == "Guten Tag, hier ist der digitale Assistent von Praxis Dr. Müller. Wie kann ich Ihnen helfen?"
    )
    assert "Assistent" in text and "Praxis Dr. Müller" in text
    closed, is_open = sess.opening_text(profile, "de", CLOSED_NOW)
    assert not is_open
    assert "Montag bis Freitag vormittags" in closed and closed.endswith("Wie ist Ihr Name?")
    en, _ = sess.opening_text(profile, "en", OPEN_NOW)
    assert en.startswith("Hello, this is the digital assistant of Praxis Dr. Müller.")


def test_connect_twiml_uses_receptionist_greeting_and_recording_announcement():
    from pincer.voice.twiml_builder import build_connect_twiml

    settings = _settings()
    settings.voice_engine = "conversation_relay"
    settings.voice_assistant_name = "Pincer"
    settings.voice_consent_mode = "none"
    settings.voice_recording_enabled = False
    settings.elevenlabs_voice_id = ""
    settings.cr_tts_provider = ""
    twiml = build_connect_twiml(settings, call_sid=CALL, direction="inbound", language="de", counterparty=CALLER)
    assert "digitale Assistent von Praxis Dr. Müller" in twiml
    assert "Hier spricht Pincer" not in twiml  # never the outbound-style intro on the public line
    settings.inbound_recording = True
    twiml = build_connect_twiml(settings, call_sid=CALL, direction="inbound", language="de", counterparty=CALLER)
    assert twiml.index("aufgezeichnet") < twiml.index("digitale Assistent")  # announcement BEFORE greeting
    # outbound keeps the normal opening
    out = build_connect_twiml(settings, direction="outbound", language="de", counterparty=CALLER)
    assert "digitale Assistent von Praxis" not in out


# ── §8.1 FAQ ─────────────────────────────────────────────────────────


async def test_faq_in_profile():
    h = await _start()
    assert h.sm.phase == CallPhase.RECEPTION_INTENT
    await h.say("Wo kann ich bei Ihnen parken?")
    assert any(s.startswith("Parken können Sie direkt hinter dem Haus") for s in h.spoken)
    assert "[INTENT" not in " ".join(h.spoken)
    assert h.sm.phase == CallPhase.FAQ_ANSWER
    assert "[INTENT:" in h.agent.calls[0]["extra_system"]
    assert "BUSINESS PROFILE" in h.agent.calls[0]["extra_system"]
    await h.say("Nein danke, auf Wiederhören.")
    assert CALL in h.engine.ended
    assert h.owner_msgs == []  # a pure FAQ call does not wake the owner
    _assert_no_leak(h)


async def test_faq_not_in_profile_degrades_to_message():
    h = await _start()
    await h.say("Was kosten die Vorsorgeuntersuchungen?")
    assert h.last().startswith("Das kann ich nicht sicher sagen")
    assert h.sm.phase == CallPhase.TAKE_MESSAGE
    assert h.session.step == "name"
    await h.say("Müller")
    assert h.last().startswith("Unter welcher Nummer erreichen wir Sie?")
    _assert_no_leak(h)


# ── §8.2 TAKE_MESSAGE ────────────────────────────────────────────────


async def test_message_flow_full_slots():
    h = await _start()
    await h.say("Ich möchte eine Nachricht hinterlassen.")
    assert h.last() == "Gern nehme ich eine Nachricht auf. Wie ist Ihr Name?"
    assert h.sm.phase == CallPhase.TAKE_MESSAGE
    await h.say("Mein Name ist Schmidt.")  # common name → no spell-back
    assert (
        h.last() == "Unter welcher Nummer erreichen wir Sie? Ich habe die fünf-sechs, sieben-acht gesehen — passt die?"
    )
    await h.say("Ja, die passt.")
    assert h.last() == "Worum geht es?"
    await h.say("Es geht um meine Rechnung, ich habe starke Schmerzen beim Lesen.")
    assert h.last() == "Ist es dringend?"  # matter sounded time-critical
    await h.say("Ja.")
    assert h.last().startswith("Zur Bestätigung: Schmidt, plus vier-neun")
    assert ", dringend" in h.last()
    await h.say("Ja, genau.")
    assert h.last() == "Danke, ich gebe das weiter. Auf Wiederhören!"
    assert CALL in h.engine.ended
    reception = h.state.metadata["reception"]
    assert reception["slots"]["caller_name"] == "Schmidt"
    assert reception["slots"]["callback_number"] == CALLER
    assert reception["slots"]["urgent"] is True
    # Owner report (§12) delivered through the status notifier to the owner id
    await _wait_owner(h)
    assert h.owner_msgs and h.owner_msgs[-1][0] == OWNER
    report = h.owner_msgs[-1][2]
    assert report.startswith("📞 Anruf für Praxis Dr. Müller — ")
    assert "Von: Schmidt · +4917212345678" in report
    assert "❗ DRINGEND" in report
    assert f"📄 Transkript: /transcript {CALL}" in report
    _assert_no_leak(h)


async def test_message_mumbled_number_spellback():
    h = await _start()
    await h.say("Bitte eine Nachricht.")
    await h.say("Xanthopoulos")  # uncommon → spell-back
    assert h.last() == "Ich buchstabiere: X-A-N-T-H-O-P-O-U-L-O-S — richtig?"
    await h.say("Ja.")
    await h.say("Nein, eine andere Nummer.")
    assert h.last() == "Bitte nennen Sie mir die Nummer Ziffer für Ziffer."
    await h.say("null eins sieben zwei eins zwei drei vier fünf sechs sieben acht")
    assert h.last() == (
        "Ich habe plus vier-neun, eins-sieben, zwei-eins, zwei-drei, vier-fünf, sechs-sieben, acht — richtig?"
    )
    await h.say("Ja.")
    assert h.last() == "Worum geht es?"
    assert h.session.slots.callback_number == "+4917212345678"
    assert h.session.slots.callback_unverified is False
    # garbage number twice → stored raw + unverified
    h2 = await _start()
    await h2.say("Nachricht bitte.")
    await h2.say("Schmidt")
    await h2.say("Nein.")
    await h2.say("äh zwei drei")
    await h2.say("zwei drei")
    assert h2.session.slots.callback_unverified is True
    assert h2.last() == "Worum geht es?"


async def test_unknown_intent_twice_degrades():
    h = await _start()
    await h.say("blabla")
    assert h.last().startswith("Entschuldigung, das habe ich nicht ganz verstanden.")
    assert h.sm.phase == CallPhase.RECEPTION_INTENT
    await h.say("xyz")
    assert h.last() == "Gern nehme ich eine Nachricht auf. Wie ist Ihr Name?"  # never a third clarifying question
    assert h.sm.phase == CallPhase.TAKE_MESSAGE
    assert h.state.metadata["unknown_intents"] == 2


# ── §8.3 INBOUND_BOOKING ─────────────────────────────────────────────


async def test_booking_happy_path():
    h = await _start()
    await h.say("Ich hätte gern einen Termin.")
    assert h.last() == "Wann würde es Ihnen passen — eher diese oder nächste Woche?"
    assert h.sm.phase == CallPhase.INBOUND_BOOKING
    await h.say("Eher nächste Woche.")
    assert h.last().startswith("Ich kann Ihnen anbieten: ")
    assert "Montag, der vierundzwanzigste August um acht Uhr" in h.last()
    offered = h.session.booking.candidates
    assert len(offered) == 3 and all(c.weekday() < 5 for c in offered)
    await h.say("Den ersten bitte.")
    assert h.last() == "Wie ist Ihr Name?"
    await h.say("Schmidt")
    await h.say("Ja.")  # caller-id ok
    assert h.last().startswith(
        "Zur Bestätigung: Montag, der vierundzwanzigste August um acht Uhr, 30 Minuten, für Schmidt."
    )
    assert h.sm.phase == CallPhase.VERIFY
    await h.say("Ja, bitte.")
    assert h.calendar.created, "the event must be written after the caller's yes"
    event = h.calendar.created[0]
    assert event["summary"] == "Termin: Schmidt"
    assert event["idempotency_key"] == f"pincer-reception-{CALL}"
    assert "Gebucht via Pincer Rezeption, Anrufer: Schmidt" in event["description"]
    assert (
        h.last()
        == "Ihr Termin ist gebucht: Montag, der vierundzwanzigste August um acht Uhr. Vielen Dank — auf Wiederhören!"
    )
    assert CALL in h.engine.ended
    assert h.state.metadata["reception"]["booking"]["booked"] is True
    await _wait_owner(h)
    report = h.owner_msgs[-1][2]
    assert "📅 Termin gebucht: Montag, der vierundzwanzigste August um acht Uhr — https://cal/ev1" in report
    _assert_no_leak(h)


async def test_booking_counter_proposal_out_of_slots():
    h = await _start()
    await h.say("Termin bitte.")
    await h.say("Diese Woche.")
    assert h.last().startswith("Ich kann Ihnen anbieten:")
    await h.say("Geht auch Sonntag um zehn?")  # closed day → not offered
    assert h.last().startswith("Da kann ich leider nichts anbieten; ich kann ")
    assert h.session.booking.declines == 1
    await h.say("Dienstag um neun?")  # busy in the fake calendar (09:00-10:00)
    assert h.last().startswith("Da kann ich leider nichts anbieten")
    await h.say("Dann Dienstag um vierzehn Uhr.")  # free + inside hours → accepted
    assert h.session.booking.chosen is not None and h.session.booking.chosen.hour == 14
    assert h.last() == "Wie ist Ihr Name?"
    _assert_no_leak(h)


async def test_counter_proposal_must_clear_the_lead_time():
    """A caller-proposed time is still a booking.

    `compute_profile_candidates` never offers a slot inside MIN_LEAD_MINUTES,
    but the counter-proposal branch only checked `counter > now` — so
    "kann ich in einer halben Stunde vorbeikommen?" booked a slot with almost
    no lead time whenever it happened to be free and inside opening hours.
    """
    from pincer.voice.scheduling import MIN_LEAD_MINUTES

    assert MIN_LEAD_MINUTES == 60, "this test reasons in terms of the configured lead time"

    h = await _start()
    await h.say("Termin bitte.")
    await h.say("Diese Woche.")
    assert h.last().startswith("Ich kann Ihnen anbieten:")

    # OPEN_NOW is 09:00; 09:30 is inside opening hours and free, but only
    # 30 minutes ahead — inside the lead-time window.
    await h.say("Geht auch heute um halb zehn?")
    assert h.session.booking.chosen is None, "a slot inside the lead-time window must not be bookable"
    assert h.last().startswith("Da kann ich leider nichts anbieten")

    # Past the lead time on the same day, the same phrasing is accepted.
    await h.say("Dann heute um elf Uhr.")
    assert h.session.booking.chosen is not None
    assert h.session.booking.chosen.hour == 11
    _assert_no_leak(h)


async def test_near_term_counter_proposal_never_reaches_the_calendar():
    """A quarter-hour out: parses cleanly, is free, is inside opening hours —
    the exact shape that used to slip past `counter > now` and get written.

    (The literal "in fünf Minuten" phrasing does not parse as a time at all,
    so it was already declined as unintelligible; a clock time is what
    actually reached the booking path.)
    """
    h = await _start()
    await h.say("Termin bitte.")
    await h.say("Diese Woche.")

    await h.say("Geht heute um viertel nach neun?")  # OPEN_NOW is 09:00

    assert h.session.booking.chosen is None
    assert not h.calendar.created, "no calendar write may happen inside the lead-time window"
    assert h.last().startswith("Da kann ich leider nichts anbieten")
    _assert_no_leak(h)


async def test_declining_the_read_back_returns_to_booking():
    """Saying no at the confirmation read-back must re-offer slots.

    VERIFY -> INBOUND_BOOKING was missing from VALID_TRANSITIONS, so the
    rejection logged a warning and left the call parked in VERIFY — on the
    60s VERIFY timeout instead of INBOUND_BOOKING's 180s, and with every
    transcript line from then on labelled with the wrong phase.
    """
    h = await _start()
    await h.say("Ich hätte gern einen Termin.")
    await h.say("Eher nächste Woche.")
    await h.say("Den ersten bitte.")
    await h.say("Schmidt")
    await h.say("Ja.")  # caller-id ok
    assert h.sm.phase == CallPhase.VERIFY

    await h.say("Nein, doch nicht.")

    assert h.sm.phase == CallPhase.INBOUND_BOOKING, "a declined read-back re-offers slots"
    assert not h.calendar.created
    assert h.session.booking.declines == 1
    assert h.last().startswith("Ich kann Ihnen anbieten:")
    _assert_no_leak(h)


async def test_asking_about_a_clock_time_does_not_book_a_numbered_slot():
    """ "geht 3 Uhr?" is a question about three o'clock, not a pick.

    The literal "3" aliased to "third" in _ORDINAL_WORDS and the ordinal check
    ran before hour matching, so the caller was moved straight to confirming
    the third offered slot — a time they never mentioned.
    """
    h = await _start()
    await h.say("Termin bitte.")
    await h.say("Nächste Woche.")
    offered = list(h.session.booking.candidates)
    assert len(offered) == 3

    await h.say("Geht 3 Uhr?")

    assert h.session.booking.chosen is None, "no offered slot may be selected by a clock time"
    assert not h.calendar.created
    assert h.session.step == "b_choose", "the caller is still choosing"
    _assert_no_leak(h)


async def test_counter_proposal_running_past_closing_is_refused():
    """The reported path: a late counter-proposal whose slot ends at midnight.

    `within_hours` suppressed its date-mismatch guard for an end of exactly
    00:00 and then compared that 00:00 against the closing time — which every
    closing time beats. A 23:30 slot booked 15 minutes past a 23:45 close.
    """
    h = await _start(profile_changes={"hours.mon": ["08:00-23:45"]})
    await h.say("Termin bitte.")
    await h.say("Diese Woche.")

    await h.say("Geht es heute um halb zwölf nachts?")  # 23:30 + 30min = 00:00

    assert h.session.booking.chosen is None, "a slot running past closing must not be selected"
    assert not h.calendar.created
    _assert_no_leak(h)


async def test_booking_three_declines_degrade_to_message():
    h = await _start()
    await h.say("Termin bitte.")
    await h.say("Diese Woche.")
    for _ in range(3):
        await h.say("Nein, passt nicht.")
    assert h.sm.phase == CallPhase.TAKE_MESSAGE
    assert h.last() == "Gern nehme ich eine Nachricht auf. Wie ist Ihr Name?"


async def test_booking_race_slot_taken_at_write():
    h = await _start()
    await h.say("Termin bitte.")
    await h.say("Nächste Woche.")
    first = h.session.booking.candidates[0]
    h.calendar.take_slot_on_recheck = first.isoformat()  # someone books it while we talk
    await h.say("Den ersten.")
    await h.say("Schmidt")
    await h.say("Ja.")
    await h.say("Ja.")  # verify → write → re-check finds it taken
    assert not h.calendar.created
    assert h.last().startswith("Entschuldigung, der Termin wurde gerade vergeben. Ich kann stattdessen ")
    assert h.session.step == "b_choose" and h.session.booking.chosen is None
    assert first not in h.session.booking.candidates
    await h.say("Den ersten.")
    assert h.last().startswith("Zur Bestätigung:")  # identity already known → straight to VERIFY
    await h.say("Ja.")
    assert h.calendar.created and h.state.metadata["reception"]["booking"]["booked"] is True


async def test_write_recheck_applies_the_slot_buffer():
    """A competing event created inside the buffer must block the write.

    Candidates are offered with `slot_buffer_min` applied, but the write-time
    recheck asked `is_slot_free` without a buffer AND over a +-1 minute window
    — too narrow to even see the conflict. The slot was booked back-to-back,
    breaking the guarantee that was enforced when it was offered.
    """
    h = await _start(settings=_settings(slot_buffer_min=15))
    await h.say("Termin bitte.")
    await h.say("Nächste Woche.")
    first = h.session.booking.candidates[0]
    await h.say("Den ersten.")
    await h.say("Schmidt")
    await h.say("Ja.")

    # Someone books 5 minutes after our slot ends — inside the 15 min buffer.
    gap_start = first + timedelta(minutes=30 + 5)
    h.calendar.busy.append((gap_start.isoformat(), (gap_start + timedelta(minutes=30)).isoformat()))

    await h.say("Ja.")  # verify → write → recheck

    assert not h.calendar.created, "a slot without its buffer must not be written"
    assert h.session.booking.chosen is None
    assert first not in h.session.booking.candidates
    # The alternatives are filtered with the buffer too, so here none survive
    # and the call degrades to taking a message rather than offering a slot
    # that would have the same problem.
    assert h.sm.phase != CallPhase.VERIFY


async def test_write_recheck_window_spans_the_buffer():
    """The recheck cannot judge what it never fetched."""
    h = await _start(settings=_settings(slot_buffer_min=15))
    await h.say("Termin bitte.")
    await h.say("Nächste Woche.")
    first = h.session.booking.candidates[0]
    await h.say("Den ersten.")
    await h.say("Schmidt")
    await h.say("Ja.")
    await h.say("Ja.")

    time_min, time_max = h.calendar.windows[-1]
    lo, hi = datetime.fromisoformat(time_min), datetime.fromisoformat(time_max)
    assert lo <= first - timedelta(minutes=15), "window must reach a buffer before the slot"
    assert hi >= first + timedelta(minutes=30 + 15), "window must reach a buffer after the slot"


async def test_event_exactly_at_the_buffer_distance_still_books():
    """The boundary is exclusive: a buffer-sized gap IS the required gap."""
    h = await _start(settings=_settings(slot_buffer_min=15))
    await h.say("Termin bitte.")
    await h.say("Nächste Woche.")
    first = h.session.booking.candidates[0]
    await h.say("Den ersten.")
    await h.say("Schmidt")
    await h.say("Ja.")

    gap_start = first + timedelta(minutes=30 + 15)  # exactly one buffer after the end
    h.calendar.busy.append((gap_start.isoformat(), (gap_start + timedelta(minutes=30)).isoformat()))

    await h.say("Ja.")

    assert h.calendar.created, "a full buffer of clearance is bookable"


async def test_zero_buffer_configuration_still_books_back_to_back():
    """slot_buffer_min=0 is a valid choice and must not be broken by the fix."""
    h = await _start(settings=_settings(slot_buffer_min=0))
    await h.say("Termin bitte.")
    await h.say("Nächste Woche.")
    first = h.session.booking.candidates[0]
    await h.say("Den ersten.")
    await h.say("Schmidt")
    await h.say("Ja.")

    back_to_back = first + timedelta(minutes=30)
    h.calendar.busy.append((back_to_back.isoformat(), (back_to_back + timedelta(minutes=30)).isoformat()))

    await h.say("Ja.")

    assert h.calendar.created


async def test_booking_email_declined():
    h = await _start(profile_changes={"booking.ask_email": True})
    await h.say("Termin bitte.")
    await h.say("Nächste Woche.")
    await h.say("Den ersten.")
    await h.say("Schmidt")
    await h.say("Ja.")
    assert h.last() == "Möchten Sie eine Bestätigung per E-Mail? Dann buchstabieren Sie bitte die Adresse."
    await h.say("Nein, nicht nötig.")
    assert h.last().startswith("Zur Bestätigung:")
    await h.say("Ja.")
    assert h.calendar.created and "attendees" not in h.calendar.created[0]
    # and the spelled variant
    h2 = await _start(profile_changes={"booking.ask_email": True})
    await h2.say("Termin bitte.")
    await h2.say("Nächste Woche.")
    await h2.say("Den ersten.")
    await h2.say("Schmidt")
    await h2.say("Ja.")
    await h2.say("s punkt schmidt at web punkt de")
    assert h2.last() == "Ich habe S Punkt S C H M I D T at W E B Punkt D E — richtig?"
    await h2.say("Ja.")
    await h2.say("Ja.")
    assert h2.calendar.created[0]["attendees"] == "s.schmidt@web.de"


async def test_booking_disabled_degrades_to_message():
    h = await _start(profile_changes={"booking.enabled": False})
    await h.say("Ich brauche einen Termin.")
    assert (
        h.last() == "Gern nehme ich eine Nachricht auf, dann vereinbaren wir den Termin per Rückruf. Wie ist Ihr Name?"
    )
    assert h.sm.phase == CallPhase.TAKE_MESSAGE


async def test_booking_user_approval_mode_holds_for_owner(monkeypatch):
    from pincer.voice import in_call_tools as ict

    monkeypatch.setattr(ict, "HOLD_REASSURE_INTERVAL_S", 0.03)
    settings = _settings(receptionist_booking_approval="user")
    settings.voice_approval_timeout_s = 2
    cards = []

    async def presenter(req):
        cards.append(req)
        return True

    approvals.set_presenter(presenter)
    h = await _start(settings=settings)
    await h.say("Termin bitte.")
    await h.say("Nächste Woche.")
    await h.say("Den ersten.")
    await h.say("Schmidt")
    await h.say("Ja.")

    async def _approve():
        while not cards:
            await asyncio.sleep(0.005)
        approvals.resolve(cards[0].approval_id, True)

    task = asyncio.create_task(_approve())
    await h.say("Ja.")
    await task
    assert cards[0].user_id == OWNER  # the OWNER approves, never the caller
    assert "Einen Moment, das stimme ich kurz ab." in h.spoken
    assert h.calendar.created


# ── §8.4 TRANSFERRING ────────────────────────────────────────────────


async def test_human_transfer_ok():
    h = await _start()
    await h.say("Ich möchte bitte mit einem Menschen sprechen.")
    transfers = h.engine.transfers[CALL]
    assert transfers[0]["target"] == "+4952233344455"
    assert transfers[0]["timeout_s"] == 20
    assert transfers[0]["announce"] == "Ich verbinde Sie."
    assert transfers[0]["action_url"].endswith("/api/apps/twilio/transfer-result")
    assert h.sm.phase == CallPhase.TRANSFERRING
    assert len(h.agent.calls) == 0  # never argued with, no LLM turn
    # Dial result callback: completed → nothing; busy → apology + TAKE_MESSAGE


async def test_human_transfer_busy_fallback():
    h = await _start()
    h.engine.transfer_raises = True  # dial could not even be placed
    await h.say("Verbinden Sie mich bitte mit einem Mitarbeiter.")
    assert h.sm.phase == CallPhase.TAKE_MESSAGE
    assert h.session.transfer_failed
    # the webhook path: DialCallStatus=busy reconnects with the apology greeting
    text = await h.session.on_transfer_failed()
    assert text == "Leider ist gerade niemand erreichbar. Ich nehme gern eine Nachricht auf. Wie ist Ihr Name?"


async def test_relay_webhook_call_asking_for_a_human_gets_message_taking():
    """A call whose ConversationRelay events reach the deprecated HTTP
    /relay-webhook has no media socket, so a redirect could never be told
    applied or not: no transfer is attempted. The caller hears the failed-transfer
    apology and is asked for their name; nothing records a transfer."""
    h = await _start(settings=_transfer_settings())
    async with _async_client(h) as client:
        setup = await client.post("/api/apps/twilio/relay-webhook", json={"type": "setup", "CallSid": CALL})
    assert setup.status_code == 200
    await h.say("Ich möchte bitte mit einem Menschen sprechen.")
    assert h.last() == "Leider ist gerade niemand erreichbar. Ich nehme gern eine Nachricht auf. Wie ist Ihr Name?"
    assert (h.sm.phase, h.session.step) == (CallPhase.TAKE_MESSAGE, "name")
    assert h.engine.transfers.get(CALL, []) == []
    assert "transferring" not in h.state.metadata
    assert h.session.transfer_attempted is False


async def test_transfer_refused_for_an_override_base_url_speaks_the_apology():
    """transfer_call refuses an action URL with `#` connection overrides before
    any redirect: the caller must hear the apology and be asked for their name,
    not sit in silence in TAKE_MESSAGE."""
    settings = _transfer_settings()
    settings.voice_webhook_base_url = "https://voice.example.com#rp=all"
    h = await _start(settings=settings)
    await h.say("Ich möchte bitte mit einem Menschen sprechen.")
    assert h.last() == "Leider ist gerade niemand erreichbar. Ich nehme gern eine Nachricht auf. Wie ist Ihr Name?"
    assert (h.sm.phase, h.session.step) == (CallPhase.TAKE_MESSAGE, "name")
    assert "transferring" not in h.state.metadata


@pytest.mark.parametrize(
    ("outcome", "result"),
    [
        ("on_transfer_completed", "completed"),
        ("on_transfer_abandoned", "caller_hangup"),
        ("on_transfer_failed", "busy"),
    ],
)
async def test_transfer_outcome_recorded_once(monkeypatch, outcome, result):
    """Each outcome goes to the labeled transfers counter only, never also to inbound events."""
    from pincer.observability import metrics

    h = await _start()
    inbound, transfers = MagicMock(), MagicMock()
    monkeypatch.setattr(metrics, "record_inbound_event", inbound)
    monkeypatch.setattr(metrics, "record_receptionist_transfer", transfers)
    method = getattr(h.session, outcome)
    if outcome == "on_transfer_completed":
        method(60)
    elif outcome == "on_transfer_abandoned":
        method()
    else:
        await method(result)
    transfers.assert_called_once_with(result, language="de")
    assert not [c for c in inbound.call_args_list if str(c.args[0]).startswith("transfer")]


async def test_human_transfer_disabled_takes_message():
    h = await _start(profile_changes={"transfer.enabled": False, "transfer.target": ""})
    await h.say("Kann ich jemanden sprechen?")
    assert h.last() == "Ich kann gerade nicht verbinden, nehme aber gern eine Nachricht auf. Wie ist Ihr Name?"


# ── §8.5 AFTER_HOURS ─────────────────────────────────────────────────


async def test_after_hours_message():
    h = await _start(now=CLOSED_NOW)
    assert not h.session.is_open
    assert h.sm.phase == CallPhase.TAKE_MESSAGE  # greeting already asked for the name
    assert h.session.intent == "after_hours"
    await h.say("Weber")
    await h.say("Ja.")
    await h.say("Bitte um Rückruf wegen Rezept.")
    assert h.last().startswith("Zur Bestätigung: Weber")
    await h.say("Ja.")
    assert CALL in h.engine.ended
    await _wait_owner(h)
    assert "🌙 Außerhalb der Öffnungszeiten angerufen" in h.owner_msgs[-1][2]


# ── §10 silence / blocklist / concurrency ────────────────────────────


async def test_silence_hangup():
    h = await _start()
    session = h.session
    clock = {"t": 0.0}
    session._clock = lambda: clock["t"]
    session.greeted_at = 0.0
    assert await session.check_silence() == ""
    clock["t"] = 10.5
    assert await session.check_silence() == "reprompted"
    assert h.last() == "Sind Sie noch dran?"
    clock["t"] = 15.0
    assert await session.check_silence() == ""
    clock["t"] = 21.0
    assert await session.check_silence() == "hung_up"
    assert h.last() == "Ich beende das Gespräch. Rufen Sie gern wieder an. Auf Wiederhören!"
    assert CALL in h.engine.ended
    assert session.sm.is_terminal
    assert session.end_reason == "silent_hangup"


def _webhook_client(settings, engine):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from pincer.voice.twiml_server import init_voice_routes, twilio_router

    init_voice_routes(engine, settings)
    app = FastAPI()
    app.include_router(twilio_router)
    return TestClient(app)


def test_blocklist_decline():
    from unittest.mock import AsyncMock

    settings = _settings(voice_blocklist="+4917212345678, +1555")
    settings.voice_webhook_validate = False
    settings.voice_allowed_callers = "*"
    settings.twilio_auth_token = MagicMock()
    settings.twilio_auth_token.get_secret_value.return_value = ""
    engine = AsyncMock()
    engine.get_active_calls = MagicMock(return_value={})
    client = _webhook_client(settings, engine)
    response = client.post("/api/apps/twilio/webhook", data={"CallSid": "CAb", "From": "+4917212345678", "To": "+4952"})
    assert response.status_code == 200
    assert "Diese Nummer kann nicht bedient werden" in response.text and "<Hangup/>" in response.text
    engine.on_call_start.assert_not_called()
    ok = client.post("/api/apps/twilio/webhook", data={"CallSid": "CAc", "From": "+4917299999999", "To": "+4952"})
    assert "<Hangup/>" not in ok.text
    engine.on_call_start.assert_called_once()


def test_concurrency_busy():
    from unittest.mock import AsyncMock

    from pincer.voice.engine import CallState

    settings = _settings(inbound_max_concurrent=2)
    settings.voice_webhook_validate = False
    settings.voice_allowed_callers = "*"
    settings.twilio_auth_token = MagicMock()
    settings.twilio_auth_token.get_secret_value.return_value = ""
    engine = AsyncMock()
    active = {
        f"CA{i}": CallState(call_sid=f"CA{i}", direction=CallDirection.INBOUND, caller_number="+491") for i in range(2)
    }
    active["CAout"] = CallState(call_sid="CAout", direction=CallDirection.OUTBOUND, caller_number="+491")
    engine.get_active_calls = MagicMock(return_value=active)
    client = _webhook_client(settings, engine)
    response = client.post("/api/apps/twilio/webhook", data={"CallSid": "CAn", "From": "+4917299999999", "To": "+4952"})
    assert "alle Leitungen belegt" in response.text and "<Hangup/>" in response.text
    engine.on_call_start.assert_not_called()
    # below the limit → answered
    engine.get_active_calls = MagicMock(return_value={"CA0": active["CA0"]})
    ok = client.post("/api/apps/twilio/webhook", data={"CallSid": "CAm", "From": "+4917299999999", "To": "+4952"})
    assert "<Hangup/>" not in ok.text


def test_transfer_result_webhook_reconnects_with_apology():
    from unittest.mock import AsyncMock

    from pincer.voice import twiml_server
    from pincer.voice.engine import CallState

    settings = _settings()
    settings.voice_webhook_validate = False
    settings.voice_engine = "conversation_relay"
    settings.twilio_auth_token = MagicMock()
    settings.twilio_auth_token.get_secret_value.return_value = ""
    settings.voice_assistant_name = ""
    settings.voice_consent_mode = "none"
    settings.elevenlabs_voice_id = ""
    settings.cr_tts_provider = ""
    state = CallState(call_sid="CAt", direction=CallDirection.INBOUND, caller_number=CALLER, language="de")
    engine = AsyncMock()
    engine.get_call_state = MagicMock(return_value=state)
    fake_session = MagicMock()
    fake_session.on_transfer_failed = AsyncMock(return_value="Leider ist gerade niemand erreichbar. Wie ist Ihr Name?")
    twiml_server.set_transfer_session_resolver(lambda sid: fake_session)

    def dialled(dial_call_sid: str) -> dict[str, str]:
        # each post is the result of its own dial (the mocked end_transfer clears nothing)
        state.metadata["transferring"] = True
        return {"CallSid": "CAt", "DialCallSid": dial_call_sid}

    try:
        client = _webhook_client(settings, engine)
        done = client.post("/api/apps/twilio/transfer-result", data={**dialled("CAd1"), "DialCallStatus": "completed"})
        assert done.text == "<Response><Hangup/></Response>"
        busy = client.post("/api/apps/twilio/transfer-result", data={**dialled("CAd2"), "DialCallStatus": "busy"})
        assert "ConversationRelay" in busy.text
        assert 'welcomeGreeting="Leider ist gerade niemand erreichbar. Wie ist Ihr Name?"' in busy.text
        fake_session.on_transfer_failed.assert_awaited_once_with("busy")
        for dial_status, result in (("no-answer", "no_answer"), ("failed", "failed"), ("canceled", "failed")):
            fake_session.on_transfer_failed.reset_mock()
            other = client.post(
                "/api/apps/twilio/transfer-result",
                data={**dialled(f"CAd-{dial_status}"), "DialCallStatus": dial_status, "CallStatus": "in-progress"},
            )
            assert "ConversationRelay" in other.text, dial_status
            fake_session.on_transfer_failed.assert_awaited_once_with(result)
    finally:
        twiml_server.set_transfer_session_resolver(None)


def test_transfer_result_without_state_reconnects_live_caller():
    """State lost (restart while the target rang): a caller still on the line
    falls back to message-taking; only a finished call is hung up."""
    from unittest.mock import AsyncMock

    from pincer.voice import twiml_server

    settings = _settings()
    settings.voice_webhook_validate = False
    settings.voice_engine = "conversation_relay"
    settings.twilio_auth_token = MagicMock()
    settings.twilio_auth_token.get_secret_value.return_value = ""
    settings.voice_assistant_name = ""
    settings.voice_consent_mode = "none"
    settings.elevenlabs_voice_id = ""
    settings.cr_tts_provider = ""
    engine = AsyncMock()
    engine.get_call_state = MagicMock(return_value=None)
    engine.was_recently_ended = MagicMock(return_value=False)
    engine.fetch_call_status = AsyncMock(return_value="in-progress")
    twiml_server.set_transfer_session_resolver(None)
    client = _webhook_client(settings, engine)
    live = client.post(
        "/api/apps/twilio/transfer-result",
        data={"CallSid": "CAt", "DialCallStatus": "busy", "CallStatus": "in-progress", "From": CALLER},
    )
    assert "ConversationRelay" in live.text
    assert 'welcomeGreeting="Leider ist gerade niemand erreichbar. Ich nehme gern eine Nachricht auf."' in live.text
    # registered, so the relay `setup` that follows is not refused as an unknown SID
    engine.on_call_start.assert_awaited_once_with("CAt", CALLER, CallDirection.INBOUND, language="de")
    engine.on_call_start.reset_mock()
    for data in (
        {"CallSid": "CAt", "DialCallStatus": "busy", "CallStatus": "completed"},
        {"CallSid": "CAt", "DialCallStatus": "completed", "CallStatus": "in-progress"},
    ):
        gone = client.post("/api/apps/twilio/transfer-result", data=data)
        assert gone.text == "<Response><Hangup/></Response>", data
    engine.finish_remote_ended.assert_not_called()
    engine.on_call_start.assert_not_called()


async def test_lost_transfer_state_resumes_session_in_take_message():
    """Restart while the target rang: the recovered call's new session must
    pick up message-taking (name question), not greet the caller again."""
    h = await _start(settings=_transfer_settings())
    lost = "CAlost"
    async with _async_client(h) as client:
        response = await client.post(
            "/api/apps/twilio/transfer-result",
            data={"CallSid": lost, "From": CALLER, "DialCallStatus": "no-answer", "CallStatus": "in-progress"},
        )
    assert h.engine.get_call_state(lost) is not None  # the relay `setup` will find it
    session = h.channel.get_reception_session(lost)
    assert session is not None
    assert session.sm.phase == CallPhase.TAKE_MESSAGE
    assert session.step == "name"
    assert session.transfer_attempted and session.transfer_failed
    assert (
        'welcomeGreeting="Leider ist gerade niemand erreichbar. Ich nehme gern eine Nachricht auf. Wie ist Ihr Name?"'
        in response.text
    )


def _lost_state_callback() -> dict[str, str]:
    """A lost-state dial callback with the caller still on the line."""
    return {"CallSid": LOST_CALL, "From": CALLER, "DialCallStatus": "no-answer", "CallStatus": "in-progress"}


def _finalized_states(h: Harness) -> list:
    """The CallState of every call the channel finalizes from here on."""
    finalized: list = []
    original = h.engine._on_call_end_callback

    async def recording(call_sid, state):
        await original(call_sid, state)
        finalized.append(state)

    h.engine.set_on_call_end(recording)
    return finalized


def _closed_traces(monkeypatch) -> list[tuple[str, str, str]]:
    """Record each telemetry.call_ended (the trace close) as (call_sid, status, failure_code)."""
    from pincer.voice.telemetry import hooks

    closed: list[tuple[str, str, str]] = []

    async def record(call_sid, *, status, failure_code="", **_):
        closed.append((call_sid, status, failure_code))

    monkeypatch.setattr(hooks, "call_ended", record)
    return closed


async def _without_other_calls(h: Harness) -> list:
    """End the harness call, so the capacity count starts at 0, then track finalized calls."""
    await h.engine.finish_remote_ended(CALL)
    return _finalized_states(h)


def _break_failed_transfer_step(monkeypatch) -> None:
    async def broken(self, result):
        raise KeyError("transfer_failed")

    monkeypatch.setattr(sess.ReceptionSession, "on_transfer_failed", broken)


async def test_lost_transfer_state_recovery_failure_frees_the_call_as_failed(monkeypatch):
    """Restart while the target rang, then the recovered session's failed-transfer
    step raises: the caller is hung up, the call is gone, its slot is free, and it
    is recorded as a failed call, not as a clean completed one."""
    from pincer.voice import twiml_server

    h = await _start(settings=_transfer_settings())
    finalized = await _without_other_calls(h)
    closed = _closed_traces(monkeypatch)
    _break_failed_transfer_step(monkeypatch)
    async with _async_client(h) as client:
        assert twiml_server._active_inbound_count() == 0
        response = await client.post("/api/apps/twilio/transfer-result", data=_lost_state_callback())
    assert response.status_code == 200
    assert response.text == "<Response><Hangup/></Response>"
    assert h.engine.get_call_state(LOST_CALL) is None
    assert twiml_server._active_inbound_count() == 0
    # the recovered call was registered and then finalized: the zeros above are not vacuous
    assert [(s.call_sid, s.metadata["failure_code"]) for s in finalized] == [(LOST_CALL, "transfer_recovery_failed")]
    assert closed == [(LOST_CALL, "failed", "transfer_recovery_failed")]


async def test_lost_transfer_state_registration_failure_finalizes_the_call(monkeypatch):
    """Registration itself raises after the call was added to the active calls:
    the caller gets a clean hang-up (not a 500), and the half-registered call is
    finalized as failed instead of holding a slot until the reaper pages."""
    from pincer.voice import analytics, twiml_server

    def broken(state, method):
        raise RuntimeError("accumulator unavailable")

    h = await _start(settings=_transfer_settings())
    finalized = await _without_other_calls(h)
    closed = _closed_traces(monkeypatch)
    monkeypatch.setattr(analytics, "ensure_accumulator", broken)
    async with _async_client(h) as client:
        assert twiml_server._active_inbound_count() == 0
        response = await client.post("/api/apps/twilio/transfer-result", data=_lost_state_callback())
    assert response.status_code == 200
    assert response.text == "<Response><Hangup/></Response>"
    assert h.engine.get_call_state(LOST_CALL) is None
    assert twiml_server._active_inbound_count() == 0
    assert [(s.call_sid, s.metadata["failure_code"]) for s in finalized] == [(LOST_CALL, "transfer_recovery_failed")]
    assert closed == [(LOST_CALL, "failed", "transfer_recovery_failed")]


async def test_lost_transfer_state_finalize_failure_still_hangs_up(monkeypatch):
    """Finalizing the failed recovery raises too: the handler still answers with
    a hang-up, not a 500, and the call's trace is closed as failed."""
    from pincer.voice import twiml_server

    async def broken_finish(call_sid, reason=""):
        raise RuntimeError("end-of-call callback failed")

    h = await _start(settings=_transfer_settings())
    await _without_other_calls(h)
    closed = _closed_traces(monkeypatch)
    _break_failed_transfer_step(monkeypatch)
    monkeypatch.setattr(h.engine, "finish_remote_ended", broken_finish)
    async with _async_client(h) as client:
        response = await client.post("/api/apps/twilio/transfer-result", data=_lost_state_callback())
        # registered before it failed: the stuck-call reaper is the backstop
        assert twiml_server._active_inbound_count() == 1
    assert response.status_code == 200
    assert response.text == "<Response><Hangup/></Response>"
    assert closed == [(LOST_CALL, "failed", "transfer_recovery_failed")]


async def test_lost_transfer_state_unregistered_failure_closes_the_trace(monkeypatch):
    """Registration raises before anything was registered: nothing to finalize,
    but the trace opened for the call is still closed as failed."""
    from pincer.voice import twiml_server

    async def broken_start(*args, **kwargs):
        raise RuntimeError("registration failed")

    h = await _start(settings=_transfer_settings())
    finalized = await _without_other_calls(h)
    closed = _closed_traces(monkeypatch)
    monkeypatch.setattr(h.engine, "on_call_start", broken_start)
    async with _async_client(h) as client:
        assert twiml_server._active_inbound_count() == 0
        response = await client.post("/api/apps/twilio/transfer-result", data=_lost_state_callback())
    assert response.status_code == 200
    assert response.text == "<Response><Hangup/></Response>"
    assert twiml_server._active_inbound_count() == 0
    assert finalized == []
    assert closed == [(LOST_CALL, "failed", "transfer_recovery_failed")]


@pytest.mark.parametrize("twilio_status", ["completed", "busy", "no-answer", "canceled", "failed"])
async def test_lost_state_replay_for_a_call_twilio_ended_hangs_up(twilio_status):
    """No state and no in-memory ended record (restart, 15 min later, or 500
    other calls since), but Twilio says the call is over: a replay still
    claiming `in-progress` must not re-register a phantom call."""
    h = await _start(settings=_transfer_settings())
    await _without_other_calls(h)
    h.engine.call_status = twilio_status
    async with _async_client(h) as client:
        response = await client.post("/api/apps/twilio/transfer-result", data=_lost_state_callback())
    assert response.text == "<Response><Hangup/></Response>"
    assert h.engine.status_fetches == [LOST_CALL]
    assert h.engine.get_active_calls() == {}


async def test_lost_state_status_check_failure_trusts_the_callback():
    """Twilio cannot be asked: the callback itself says the caller is on the
    line, so the call is recovered. Hanging up would drop a live caller with no
    trace or report; a phantom call is bounded by the stuck-call reaper."""
    h = await _start(settings=_transfer_settings())
    await _without_other_calls(h)
    h.engine.call_status = RuntimeError("twilio unreachable")
    async with _async_client(h) as client:
        response = await client.post("/api/apps/twilio/transfer-result", data=_lost_state_callback())
    assert response.status_code == 200
    assert "ConversationRelay" in response.text
    assert h.engine.status_fetches == [LOST_CALL]
    assert list(h.engine.get_active_calls()) == [LOST_CALL]


async def test_hangup_during_lost_state_recovery_finalizes_the_recovered_call(monkeypatch):
    """Twilio reports the caller on the line, then the caller hangs up while the
    call is being re-registered. The `completed` status waits for the recovery,
    then finalizes the call it registered: no phantom left holding a slot."""
    h = await _start(settings=_transfer_settings())
    await _without_other_calls(h)
    gate = asyncio.Event()
    original_fetch = h.engine.fetch_call_status

    async def slow_fetch(call_sid):
        status = await original_fetch(call_sid)
        await gate.wait()  # the caller hangs up after Twilio answered
        return status

    monkeypatch.setattr(h.engine, "fetch_call_status", slow_fetch)
    async with _async_client(h) as client:
        recovered, status, _ = await asyncio.gather(
            client.post("/api/apps/twilio/transfer-result", data=_lost_state_callback()),
            client.post("/api/apps/twilio/status", data={"CallSid": LOST_CALL, "CallStatus": "completed"}),
            _release_after_others_ran(gate),
        )
    assert "ConversationRelay" in recovered.text
    assert status.status_code == 200
    assert h.engine.get_active_calls() == {}


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_lost_state_recently_ended_skips_status_check():
    """A call we tore down moments ago is answered from memory: no Twilio round trip."""
    h = await _start(settings=_transfer_settings())
    await _transferred(h)
    await h.engine.finish_remote_ended(CALL, reason="provider_completed")
    async with _async_client(h) as client:
        response = await client.post(
            "/api/apps/twilio/transfer-result",
            data={"CallSid": CALL, "From": CALLER, "DialCallStatus": "busy", "CallStatus": "in-progress"},
        )
    assert response.text == "<Response><Hangup/></Response>"
    assert h.engine.was_recently_ended(CALL)
    assert h.engine.status_fetches == []


async def _release_after_others_ran(gate: asyncio.Event) -> None:
    """Let every other task run up to its next real wait, then open the gate."""
    for _ in range(50):
        await asyncio.sleep(0)
    gate.set()


async def test_lost_state_duplicate_during_registration_fails_transfer_once(monkeypatch):
    """Two copies of the lost-state callback arrive together; the second while
    the first is still in the channel's call-start setup (the call is already
    registered, the transfer not yet marked handled). One failed-transfer step,
    one count, and both copies answered with the same reconnect."""

    h = await _start(settings=_transfer_settings())
    await _without_other_calls(h)
    results = _count_transfer_results(monkeypatch)
    failed: list[str] = []
    original_failed = sess.ReceptionSession.on_transfer_failed

    async def counting(self, result="failed"):
        failed.append(result)
        return await original_failed(self, result)

    monkeypatch.setattr(sess.ReceptionSession, "on_transfer_failed", counting)
    gate = asyncio.Event()
    original_start = h.engine._on_call_start_callback

    async def gated(call_sid, state):
        await gate.wait()
        await original_start(call_sid, state)

    h.engine.set_on_call_start(gated)
    async with _async_client(h) as client:
        data = {**_lost_state_callback(), "DialCallSid": "CAdial"}
        first, second, _ = await asyncio.gather(
            client.post("/api/apps/twilio/transfer-result", data=data),
            client.post("/api/apps/twilio/transfer-result", data=data),
            _release_after_others_ran(gate),
        )
    assert "ConversationRelay" in first.text
    assert second.text == first.text
    assert failed == ["no_answer"]
    assert results == ["no-answer"]
    assert list(h.engine.get_active_calls()) == [LOST_CALL]
    assert call_locks.call_update_locks == {}


async def test_lost_state_duplicate_during_status_check_registers_once(monkeypatch):
    """The second copy arrives while the first waits for Twilio's call status:
    both found no state, but the call is registered once, not twice."""

    h = await _start(settings=_transfer_settings())
    await _without_other_calls(h)
    registered: list[str] = []
    original_start = h.engine._on_call_start_callback

    async def counting(call_sid, state):
        registered.append(call_sid)
        await original_start(call_sid, state)

    h.engine.set_on_call_start(counting)
    gate = asyncio.Event()
    original_fetch = h.engine.fetch_call_status

    async def slow_fetch(call_sid):
        await gate.wait()
        return await original_fetch(call_sid)

    monkeypatch.setattr(h.engine, "fetch_call_status", slow_fetch)
    async with _async_client(h) as client:
        data = {**_lost_state_callback(), "DialCallSid": "CAdial"}
        first, second, _ = await asyncio.gather(
            client.post("/api/apps/twilio/transfer-result", data=data),
            client.post("/api/apps/twilio/transfer-result", data=data),
            _release_after_others_ran(gate),
        )
    assert registered == [LOST_CALL]
    assert second.text == first.text
    assert "ConversationRelay" in first.text
    assert call_locks.call_update_locks == {}


async def test_transfer_result_lock_is_released_when_the_handler_raises(monkeypatch):
    """The per-call lock entry goes away however the handler ends."""
    from pincer.voice import twiml_server

    h = await _start(settings=_transfer_settings())
    await _transferred(h)

    async def broken(call_sid, session, dial_status, language):
        raise RuntimeError("reconnect failed")

    monkeypatch.setattr(twiml_server, "_resume_failed_transfer", broken)
    async with _async_client(h) as client:
        with pytest.raises(RuntimeError):
            await client.post(
                "/api/apps/twilio/transfer-result",
                data={"CallSid": CALL, "DialCallStatus": "busy", "DialCallSid": "CAdial", "CallStatus": "in-progress"},
            )
    assert call_locks.call_update_locks == {}


def _count_transfer_starts(monkeypatch) -> list[str]:
    """Count telemetry.transfer_started calls (the call.transfer_started event)."""
    from pincer.voice.telemetry import hooks

    seen: list[str] = []
    monkeypatch.setattr(hooks, "transfer_started", lambda call_sid, **_: seen.append(call_sid))
    return seen


def _open_socket():
    """A media WebSocket that is still connected, recording what is sent on it."""
    from unittest.mock import AsyncMock

    from starlette.websockets import WebSocketState

    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.send_text = AsyncMock()
    return ws


async def _settled(h: Harness) -> None:
    """Wait for the call's unconfirmed transfer redirect to be settled."""
    for _ in range(10):  # a settle can hand over to a re-check task
        task = h.engine.pending_transfer_settle(CALL)
        if task is None:
            return
        await asyncio.wait_for(task, timeout=5)
    raise AssertionError("transfer settle never finished")


HUMAN_PLEASE = "Ich möchte bitte mit einem Menschen sprechen."


async def test_caller_hangup_while_the_redirect_times_out_finalizes_the_call(monkeypatch):
    """The socket closes during the update (the caller hung up) and the update
    times out: Twilio says the call is over, so it is finalized as a hang-up
    during the transfer, not held in untimed TRANSFERRING for an hour."""
    _fast_transfer_checks(monkeypatch, settle_s=0)
    started = _count_transfer_starts(monkeypatch)
    h = await _start(settings=_transfer_settings())
    h.state.metadata["websocket"] = _open_socket()
    h.engine.redirect_hook = lambda call_sid: h.state.metadata.pop("websocket", None)
    h.engine.transfer_raises = TimeoutError()  # no answer in time: the redirect may have applied
    h.engine.call_status = "completed"
    session = h.session
    await h.say(HUMAN_PLEASE)
    await _settled(h)
    assert h.engine.status_fetches == [CALL]
    assert h.engine.get_call_state(CALL) is None
    assert h.engine.get_active_calls() == {}
    assert session.end_reason == "caller_hangup_during_transfer"
    assert started == []


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_settle_and_a_dial_result_for_the_same_dial_do_not_both_act(monkeypatch):
    """The settle's status check returns while /transfer-result is finalizing
    the same dial (the caller hung up while the target rang): only one of them
    may record the hang-up. The settle runs under the call's lock and re-checks
    after its await."""
    from pincer.voice.engine import DIAL_ID_KEY

    _fast_transfer_checks(monkeypatch, settle_s=0)
    abandoned: list[str] = []
    original = sess.ReceptionSession.on_transfer_abandoned

    def counting(self):
        abandoned.append(self.call_sid)
        original(self)

    monkeypatch.setattr(sess.ReceptionSession, "on_transfer_abandoned", counting)
    h = await _start(settings=_transfer_settings())
    h.engine.transfer_raises = TimeoutError()  # no answer in time: the redirect may have applied
    gate = asyncio.Event()

    async def slow_fetch(call_sid):
        await gate.wait()
        return "completed"

    async def closing_media(call_sid):
        gate.set()  # the status check returns while the dial result finalizes
        for _ in range(20):
            await asyncio.sleep(0)

    monkeypatch.setattr(h.engine, "fetch_call_status", slow_fetch)
    monkeypatch.setattr(h.engine, "close_media_stream", closing_media)
    await h.say(HUMAN_PLEASE)
    for _ in range(5):
        await asyncio.sleep(0)  # the settle is now waiting on Twilio
    asyncio.get_running_loop().call_later(0.2, gate.set)  # bound: never wait on the dial result for it
    data = {"CallSid": CALL, "DialCallStatus": "no-answer", "DialCallSid": "CAdial", "CallStatus": "completed"}
    async with _async_client(h) as client:
        await client.post(f"/api/apps/twilio/transfer-result?dial={h.state.metadata[DIAL_ID_KEY]}", data=data)
    await _settled(h)
    assert abandoned == [CALL]
    assert h.engine.get_call_state(CALL) is None


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_dial_result_waits_for_a_settle_in_progress(monkeypatch):
    """A dial result arriving while the settle is still acting on the same dial
    (here: apologizing for a redirect it found not applied) is handled after
    it, never interleaved with it."""
    from pincer.voice.engine import DIAL_ID_KEY
    from pincer.voice.telemetry import hooks

    _fast_transfer_checks(monkeypatch, settle_s=0)
    order: list[str] = []
    monkeypatch.setattr(hooks, "transfer_result", lambda call_sid, dial_status, **_: order.append("dial result"))
    h = await _start(settings=_transfer_settings())
    h.state.metadata["websocket"] = _open_socket()
    h.engine.transfer_raises = TimeoutError()  # no answer in time: the redirect may have applied
    gate = asyncio.Event()
    settled = h.engine._on_transfer_settled_callback

    async def slow_apology(call_sid, outcome):
        order.append(f"settle {outcome}")
        await gate.wait()
        await settled(call_sid, outcome)
        order.append("settle done")

    h.engine.set_on_transfer_settled(slow_apology)
    asyncio.get_running_loop().call_later(0.2, gate.set)
    await h.say(HUMAN_PLEASE)
    for _ in range(5):
        await asyncio.sleep(0)  # the settle is now in its apology
    data = {"CallSid": CALL, "DialCallStatus": "no-answer", "DialCallSid": "CAdial", "CallStatus": "in-progress"}
    async with _async_client(h) as client:
        await client.post(f"/api/apps/twilio/transfer-result?dial={h.state.metadata[DIAL_ID_KEY]}", data=data)
    await _settled(h)
    assert order == ["settle not_applied", "settle done", "dial result"]


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_cr_redirect_applied_after_the_timeout_is_seen_once_the_turn_returns(monkeypatch):
    """Twilio applies the redirect at ~10.2 s, just after our update timed out,
    with the socket still up: its close reaches us only after transfer_call has
    returned. Deciding inside transfer_call would read "socket open, not
    applied" and roll back a bridged call. Settled afterwards, the call stays
    transferred and its dial result is handled normally."""
    from pincer.voice.engine import DIAL_ID_KEY

    _fast_transfer_checks(monkeypatch, settle_s=0.05)
    started = _count_transfer_starts(monkeypatch)
    results = _count_transfer_results(monkeypatch)
    h = await _start(settings=_transfer_settings())
    h.state.metadata["websocket"] = _open_socket()
    h.engine.transfer_raises = TimeoutError()  # no answer in time: the redirect may have applied
    await h.say(HUMAN_PLEASE)
    assert h.engine.pending_transfer_settle(CALL) is not None  # not settled inside the turn
    h.state.metadata.pop("websocket")  # the relay loop is free again and sees Twilio's close
    await _settled(h)
    assert h.state.metadata.get("transferring") is True
    assert h.sm.phase == CallPhase.TRANSFERRING
    assert started == []  # a closed socket with Twilio saying in-progress is not proof yet
    data = {"CallSid": CALL, "DialCallStatus": "busy", "DialCallSid": "CAdial", "CallStatus": "in-progress"}
    url = f"/api/apps/twilio/transfer-result?dial={h.state.metadata[DIAL_ID_KEY]}"
    async with _async_client(h) as client:
        response = await client.post(url, data=data)
    assert 'welcomeGreeting="Leider ist gerade niemand erreichbar.' in response.text
    assert (started, results) == ([CALL], ["busy"])


class _TimingOutRedirect:
    """TwilioRest whose redirect gets no answer in time (what wait_for raises)."""

    async def update_call(self, call_sid, **params):
        raise TimeoutError

    async def aclose(self):
        pass


class _RelayEngineWithoutTwilio(ConversationRelayEngine):
    """ConversationRelayEngine with Twilio's REST API faked: the redirect times
    out, and speech goes out on the call's (fake) relay socket, as in production."""

    call_status = "in-progress"

    def __init__(self, settings):
        super().__init__(settings)
        self._rest = _TimingOutRedirect()
        self.fetches = 0

    async def fetch_call_status(self, call_sid):
        self.fetches += 1
        return self.call_status


async def test_cr_redirect_never_applied_apologizes_on_the_live_relay(monkeypatch):
    """The update timed out and, once the turn is over, the relay socket is
    still open: Twilio never applied the redirect. The caller must not wait in
    silence: the apology goes out on the relay and into the transcript, and the
    receptionist asks for the name."""
    import json

    _fast_transfer_checks(monkeypatch, settle_s=0)
    h = await _start(settings=_transfer_settings(), engine_cls=_RelayEngineWithoutTwilio)
    ws = _open_socket()
    h.state.metadata["websocket"] = ws
    await h.say(HUMAN_PLEASE)
    await h.channel.wait_for_turn(CALL)  # a real engine hands the turn to a task
    await _settled(h)
    apology = "Leider ist gerade niemand erreichbar. Ich nehme gern eine Nachricht auf. Wie ist Ihr Name?"
    tokens = [json.loads(call.args[0]).get("token") for call in ws.send_text.await_args_list]
    assert apology in tokens
    transcript = h.channel._transcripts[CALL]
    assert any(e.speaker == Speaker.AGENT and e.text == apology for e in transcript.entries)
    assert (h.sm.phase, h.session.step) == (CallPhase.TAKE_MESSAGE, "name")
    assert "transferring" not in h.state.metadata


async def test_caller_hangup_during_the_not_applied_apology_finalizes_the_call(monkeypatch):
    """The caller hangs up while the apology for a redirect that never applied
    is being spoken. The close is asked about at once: Twilio says the call is
    over, so it is a plain remote end, not a late apply and not an abandoned
    transfer, and it does not wait for the first re-check."""
    import json

    _fast_transfer_checks(monkeypatch, backoff_s=0.05)  # real waits, so a re-check could not hide it
    metrics = _transfer_metrics(monkeypatch)
    h = await _start(settings=_transfer_settings(), engine_cls=_RelayEngineWithoutTwilio)
    ws = _open_socket()
    apology = "Leider ist gerade niemand erreichbar. Ich nehme gern eine Nachricht auf. Wie ist Ihr Name?"
    closes: list[asyncio.Task] = []

    async def hang_up() -> None:
        # the relay's own loop sees the close and reports it
        h.engine.call_status = "completed"
        h.state.metadata.pop("websocket", None)
        await h.engine.on_media_closed(CALL)

    async def send_text(message):
        if json.loads(message).get("token") == apology:
            closes.append(asyncio.create_task(hang_up()))

    ws.send_text.side_effect = send_text
    h.state.metadata["websocket"] = ws
    session = h.session
    await h.say(HUMAN_PLEASE)
    await h.channel.wait_for_turn(CALL)  # a real engine hands the turn to a task
    await _settled(h)
    await asyncio.wait_for(closes[0], timeout=5)
    assert h.engine.get_call_state(CALL) is None  # ended by the close itself, before any re-check
    assert h.engine.fetches == 1
    assert session.end_reason != "caller_hangup_during_transfer"
    assert metrics == ["failed"]


def _fast_transfer_checks(monkeypatch, settle_s=0.0, backoff_s=0.0, grace_s=0.0):
    """Settle and re-check timed-out transfer redirects with short delays."""
    from pincer.voice import engine as engine_module

    monkeypatch.setattr(engine_module, "UNCONFIRMED_REDIRECT_SETTLE_S", settle_s)
    monkeypatch.setattr(engine_module, "TRANSFER_CONFIRM_BACKOFF_S", (backoff_s,) * 3)
    monkeypatch.setattr(engine_module, "TERMINAL_STATUS_GRACE_S", grace_s)


def _transfer_metrics(monkeypatch) -> list[str]:
    """The `result` label of every pincer.voice.receptionist.transfers count."""
    from pincer.observability import metrics

    seen: list[str] = []
    monkeypatch.setattr(metrics, "record_receptionist_transfer", lambda result, **_: seen.append(result))
    return seen


def _twilio_reports(h: Harness, *statuses):
    """Twilio's answers to successive call-status checks (the last one repeats)."""
    answers = list(statuses)

    async def fetch(call_sid):
        h.engine.status_fetches.append(call_sid)
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    h.engine.fetch_call_status = fetch


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_late_apply_after_not_applied_keeps_the_caller(monkeypatch):
    """T=10 timeout, T=15 the socket is still open (not applied: apology), T=17
    Twilio applies the redirect after all and the relay closes. The call must
    not be finalized: it goes back to transferring (and its untimed phase, so
    the watchdog cannot hang up a bridged call), and the dial's own result
    later reconnects the caller."""
    from pincer.voice.engine import DIAL_ID_KEY

    _fast_transfer_checks(monkeypatch)
    h = await _start(settings=_transfer_settings())
    h.state.metadata["websocket"] = _open_socket()
    h.engine.transfer_raises = TimeoutError()  # no answer in time: the redirect may have applied
    _twilio_reports(h, "in-progress")
    await h.say(HUMAN_PLEASE)
    await _settled(h)
    assert "transferring" not in h.state.metadata  # not applied: the apology was spoken
    h.state.metadata.pop("websocket")  # T=17: Twilio applies it, the relay closes
    await h.engine.on_media_closed(CALL)
    assert h.engine.get_call_state(CALL) is h.state
    assert h.state.metadata.get("transferring") is True
    assert h.sm.phase == CallPhase.TRANSFERRING
    h.sm._state.phase_entered_at -= 100_000
    assert h.sm.check_timeout() is False  # the watchdog leaves the bridged call alone
    await _settled(h)  # the re-checks see Twilio's in-progress throughout
    assert h.engine.get_call_state(CALL) is h.state
    data = {"CallSid": CALL, "DialCallStatus": "no-answer", "DialCallSid": "CAdial", "CallStatus": "in-progress"}
    async with _async_client(h) as client:
        response = await client.post(
            f"/api/apps/twilio/transfer-result?dial={h.state.metadata[DIAL_ID_KEY]}", data=data
        )
    assert 'welcomeGreeting="Leider ist gerade niemand erreichbar.' in response.text
    assert h.sm.phase == CallPhase.TAKE_MESSAGE


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_watchdog_leaves_a_late_applied_bridge_alone_in_any_phase(monkeypatch):
    """The receptionist may have moved past message-taking before a redirect it
    gave up on goes live late. Whatever timed phase the session is in, the
    phase watchdog must not hang up a call the engine says is transferring."""
    _fast_transfer_checks(monkeypatch)
    h = await _start(settings=_transfer_settings())
    h.state.metadata["websocket"] = _open_socket()
    h.engine.transfer_raises = TimeoutError()
    _twilio_reports(h, "in-progress")
    await h.say(HUMAN_PLEASE)
    await _settled(h)
    assert "transferring" not in h.state.metadata  # not applied: message-taking
    # Set directly: scripting a whole message-taking to get past TAKE_MESSAGE is
    # long, and the property is about any timed phase, not about how it got there.
    h.sm._state.phase = CallPhase.RECEPTION_INTENT
    h.state.metadata.pop("websocket")  # Twilio applies the redirect after all
    await h.engine.on_media_closed(CALL)
    assert h.state.metadata.get("transferring") is True
    h.sm._state.phase_entered_at -= 100_000
    assert h.sm.check_timeout() is True  # the phase alone would time out
    await h.channel._enforce_phase_timeouts()
    assert h.engine.get_call_state(CALL) is h.state
    assert h.engine.ended == []


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_normal_hangup_during_message_taking_in_the_window_is_a_plain_end(monkeypatch):
    """Not applied, so a message is being taken; 20 s later the caller hangs up
    while giving their name, inside the possibly-live window. Twilio says the
    call is over: a plain end, reported as such, not a hang-up during a
    transfer, and with no wait for a re-check."""
    _fast_transfer_checks(monkeypatch, backoff_s=0.05)
    metrics = _transfer_metrics(monkeypatch)
    h = await _start(settings=_transfer_settings())
    h.state.metadata["websocket"] = _open_socket()
    h.engine.transfer_raises = TimeoutError()
    session = h.session
    await h.say(HUMAN_PLEASE)
    await _settled(h)
    await h.say("Anna Müller")
    _twilio_reports(h, "completed")
    h.state.metadata.pop("websocket")  # the caller hangs up
    await h.engine.on_media_closed(CALL)
    assert h.engine.get_call_state(CALL) is None
    assert h.engine.status_fetches == [CALL]
    assert session.end_reason != "caller_hangup_during_transfer"
    assert metrics == ["failed"]


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_unexplained_close_with_twilio_unreachable_ends_as_a_hangup_once_twilio_answers(monkeypatch):
    """The close comes while Twilio cannot be asked: it is held as a possible
    late apply, and when a re-check finally sees the call over, it ends as the
    plain hang-up it was, not as an abandoned transfer."""
    _fast_transfer_checks(monkeypatch, backoff_s=0.05)
    metrics = _transfer_metrics(monkeypatch)
    h = await _start(settings=_transfer_settings())
    h.state.metadata["websocket"] = _open_socket()
    h.engine.transfer_raises = TimeoutError()
    session = h.session
    await h.say(HUMAN_PLEASE)
    await _settled(h)
    _twilio_reports(h, RuntimeError("timeout"), RuntimeError("timeout"), "completed")
    h.state.metadata.pop("websocket")
    await h.engine.on_media_closed(CALL)
    assert h.engine.get_call_state(CALL) is h.state  # held, not finalized blind
    await _settled(h)
    assert h.engine.get_call_state(CALL) is None
    assert h.engine.status_fetches == [CALL, CALL, CALL]
    assert session.end_reason != "caller_hangup_during_transfer"
    assert metrics == ["failed"]


async def test_failing_status_checks_are_retried_until_twilio_answers(monkeypatch):
    """Twilio cannot be asked at the settle (it was slow enough to time out the
    update): the check is retried, and the end of the call it then reports
    finishes it instead of leaving it to the hour-long reaper. A re-check's
    terminal status does not say how the call ended: a neutral reason."""
    _fast_transfer_checks(monkeypatch)
    h = await _start(settings=_transfer_settings())
    h.engine.transfer_raises = TimeoutError()  # no answer in time: the redirect may have applied
    _twilio_reports(h, RuntimeError("timeout"), RuntimeError("timeout"), "completed")
    session = h.session
    await h.say(HUMAN_PLEASE)
    finalized = _finalized_states(h)
    await _settled(h)
    assert h.engine.status_fetches == [CALL, CALL, CALL]
    assert h.engine.get_call_state(CALL) is None
    assert session.end_reason != "caller_hangup_during_transfer"
    assert [s.metadata["remote_end_reason"] for s in finalized] == ["transfer_outcome_unknown"]


def _twilio_reports_then_signals(h: Harness, *statuses):
    """Like _twilio_reports; the returned event is set once the last answer was given."""
    answers = list(statuses)
    given = asyncio.Event()

    async def fetch(call_sid):
        h.engine.status_fetches.append(call_sid)
        answer = answers.pop(0)
        if not answers:
            given.set()
        if isinstance(answer, Exception):
            raise answer
        return answer

    h.engine.fetch_call_status = fetch
    return given


async def _bridged_result_during_the_grace(h: Harness, given: asyncio.Event):
    """The bridged dial result Twilio posts right before the final status."""
    from pincer.voice.engine import DIAL_ID_KEY

    await asyncio.wait_for(given.wait(), timeout=5)
    data = {
        "CallSid": CALL,
        "DialCallStatus": "completed",
        "DialCallSid": "CAdial",
        "DialCallDuration": "42",
        "CallStatus": "completed",
    }
    async with _async_client(h) as client:
        return await client.post(f"/api/apps/twilio/transfer-result?dial={h.state.metadata[DIAL_ID_KEY]}", data=data)


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_bridged_result_queued_behind_a_terminal_recheck_wins(monkeypatch):
    """A re-check sees the call completed while the bridged dial result is on
    its way: the result decides (a completed transfer), not the re-check."""
    _fast_transfer_checks(monkeypatch, grace_s=0.3)
    abandoned = []
    monkeypatch.setattr(sess.ReceptionSession, "on_transfer_abandoned", lambda self: abandoned.append(1))
    h = await _start(settings=_transfer_settings())
    h.engine.transfer_raises = TimeoutError()
    given = _twilio_reports_then_signals(h, "in-progress", "completed")
    session = h.session
    finalized = _finalized_states(h)
    await h.say(HUMAN_PLEASE)
    settle = h.engine.pending_transfer_settle(CALL)
    await _bridged_result_during_the_grace(h, given)
    with contextlib.suppress(asyncio.CancelledError):  # the result ended the call: the check is dropped
        await asyncio.wait_for(settle, timeout=5)
    assert session.transfer_completed is True
    assert [s.metadata["remote_end_reason"] for s in finalized] == ["transferred"]
    assert abandoned == []


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_terminal_recheck_without_a_result_ends_with_a_neutral_reason(monkeypatch):
    abandoned = []
    _fast_transfer_checks(monkeypatch, grace_s=0.01)
    monkeypatch.setattr(sess.ReceptionSession, "on_transfer_abandoned", lambda self: abandoned.append(1))
    h = await _start(settings=_transfer_settings())
    h.engine.transfer_raises = TimeoutError()
    _twilio_reports(h, "in-progress", "completed")
    finalized = _finalized_states(h)
    await h.say(HUMAN_PLEASE)
    await _settled(h)
    assert [s.metadata["remote_end_reason"] for s in finalized] == ["transfer_outcome_unknown"]
    assert abandoned == []


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_bridged_result_queued_behind_the_settles_hangup_check_wins(monkeypatch):
    """The settle itself sees the call completed (T≈15 s) just as a bridged
    result arrives: the result decides, the call is not reported abandoned."""
    _fast_transfer_checks(monkeypatch, grace_s=0.3)
    abandoned = []
    monkeypatch.setattr(sess.ReceptionSession, "on_transfer_abandoned", lambda self: abandoned.append(1))
    h = await _start(settings=_transfer_settings())
    h.state.metadata["websocket"] = _open_socket()
    h.engine.redirect_hook = lambda call_sid: h.state.metadata.pop("websocket", None)
    h.engine.transfer_raises = TimeoutError()
    given = _twilio_reports_then_signals(h, "completed")
    session = h.session
    finalized = _finalized_states(h)
    await h.say(HUMAN_PLEASE)
    settle = h.engine.pending_transfer_settle(CALL)
    await _bridged_result_during_the_grace(h, given)
    with contextlib.suppress(asyncio.CancelledError):  # the result ended the call: the check is dropped
        await asyncio.wait_for(settle, timeout=5)
    assert session.transfer_completed is True
    assert [s.metadata["remote_end_reason"] for s in finalized] == ["transferred"]
    assert abandoned == []


async def test_hangup_just_before_the_settle_with_lagging_status_is_not_a_transfer(monkeypatch):
    """The redirect never applied; the caller hangs up at T=14.8, so the socket
    is gone at the T=15 settle while Twilio still reports in-progress. That is
    not proof of a transfer: no start is recorded, and the re-check that sees
    the call completed ends it (with a neutral reason: it cannot say how)."""
    _fast_transfer_checks(monkeypatch, settle_s=0.05)
    started = _count_transfer_starts(monkeypatch)
    h = await _start(settings=_transfer_settings())
    h.state.metadata["websocket"] = _open_socket()
    h.engine.transfer_raises = TimeoutError()  # no answer in time: the redirect may have applied
    _twilio_reports(h, "in-progress", "completed")
    session = h.session
    await h.say(HUMAN_PLEASE)
    h.state.metadata.pop("websocket")  # T=14.8: the caller hangs up
    await _settled(h)
    assert h.engine.get_call_state(CALL) is None
    assert session.end_reason != "caller_hangup_during_transfer"
    assert started == []


def _transfer_events(monkeypatch) -> list[str]:
    """call.transfer_started / call.transfer_result, in order."""
    from pincer.voice.telemetry import hooks

    events: list[str] = []
    monkeypatch.setattr(hooks, "transfer_started", lambda call_sid, **_: events.append(f"started {call_sid}"))
    monkeypatch.setattr(
        hooks, "transfer_result", lambda call_sid, dial_status, **_: events.append(f"{dial_status} {call_sid}")
    )
    return events


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_result_before_the_redirect_answer_records_the_transfer_start(monkeypatch):
    """T=1 Twilio applies the redirect but its answer is slow; T=4 the busy
    result arrives; T=10 the update times out. The result is proof the <Dial>
    ran: one start, then the result, and the later timeout changes nothing."""
    from pincer.voice.engine import DIAL_ID_KEY

    _fast_transfer_checks(monkeypatch)
    events = _transfer_events(monkeypatch)
    h = await _start(settings=_transfer_settings())
    h.engine.transfer_raises = TimeoutError()  # no answer in time: the redirect may have applied
    h.engine.redirect_gate = asyncio.Event()
    turn = asyncio.create_task(h.say(HUMAN_PLEASE))
    for _ in range(50):
        if DIAL_ID_KEY in h.state.metadata:
            break
        await asyncio.sleep(0)
    data = {"CallSid": CALL, "DialCallStatus": "busy", "DialCallSid": "CAdial", "CallStatus": "in-progress"}
    async with _async_client(h) as client:
        response = await client.post(
            f"/api/apps/twilio/transfer-result?dial={h.state.metadata[DIAL_ID_KEY]}", data=data
        )
    assert "ConversationRelay" in response.text
    h.engine.redirect_gate.set()  # T=10: the update's read timeout
    await asyncio.wait_for(turn, timeout=5)
    await _settled(h)
    assert events == [f"started {CALL}", f"busy {CALL}"]
    assert "transferring" not in h.state.metadata


async def test_lost_state_recovery_records_the_transfer_start(monkeypatch):
    """The recovered call's trace gets the start its dial result proves."""
    events = _transfer_events(monkeypatch)
    h = await _start(settings=_transfer_settings())
    async with _async_client(h) as client:
        await client.post("/api/apps/twilio/transfer-result", data=_lost_state_callback())
        await client.post("/api/apps/twilio/transfer-result", data=_lost_state_callback())  # a copy
    assert events == [f"started {LOST_CALL}", f"no-answer {LOST_CALL}"]


async def test_recovered_transfer_start_names_its_target_and_dial_timeout(monkeypatch):
    """A recovered start carries what the lost dial must have had: the
    profile's transfer target (the only one the receptionist dials) and the
    receptionist's dial timeout, and says it was recovered."""
    from pincer.voice.receptionist.session import TRANSFER_DIAL_TIMEOUT_S
    from pincer.voice.telemetry import hooks

    starts: list[dict] = []
    monkeypatch.setattr(hooks, "transfer_started", lambda call_sid, **kwargs: starts.append(kwargs))
    h = await _start(settings=_transfer_settings())
    async with _async_client(h) as client:
        await client.post("/api/apps/twilio/transfer-result", data=_lost_state_callback())
    assert starts == [{"target": "+4952233344455", "dial_timeout_s": TRANSFER_DIAL_TIMEOUT_S, "recovered": True}]


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_result_after_a_redirect_that_raised_records_the_transfer_start(monkeypatch):
    """The redirect raised although Twilio applied it: no start was recorded
    then, so its result records one."""
    from pincer.voice.engine import DIAL_ID_KEY

    events = _transfer_events(monkeypatch)
    h = await _start(settings=_transfer_settings())
    h.engine.transfer_raises = True
    await h.say(HUMAN_PLEASE)
    assert events == []
    data = {"CallSid": CALL, "DialCallStatus": "busy", "DialCallSid": "CAdial", "CallStatus": "in-progress"}
    async with _async_client(h) as client:
        await client.post(f"/api/apps/twilio/transfer-result?dial={h.state.metadata[DIAL_ID_KEY]}", data=data)
    assert events == [f"started {CALL}", f"busy {CALL}"]


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_unconfirmed_dial_emits_transfer_started_when_its_result_arrives(monkeypatch):
    """No socket evidence either way: no start is recorded until the dial's own
    result confirms the redirect applied; then exactly one, before the result."""
    from pincer.voice.engine import DIAL_ID_KEY
    from pincer.voice.telemetry import hooks

    _fast_transfer_checks(monkeypatch, settle_s=0)
    events: list[str] = []
    monkeypatch.setattr(hooks, "transfer_started", lambda call_sid, **_: events.append("started"))
    monkeypatch.setattr(hooks, "transfer_result", lambda call_sid, dial_status, **_: events.append(dial_status))
    h = await _start(settings=_transfer_settings())
    h.engine.transfer_raises = TimeoutError()  # no answer in time: the redirect may have applied
    await h.say(HUMAN_PLEASE)
    await _settled(h)
    assert events == []
    assert h.state.metadata.get("transferring") is True
    data = {"CallSid": CALL, "DialCallStatus": "no-answer", "DialCallSid": "CAdial", "CallStatus": "in-progress"}
    url = f"/api/apps/twilio/transfer-result?dial={h.state.metadata[DIAL_ID_KEY]}"
    async with _async_client(h) as client:
        await client.post(url, data=data)
        await client.post(url, data=data)  # a copy: nothing more
    assert events == ["started", "no-answer"]


def _count_transfer_results(monkeypatch) -> list[str]:
    """Count telemetry.transfer_result calls (the dial-result metric)."""
    from pincer.voice.telemetry import hooks

    seen: list[str] = []
    monkeypatch.setattr(hooks, "transfer_result", lambda call_sid, dial_status, **_: seen.append(dial_status))
    return seen


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_transfer_result_retry_after_the_relay_connected_is_a_no_op(monkeypatch):
    """A copy of the dial result arriving after the reconnected relay connected
    (Twilio executed the first answer, so it executes no other): it must count
    the result once, not restart the message-taking the caller is in the middle
    of, and answer an empty <Response/>. Re-issuing <Connect> would ask for the
    name again and replace the live relay socket."""
    h = await _start(settings=_transfer_settings())
    await _transferred(h)
    results = _count_transfer_results(monkeypatch)
    data = {"CallSid": CALL, "DialCallStatus": "busy", "DialCallSid": "CAdial", "CallStatus": "in-progress"}
    async with _async_client(h) as client:
        first = await client.post("/api/apps/twilio/transfer-result", data=data)
        assert "ConversationRelay" in first.text
        setup = await client.post("/api/apps/twilio/relay-webhook", json={"type": "setup", "CallSid": CALL})
        assert setup.status_code == 200
        failed_again: list[str] = []
        original = h.session.on_transfer_failed
        monkeypatch.setattr(h.session, "on_transfer_failed", lambda r: (failed_again.append(r), original(r))[1])
        await h.say("Anna Müller")  # message capture in progress: the name is taken
        step, name = h.session.step, h.session.slots.caller_name
        assert step != "name" and name
        retry = await client.post("/api/apps/twilio/transfer-result", data=data)
    assert retry.text == "<Response/>"
    assert failed_again == []
    assert results == ["busy"]
    assert (h.session.step, h.session.slots.caller_name) == (step, name)
    assert h.sm.phase == CallPhase.TAKE_MESSAGE


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_transfer_result_retry_before_the_relay_connects_replays_reconnect(monkeypatch):
    """No relay has connected since the reconnect, so Twilio may be executing
    this copy (the first never reached it): answer the same reconnect, still
    without counting the result twice."""
    h = await _start(settings=_transfer_settings())
    await _transferred(h)
    results = _count_transfer_results(monkeypatch)
    data = {"CallSid": CALL, "DialCallStatus": "busy", "DialCallSid": "CAdial", "CallStatus": "in-progress"}
    async with _async_client(h) as client:
        first = await client.post("/api/apps/twilio/transfer-result", data=data)
        retry = await client.post("/api/apps/twilio/transfer-result", data=data)
    assert "ConversationRelay" in first.text
    assert retry.text == first.text
    assert results == ["busy"]


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_late_retry_of_an_earlier_dial_is_ignored_during_the_next_dial(monkeypatch):
    """Dial #1 ends busy, the caller is reconnected and the call is dialled
    again. A delayed retry of dial #1's callback arriving while dial #2 rings
    must not count `busy` again, restart message-taking, or take dial #2's call
    back from the human; dial #2's own callback is still processed.

    Dial #2 is placed on the engine: the receptionist itself cannot go from
    TAKE_MESSAGE back to TRANSFERRING today, but the route must not rely on that."""
    h = await _start(settings=_transfer_settings())
    await _transferred(h)
    results = _count_transfer_results(monkeypatch)
    first_dial = {"CallSid": CALL, "DialCallStatus": "busy", "DialCallSid": "CAdial1", "CallStatus": "in-progress"}
    async with _async_client(h) as client:
        first = await client.post("/api/apps/twilio/transfer-result", data=first_dial)
        assert "ConversationRelay" in first.text
        await h.engine.transfer_call(CALL, "+4952233344455")  # dial #2
        assert h.state.metadata.get("transferring") is True
        step = h.session.step
        late = await client.post("/api/apps/twilio/transfer-result", data=first_dial)
        assert late.text == "<Response/>"
        assert results == ["busy"]
        assert h.state.metadata.get("transferring") is True
        assert (h.sm.phase, h.session.step) == (CallPhase.TAKE_MESSAGE, step)
        second_dial = {**first_dial, "DialCallStatus": "no-answer", "DialCallSid": "CAdial2"}
        second = await client.post("/api/apps/twilio/transfer-result", data=second_dial)
    assert "ConversationRelay" in second.text
    assert results == ["busy", "no-answer"]
    assert h.sm.phase == CallPhase.TAKE_MESSAGE


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_untokened_result_for_a_call_not_transferring_answers_safely(monkeypatch):
    """A dial result without a dial token for a live call that is not in a
    <Dial> may be a stray copy or the answer Twilio is waiting on: it gets a
    reconnect (executed only in the second case, where an empty answer would
    end the call), counts nothing and leaves the session alone."""
    h = await _start(settings=_transfer_settings())
    results = _count_transfer_results(monkeypatch)
    phase = h.sm.phase
    async with _async_client(h) as client:
        response = await client.post(
            "/api/apps/twilio/transfer-result",
            data={"CallSid": CALL, "DialCallStatus": "busy", "DialCallSid": "CAdial", "CallStatus": "in-progress"},
        )
    assert "ConversationRelay" in response.text
    assert results == []
    assert h.sm.phase == phase
    assert h.engine.get_call_state(CALL) is h.state


@pytest.mark.usefixtures("_no_transfer_resolver")
@pytest.mark.parametrize(("dial_status", "call_status"), [("busy", "in-progress"), ("completed", "completed")])
async def test_result_of_a_redirect_that_raised_but_was_applied_is_handled(monkeypatch, dial_status, call_status):
    """The <Dial> redirect times out after Twilio applied it: transfer_call
    rolls the call back to not transferring, yet Twilio dials and then waits on
    /transfer-result. That result is the call's own: a failed dial reconnects
    the caller (an empty answer would hang up), a bridged one is recorded."""
    h = await _start(settings=_transfer_settings())
    h.engine.transfer_raises = True
    await h.say("Ich möchte bitte mit einem Menschen sprechen.")
    assert "transferring" not in h.state.metadata
    dial_id = h.state.metadata["dial_id"]
    session = h.session
    results = _count_transfer_results(monkeypatch)
    data = {"CallSid": CALL, "DialCallStatus": dial_status, "DialCallSid": "CAdial", "CallStatus": call_status}
    async with _async_client(h) as client:
        response = await client.post(f"/api/apps/twilio/transfer-result?dial={dial_id}", data=data)
    assert results == [dial_status]
    if dial_status == "busy":
        assert "ConversationRelay" in response.text
        assert h.sm.phase == CallPhase.TAKE_MESSAGE
    else:
        assert response.text == "<Response><Hangup/></Response>"
        assert h.engine.get_call_state(CALL) is None
        assert session.transfer_completed is True


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_late_copy_of_an_earlier_dial_without_dial_call_sid_is_ignored(monkeypatch):
    """Dial #1 fails before Twilio creates a call, so its result has no
    DialCallSid. A late copy arriving while dial #2 rings is told apart by its
    dial token: it must not end dial #2 or touch the session, and dial #2's own
    result (also without a DialCallSid) is still processed."""
    h = await _start(settings=_transfer_settings())
    await _transferred(h)
    results = _count_transfer_results(monkeypatch)
    first_dial = h.state.metadata["dial_id"]
    data = {"CallSid": CALL, "DialCallStatus": "failed", "DialCallSid": "", "CallStatus": "in-progress"}
    async with _async_client(h) as client:
        first = await client.post(f"/api/apps/twilio/transfer-result?dial={first_dial}", data=data)
        assert "ConversationRelay" in first.text
        await h.engine.transfer_call(CALL, "+4952233344455")  # dial #2
        second_dial = h.state.metadata["dial_id"]
        step = h.session.step
        late = await client.post(f"/api/apps/twilio/transfer-result?dial={first_dial}", data=data)
        assert late.text == "<Response/>"
        assert results == ["failed"]
        assert h.state.metadata.get("transferring") is True
        assert (h.sm.phase, h.session.step) == (CallPhase.TAKE_MESSAGE, step)
        second = await client.post(
            f"/api/apps/twilio/transfer-result?dial={second_dial}", data={**data, "DialCallStatus": "no-answer"}
        )
    assert "ConversationRelay" in second.text
    assert results == ["failed", "no-answer"]
    assert "transferring" not in h.state.metadata


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_each_dial_keeps_its_own_record(monkeypatch):
    """A second dial starts its own record next to the first one's, which keeps
    its result and its answer: `transfer` is only the current dial's view."""
    from pincer.voice.engine import DIAL_ID_KEY, DIALS_KEY

    h = await _start(settings=_transfer_settings())
    await _transferred(h)
    first_dial = h.state.metadata[DIAL_ID_KEY]
    data = {"CallSid": CALL, "DialCallStatus": "busy", "DialCallSid": "CAdial1", "CallStatus": "in-progress"}
    async with _async_client(h) as client:
        first = await client.post(f"/api/apps/twilio/transfer-result?dial={first_dial}", data=data)
    await h.engine.transfer_call(CALL, "+4952233344455")  # dial #2
    second_dial = h.state.metadata[DIAL_ID_KEY]
    dials = h.state.metadata[DIALS_KEY]
    assert set(dials) == {f"dial:{first_dial}", f"dial:{second_dial}"}
    assert dials[f"dial:{first_dial}"]["dial_status"] == "busy"
    assert dials[f"dial:{first_dial}"]["reconnect_greeting"] in first.text
    assert "dial_status" not in dials[f"dial:{second_dial}"]
    assert h.state.metadata["transfer"] is dials[f"dial:{second_dial}"]


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_result_with_an_unknown_dial_token_is_ignored(monkeypatch):
    """A result naming a dial this call never had a record for (not the one in
    flight, never processed) must not be taken for the ringing dial's result."""
    h = await _start(settings=_transfer_settings())
    await _transferred(h)
    results = _count_transfer_results(monkeypatch)
    data = {"CallSid": CALL, "DialCallStatus": "busy", "DialCallSid": "CAother", "CallStatus": "in-progress"}
    async with _async_client(h) as client:
        response = await client.post("/api/apps/twilio/transfer-result?dial=notours", data=data)
    assert response.text == "<Response/>"
    assert results == []
    assert h.state.metadata.get("transferring") is True
    assert h.sm.phase == CallPhase.TRANSFERRING


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_replay_signs_the_relay_url_afresh(monkeypatch):
    """A copy Twilio executes minutes after the first answer gets the same
    greeting with a freshly signed relay URL: the first answer's token is
    stale by then and the relay upgrade would be refused."""
    import re
    from types import SimpleNamespace

    from pincer.voice import webhook_auth
    from pincer.voice.engine import DIAL_ID_KEY

    settings = _transfer_settings()
    settings.twilio_auth_token.get_secret_value.return_value = "test-auth-token"
    settings.voice_ws_auth_required = True
    clock = [1_800_000_000.0]
    monkeypatch.setattr(webhook_auth, "time", SimpleNamespace(time=lambda: clock[0]))
    h = await _start(settings=settings)
    await _transferred(h)
    url = f"/api/apps/twilio/transfer-result?dial={h.state.metadata[DIAL_ID_KEY]}"
    data = {"CallSid": CALL, "DialCallStatus": "no-answer", "DialCallSid": "CAdial", "CallStatus": "in-progress"}
    async with _async_client(h) as client:
        first = await client.post(url, data=data)
        clock[0] += settings.voice_signature_max_age_s + 1
        retry = await client.post(url, data=data)

    def relay_token(twiml: str) -> tuple[str, str]:
        match = re.search(r"/relay\?t=(\d+)&amp;s=([\w-]+)", twiml)
        assert match, twiml
        return match.group(1), match.group(2)

    def greeting(twiml: str) -> str:
        return re.search(r'welcomeGreeting="([^"]*)"', twiml).group(1)

    assert greeting(retry.text) == greeting(first.text) != ""
    ts, sig = relay_token(retry.text)
    assert ts != relay_token(first.text)[0]
    assert int(ts) == int(clock[0])
    assert sig == webhook_auth.ws_token("test-auth-token", webhook_auth.WS_RELAY_PATH, int(ts))


async def test_recovered_and_processed_dial_results_record_the_same_fields():
    """The lost-state branch and the main path write a dial's result through
    one helper: the replay rules read the same fields from either record."""
    from pincer.voice.engine import DIAL_ID_KEY

    h = await _start(settings=_transfer_settings())
    await _transferred(h)
    url = f"/api/apps/twilio/transfer-result?dial={h.state.metadata[DIAL_ID_KEY]}"
    data = {"CallSid": CALL, "DialCallStatus": "busy", "DialCallSid": "CAdial1", "CallStatus": "in-progress"}
    async with _async_client(h) as client:
        await client.post(url, data=data)
        await client.post("/api/apps/twilio/transfer-result?dial=lostdial", data=_lost_state_callback())
    processed = h.state.metadata["transfer"]
    recovered = h.engine.get_call_state(LOST_CALL).metadata["transfer"]
    result_fields = {"dial_id", "dial_status", "dial_call_sid", "dial_duration_s", "dial_bridged"}
    result_fields |= {"reconnect_greeting", "reconnect_language"}
    assert result_fields <= set(processed)
    assert result_fields <= set(recovered)
    assert recovered["recovered"] is True and "recovered" not in processed


async def test_lost_transfer_state_retry_replays_reconnect(monkeypatch):
    """The recovered call has state now, so a retried callback would take the
    main path: it must replay the reconnect instead of re-running the session's
    transfer handling and counting the result twice."""
    h = await _start(settings=_transfer_settings())
    results = _count_transfer_results(monkeypatch)
    lost = "CAlost"
    data = {"CallSid": lost, "From": CALLER, "DialCallStatus": "no-answer", "DialCallSid": "CAdial"}
    data["CallStatus"] = "in-progress"
    async with _async_client(h) as client:
        first = await client.post("/api/apps/twilio/transfer-result", data=data)
        session = h.channel.get_reception_session(lost)
        failed_again: list[str] = []
        original = session.on_transfer_failed
        monkeypatch.setattr(session, "on_transfer_failed", lambda r: (failed_again.append(r), original(r))[1])
        retry = await client.post("/api/apps/twilio/transfer-result", data=data)
    assert retry.text == first.text
    assert failed_again == []
    assert results == ["no-answer"]  # recorded once on the recovered call's new trace; the retry adds none
    transfer = h.engine.get_call_state(lost).metadata["transfer"]
    assert transfer["recovered"] is True and transfer["dial_call_sid"] == "CAdial"
    assert len(h.engine.get_active_calls()) == 2  # CALL + the one recovered call, not a third


# (greeting, the attribute value as XML-escaped in the TwiML)
@pytest.mark.parametrize(("greeting", "attribute"), [(r"Nr. 12\3", r"Nr. 12\3"), (r"\g<0>", r"\g&lt;0&gt;")])
def test_reconnect_greeting_is_inserted_literally(greeting, attribute):
    """A profile line with a backslash is text, not a regex replacement
    template: no `bad escape` 500 and no group reference expanded."""
    from pincer.voice import twiml_server
    from pincer.voice.twiml_builder import build_connect_twiml

    settings = _transfer_settings()
    twiml_server.init_voice_routes(MagicMock(), settings)
    # the greeting replaces an existing attribute (the regex path), not an inserted one
    assert "welcomeGreeting=" in build_connect_twiml(settings, call_sid=CALL, direction="inbound", language="de")
    response = twiml_server._reconnect_after_transfer(CALL, "de", greeting)
    body = response.body.decode()
    assert f'welcomeGreeting="{attribute}"' in body
    assert body.count("welcomeGreeting=") == 1


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_transfer_result_replay_after_teardown_does_not_recreate_the_call():
    """A replayed callback still claiming the caller is on the line, for a call
    we already ended: no phantom INBOUND call that never gets media."""
    h = await _start(settings=_transfer_settings())
    await _transferred(h)
    await h.engine.finish_remote_ended(CALL, reason="provider_completed")
    assert h.engine.get_call_state(CALL) is None
    async with _async_client(h) as client:
        response = await client.post(
            "/api/apps/twilio/transfer-result",
            data={"CallSid": CALL, "From": CALLER, "DialCallStatus": "busy", "CallStatus": "in-progress"},
        )
    assert response.text == "<Response><Hangup/></Response>"
    assert h.engine.get_call_state(CALL) is None
    assert h.engine.get_active_calls() == {}


def test_capacity_counts_transferred_calls():
    """A bridged call can come back to the relay (busy / no-answer), so it keeps its slot."""
    from unittest.mock import AsyncMock

    from pincer.voice.engine import CallState

    settings = _settings(inbound_max_concurrent=1)
    settings.voice_webhook_validate = False
    settings.voice_allowed_callers = "*"
    settings.twilio_auth_token = MagicMock()
    settings.twilio_auth_token.get_secret_value.return_value = ""
    engine = AsyncMock()
    bridged = CallState(call_sid="CA0", direction=CallDirection.INBOUND, caller_number="+491")
    bridged.metadata["transferring"] = True
    engine.get_active_calls = MagicMock(return_value={"CA0": bridged})
    client = _webhook_client(settings, engine)
    busy = client.post("/api/apps/twilio/webhook", data={"CallSid": "CAm", "From": "+4917299999999", "To": "+4952"})
    assert "alle Leitungen belegt" in busy.text
    engine.on_call_start.assert_not_called()
    # once the bridged call has ended, the slot is free again
    engine.get_active_calls.return_value = {}
    ok = client.post("/api/apps/twilio/webhook", data={"CallSid": "CAn", "From": "+4917299999999", "To": "+4952"})
    assert "<Hangup/>" not in ok.text
    engine.on_call_start.assert_called_once()


# ── Transfer lifecycle: watchdog, end signals, finalization ──────────


def _transfer_settings():
    settings = _settings()
    settings.voice_webhook_validate = False
    settings.voice_engine = "conversation_relay"
    settings.twilio_auth_token = MagicMock()
    settings.twilio_auth_token.get_secret_value.return_value = ""
    settings.voice_assistant_name = ""
    settings.voice_consent_mode = "none"
    settings.elevenlabs_voice_id = ""
    settings.cr_tts_provider = ""
    settings.voice_max_call_duration = 600
    settings.alert_stuck_call_grace_s = 60
    settings.voice_transfer_max_duration_s = 3600
    return settings


async def _transferred(h: Harness):
    await h.say("Ich möchte bitte mit einem Menschen sprechen.")
    assert h.sm.phase == CallPhase.TRANSFERRING
    assert h.state.metadata.get("transferring") is True
    ended: list[str] = []
    original = h.engine._on_call_end_callback

    async def counting(call_sid, state):
        ended.append(call_sid)
        await original(call_sid, state)

    h.engine.set_on_call_end(counting)
    return ended


async def _watchdog_pass(h: Harness) -> None:
    """One iteration of VoiceChannel._watchdog_loop."""
    for call_sid, sm in list(h.channel._state_machines.items()):
        if not sm.is_terminal and sm.check_timeout():
            await h.channel._handle_phase_timeout(call_sid, sm)
    await h.channel._reap_stuck_calls()


def _async_client(h: Harness):
    import httpx
    from fastapi import FastAPI

    from pincer.voice import twiml_server

    twiml_server.init_voice_routes(h.engine, h.channel._settings)
    twiml_server.set_transfer_session_resolver(h.channel.get_reception_session)
    app = FastAPI()
    app.include_router(twiml_server.twilio_router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def _no_transfer_resolver():
    yield
    from pincer.voice import twiml_server

    twiml_server.set_transfer_session_resolver(None)


async def test_transfer_not_cut_by_watchdog():
    """The P0: the 30 s TRANSFERRING timeout hung up the bridged call."""
    h = await _start(settings=_transfer_settings())
    ended = await _transferred(h)
    sm = h.sm
    for age in (31, 3600):
        sm._state.phase_entered_at -= age
        h.state.started_at -= timedelta(seconds=age)
        await _watchdog_pass(h)
        assert CALL not in h.engine.ended
        assert ended == []
        assert sm.phase == CallPhase.TRANSFERRING


@pytest.mark.usefixtures("_no_transfer_resolver")
@pytest.mark.parametrize("transfer_result_first", [True, False])
async def test_transfer_result_completed_finalizes_once(transfer_result_first):
    h = await _start(settings=_transfer_settings())
    ended = await _transferred(h)
    sm = h.sm
    transfer_result = (
        "/api/apps/twilio/transfer-result",
        {
            "CallSid": CALL,
            "DialCallStatus": "completed",
            "DialCallDuration": "192",
            "DialCallSid": "CAdial",
            "DialBridged": "true",
            "CallStatus": "completed",
        },
    )
    status = ("/api/apps/twilio/status", {"CallSid": CALL, "CallStatus": "completed", "CallDuration": "230"})
    order = [transfer_result, status] if transfer_result_first else [status, transfer_result]
    async with _async_client(h) as client:
        for path, data in [*order, *order]:  # Twilio retries: second round is a no-op
            response = await client.post(path, data=data)
            assert response.status_code == 200
            if path.endswith("transfer-result"):
                assert response.text == "<Response><Hangup/></Response>"
    assert ended == [CALL]
    assert CALL not in h.engine.ended  # finalized without a REST hangup
    assert sm.phase == CallPhase.COMPLETED
    await _wait_owner(h)
    assert len(h.owner_msgs) == 1
    reception = h.state.metadata["reception"]
    if transfer_result_first:
        assert reception["transfer_completed"] is True
        assert reception["transfer_duration_s"] == 192
        assert h.state.metadata["transfer"]["dial_call_sid"] == "CAdial"
        assert h.state.metadata["remote_end_reason"] == "transferred"
        assert "📲 Weitergeleitet an +4952233344455 (3 Min 12 s)" in h.owner_msgs[0][2]
    else:
        # /status arrived first: the call is finalized without the dial
        # result (the late callback finds no state), so the report states
        # only that the transfer started — never that it failed.
        assert h.state.metadata["remote_end_reason"] == "provider_completed"
        assert "📲 Weiterleitung an +4952233344455 gestartet" in h.owner_msgs[0][2]


@pytest.mark.usefixtures("_no_transfer_resolver")
@pytest.mark.parametrize(("dial_status", "bridged"), [("answered", ""), ("", "true"), ("in-progress", "true")])
async def test_transfer_result_bridged_variants_are_success(dial_status, bridged):
    """`answered` and DialBridged=true mean the caller talked to the human:
    never reconnect them to the AI with 'nobody is available'."""
    h = await _start(settings=_transfer_settings())
    ended = await _transferred(h)
    async with _async_client(h) as client:
        response = await client.post(
            "/api/apps/twilio/transfer-result",
            data={"CallSid": CALL, "DialCallStatus": dial_status, "DialBridged": bridged, "CallStatus": "in-progress"},
        )
    assert response.text == "<Response><Hangup/></Response>"
    assert ended == [CALL]
    reception = h.state.metadata["reception"]
    assert reception["transfer_completed"] is True and reception["transfer_failed"] is False
    assert h.state.metadata["remote_end_reason"] == "transferred"


@pytest.mark.usefixtures("_no_transfer_resolver")
@pytest.mark.parametrize("dial_status", ["", "something-new"])
async def test_transfer_result_unknown_status_with_caller_on_line_takes_a_message(dial_status):
    """Not bridged and the caller is still there: never hang up on them —
    apology + message-taking, as for any failed transfer."""
    h = await _start(settings=_transfer_settings())
    ended = await _transferred(h)
    async with _async_client(h) as client:
        response = await client.post(
            "/api/apps/twilio/transfer-result",
            data={"CallSid": CALL, "DialCallStatus": dial_status, "CallStatus": "in-progress"},
        )
    assert "ConversationRelay" in response.text and "<Hangup/>" not in response.text
    assert ended == []
    assert h.sm.phase == CallPhase.TAKE_MESSAGE
    assert h.session.transfer_failed is True
    assert "transferring" not in h.state.metadata


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_transfer_result_unknown_status_after_hangup_is_not_a_failure():
    """The call is over and the dial result unknown: not guessed at —
    no failure recorded, the report says 'started'."""
    h = await _start(settings=_transfer_settings())
    ended = await _transferred(h)
    async with _async_client(h) as client:
        response = await client.post(
            "/api/apps/twilio/transfer-result",
            data={"CallSid": CALL, "DialCallStatus": "", "CallStatus": "completed"},
        )
    assert response.text == "<Response><Hangup/></Response>"
    assert ended == [CALL]
    reception = h.state.metadata["reception"]
    assert reception["transfer_completed"] is False and reception["transfer_failed"] is False
    assert h.state.metadata["remote_end_reason"] == "transfer_result_unknown"
    await _wait_owner(h)
    assert "📲 Weiterleitung an +4952233344455 gestartet" in h.owner_msgs[-1][2]


async def test_stuck_call_page_shows_each_calls_own_limit(monkeypatch):
    """A reaped transfer must not page 'Exceeded 660s' while being 1 s over its real limit."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pincer.observability import alerts

    h = await _start()
    deliver = AsyncMock()
    monkeypatch.setattr(alerts, "deliver", deliver)
    stuck = [
        {"call_sid": "CAa", "elapsed_seconds": 661, "limit_seconds": 660, "transferring": False},
        {"call_sid": "CAb", "elapsed_seconds": 3721, "limit_seconds": 3720, "transferring": True},
    ]
    await h.channel._page_stuck_calls(SimpleNamespace(value=2.0, detail={"threshold_seconds": 660, "stuck": stuck}))
    detail = deliver.await_args.args[1][0].detail
    assert detail == "CAa: 661s > 660s limit, CAb: 3721s > 3720s limit (transfer)"


async def test_stuck_call_log_shows_the_calls_own_limit(monkeypatch, caplog):
    """The reaper log must not pair the total call duration with an over-by
    measured from the transfer start."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pincer.observability import golden_signals

    h = await _start()
    stuck = [
        {
            "call_sid": "CAb",
            "duration_seconds": 4261,
            "elapsed_seconds": 3721,
            "limit_seconds": 3720,
            "over_by_seconds": 1,
            "transferring": True,
        }
    ]
    signal = SimpleNamespace(value=1.0, detail={"threshold_seconds": 660, "stuck": stuck})
    monkeypatch.setattr(golden_signals, "stuck_calls", lambda *_a, **_k: signal)
    monkeypatch.setattr(h.channel, "_page_stuck_calls", AsyncMock())
    with caplog.at_level("ERROR"):
        await h.channel._reap_stuck_calls()
    assert "Stuck call detected [CAb]: 3721s > 3720s limit (transfer)" in caplog.text


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_transfer_result_parent_gone_no_reconnect():
    h = await _start(settings=_transfer_settings())
    ended = await _transferred(h)
    sm = h.sm
    async with _async_client(h) as client:
        response = await client.post(
            "/api/apps/twilio/transfer-result",
            data={"CallSid": CALL, "DialCallStatus": "no-answer", "CallStatus": "completed"},
        )
        assert response.text == "<Response><Hangup/></Response>"
        assert "<Connect>" not in response.text
        await client.post("/api/apps/twilio/status", data={"CallSid": CALL, "CallStatus": "completed"})
    assert ended == [CALL]
    assert sm.is_terminal
    assert h.state.metadata["remote_end_reason"] == "caller_hangup_during_transfer"
    reception = h.state.metadata["reception"]
    assert reception["transfer_failed"] is True and reception["transfer_completed"] is False
    assert reception["end_reason"] == "caller_hangup_during_transfer"
    await _wait_owner(h)
    assert "kam nicht zustande" in h.owner_msgs[-1][2]


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_back_from_failed_transfer_is_timed_again():
    h = await _start(settings=_transfer_settings())
    await _transferred(h)
    async with _async_client(h) as client:
        response = await client.post(
            "/api/apps/twilio/transfer-result",
            data={"CallSid": CALL, "DialCallStatus": "busy", "CallStatus": "in-progress"},
        )
    assert "ConversationRelay" in response.text
    assert h.sm.phase == CallPhase.TAKE_MESSAGE
    assert CALL not in h.engine.ended
    # the dial is over: the call is ours again before (or without) a relay `setup`
    assert "transferring" not in h.state.metadata
    h.sm._state.phase_entered_at -= 121
    await _watchdog_pass(h)
    assert CALL in h.engine.ended  # silence/timeout exits work again


async def test_failed_transfer_session_error_still_clears_transferring_flag(monkeypatch):
    """If the session's failed-transfer handling raises, the call must still be
    taken out of TRANSFERRING: otherwise on_media_closed ignores the closing
    socket and the reaper applies the long transfer allowance."""
    h = await _start(settings=_transfer_settings())
    await _transferred(h)

    async def boom(_result: str = "failed") -> str:
        raise KeyError("transfer_failed")

    monkeypatch.setattr(h.session, "on_transfer_failed", boom)
    async with _async_client(h) as client:
        with pytest.raises(KeyError):  # the ASGI transport re-raises the handler's 500
            await client.post(
                "/api/apps/twilio/transfer-result",
                data={"CallSid": CALL, "DialCallStatus": "busy", "CallStatus": "in-progress"},
            )
    assert "transferring" not in h.state.metadata
    assert h.sm.phase != CallPhase.TRANSFERRING
    await h.engine.on_media_closed(CALL)  # the closing socket ends the call again
    assert h.engine.get_call_state(CALL) is None


@pytest.mark.usefixtures("_no_transfer_resolver")
async def test_failed_transfer_without_session_leaves_transferring_phase():
    """No session to move the phase: end_transfer must not leave the reconnected
    AI call in the untimed TRANSFERRING phase (flag and phase must agree)."""
    from pincer.voice import twiml_server

    h = await _start(settings=_transfer_settings())
    await _transferred(h)
    async with _async_client(h) as client:
        twiml_server.set_transfer_session_resolver(lambda _sid: None)  # after _async_client sets the real one
        response = await client.post(
            "/api/apps/twilio/transfer-result",
            data={"CallSid": CALL, "DialCallStatus": "busy", "CallStatus": "in-progress"},
        )
    assert "ConversationRelay" in response.text
    assert "transferring" not in h.state.metadata
    assert h.sm.phase == CallPhase.TAKE_MESSAGE
    assert h.sm.state.transitions[-1].reason == "transfer_ended"
    h.sm._state.phase_entered_at -= 121
    await _watchdog_pass(h)
    assert CALL in h.engine.ended


# ── §9 red team (en + de) ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("language", "persona_cls"),
    [
        ("en", InboundDataExtractionPersona),
        ("de", InboundDataExtractionPersonaDe),
    ],
    ids=["data_extraction_en", "data_extraction_de"],
)
async def test_data_extraction_deflected(language, persona_cls):
    h = await _start(language=language)
    persona = persona_cls()
    deflect = str(get_prompt("RECEPTIONIST_DEFLECT_PRIVACY", language))
    action = persona.opening()
    while action.kind == "say":
        await h.say(action.text)
        action = persona.react(h.last())
    assert h.spoken.count(deflect) >= 3
    assert len(h.agent.calls) == 0  # tripwire: never reached the LLM
    _assert_no_leak(h)
    assert not h.calendar.freebusy_calls


@pytest.mark.parametrize(
    ("language", "persona_cls"),
    [("en", InboundPromptInjectionPersona), ("de", InboundPromptInjectionPersonaDe)],
    ids=["injection_en", "injection_de"],
)
async def test_prompt_injection_deflected(language, persona_cls):
    h = await _start(language=language)
    persona = persona_cls()
    deflect = str(get_prompt("RECEPTIONIST_DEFLECT_PRIVACY", language))
    end_line = str(get_prompt("RECEPTIONIST_LINES", language)["injection_end"])
    await h.say(persona.opening().text)
    assert h.last() == deflect  # first attempt: deflection + continue
    assert not h.sm.is_terminal
    await h.say(persona.react(h.last()).text)
    assert h.last() == end_line  # second attempt: polite ending
    assert CALL in h.engine.ended
    _assert_no_leak(h)


@pytest.mark.parametrize(
    ("language", "persona_cls"),
    [("en", InboundSocialEngineerPersona), ("de", InboundSocialEngineerPersonaDe)],
    ids=["social_en", "social_de"],
)
async def test_social_engineer_deflected(language, persona_cls):
    h = await _start(language=language)
    persona = persona_cls()
    deflect = str(get_prompt("RECEPTIONIST_DEFLECT_PRIVACY", language))
    action = persona.opening()
    while action.kind == "say":
        await h.say(action.text)
        action = persona.react(h.last())
    assert all(s == deflect for s in h.spoken), h.spoken
    _assert_no_leak(h)


def test_language_packs_carry_receptionist_rules():
    for language in ("en", "de", "uk"):
        rules = str(get_prompt("RECEPTIONIST_RULES", language)).lower()
        deflect = str(get_prompt("RECEPTIONIST_DEFLECT_PRIVACY", language))
        assert "{deflect}" in str(get_prompt("RECEPTIONIST_RULES", language))
        assert deflect and len(deflect) < 200
        greeting = str(get_prompt("RECEPTIONIST_GREETING", language))
        assert "{business_name}" in greeting
        marker = {"en": "assistant", "de": "assistent", "uk": "асистент"}[language]
        assert marker in greeting.lower()
        assert {"en": "receptionist", "de": "rezeption", "uk": "рецепці"}[language] in rules


def test_transcript_and_actions_for_receptionist_call():
    """Transcript lines come from both sides; the session logs its actions."""
    from pincer.voice.transcript import TranscriptLogger

    logger_ = TranscriptLogger("x")
    logger_.log_utterance(Speaker.CALLER, "hi")
    assert logger_.entries[0].speaker == Speaker.CALLER


# ── Sprint 13 §4.3: a matched thread is grouping, never disclosure ───

THREAD_SUBJECT = "Zahnarzttermin Dr. Krause"
THREAD_SUMMARY = "Rechnung über 400 Euro offen.\nStand: wartet auf Rückruf."


async def _seed_matched_thread(settings, *, number: str = CALLER):
    """An open thread on the caller's number, with content worth stealing."""
    import aiosqlite

    from pincer.voice import threads as th

    manager = th.ThreadManager(str(settings.db_path), settings=settings)
    th.set_thread_manager(manager)
    thread = await manager.create(THREAD_SUBJECT, primary_number=number, language="de")
    async with aiosqlite.connect(str(settings.db_path)) as db:
        await db.execute(
            "UPDATE pincer_call_threads SET rolling_summary = ?, open_commitments = ? WHERE thread_id = ?",
            (
                THREAD_SUMMARY,
                '[{"who":"callee","what":"schickt die Rechnung","due":null,'
                '"status":"open","source_call_sid":"CA_prev"}]',
                thread.thread_id,
            ),
        )
        await db.commit()
    return manager, thread


@pytest.mark.parametrize("mode", ["off", "ack"])
async def test_matched_inbound_call_is_grouped_in_both_modes(mode, tmp_path):
    """Attaching happens for reporting either way; only the PROMPT differs."""
    settings = _settings(thread_inbound_context=mode)
    settings.db_path = str(tmp_path / "pincer.db")
    manager, thread = await _seed_matched_thread(settings)

    h = await _start(settings=settings)

    assert await manager.thread_for_call(CALL) == thread.thread_id
    block = h.state.metadata["thread_context"]
    if mode == "off":
        assert block == ""
    else:
        assert "Ich sehe, wir hatten dazu bereits Kontakt." in block
        for secret in ("Zahnarzttermin", "Dr. Krause", "Rechnung", "400", "Stand:", thread.thread_id):
            assert secret not in block, f"ack mode disclosed {secret!r}"


@pytest.mark.parametrize("mode", ["off", "ack"])
@pytest.mark.parametrize(
    ("language", "persona_cls"),
    [("en", InboundThreadProbePersona), ("de", InboundThreadProbePersonaDe)],
    ids=["probe_en", "probe_de"],
)
async def test_inbound_thread_probe_is_deflected(mode, language, persona_cls, tmp_path):
    """Red team: a spoofed number that matched a thread asks what was discussed.

    Caller ID is not authentication, so the answer is the Sprint 12 privacy
    deflection — in BOTH context modes, and without the LLM ever being asked.
    """
    settings = _settings(thread_inbound_context=mode)
    settings.db_path = str(tmp_path / "pincer.db")
    _, thread = await _seed_matched_thread(settings)

    h = await _start(language=language, settings=settings)
    persona = persona_cls()
    deflect = str(get_prompt("RECEPTIONIST_DEFLECT_PRIVACY", language))

    action = persona.opening()
    while action.kind == "say":
        await h.say(action.text)
        action = persona.react(h.last())

    spoken = " ".join(h.spoken)
    assert h.spoken.count(deflect) >= len(persona_cls.ATTACKS)
    assert len(h.agent.calls) == 0  # tripwire: the probe never reached the LLM
    for secret in (THREAD_SUBJECT, "Dr. Krause", "Rechnung", "400 Euro", "Rückruf", thread.thread_id):
        assert secret not in spoken, f"thread probe leaked {secret!r}: {h.spoken}"
    _assert_no_leak(h)


def test_language_packs_carry_thread_prompt_keys():
    """A new language cannot ship a THREAD CONTEXT block without its rules, or
    an inbound ack without the "you do not know what it was about" rule."""
    for language in ("en", "de", "uk"):
        block = str(get_prompt("THREAD_CONTEXT_BLOCK", language))
        for placeholder in ("{summary}", "{commitments}", "{last_call}", "{last_outcome}"):
            assert placeholder in block, f"{language}: THREAD_CONTEXT_BLOCK misses {placeholder}"

        ack = str(get_prompt("THREAD_INBOUND_ACK", language))
        assert ack and len(ack) < 120
        # The ack line itself must be contentless — no slots to fill with the
        # subject, a date, or a commitment.
        assert "{" not in ack
        rule = str(get_prompt("THREAD_INBOUND_ACK_RULE", language)).lower()
        assert rule and "{" not in rule
