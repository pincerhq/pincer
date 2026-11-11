"""
MCP Sampling — LLM backend abstraction for MCP sampling requests.

MCP servers can request the connected client to perform LLM inference on their
behalf (server-initiated sampling). Pincer extends this: in standalone mode,
Pincer's own LLM can handle sampling requests that external servers initiate.

Sampling is disabled by default. Enable in pincer.toml::

    [mcp.server.sampling]
    enabled = true
    model = "claude-sonnet-5-5"
    budget_per_day = 5.00

The SamplingHandler wraps a LLMBackend and enforces:
- Daily budget limit (tracked in memory; resets at midnight UTC)
- Per-request token estimate-based cost tracking
- Sampling disabled flag (no-op when disabled)
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pincer.config.llm import STRUCTURED_CALL_EFFORT, STRUCTURED_CALL_THINKING
from pincer.exceptions import BudgetCheckFailedError, DailyBudgetReachedError
from pincer.llm.base import ImageContent, LLMMessage, MessageRole

if TYPE_CHECKING:
    from pincer.config.pricing import PriceTable
    from pincer.llm.cost_tracker import MeteredLLM

logger = logging.getLogger("pincer.mcp.sampling")

DEFAULT_SAMPLING_MODEL = "claude-sonnet-5-5"
SAMPLING_PROVIDER = "anthropic"  # standalone sampling calls the Anthropic API directly
# The image types the Anthropic API takes (the SDK's Base64ImageSourceParam.media_type)
_IMAGE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")
# When the spend can't be checked (or recorded): sampling fails closed, like an outbound dial
SAMPLING_BUDGET_UNCHECKED = "[Sampling: could not check the daily budget, request not sent]"


class SamplingRefusedError(Exception):
    """A sampling request refused before anything was sent; `reply` is what the client gets."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        super().__init__(reply)


# ── LLM backend abstraction ────────────────────────────────────────────────────


class LLMBackend(ABC):
    """
    Abstract interface for an LLM that can process sampling requests.

    Implementors: AgentLLMBackend (embedded mode) and StandaloneLLMBackend.
    """

    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        """
        Run LLM completion on *messages* and return the assistant reply as text.

        Args:
            messages:   List of {"role": ..., "content": ...} dicts.
            model:      Model identifier override (or None to use backend default).
            max_tokens: Maximum tokens to generate.

        Returns:
            The assistant's reply text.
        """


class AgentLLMBackend(LLMBackend):
    """
    LLM backend backed by the running Pincer agent's LLM router.

    Used in embedded mode to route sampling requests through the same
    LLM provider that the agent uses (inherits API key, rate limits, etc.).
    """

    def __init__(self, llm_router: Any) -> None:
        self._router = llm_router

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        try:
            result: Any = await self._router.complete(messages, model=model, max_tokens=max_tokens)
            return str(result)
        except AttributeError:
            # Fallback: try chat() / generate() depending on router interface
            response = await self._router.chat(messages)
            return response if isinstance(response, str) else str(response)


class StandaloneLLMBackend(LLMBackend):
    """
    LLM backend that calls the Anthropic API directly (no agent required).

    Used in standalone mode. Every request goes through the daily budget check first
    and is recorded in the cost log after (`MeteredLLM`, issue #357); like the agent's
    other internal calls it pins its own effort and thinking, not the operator's.

    Only priced models are sent: a model the caller asks for that has no `[pricing]`
    entry would be billed but never counted, so the configured model is sent instead.

    Args:
        llm:           The metered Anthropic provider.
        default_model: Model to use when caller doesn't specify one, or asks for an unpriced one.
        prices:        The price table the cost log prices with.
    """

    def __init__(self, llm: MeteredLLM, default_model: str, prices: PriceTable) -> None:
        self._llm = llm
        self._default_model = default_model
        self._prices = prices

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        sent = model or self._default_model
        if sent not in self._prices:
            logger.info(
                "MCP sampling: no [pricing] entry for the requested model %r; using %r", sent, self._default_model
            )
            sent = self._default_model
        response = await self._llm.complete(
            _to_llm_messages(messages),
            model=sent,
            max_tokens=max_tokens,
            reasoning_effort=STRUCTURED_CALL_EFFORT,
            thinking=STRUCTURED_CALL_THINKING,
            pinned=True,
        )
        return response.content


class MeteringUnavailableBackend(LLMBackend):
    """Stands in when sampling's metering could not be set up at startup (logged then, once):
    every request is refused before anything is sent, since its spend could not be checked."""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        raise BudgetCheckFailedError("the daily budget can't be checked: the sampling request was not sent")


def _to_llm_messages(messages: list[dict[str, Any]]) -> list[LLMMessage]:
    """MCP sampling messages as Pincer's. `content` is a string, one content block or a list of
    them; text blocks become the message text and image blocks its images."""
    return [_to_llm_message(m["role"], m.get("content", "")) for m in messages]


def _to_llm_message(role: str, content: Any) -> LLMMessage:
    blocks = content if isinstance(content, list) else [content]
    texts: list[str] = []
    images: list[ImageContent] = []
    for block in blocks:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            texts.append(str(block.get("text", "")))
        elif isinstance(block, dict) and block.get("type") == "image":
            if role != MessageRole.USER:
                raise SamplingRefusedError("[Sampling: images are only supported in user messages, request not sent]")
            if block.get("mimeType") not in _IMAGE_TYPES:
                raise SamplingRefusedError(
                    f"[Sampling: {block.get('mimeType')} images are not supported "
                    f"(use {', '.join(_IMAGE_TYPES[:-1])} or {_IMAGE_TYPES[-1]}), request not sent]"
                )
            images.append(ImageContent(data=str(block.get("data", "")), media_type=str(block.get("mimeType", ""))))
        else:
            # Audio has no Anthropic content block, and sampling sends no tools to use or answer
            kind = block.get("type") if isinstance(block, dict) else type(block).__name__
            raise SamplingRefusedError(f"[Sampling: {kind} content is not supported, request not sent]")
    return LLMMessage(role=MessageRole(role), content="".join(texts), images=images)


# ── Sampling handler ───────────────────────────────────────────────────────────


@dataclass
class _DailyBudgetTracker:
    """Simple in-memory daily spend tracker (resets at midnight UTC)."""

    budget: float
    _spend: float = field(default=0.0, init=False)
    _day: int = field(default=-1, init=False)

    def _today(self) -> int:
        return int(time.time() // 86400)

    def _maybe_reset(self) -> None:
        today = self._today()
        if today != self._day:
            self._spend = 0.0
            self._day = today

    def remaining(self) -> float:
        self._maybe_reset()
        return max(0.0, self.budget - self._spend)

    def record(self, cost: float) -> None:
        self._maybe_reset()
        self._spend += cost

    @property
    def exhausted(self) -> bool:
        return self.remaining() <= 0.0


class SamplingHandler:
    """
    Handles MCP sampling requests via a pluggable LLMBackend.

    Sampling is opt-in: if `enabled=False` (default), all sampling requests
    return a clear error message rather than calling the LLM.

    Features:
    - Daily budget enforcement (cost estimated by token count × rate)
    - Per-request max_tokens cap
    - Graceful degradation when LLM is unavailable

    Args:
        llm_backend:     LLM backend to route requests to.
        enabled:         Whether to handle sampling requests (default False).
        budget_per_day:  Maximum USD spend per day (default 5.00).
        max_tokens:      Cap per-request token count (default 1024).
        cost_per_token:  Estimated cost per output token in USD (default 0.000003).
        default_model:   Model to use when caller doesn't specify one.
    """

    def __init__(
        self,
        llm_backend: LLMBackend | None = None,
        enabled: bool = False,
        budget_per_day: float = 5.00,
        max_tokens: int = 1024,
        cost_per_token: float = 0.000003,  # ~$3/M tokens (Sonnet tier)
        default_model: str | None = None,
    ) -> None:
        self._backend = llm_backend
        self.enabled = enabled
        self._budget = _DailyBudgetTracker(budget=budget_per_day)
        self._max_tokens = max_tokens
        self._cost_per_token = cost_per_token
        self._default_model = default_model

    async def handle(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """
        Process a sampling request.

        Returns the LLM response text, or an error message string if
        sampling is disabled, budget exhausted, or backend unavailable.
        """
        if not self.enabled:
            return "[Sampling disabled — set mcp.server.sampling.enabled = true to enable]"

        if self._backend is None:
            return "[Sampling: no LLM backend configured]"

        if self._budget.exhausted:
            return f"[Sampling budget exhausted — ${self._budget.budget:.2f}/day limit reached]"

        tokens = min(max_tokens or self._max_tokens, self._max_tokens)

        try:
            result = await self._backend.complete(
                messages=messages,
                model=model or self._default_model,
                max_tokens=tokens,
            )
            # Estimate cost by output length (chars / 4 ≈ tokens)
            estimated_tokens = len(result) / 4
            estimated_cost = estimated_tokens * self._cost_per_token
            self._budget.record(estimated_cost)
            logger.debug(
                "Sampling: ~%.0f tokens, ~$%.5f (remaining budget: $%.4f)",
                estimated_tokens,
                estimated_cost,
                self._budget.remaining(),
            )
            return result
        except SamplingRefusedError as e:
            logger.info("Sampling request refused: %s", e.reply)
            return e.reply
        except DailyBudgetReachedError:
            logger.info("Sampling request refused: the daily budget is reached")
            return "[Sampling: daily budget reached]"
        except BudgetCheckFailedError:
            logger.warning("Sampling request refused: the daily budget can't be checked", exc_info=True)
            return SAMPLING_BUDGET_UNCHECKED
        except Exception as e:
            logger.exception("Sampling request failed: %s", e)
            return f"[Sampling error: {type(e).__name__}: {e}]"

    def register_on(self, fmcp: Any) -> None:
        """
        Wire this handler into a FastMCP instance's sampling callback.

        No-op if FastMCP doesn't expose a sampling hook (older SDK versions).
        """
        # FastMCP's sampling support varies by version; hook in if available.
        set_sampling = getattr(fmcp, "set_sampling_callback", None)
        if set_sampling is None:
            logger.debug("FastMCP sampling hook not available — sampling not wired")
            return

        handler = self

        async def _sampling_callback(
            messages: list[dict[str, Any]],
            model: str | None = None,
            max_tokens: int | None = None,
        ) -> str:
            return await handler.handle(messages, model=model, max_tokens=max_tokens)

        set_sampling(_sampling_callback)
        logger.info("MCP sampling handler registered (enabled=%s)", self.enabled)

    @property
    def budget_remaining(self) -> float:
        """Remaining daily budget in USD."""
        return self._budget.remaining()

    @property
    def is_exhausted(self) -> bool:
        """True if the daily budget is spent."""
        return self._budget.exhausted
