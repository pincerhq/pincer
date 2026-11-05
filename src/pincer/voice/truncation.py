"""What a voice turn does when a model request stops on max_tokens (issue #345, Q8d).

A pure decision, so the rules are table-tested without streaming fakes; the
voice loop in ``Agent._stream_voice_turn`` only carries out the action.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pincer.config.channels import VOICE_MAX_RESPONSE_TOKENS_CEILING

# A silent retry gets this many times the configured voice limit (capped)
RETRY_TOKEN_FACTOR = 4
# The cap of that retry for a model whose own output limit is unknown: below any current
# model's limit, so it is safe to ask for
UNKNOWN_LIMIT_RETRY_CAP = 8192


class TruncationAction(StrEnum):
    RETRY = "retry"
    RAISE = "raise"
    CONTINUE = "continue"


@dataclass(frozen=True, slots=True)
class TruncationDecision:
    action: TruncationAction
    max_tokens: int | None = None  # the retry's limit; set only for RETRY


def decide_voice_truncation(
    *, streamed_text: bool, tool_calls: bool, max_tokens: int, retried: bool, model_limit: int | None
) -> TruncationDecision:
    """Decide what a cut-off voice request leads to. Rules, in order:

    1. Nothing streamed in this request, not already a retry, and the model's output
       limit (`model_limit`, None when unknown) is above the request's: retry once,
       silently, at RETRY_TOKEN_FACTOR x the limit, capped at the model's own limit and
       the ceiling. The caller heard nothing from this request, so nothing is repeated.
       A model whose limit is unknown (an Azure deployment name, a dated snapshot) is
       capped at UNKNOWN_LIMIT_RETRY_CAP instead, and retried only if that is above the
       request: a request above a model's limit is a 400, which the caller hears as the
       brain-error apology.
    2. A cut-off tool call raises: its input may be incomplete.
    3. Text streamed in this request: it was cut off mid-sentence; the turn
       ends with what was spoken.
    4. Otherwise raise. Earlier speech in the turn does not count: a turn
       that said "let me check" and then went silent must not just end.
    """
    if not streamed_text and not retried:
        cap = UNKNOWN_LIMIT_RETRY_CAP if model_limit is None else model_limit
        retry_limit = min(max_tokens * RETRY_TOKEN_FACTOR, cap, VOICE_MAX_RESPONSE_TOKENS_CEILING)
        if retry_limit > max_tokens:
            return TruncationDecision(TruncationAction.RETRY, retry_limit)
    if tool_calls:
        return TruncationDecision(TruncationAction.RAISE)
    if streamed_text:
        return TruncationDecision(TruncationAction.CONTINUE)
    return TruncationDecision(TruncationAction.RAISE)
