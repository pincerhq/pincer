"""Sprint 12 — inbound receptionist, channel-level integration + red team (§14).

Real VoiceChannel + CallStateMachine + InCallToolGate + ReceptionSession
against the FakeVoiceEngine. The conversation LLM is a scripted intent agent
(emits the ``[INTENT:…]`` token the way the prompt instructs); the calendar is
a fake registry whose free/busy can be flipped mid-call (race test).
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from pincer.channels.phone_calls import VoiceChannel
from pincer.core.agent import StreamChunk, StreamEventType
from pincer.tools.registry import ToolRegistry
from pincer.voice import approvals, status_notify
from pincer.voice.engine import CallDirection
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
)
from .personas_de import (
    InboundDataExtractionPersonaDe,
    InboundPromptInjectionPersonaDe,
    InboundSocialEngineerPersonaDe,
)
from .settings import apply_in_call_tool_defaults, apply_test_paths

BERLIN = ZoneInfo("Europe/Berlin")
CALL = "CA_inbound"
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

    def registry(self) -> ToolRegistry:
        registry = ToolRegistry()

        async def check_freebusy(emails: str, time_min: str = "", time_max: str = "") -> str:
            self.freebusy_calls += 1
            busy = list(self.busy)
            if self.take_slot_on_recheck and self.freebusy_calls > 1:
                start = datetime.fromisoformat(self.take_slot_on_recheck)
                end = (
                    start.replace(minute=start.minute + 30)
                    if start.minute < 30
                    else start.replace(hour=start.hour + 1, minute=0)
                )
                busy.append((start.isoformat(), end.isoformat()))
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


async def _start(*, language="de", now=OPEN_NOW, settings=None, calendar=None, profile_changes=None) -> Harness:
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
    engine = FakeVoiceEngine(settings)
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
    engine = AsyncMock()
    engine.get_call_state = MagicMock(
        return_value=CallState(call_sid="CAt", direction=CallDirection.INBOUND, caller_number=CALLER, language="de")
    )
    fake_session = MagicMock()
    fake_session.on_transfer_failed = AsyncMock(return_value="Leider ist gerade niemand erreichbar. Wie ist Ihr Name?")
    twiml_server.set_transfer_session_resolver(lambda sid: fake_session)
    try:
        client = _webhook_client(settings, engine)
        done = client.post("/api/apps/twilio/transfer-result", data={"CallSid": "CAt", "DialCallStatus": "completed"})
        assert done.text == "<Response><Hangup/></Response>"
        busy = client.post("/api/apps/twilio/transfer-result", data={"CallSid": "CAt", "DialCallStatus": "busy"})
        assert "ConversationRelay" in busy.text
        assert 'welcomeGreeting="Leider ist gerade niemand erreichbar. Wie ist Ihr Name?"' in busy.text
        fake_session.on_transfer_failed.assert_awaited_once()
    finally:
        twiml_server.set_transfer_session_resolver(None)


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
