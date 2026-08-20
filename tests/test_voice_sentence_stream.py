"""Unit tests for voice/sentence_stream.py — sentence-boundary streaming."""

from __future__ import annotations

from pincer.voice.sentence_stream import SentenceAssembler


def feed_all(assembler: SentenceAssembler, text: str, chunk_size: int = 4) -> list[str]:
    """Feed text in small token-like chunks, collecting released sentences."""
    out: list[str] = []
    for i in range(0, len(text), chunk_size):
        out.extend(assembler.feed(text[i : i + chunk_size]))
    return out


class TestSentenceBoundaries:
    def test_simple_sentences_released_incrementally(self):
        assembler = SentenceAssembler()
        sentences = feed_all(assembler, "Gerne, das mache ich sofort. Welche Uhrzeit passt Ihnen am besten? Danke")
        assert sentences == [
            "Gerne, das mache ich sofort.",
            "Welche Uhrzeit passt Ihnen am besten?",
        ]
        assert assembler.flush() == "Danke"

    def test_sentence_released_before_stream_ends(self):
        """The core streaming property: sentence 1 is available while the
        'LLM' is still writing sentence 2."""
        assembler = SentenceAssembler()
        released = assembler.feed("Ich prüfe das kurz für Sie. Einen Mo")
        assert released == ["Ich prüfe das kurz für Sie."]
        assert assembler.pending.startswith("Einen Mo")

    def test_terminator_at_buffer_end_waits_for_more(self):
        # A terminator at the very end may still grow ("..." mid-stream) —
        # release only once a following character proves the sentence ended.
        assembler = SentenceAssembler()
        assert assembler.feed("Das ist alles.") == []
        assert assembler.feed(" Und noch etwas dazu. ") == ["Das ist alles.", "Und noch etwas dazu."]
        assert assembler.flush() == ""


class TestAbbreviationGuard:
    def test_german_abbreviations_do_not_split(self):
        assembler = SentenceAssembler()
        text = "Ich rufe Dr. Müller an, z.B. morgen früh, bzw. am Dienstag. Passt das für Sie? "
        sentences = feed_all(assembler, text)
        assert sentences == [
            "Ich rufe Dr. Müller an, z.B. morgen früh, bzw. am Dienstag.",
            "Passt das für Sie?",
        ]

    def test_english_abbreviations_do_not_split(self):
        assembler = SentenceAssembler()
        sentences = feed_all(assembler, "I will call Mr. Smith at approx. ten. Does that work for you? ")
        assert sentences == ["I will call Mr. Smith at approx. ten.", "Does that work for you?"]

    def test_decimals_and_times_survive(self):
        assembler = SentenceAssembler()
        sentences = feed_all(assembler, "Der Termin dauert 1.5 Stunden und beginnt um 14.30 Uhr. Ist das okay? ")
        assert sentences == [
            "Der Termin dauert 1.5 Stunden und beginnt um 14.30 Uhr.",
            "Ist das okay?",
        ]

    def test_german_ordinal_date_does_not_split(self):
        assembler = SentenceAssembler()
        sentences = feed_all(assembler, "Wir treffen uns am 3. Oktober um zehn Uhr. Passt Ihnen das gut? ")
        assert sentences == ["Wir treffen uns am 3. Oktober um zehn Uhr.", "Passt Ihnen das gut?"]


class TestMinWords:
    def test_short_fragment_merges_into_next_sentence(self):
        assembler = SentenceAssembler()
        sentences = feed_all(assembler, "Okay. Ich trage den Termin gleich ein. Noch etwas anderes für Sie? ")
        # "Okay." alone is too choppy — merged with the following sentence
        assert sentences == [
            "Okay. Ich trage den Termin gleich ein.",
            "Noch etwas anderes für Sie?",
        ]

    def test_flush_releases_short_remainder(self):
        assembler = SentenceAssembler()
        assert assembler.feed("Gerne!") == []
        assert assembler.flush() == "Gerne!"  # min-words rule waived at turn end

    def test_flush_empty(self):
        assert SentenceAssembler().flush() == ""

    def test_multiple_sentences_in_one_token(self):
        assembler = SentenceAssembler()
        sentences = assembler.feed("Das passt sehr gut. Ich trage es direkt ein. Vielen Dank dafür. Und ")
        assert sentences == ["Das passt sehr gut.", "Ich trage es direkt ein.", "Vielen Dank dafür."]
        assert assembler.flush() == "Und"


class TestStripTtsMarkup:
    def test_markdown_removed_words_kept(self):
        from pincer.voice.sentence_stream import strip_tts_markup

        stripped = strip_tts_markup("**California** - particularly *Silicon Valley*")
        assert stripped == "California - particularly Silicon Valley"
        lists = strip_tts_markup("Here are my thoughts:\n- first item\n- second item")
        assert lists == "Here are my thoughts:\nfirst item\nsecond item"
        assert strip_tts_markup("Use `pincer run` and # heading") == "Use pincer run and heading"

    def test_plain_speech_untouched(self):
        from pincer.voice.sentence_stream import strip_tts_markup

        text = "Gerne, das mache ich sofort. Passt Dienstag um 14 Uhr?"
        assert strip_tts_markup(text) == text
