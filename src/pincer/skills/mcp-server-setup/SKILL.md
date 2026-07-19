---
name: mcp-server-setup
description: How to connect an external MCP server to Pincer (config file location, format, auth) or expose Pincer's own tools to external MCP clients. Load this when the user wants to add, configure, or troubleshoot an MCP server connection.
---

# MCP server setup

Pincer is both an MCP **client** (connects to external MCP servers like
GitHub, Slack, Postgres, filesystem, browser automation) and an MCP
**server** (exposes Pincer's own tools to external MCP clients via OAuth
2.0). Skills and MCP servers now coexist — connecting an MCP server does
not disable skills.

## Connecting an external MCP server (client mode)

1. Install the optional extra: `uv pip install "pincer-agent[mcp]"`.
2. Add a `[[mcp.servers]]` block to `pincer.toml` (or the equivalent env
   vars) with the server's name, transport (stdio/SSE/HTTP), command or
   URL, and any required auth (API key, OAuth token).
3. Restart the agent (`pincer run`). On startup you'll see
   `MCP '<name>' connected` or a `failed to connect` warning in the
   console — check that message first if something's wrong.
4. MCP tools appear in the agent's tool list identically to built-in
   tools; the LLM calls them the same way, with the same approval flow.

For the full config reference (transports, auth schemes, scopes, examples
for popular servers), load the detailed guide at
`docs/guides/mcp-guide.md` in the repository, or reference it via
`load_skill_reference` if it's bundled alongside this skill.

## Exposing Pincer as an MCP server

Pincer's own tools can be exposed to external MCP clients (Claude Desktop,
other agents) via OAuth 2.0 with PKCE and scope-based access control.
Which tools are exposed by default is controlled by an `expose_tools`
allowlist in the MCP config — read-only tools are typically exposed by
default, execute/write tools are opt-in.

## Troubleshooting

- **"MCP config load failed"** at startup: check `pincer.toml` syntax —
  the agent falls back to running without MCP rather than crashing.
- **A server shows connected but its tools aren't callable**: check the
  server's own scope/permission settings, not just Pincer's config.
- **Too many tools registered** (>100 from one server): the agent warns
  that LLM tool selection may degrade — consider filtering which tools
  that server exposes.
