"""The replay rule: when a stored message's provider output goes back to the provider as it was.

An assistant message keeps what its provider returned (`LLMMessage.raw`, reasoning included) and the
wire that produced it (`"<provider name>:<model>"`). A request to the same wire sends that output
verbatim; any other wire rebuilds the message from its text and tool calls, so one provider's signed
or encrypted reasoning is never sent to another endpoint or model. Every provider's converter asks
here, and this is the only place a wire mismatch removes reasoning.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.llm.base import LLMMessage


def wire_tag(provider: str, model: str) -> str:
    """The wire of a request: the provider's name and the model it asks for."""
    return f"{provider}:{model}"


def replayable_raw(message: LLMMessage, target_wire: str) -> list[dict[str, Any]] | None:
    """A copy of the message's raw output when it goes back verbatim to ``target_wire``, else None
    (rebuild the message from its content and tool calls)."""
    if not message.raw or message.wire != target_wire:
        return None
    return copy.deepcopy(message.raw)
