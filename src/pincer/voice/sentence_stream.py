"""
Sentence-boundary streaming for voice turns (Sprint 5, T5.2).

LLM tokens accumulate here and complete sentences are released to TTS the
moment they end — TTS synthesizes sentence 1 while the LLM is still writing
sentence 2, taking full-generation time off the voice-to-voice critical path.

Splitting is deliberately conservative: German/English abbreviations
("Dr.", "z.B.", "bzw.", ordinal "3.") must not cut mid-thought, decimals and
times ("14.30", "1.5") must survive, and fragments shorter than MIN_WORDS
words are held and merged into the next sentence so speech never sounds
choppy.
"""

from __future__ import annotations

import re

# A fragment shorter than this many words is held for the next boundary
MIN_WORDS = 3

# Sentence terminators. The ellipsis and CJK marks are included defensively.
_TERMINATORS = ".!?…"

# Abbreviations (lowercased, without the trailing dot) that must never end a
# sentence — German first (business calls), plus common English.
_ABBREVIATIONS = frozenset(
    (
        # German
        "z.b",
        "d.h",
        "u.a",
        "bzw",
        "ca",
        "evtl",
        "ggf",
        "inkl",
        "max",
        "min",
        "nr",
        "tel",
        "str",
        "usw",
        "vgl",
        "zzgl",
        "mwst",
        "dr",
        "prof",
        "dipl",
        "ing",
        "fr",
        "hr",
        "abs",
        "bzgl",
        "sog",
        "u.u",
        # English
        "mr",
        "mrs",
        "ms",
        "st",
        "vs",
        "etc",
        "e.g",
        "i.e",
        "approx",
        "dept",
        "inc",
        "jr",
        "sr",
        "no",
    )
)

# Word (possibly dotted like "z.B") directly before a terminator dot
_TAIL_WORD_RE = re.compile(r"([A-Za-zÄÖÜäöüß]+(?:\.[A-Za-zÄÖÜäöüß]+)*|\d+)$")

_WORD_RE = re.compile(r"[\wÄÖÜäöüß']+")


def _is_boundary(buffer: str, index: int) -> bool:
    """True when buffer[index] (a terminator) really ends a sentence."""
    char = buffer[index]
    nxt = buffer[index + 1] if index + 1 < len(buffer) else ""

    # Terminator must be followed by whitespace/end — "1.5", "14.30", URLs survive
    if nxt and not nxt.isspace():
        return False

    if char == ".":
        head = buffer[:index]
        match = _TAIL_WORD_RE.search(head)
        if match:
            tail = match.group(1).lower()
            if tail in _ABBREVIATIONS:
                return False
            # A number directly before the dot is an ordinal/date far more
            # often than a sentence end ("am 3. Oktober", "der 25. August") —
            # German capitalizes the following noun, so casing can't decide.
            # Never split here; a real sentence-final number merges into the
            # next sentence (long utterance beats broken date prosody), and
            # the turn's last sentence is released by flush() regardless.
            if tail.isdigit():
                return False
    return True


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


_MARKUP_CHARS_RE = re.compile(r"[*_`#]+")
_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-•]|\d+\.)\s+", re.MULTILINE)


def strip_tts_markup(text: str) -> str:
    """Safety net before TTS: the voice prompt forbids markdown, but a model
    under a long system prompt still slips in "**bold**" or list bullets —
    which TTS reads aloud as symbols. Strip formatting, keep the words."""
    text = _LIST_PREFIX_RE.sub("", text)
    text = _MARKUP_CHARS_RE.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


class SentenceAssembler:
    """Feed LLM token deltas, get back completed sentences ready for TTS."""

    def __init__(self, min_words: int = MIN_WORDS) -> None:
        self._buffer = ""
        self._min_words = min_words

    @property
    def pending(self) -> str:
        return self._buffer

    def feed(self, token: str) -> list[str]:
        """Add a token delta; return zero or more completed sentences."""
        self._buffer += token
        sentences: list[str] = []
        search_from = 0
        while True:
            cut = self._find_cut(search_from)
            if cut is None:
                break
            candidate = self._buffer[:cut].strip()
            if _word_count(candidate) < self._min_words:
                # Too short to speak alone ("Okay.") — merge it into the next
                # sentence by searching for the following boundary instead.
                search_from = cut
                continue
            self._buffer = self._buffer[cut:].lstrip()
            sentences.append(candidate)
            search_from = 0
        return sentences

    def _find_cut(self, start: int = 0) -> int | None:
        """Index just past the first real sentence boundary at/after `start`."""
        for index in range(start, len(self._buffer)):
            char = self._buffer[index]
            if char in _TERMINATORS and _is_boundary(self._buffer, index):
                # Consume any run of terminators ("?!", "...")
                end = index + 1
                while end < len(self._buffer) and self._buffer[end] in _TERMINATORS:
                    end += 1
                # Only cut when something follows — a trailing terminator with
                # nothing after it may still grow ("..." mid-stream); the
                # final sentence is released by flush() at turn end.
                if end < len(self._buffer):
                    return end
        return None

    def flush(self) -> str:
        """Turn is over: whatever remains is the last utterance (min-words
        rule no longer applies — the final fragment must be spoken)."""
        remainder = self._buffer.strip()
        self._buffer = ""
        return remainder
