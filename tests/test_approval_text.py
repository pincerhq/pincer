"""Tests for the yes/no parser shared by the chat channels."""

import pytest

from pincer.channels.approval_text import norm_emoji, parse_yes_no


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("yes", True),
        ("Y", True),
        ("Yeah", True),
        ("yep", True),
        ("nope", False),
        (" Approve ", True),
        ("Yes!", True),
        ("yes please", True),
        ("*no*", False),
        ("DENY", False),
        ("👍🏽", True),
        ("так", True),
        ("nein!", False),
        ("maybe", None),
        ("maybe yes", None),
        ("", None),
        ("!!!", None),
        ("yes, go ahead please", True),
        ("no thanks", False),
        ("_no_", False),
        ("да, пожалуйста", True),
        # Repeated emoji and emoji attached to a word.
        ("👍👍", True),
        ("👍🏻👍🏻", True),
        ("Yes👍", True),
        ("ok👌", True),
        ("👎👎", False),
        # A hedge or correction after the answer is not an answer.
        ("ok wait, wrong account", None),
        ("yes but use the other address", None),
        ("yes?", None),
        ("yes no", None),
        ("no idea what you mean", None),
        # "так" is also "so/well", and "да нет" means "no".
        ("да нет", None),
        ("Так, стоп", None),
        ("да нет, не надо", None),
    ],
)
def test_parse_yes_no(text, expected):
    assert parse_yes_no(text) is expected


def test_max_words_rejects_longer_messages():
    assert parse_yes_no("yes, go ahead", max_words=3) is True
    assert parse_yes_no("👍👍👍👍", max_words=3) is True
    assert parse_yes_no("yes go ahead please", max_words=3) is None
    assert parse_yes_no("yes go ahead please") is True
    assert parse_yes_no("ok guys", max_words=3) is None


def test_norm_emoji_drops_selectors_and_skin_tones():
    assert norm_emoji("❤️") == "❤"
    assert norm_emoji("👍🏿") == "👍"
