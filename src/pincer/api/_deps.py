"""Build a lean Agent for the web chat surface.

Like `cli._run_agent`, the web agent registers the full default tool
suite via `pincer.tools.bootstrap.register_default_tools` so Google
Workspace, Microsoft 365, and Slack writes are first-class — without
those, the LLM hallucinates `https://docs.google.com/...` template URLs
that produce a 400 in the browser.

We also wire `request_web_approval` so destructive Google/MS365/Slack
calls trigger an SSE `approval` event that the browser renders as an
inline approval card. The per-request SSE `send_event` is bound to the
agent via a `contextvars.ContextVar` (see `pincer.api.approvals`); no MCP,
no skills, no rate limiter — those stay in the CLI.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pincer.api.approvals import request_web_approval
from pincer.core.agent import Agent
from pincer.core.session import SessionManager
from pincer.llm.cost_tracker import CostTracker
from pincer.tools.bootstrap import register_default_tools
from pincer.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from pincer.config import Settings
    from pincer.llm.base import BaseLLMProvider

logger = logging.getLogger(__name__)


def _build_llm(settings: Settings) -> BaseLLMProvider:
    provider = settings.default_provider.value
    if provider == "anthropic":
        from pincer.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings)
    from pincer.llm._openai_common import OpenAICompatibleProvider

    return OpenAICompatibleProvider(settings)


async def build_agent_from_settings() -> Agent:
    from pincer.config import get_settings_relaxed

    settings = get_settings_relaxed()

    session_mgr = SessionManager(settings.db_path, settings.max_session_messages)
    await session_mgr.initialize()

    cost_tracker = CostTracker(settings.db_path, settings.daily_budget_usd)
    await cost_tracker.initialize()

    llm = _build_llm(settings)

    tools = ToolRegistry()
    report = register_default_tools(tools, settings)
    logger.info("Web agent tools registered: %s (total=%d)", report, len(tools.list_tools()))

    return Agent(
        settings=settings,
        llm=llm,
        session_manager=session_mgr,
        cost_tracker=cost_tracker,
        tool_registry=tools,
        approval_callback=request_web_approval,
    )
