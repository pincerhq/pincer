"""The voice truncation policy: retry, raise or continue after a max_tokens stop (issue #345, Q8d)."""

from __future__ import annotations

import pytest

from pincer.voice.truncation import TruncationAction, TruncationDecision, decide_voice_truncation

# A model whose output limit is the 5.5 models' 128000
_BIG = 128_000
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
        streamed_text=streamed_text, tool_calls=tool_calls, max_tokens=220, retried=retried, model_limit=_BIG
    )
    assert decision == expected


@pytest.mark.parametrize(
    ("configured", "retry_limit"),
    [(32, 128), (220, 880), (1024, 4096), (31_999, 127_996), (32_000, 128_000), (40_000, 128_000)],
)
def test_retry_limit_is_four_times_the_configured_limit_capped(configured, retry_limit):
    decision = decide_voice_truncation(
        streamed_text=False, tool_calls=False, max_tokens=configured, retried=False, model_limit=_BIG
    )
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
        streamed_text=streamed_text, tool_calls=tool_calls, max_tokens=128_000, retried=retried, model_limit=_BIG
    )
    assert decision == expected


# ── The retry never asks for more than the model can output (review round 1) ──


def test_retry_is_capped_at_the_models_own_output_limit():
    """8192 on a 16k-output model: 4x would be 32768, which the API rejects with a 400."""
    decision = decide_voice_truncation(
        streamed_text=False, tool_calls=False, max_tokens=8192, retried=False, model_limit=16_384
    )
    assert decision == TruncationDecision(RETRY, 16_384)


def test_no_retry_when_the_request_already_asks_for_the_models_limit():
    decision = decide_voice_truncation(
        streamed_text=False, tool_calls=False, max_tokens=16_384, retried=False, model_limit=16_384
    )
    assert decision == TruncationDecision(RAISE)


@pytest.mark.parametrize(
    ("streamed_text", "tool_calls", "expected"),
    [
        (False, False, TruncationDecision(RETRY, 880)),
        (False, True, TruncationDecision(RETRY, 880)),
        (True, False, TruncationDecision(CONTINUE)),
        (True, True, TruncationDecision(RAISE)),
    ],
)
def test_a_model_whose_limit_is_unknown_is_retried_at_a_conservative_cap(streamed_text, tool_calls, expected):
    """Azure deployment names and dated snapshots must not lose the silent retry (review round 2)."""
    decision = decide_voice_truncation(
        streamed_text=streamed_text, tool_calls=tool_calls, max_tokens=220, retried=False, model_limit=None
    )
    assert decision == expected


@pytest.mark.parametrize(
    ("max_tokens", "retried", "expected"),
    [
        (1024, False, TruncationDecision(RETRY, 4096)),
        (4096, False, TruncationDecision(RETRY, 8192)),  # 4x would be 16384: capped at 8192
        (8192, False, TruncationDecision(RAISE)),  # the cap is not above the request
        (1024, True, TruncationDecision(RAISE)),  # a retry is not retried
    ],
)
def test_the_conservative_cap_for_an_unknown_limit(max_tokens, retried, expected):
    decision = decide_voice_truncation(
        streamed_text=False, tool_calls=False, max_tokens=max_tokens, retried=retried, model_limit=None
    )
    assert decision == expected
