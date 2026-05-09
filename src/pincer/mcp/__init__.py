"""
Pincer MCP — sandboxed Model Context Protocol integration.

Connects Pincer to external MCP servers (GitHub, Slack, databases, etc.)
while preserving Pincer's security guarantees: subprocess isolation,
approval prompts, cost tracking, and structured audit logging.

Usage (embedded):
    from pincer.mcp import EmbeddedMCPShell, load_mcp_config

    config = load_mcp_config()
    shell = EmbeddedMCPShell(agent=agent, mcp_config=config)
    await shell.start()

Usage (standalone):
    from pincer.mcp import StandaloneMCPShell, load_mcp_config

    config = load_mcp_config()
    shell = StandaloneMCPShell(mcp_config=config, approval_mode="policy")
    await shell.run()
"""

from pincer.mcp.approval import (
    ApprovalBackend,
    ApprovalDecision,
    ApprovalRequest,
    ChannelApprovalBackend,
    CLIApprovalBackend,
    PolicyApprovalBackend,
    WebhookApprovalBackend,
    classify_risk,
)
from pincer.mcp.auth import (
    AuthServerConfig,
    ClientRegistry,
    MCPAuthMiddleware,
    MCPAuthProvider,
    MCPOAuthClient,
    OAuthClient,
    TokenClaims,
    TokenService,
    TokenStore,
    mount_oauth_endpoints,
)
from pincer.mcp.config import (
    MCPApprovalPolicyConfig,
    MCPConfig,
    MCPSamplingConfig,
    MCPServerConfig,
    MCPServerExportConfig,
    MCPTransport,
    load_mcp_config,
)
from pincer.mcp.core import MCPServiceCore
from pincer.mcp.embedded import EmbeddedMCPShell
from pincer.mcp.exporter import PincerToolExporter
from pincer.mcp.manager import MCPClientManager
from pincer.mcp.prompts import PromptRegistration, PromptRegistry, register_builtin_prompts
from pincer.mcp.resources import ResourceRegistration, ResourceRegistry
from pincer.mcp.sampling import AgentLLMBackend, LLMBackend, SamplingHandler, StandaloneLLMBackend
from pincer.mcp.sandbox import MCPSandbox
from pincer.mcp.security import MCPSecurityGate, SlidingWindowRateLimiter
from pincer.mcp.server import PincerMCPServer
from pincer.mcp.standalone import StandaloneMCPShell

__all__ = [
    # Approval
    "ApprovalBackend",
    "ApprovalDecision",
    "ApprovalRequest",
    "ChannelApprovalBackend",
    "CLIApprovalBackend",
    "PolicyApprovalBackend",
    "WebhookApprovalBackend",
    "classify_risk",
    # Auth (OAuth 2.1)
    "AuthServerConfig",
    "ClientRegistry",
    "MCPAuthMiddleware",
    "MCPAuthProvider",
    "MCPOAuthClient",
    "OAuthClient",
    "TokenClaims",
    "TokenService",
    "TokenStore",
    "mount_oauth_endpoints",
    # Config
    "MCPApprovalPolicyConfig",
    "MCPConfig",
    "MCPSamplingConfig",
    "MCPServerConfig",
    "MCPServerExportConfig",
    "MCPTransport",
    "load_mcp_config",
    # Core
    "MCPServiceCore",
    # Shells
    "EmbeddedMCPShell",
    "StandaloneMCPShell",
    # Client
    "MCPClientManager",
    # Server
    "PincerMCPServer",
    "PincerToolExporter",
    # Resources & Prompts
    "PromptRegistry",
    "PromptRegistration",
    "register_builtin_prompts",
    "ResourceRegistry",
    "ResourceRegistration",
    # Sampling
    "AgentLLMBackend",
    "LLMBackend",
    "SamplingHandler",
    "StandaloneLLMBackend",
    # Security
    "MCPSandbox",
    "MCPSecurityGate",
    "SlidingWindowRateLimiter",
]
