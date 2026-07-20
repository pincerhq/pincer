"""
StandaloneMCPShell — run Pincer's MCP server as a standalone process.

No agent loop, no messaging channels, no proactive scheduling.
Approval decisions are handled by one of:
  - "policy"  : auto-approve/deny based on config rules (default, headless)
  - "cli"     : interactive terminal prompt (foreground, interactive)
  - "webhook" : POST to an external approval service (headless, remote)

Use cases:
- Secure MCP gateway for teams (Claude Desktop → Pincer → tools)
- Headless API deployment (CI/CD, servers)
- Development/testing of MCP integrations
- Enterprise deployment where full messaging agent is unwanted

Usage::

    shell = StandaloneMCPShell(mcp_config=cfg, approval_mode="policy")
    await shell.run()  # blocks until Ctrl+C
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from pincer.mcp.sampling import StandaloneLLMBackend

if TYPE_CHECKING:
    from pincer.mcp.config import MCPConfig

logger = logging.getLogger("pincer.mcp.standalone")


# ── Built-in tools available in standalone mode ────────────────────────────────
# Only tools that don't require agent state (no memory, no messaging channels).
_STANDALONE_SAFE_TOOL_NAMES = (
    "file_read",
    "memory_search",
    "load_skill",
    "load_skill_reference",
)


class StandaloneMCPShell:
    """
    Run Pincer's MCP capabilities as a standalone process.

    Args:
        mcp_config:       MCP configuration.
        approval_mode:    One of "policy", "cli", "webhook" (default "policy").
        webhook_url:      Required if approval_mode="webhook".
        approval_policy:  Dict for policy mode, e.g.
                          {"read": "approve", "write": "deny", "default": "deny"}.
                          Defaults to deny-all if not provided.
        llm_api_key:      API key for sampling backend (uses env var if None).
    """

    def __init__(
        self,
        mcp_config: MCPConfig,
        approval_mode: str = "policy",
        webhook_url: str | None = None,
        approval_policy: dict[str, str] | None = None,
        llm_api_key: str | None = None,
    ) -> None:
        self._config = mcp_config
        self._approval_mode = approval_mode
        self._webhook_url = webhook_url
        self._approval_policy = approval_policy
        self._llm_api_key = llm_api_key
        self._core: Any | None = None

    async def run(self) -> None:
        """
        Start the standalone MCP server and block until interrupted.

        Handles startup, serves clients, then shuts down cleanly on
        KeyboardInterrupt or asyncio.CancelledError.
        """
        from pincer.mcp.core import MCPServiceCore

        approval = self._build_approval_backend()
        llm = self._build_llm_backend()

        self._core = MCPServiceCore(
            config=self._config,
            approval_backend=approval,
            client_tool_registry=None,  # standalone has its own registry
            llm_backend=llm,
        )

        # Register tools safe for standalone (don't need agent)
        self._register_standalone_tools()

        # Register built-in prompts
        from pincer.mcp.prompts import register_builtin_prompts

        register_builtin_prompts(self._core.prompt_registry)

        # Connect to external MCP servers (client side)
        if self._config.servers:
            client_results = await self._core.start_clients()
            for name, ok in client_results.items():
                status = "connected" if ok else "FAILED"
                logger.info("Standalone MCP client '%s': %s", name, status)

        # Start MCP server
        server_cfg = getattr(self._config, "server", None)
        if server_cfg is None or not getattr(server_cfg, "enabled", False):
            logger.warning(
                "StandaloneMCPShell: [mcp.server] is not enabled — "
                "set enabled = true in pincer.toml or PINCER_MCP_SERVER_EXPORT_ENABLED=true"
            )
            return

        await self._core.start_server()

        host = getattr(server_cfg, "host", "127.0.0.1")
        port = getattr(server_cfg, "port", 18800)
        path = getattr(server_cfg, "path", "/mcp")
        logger.info(
            "Pincer MCP server running at http://%s:%d%s  (approval=%s)",
            host,
            port,
            path,
            self._approval_mode,
        )
        logger.info("Press Ctrl+C to stop")

        # Block until interrupted
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("StandaloneMCPShell: shutting down...")
        finally:
            await self._core.stop_server()
            await self._core.stop_clients()
            self._core = None

    # ── Private helpers ────────────────────────────────────────────────────────

    def _build_approval_backend(self) -> Any:
        from pincer.mcp.approval import (
            CLIApprovalBackend,
            PolicyApprovalBackend,
            WebhookApprovalBackend,
        )

        mode = self._approval_mode

        if mode == "cli":
            return CLIApprovalBackend()

        if mode == "webhook":
            if not self._webhook_url:
                raise ValueError("StandaloneMCPShell: approval_mode='webhook' requires webhook_url")
            return WebhookApprovalBackend(webhook_url=self._webhook_url)

        # Default: policy
        policy = self._approval_policy or {"default": "deny"}
        return PolicyApprovalBackend(policy=policy)

    def _build_llm_backend(self) -> Any | None:
        server_cfg = getattr(self._config, "server", None)
        if server_cfg is None:
            return None

        sampling_cfg = getattr(server_cfg, "sampling", None)
        if sampling_cfg is None or not getattr(sampling_cfg, "enabled", False):
            return None

        return StandaloneLLMBackend(
            api_key=self._llm_api_key,
            default_model=getattr(sampling_cfg, "model", "claude-sonnet-4-20250514"),
        )

    def _register_standalone_tools(self) -> None:
        """Register built-in Pincer tools that are safe without a full agent."""
        if self._core is None:
            return

        server_cfg = getattr(self._config, "server", None)
        expose_tools: list[str] = getattr(server_cfg, "expose_tools", list(_STANDALONE_SAFE_TOOL_NAMES))

        try:
            from pincer.tools.registry import ToolRegistry

            temp_registry = ToolRegistry()
            # Attempt to load built-in tools
            _load_builtin_tools_into(temp_registry)
        except Exception as e:
            logger.debug("Could not load built-in tools for standalone mode: %s", e)
            return

        for tool_name in expose_tools:
            if tool_name not in temp_registry.list_tools():
                logger.debug("Standalone: tool '%s' not available — skipping", tool_name)
                continue

            tool_def = temp_registry._tools.get(tool_name)  # noqa: SLF001
            if tool_def is None:
                continue

            mcp_name = tool_name if tool_name.startswith("pincer_") else f"pincer_{tool_name}"

            self._core.register_tool(
                name=mcp_name,
                handler=tool_def.handler,
                schema=tool_def.parameters,
                approval_required=temp_registry.requires_approval(tool_name),
                description=tool_def.description,
            )
            logger.debug("Standalone: registered tool '%s'", mcp_name)

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def core(self) -> Any | None:
        return self._core

    @property
    def server_running(self) -> bool:
        return self._core is not None and self._core.server_running

    @property
    def server_endpoint(self) -> str | None:
        return self._core.server_endpoint if self._core else None


# ── Helper ────────────────────────────────────────────────────────────────────


def _load_builtin_tools_into(registry: Any) -> None:
    """
    Load standalone-safe built-in tools into *registry*.

    Only tools that work without a full agent context are loaded.
    This is a best-effort operation; missing tools are silently skipped.

    Web search is no longer a builtin — standalone mode picks it up via the
    ordinary MCP-client path (a ``websearch`` entry in ``[[mcp.servers]]``)
    like any other external tool, not through this shortcut.
    """
    try:
        from pincer.config import get_settings_relaxed
        from pincer.tools.builtin.skills_tools import make_skills_tools
        from pincer.tools.skills.index import BUNDLED_SKILLS_DIR, SkillIndex

        settings = get_settings_relaxed()
        index = SkillIndex(
            bundled_dir=BUNDLED_SKILLS_DIR,
            user_dir=settings.skills_dir,
            max_per_root=settings.skills_max_loaded_per_root,
        )
        index.discover()
        skill_tools = make_skills_tools(index, sandbox_disabled=settings.skill_sandbox_disabled)
        registry.register(
            name="load_skill",
            description="Load a skill's full instructions by name.",
            handler=skill_tools["load_skill"],
        )
        registry.register(
            name="load_skill_reference",
            description="Read a file referenced by a skill, by relative path.",
            handler=skill_tools["load_skill_reference"],
        )
    except Exception:
        logger.debug("Could not load skill tools for standalone mode", exc_info=True)
