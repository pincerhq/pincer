"""
MCP Sampling — LLM backend abstraction for MCP sampling requests.

MCP servers can request the connected client to perform LLM inference on their
behalf (server-initiated sampling). Pincer extends this: in standalone mode,
Pincer's own LLM can handle sampling requests that external servers initiate.

Sampling is disabled by default. Enable in pincer.toml::

    [mcp.server.sampling]
    enabled = true
    model = "claude-sonnet-4-20250514"
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
from typing import Any

logger = logging.getLogger("pincer.mcp.sampling")


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

    Used in standalone mode. Requires PINCER_LLM_API_KEY or ANTHROPIC_API_KEY
    in the environment, or an explicit api_key argument.

    Args:
        api_key:       Anthropic API key. Falls back to env vars if None.
        default_model: Model to use when caller doesn't specify one.
    """

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = "claude-sonnet-4-20250514",
    ) -> None:
        import os
        self._api_key = (
            api_key
            or os.environ.get("PINCER_LLM_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or ""
        )
        self._default_model = default_model

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError(
                "anthropic package required for standalone sampling: "
                "uv pip install anthropic"
            ) from e

        from typing import cast
        client = anthropic.AsyncAnthropic(api_key=self._api_key)
        resp = await client.messages.create(
            model=model or self._default_model,
            messages=cast(Any, messages),
            max_tokens=max_tokens,
        )
        content = resp.content
        if content and hasattr(content[0], "text"):
            return content[0].text
        return str(content)


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
