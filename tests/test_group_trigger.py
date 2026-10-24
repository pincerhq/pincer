"""Tests for the shared group-chat trigger matcher."""

from __future__ import annotations

import pytest

from pincer.channels.group_trigger import matches_group_trigger, parse_group_triggers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ()),
        ("   ", ()),
        (",, ,", ()),
        ("pincer", ("pincer",)),
        ("  pincer  ,  hey pincer ", ("pincer", "hey pincer")),
        ("pincer,", ("pincer",)),
        ("pincer,,@pincer,", ("pincer", "@pincer")),
        ("@3Days Pincer", ("@3Days Pincer",)),
    ],
)
def test_parse_group_triggers(raw: str, expected: tuple[str, ...]) -> None:
    assert parse_group_triggers(raw) == expected


@pytest.mark.parametrize(
    ("raw", "text", "expected"),
    [
        # Whole-word matching
        ("pincer", "hey pincer, what's up?", True),
        ("pincer", "pincer", True),
        ("pincer", "@pincer help", True),
        ("pincer", "(pincer)", True),
        ("pincer", "pincers are cool", False),
        ("pincer", "spincer", False),
        ("pincer", "@pincer_bot help", False),
        ("pinc", "hey pincer", False),
        # @-prefixed entries require the @
        ("@pincer", "hey @pincer", True),
        ("@pincer", "@pincer: help", True),
        ("@pincer", "hey pincer", False),
        ("@pincer", "mail a@pincer.com", False),
        ("@pincer", "@pincers", False),
        # Multi-word phrases match any whitespace
        ("hey pincer", "hey pincer", True),
        ("hey pincer", "Hey   Pincer", True),
        ("hey pincer", "hey\tpincer", True),
        ("hey pincer", "hey\npincer", True),
        ("hey pincer", "hey\n pincer", True),
        ("hey pincer", "heypincer", False),
        ("hey pincer", "they pincer", False),
        ("hey pincer", "hey pincers", False),
        # @-handle with digits and an inner space
        ("@3Days Pincer", "@3Days Pincer", True),
        ("@3Days Pincer", "hi @3Days Pincer, help", True),
        ("@3Days Pincer", "@3days  pincer", True),
        ("@3Days Pincer", "@3DAYS PINCER what's up", True),
        ("@3Days Pincer", "@3Days Pincer help", True),
        ("@3Days Pincer", "@3Days Pincer!", True),
        ("@3Days Pincer", "3Days Pincer", False),
        ("@3Days Pincer", "@3Days Pincers", False),
        ("@3Days Pincer", "@3DaysPincer", False),
        # Case-insensitivity
        ("PINCER", "hey pincer", True),
        ("pincer", "HEY PINCER", True),
        # Multiple entries: any one is enough
        ("@pincer, hey pincer", "hey\n pincer", True),
        ("@pincer, hey pincer", "yo @pincer", True),
        ("@pincer, hey pincer", "pincer alone", False),
        ("foo, bar, pincer", "ask pincer", True),
        # Empty setting / empty text
        ("", "hey pincer", False),
        (" , ", "hey pincer", False),
        ("pincer", "", False),
    ],
)
def test_matches_group_trigger(raw: str, text: str, expected: bool) -> None:
    assert matches_group_trigger(text, raw) is expected


def test_regex_metacharacters_are_literal() -> None:
    assert matches_group_trigger("ask p.ncer now", "p.ncer") is True
    assert matches_group_trigger("ask pincer now", "p.ncer") is False
