"""
Language guard — the call speaks exactly one language (pinned in
``CallState.language``) until the caller explicitly asks to switch.

Sits between the LLM output and ``engine.send_speech`` (wired in
``channels/phone_calls._handle_speech``) and does two jobs:

1. **Explicit switch flow** — the LLM signals a caller-confirmed switch with
   the ``[SWITCH_LANGUAGE:<code>]`` token (see LANGUAGE_POLICY in the prompt
   packs). The token is parsed and stripped here, and ``perform_switch``
   updates every layer at once: CallState (source of truth for prompts and
   the Media Streams TTS voice), STT, and the ConversationRelay session.

2. **Drift correction** — a cheap, deliberately conservative stopword
   heuristic flags responses CLEARLY in the wrong language (LLM mirroring
   the callee or garbled STT). One regeneration with a corrective note is
   requested; if that still mismatches, the reply is sent anyway — the guard
   never blocks a live call. The guard corrects drift; it never initiates a
   switch.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from pincer.voice.language import de_formality, relay_language, supported_languages
from pincer.voice.prompts import get_prompt
from pincer.voice.transcript import Speaker

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pincer.voice.engine import CallState, VoiceEngine
    from pincer.voice.transcript import TranscriptLogger

logger = logging.getLogger(__name__)

LANG_NAMES = {"en": "English", "de": "Deutsch", "uk": "українська"}

SWITCH_TOKEN_RE = re.compile(r"\[SWITCH_LANGUAGE:\s*([A-Za-z-]{2,5})\s*\]")

# Detection is conservative on purpose: proper nouns, borrowed words
# ("Meeting", "Termin"), and short confirmations ("Okay") must never trigger
# it. A mismatch is flagged only for utterances of >= MIN_WORDS words with a
# strong, one-sided stopword signal.
MIN_WORDS = 6

# Function-word sets, hand-pruned so no word appears in both languages
# (excluded ambiguous forms: was/so/man/will/in/an/am/hat/war/die-as-verb...).
_EN_STOPWORDS = frozenset(
    [
        "the",
        "is",
        "are",
        "you",
        "it",
        "we",
        "they",
        "to",
        "of",
        "that",
        "this",
        "have",
        "has",
        "do",
        "does",
        "can",
        "could",
        "would",
        "should",
        "please",
        "what",
        "how",
        "your",
        "my",
        "not",
        "with",
        "for",
        "be",
        "been",
        "but",
        "about",
        "there",
        "here",
        "if",
        "or",
        "on",
        "at",
        "from",
        "thanks",
        "thank",
        "yes",
        "anything",
        "something",
        "right",
        "okay",
        "sure",
        "let's",
        "i'm",
        "don't",
        "can't",
        "it's",
        "that's",
    ]
)
_DE_STOPWORDS = frozenset(
    [
        "der",
        "das",
        "und",
        "ist",
        "sind",
        "nicht",
        "ich",
        "sie",
        "wir",
        "ihr",
        "ein",
        "eine",
        "einen",
        "einem",
        "einer",
        "zu",
        "mit",
        "für",
        "auf",
        "aber",
        "oder",
        "wenn",
        "dass",
        "ja",
        "nein",
        "bitte",
        "danke",
        "kann",
        "können",
        "möchte",
        "gerne",
        "haben",
        "habe",
        "wird",
        "werden",
        "noch",
        "schon",
        "auch",
        "hier",
        "jetzt",
        "gut",
        "sehr",
        "um",
        "bei",
        "nach",
        "vor",
        "aus",
        "wie",
        "wo",
        "warum",
        "doch",
        "mal",
        "dann",
        "denn",
        "etwas",
        "alles",
        "kein",
        "keine",
        "nichts",
        "wiederhören",
        "tschüss",
    ]
)

_WORD_RE = re.compile(r"[a-zA-ZäöüÄÖÜßéèáàî']+")
_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
_LATIN_RE = re.compile(r"[A-Za-zÄÖÜäöüß]")


def detect_language(text: str) -> str | None:
    """Best-effort language of ``text``: 'en' | 'de' | 'uk' | None (ambiguous).

    Cheap and local (no LLM call). Returns None unless the signal is strong:
    short lines, mixed-language lines, and borrowed-word sprinkle all read as
    ambiguous rather than as a detection.
    """
    cyrillic = len(_CYRILLIC_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    if cyrillic + latin == 0:
        return None
    if cyrillic >= (cyrillic + latin) * 0.5:
        # Script is a strong signal on its own, but stay conservative on length
        return "uk" if cyrillic >= 12 else None

    words = [w.lower() for w in _WORD_RE.findall(text)]
    if len(words) < MIN_WORDS:
        return None

    en_hits = sum(1 for w in words if w in _EN_STOPWORDS)
    de_hits = sum(1 for w in words if w in _DE_STOPWORDS)
    winner, w_hits, l_hits = ("en", en_hits, de_hits) if en_hits >= de_hits else ("de", de_hits, en_hits)

    if w_hits < 3:
        return None
    if l_hits >= 2 or (l_hits == 1 and w_hits < 4):  # counter-signal: mixed or weak — ambiguous
        return None
    if w_hits / len(words) < 0.2:
        return None
    return winner


def parse_switch_token(response: str) -> tuple[str | None, str]:
    """(requested language code or None, response with all tokens stripped)."""
    match = SWITCH_TOKEN_RE.search(response)
    if not match:
        return None, response
    code = match.group(1).strip().lower()[:2]
    stripped = SWITCH_TOKEN_RE.sub("", response).strip()
    return code, stripped


async def perform_switch(
    state: CallState,
    new_lang: str,
    *,
    engine: VoiceEngine,
    settings: Any,
    transcript: TranscriptLogger | None = None,
) -> None:
    """Atomic language transition — all layers at once.

    - ``state.language`` (source of truth): subsequent prompts, the Media
      Streams TTS voice (``voice_for`` resolves per utterance), and STT
      configs follow automatically.
    - Media Streams: the STT stream is closed and reopened with the new
      language.
    - ConversationRelay: the documented session ``language`` message updates
      both transcription and TTS language mid-call.
    - The switch lands in the transcript as a SYSTEM entry (audit trail).
    """
    old_lang = state.language
    state.language = new_lang

    ws = state.metadata.get("websocket")
    if hasattr(engine, "setup_media_stream_stt"):
        stream_sid = str(state.metadata.get("stream_sid", ""))
        if state.metadata.get("stt_stream") is not None and stream_sid:
            await engine.close_media_stream(state.call_sid)
            await engine.setup_media_stream_stt(state.call_sid, stream_sid)
        else:
            logger.warning(
                "Language switch [%s]: no live STT stream to reopen — transcription language unchanged",
                state.call_sid,
            )
    elif getattr(engine, "engine_name", "") == "conversation_relay":
        if ws is not None:
            code = relay_language(new_lang)
            msg = json.dumps({"type": "language", "ttsLanguage": code, "transcriptionLanguage": code})
            try:
                await ws.send_text(msg)
            except Exception:
                logger.exception(
                    "Language switch [%s]: CR language message failed — transcription stays %s",
                    state.call_sid,
                    old_lang,
                )
        else:
            logger.warning(
                "Language switch [%s]: no CR websocket — transcription stays on %s", state.call_sid, old_lang
            )
    else:
        logger.debug("Language switch [%s]: engine %s follows state.language", state.call_sid, engine.engine_name)

    if transcript is not None:
        transcript.log_utterance(
            Speaker.SYSTEM,
            f"language switched from {old_lang} to {new_lang} at caller request",
            state="language_switch",
        )
    logger.info("Language switch [%s]: %s -> %s (caller request)", state.call_sid, old_lang, new_lang)


async def check_and_fix(
    response: str,
    state: CallState,
    *,
    engine: VoiceEngine,
    settings: Any,
    transcript: TranscriptLogger | None = None,
    regenerate: Callable[[str], Awaitable[str | None]] | None = None,
) -> tuple[str, bool]:
    """Guard one LLM response between generation and ``send_speech``.

    Returns ``(text_to_speak, switched)``. Happy path adds no latency
    (local parsing/detection only); a clear language mismatch costs at most
    one regeneration. Never raises, never blocks the call.
    """
    call_lang = str(state.language or "en").strip().lower()[:2]
    formality = de_formality(settings)

    requested, stripped = parse_switch_token(response)
    if requested is not None:
        if requested == call_lang:
            return stripped or response, False
        if requested in supported_languages(settings):
            await perform_switch(state, requested, engine=engine, settings=settings, transcript=transcript)
            return stripped or str(get_prompt("LANGUAGE_SWITCH_ACK", requested, de_formality(settings))), True
        logger.warning("Unsupported language switch requested [%s]: %s", state.call_sid, requested)
        return str(get_prompt("LANGUAGE_SWITCH_UNSUPPORTED", call_lang, formality)), False

    detected = detect_language(response)
    if detected is None or detected == call_lang:
        return response, False

    logger.warning(
        "Language drift [%s]: call is %s but response reads as %s — requesting one regeneration: %r",
        state.call_sid,
        call_lang,
        detected,
        response[:120],
    )
    if regenerate is None:
        return response, False

    note = str(get_prompt("LANGUAGE_REGEN_NOTE", call_lang, formality))
    try:
        retry = await regenerate(note)
    except Exception:
        logger.exception("Language regeneration failed [%s] — sending original response", state.call_sid)
        return response, False
    if not retry:
        return response, False

    # Strip any switch token defensively — drift correction never switches.
    _token, retry_text = parse_switch_token(retry)
    retry_text = retry_text or retry
    if detect_language(retry_text) not in (None, call_lang):
        logger.error(
            "Language drift persists after regeneration [%s] — sending anyway (never block the call): %r",
            state.call_sid,
            retry_text[:120],
        )
    return retry_text, False
