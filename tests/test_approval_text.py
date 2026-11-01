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
    ],
)
def test_parse_yes_no(text, expected):
    assert parse_yes_no(text) is expected


def test_max_words_rejects_longer_messages():
    assert parse_yes_no("yes, go ahead", max_words=3) is True
    assert parse_yes_no("ok guys, see you at 8", max_words=3) is None
    assert parse_yes_no("ok guys, see you at 8") is True


def test_norm_emoji_drops_selectors_and_skin_tones():
    assert norm_emoji("❤️") == "❤"
    assert norm_emoji("👍🏿") == "👍"
