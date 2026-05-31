# Pincer

Your personal AI agent. Text it on Telegram, WhatsApp, or Discord. It does stuff.

---

## Getting Started

- [Quickstart](getting-started/quickstart.md) — running in 5 minutes
- [Deployment](getting-started/deployment.md) — Docker, Docker Compose, cloud
- [Project Structure](getting-started/project-structure.md) — architecture and module map
- [Development](getting-started/development.md) — contributing and local setup

## Core Components

- [CLI](core-components/cli.md) — `pincer` command reference
- [Tools](core-components/tools.md) — built-in tool catalog
- [Pincer MCP Server](core-components/pincer-mcp-server.md) — exposing Pincer tools to external MCP clients
- [MCP Servers](core-components/mcp-servers.md) — stdio vs HTTP, bundled server configuration
- [Security Model](core-components/security-model.md) — sandboxing, approval gates, audit log
- [Voice Calling](core-components/voice-calling.md) — Twilio voice integration

## Guides

- [MCP Guide](guides/mcp-guide.md) — connecting Pincer to external MCP servers
- [Skills Guide](guides/skills-guide.md) — *(deprecated in 0.8.0, removed in 0.9.0 — use MCP)*
- [WhatsApp Troubleshooting](guides/whatsapp-troubleshooting.md)
- [MS365 Setup](guides/ms365-setup.md) — Microsoft 365 integration
- [MS365 MCP Server](guides/ms365-mcp.md) — expose the 69 Microsoft 365 tools as an MCP server
- [Signal Setup](guides/signal-setup.md)
- [Voice Calling Setup](guides/voice-calling-setup.md)
- [Migration from OpenClaw](guides/migration-from-openclaw.md)

## Reference

- [CLI Reference](reference/cli.md)
- [REST API](reference/rest-api.md)

## Announcements

- [0.8.0 — Skills deprecated, MCP as extension standard](changelog/v0.8.0.md)
- [0.7.6 — Slack Native, MCP OAuth 2.0, Tools Catalog](changelog/v0.7.6.md)

## Changelog

- [Full Changelog](changelog/index.md)

## Contributing

- [Contributing Guide](contributing/contributing.md)
- [Bug Report](contributing/bug-report.md)
- [Feature Request](contributing/feature-request.md)
- [Security Policy](contributing/security-policy.md)
- [Code of Conduct](contributing/code-of-conduct.md)
