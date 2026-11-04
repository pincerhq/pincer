"""The voice truncation policy: retry, raise or continue after a max_tokens stop (issue #345, Q8d)."""

from __future__ import annotations

import pytest

from pincer.voice.truncation import TruncationAction, TruncationDecision, decide_voice_truncation

RETRY = TruncationAction.RETRY
RAISE = TruncationAction.RAISE
CONTINUE = TruncationAction.CONTINUE

# (streamed text in this request, tool calls, already a retry) -> expected at a limit of 220
_BELOW_CAP = [
    (False, False, False, TruncationDecision(RETRY, 880)),
    (False, True, False, TruncationDecision(RETRY, 880)),
    (True, False, False, TruncationDecision(CONTINUE)),
    (True, True, False, TruncationDecision(RAISE)),
    (False, False, True, TruncationDecision(RAISE)),
    (False, True, True, TruncationDecision(RAISE)),
    (True, False, True, TruncationDecision(CONTINUE)),
    (True, True, True, TruncationDecision(RAISE)),
]


@pytest.mark.parametrize(("streamed_text", "tool_calls", "retried", "expected"), _BELOW_CAP)
def test_decision_below_the_cap(streamed_text, tool_calls, retried, expected):
    decision = decide_voice_truncation(
        streamed_text=streamed_text, tool_calls=tool_calls, max_tokens=220, retried=retried
    )
    assert decision == expected


@pytest.mark.parametrize(
    ("configured", "retry_limit"),
    [(32, 128), (220, 880), (1024, 4096), (31_999, 127_996), (32_000, 128_000), (40_000, 128_000)],
)
def test_retry_limit_is_four_times_the_configured_limit_capped(configured, retry_limit):
    decision = decide_voice_truncation(streamed_text=False, tool_calls=False, max_tokens=configured, retried=False)
    assert decision == TruncationDecision(RETRY, retry_limit)


# The same rows at the cap: a request already at 128000 is never retried.
_AT_CAP = [
    (False, False, False, TruncationDecision(RAISE)),
    (False, True, False, TruncationDecision(RAISE)),
    (True, False, False, TruncationDecision(CONTINUE)),
    (True, True, False, TruncationDecision(RAISE)),
    (False, False, True, TruncationDecision(RAISE)),
    (False, True, True, TruncationDecision(RAISE)),
    (True, False, True, TruncationDecision(CONTINUE)),
    (True, True, True, TruncationDecision(RAISE)),
]


@pytest.mark.parametrize(("streamed_text", "tool_calls", "retried", "expected"), _AT_CAP)
def test_decision_at_the_cap(streamed_text, tool_calls, retried, expected):
    decision = decide_voice_truncation(
        streamed_text=streamed_text, tool_calls=tool_calls, max_tokens=128_000, retried=retried
    )
    assert decision == expected
