# Changelog

All notable changes to Pincer. Format: [Version] — Date.

---

## [0.9.0] — Upcoming

### docTR MCP Server — OCR (6 tools)

**New module:** `src/doctr-mcp/` — a FastMCP server wrapping
[mindee/doctr](https://github.com/mindee/doctr) for OCR: `ocr_document_text`,
`ocr_document`, `detect_text`, `recognize_text`, `extract_key_info`,
`list_architectures`. Defaults to HTTP transport (the heaviest bundled
server, torch-based) with a lazy bounded-LRU predictor cache. Docker/compose
setup with a pre-baked model cache volume; wired into CI and
`docs/core-components/mcp-servers.md`.

### Background task execution rework — repid + chat-driven scheduling

The hand-rolled `CronScheduler` poll/fire loop is replaced by durable,
retryable [repid](https://pypi.org/project/repid/) actors (new
`src/pincer/tasks/` package). Schedules are now created, listed, removed,
and toggled entirely via chat (`schedule_create`/`list`/`remove`/`toggle`
tools) instead of the CLI, and run through a session-free `Agent.run_headless`
execution path. A new `Deliverer`/`ResultRelay` pub-sub bridge lets a
standalone `pincer run tasks` worker (Redis-backed) scale schedule/webhook
execution independently of the main agent process —
`docker-compose.yml`'s `http` profile ships a `redis` service and a
`pincer-http-tasks` worker service as the reference topology.

See [changelog/v0.9.0.md](v0.9.0.md) for the full write-up, and
[Background Tasks](../core-components/background-tasks.md) for the
architecture and config reference.

---

## [0.8.0] — 2026-07-19

### Deprecated

#### Skills system deprecated — replaced by MCP

The `skills/` directory, `pincer skills` CLI commands, skill loader, sandbox, security scanner, and signing infrastructure are **deprecated in 0.8.0** and will be **removed in 0.9.0**.

Skills are superseded by MCP servers, which provide the same extensibility with better isolation (subprocess sandboxing via `MCPSandbox`), language independence (any runtime that speaks JSON-RPC over stdio or HTTP), and ecosystem interoperability (MCP clients like Claude Desktop, Cursor, and VS Code connect directly).

**What still works in 0.8.0:**
- All existing `skills/` directories continue to load and run unchanged.
- `pincer skills list/install/remove/scan/verify` commands continue to work.
- Skill tool registration and execution are unaffected.

**What changes in 0.9.0:**
- The `skills/` directory is no longer scanned at startup.
- `pincer skills` subcommands are removed from the CLI.
- The skill sandbox, security scanner, and signing modules are removed.
- `pincer.tools.tool` decorator still exists for use in MCP servers.

**Migration path:**

Replace each skill with a FastMCP server:

```python
# Before (skills/my_skill/main.py)
from pincer.tools import tool

@tool(name="my_tool", description="Does something")
async def my_tool(param: str) -> str:
    return f"Result: {param}"
```

```python
# After (my-skill/server.py)
from fastmcp import FastMCP

mcp = FastMCP(name="my_skill")

@mcp.tool
async def my_tool(param: str) -> str:
    """Does something."""
    return f"Result: {param}"

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

Then add the server to `pincer.toml`:

```toml
[[mcp.servers]]
name      = "my_skill"
transport = "stdio"
command   = "python"
args      = ["my-skill/server.py"]
```

See [MCP Servers](../core-components/mcp-servers.md) for full details on stdio and HTTP deployment options.

---

## [0.7.6] — 2026-04-19

### WhatsApp `err-client-outdated` — actionable failure surface

WhatsApp periodically rejects older `whatsmeow` client versions with a silent
`Login event: err-client-outdated` line that previously let `pincer run`
hang to its 120-second timeout. Now:

- `src/pincer/channels/whatsapp.py` subscribes to `ConnectFailureEv`,
  `ClientOutdatedEv`, `LoggedOutEv`, and `StreamErrorEv`. Each failure logs
  a structured ERROR with the reason, installed `neonize` version, and the
  exact command to run, then `start()` raises with the same message instead
  of hanging.
- `pincer doctor` adds a `whatsapp_neonize_version` check: PASS at or above
  the known-good minimum, WARNING otherwise, with `uv pip install -U
  'neonize>=X.Y.Z'` in the fix hint.
- `pyproject.toml` pin raised: `neonize>=0.3.16` (was `>=0.3.14`).
- New: `docs/whatsapp-troubleshooting.md` covering `err-client-outdated`,
  `logged_out`, `temp_banned`, and `main_device_gone` reasons.

### Tools Catalog + Slack Native + MCP OAuth 2.0 documented

**Consolidation release.** No new first-party tool code — docs catch up with what's already shipped on `dev`.

- `docs/TOOLS_CATALOG.md` — new, authoritative catalog of every tool Pincer can call: **304 native tools** (24 core + 27 bundled-skill functions + 113 Google Workspace + 69 Microsoft 365 + 71 Slack native) plus unlimited via MCP
- `README.md` — refreshed headline ("600+ tools"), design trade-offs table, roadmap, architecture diagram, CLI reference, and doc index
- `docs/PROJECT_STRUCTURE.md` — sprint history updated (Sprint 9.5 Google Meet, Sprint 11 Slack Native, Sprint 12 MCP OAuth, Sprint 13 Tools Catalog); architecture diagram updated to reflect 304 native tools
- Google Workspace total corrected from 85 → 113 (Google Meet v2 REST surface was added in Sprint 9.5 but undercounted in prior docs)

### Slack Native Integration — 71 tools

**New module:** `src/pincer/integrations/slack/` — full native Slack tool surface (bot + user tokens) separate from the Slack *channel* code.

- `tools_messages.py` — 18 tools (channel history, threads, post, schedule, Block Kit, DM, broadcast replies, summarize channel)
- `tools_channels.py` — 16 tools (list/create/archive/rename/topic/purpose/join/leave/invite/kick/DM/group DM)
- `tools_users.py` — 10 tools (list users, lookup by email, presence, user groups CRUD)
- `tools_files.py` — 10 tools (list/upload/download/share/delete + remote-file references)
- `tools_misc.py` — 12 tools (pins, bookmarks, reminders, search messages + files via user token)
- `tools_reactions.py` — 5 tools (add/remove reactions, list emoji)

Enabled automatically when `PINCER_SLACK_BOT_TOKEN` is set; search tools additionally require `PINCER_SLACK_USER_TOKEN`.

### MCP OAuth 2.0 Authorization Server

**New module:** `src/pincer/mcp/auth/` — full OAuth 2.0 server for exposing Pincer tools to MCP clients.

- `/authorize`, `/token`, `/introspect`, `/revoke` endpoints
- RFC 8414 server metadata
- PKCE (verifier/challenge helpers)
- JWT issuance + validation via `PyJWT[crypto]>=2.8.0`
- Scope-based access control
- Bearer token middleware for protected routes
- Token storage via SQLite + OS keyring

### Google Workspace — Meet v2 REST surface (27 tools)

Extends Sprint 9 (85 tools) to **113 tools** total. Added: spaces (create/get/update/end/moderation/artifacts/members), conference records + participants + sessions, recordings (list/get/download/status), transcripts + entries + summarization, smart notes, event subscriptions.

---

## [0.7.5] — 2026-04-06

### Microsoft 365 Integration — 69 tools

**New module:** `src/pincer/integrations/ms365/` — native Microsoft Graph REST integration covering Outlook, Calendar, OneDrive, To Do, Teams, Contacts, and OneNote.

#### New files

- `integrations/ms365/__init__.py` — `get_ms365_client()` / `register_all_tools()` public API
- `integrations/ms365/config.py` — `MS365IntegrationConfig` dataclass; reads `[integrations.ms365]` from `pincer.toml` + `PINCER_MS365_CLIENT_ID` / `PINCER_MS365_TENANT_ID` env vars; supports `${VAR}` references in `client_id`
- `integrations/ms365/auth.py` — `MS365Auth`: MSAL-backed OAuth via device code or interactive browser flow; token cached to `~/.pincer/ms365_token_cache.json` (mode `0600`); automatic token refresh
- `integrations/ms365/graph_client.py` — `GraphClient`: authenticated httpx async client; rate-limit retry (429 + `Retry-After`); pagination via `@odata.nextLink`
- `integrations/ms365/tools_email.py` — 17 Outlook email tools (`outlook__*`)
- `integrations/ms365/tools_calendar.py` — 12 calendar tools (`calendar__*`)
- `integrations/ms365/tools_onedrive.py` — 14 OneDrive tools (`onedrive__*`)
- `integrations/ms365/tools_todo.py` — 8 To Do tools (`ms_todo__*`)
- `integrations/ms365/tools_teams.py` — 7 Teams tools (`teams__*`)
- `integrations/ms365/tools_contacts.py` — 6 Contacts tools (`contacts__*`)
- `integrations/ms365/tools_onenote.py` — 5 OneNote tools (`onenote__*`)

#### CLI

- `pincer setup-ms365` — one-time device code auth flow; saves token + appends `[integrations.ms365]` to `pincer.toml`

#### Agent integration (`cli.py`)

- MS365 tools auto-registered at startup when `[integrations.ms365]` is configured and a cached token exists
- Yellow warning + re-auth hint shown when token is missing or `RuntimeError` raised
- Grok/xAI provider wired in `_run_agent()` (`settings.default_provider == "grok"`)

#### Other changes

- `pyproject.toml`: version bumped to `0.7.5`; `python-multipart>=0.0.9` added to core deps; `voice`, `image`, `mcp` optional dep groups added; `[all]` extended; `bandit`, `types-croniter`, `types-qrcode` added to `dev`; `mypy` overrides extended to suppress errors in generated/third-party-heavy modules

#### Tests

- `tests/integrations/ms365/` — `test_auth.py`, `test_server.py`, `test_tools_calendar.py`, `test_tools_email.py`, `test_tools_onedrive.py`, `test_tools_other.py` (To Do, Teams, Contacts, OneNote)

---

## [0.7.4] — 2026-03-21

### MCP Architecture Overhaul — Sprint A0

**Replaces Sprints 8–9 from original roadmap. Delivers a layered architecture that supports both embedded and standalone MCP operation from a single shared core.**

#### MCPServiceCore — agent-independent coordination layer
- New `src/pincer/mcp/core.py`: `MCPServiceCore` owns tool/resource/prompt registries, sampling handler, security, and audit — with no dependency on Telegram, WhatsApp, or the agent loop
- Layered design: one shared core, two thin shells (embedded + standalone)
- `register_tool()`, `register_resource()`, `register_prompt()` API for both shells
- `start_clients()` / `stop_clients()` — consume external MCP servers
- `start_server()` / `stop_server()` — expose tools/resources/prompts to MCP clients

#### ApprovalBackend — pluggable approval interface
- New `src/pincer/mcp/approval.py`: `ApprovalBackend` ABC with four implementations
- `ChannelApprovalBackend` — routes to user's active messaging channel (Telegram, WhatsApp, Signal)
- `PolicyApprovalBackend` — auto-approve/deny from `[mcp.server.approval_policy]` config
- `CLIApprovalBackend` — interactive terminal prompt for foreground standalone mode
- `WebhookApprovalBackend` — POST to external approval service, poll for decision
- `classify_risk()` — keyword-based risk classification (read / write / destructive / unknown)

#### MCP Resources (spec section: resources/*)
- New `src/pincer/mcp/resources.py`: `ResourceRegistry` with `export_to(fmcp)` integration
- Embedded mode: live resources — `pincer://memory/recent`, `pincer://agent/status`
- Standalone mode: static or file-backed resources
- Resources exported to FastMCP via programmatic decorator registration

#### MCP Prompts (spec section: prompts/*)
- New `src/pincer/mcp/prompts.py`: `PromptRegistry` with `export_to(fmcp)` integration
- Built-in templates: `pincer_summarize`, `pincer_explain_code`
- Both embedded and standalone modes expose prompts to connected MCP clients

#### MCP Sampling (spec section: sampling/*)
- New `src/pincer/mcp/sampling.py`: `SamplingHandler` with daily budget enforcement
- `LLMBackend` ABC; `AgentLLMBackend` (embedded) and `StandaloneLLMBackend` (direct Anthropic API)
- Disabled by default; enable via `[mcp.server.sampling] enabled = true`
- Per-call cost estimation; daily budget cap; automatic reset at midnight UTC

#### EmbeddedMCPShell
- New `src/pincer/mcp/embedded.py`: thin shell wiring `MCPServiceCore` to the running agent
- Agent's built-in tools registered for server-side export (filtered by `expose_tools`)
- `pincer_ask_user` always registered — routes to user's active messaging channel
- Live resources registered from agent's memory and status
- Shares agent's `ToolRegistry` for MCP client imports (tools appear alongside built-ins)

#### StandaloneMCPShell + `pincer mcp serve`
- New `src/pincer/mcp/standalone.py`: run Pincer's MCP server without a full agent
- Use cases: secure MCP gateway, headless API, enterprise deployment, CI/CD
- `pincer mcp serve` CLI command (`--approval policy|cli|webhook`, `--host`, `--port`, `--verbose`)
- Built-in prompts registered automatically in standalone mode

#### Config extensions
- `MCPApprovalPolicyConfig` — per-risk-level approve/deny rules for standalone mode
- `MCPSamplingConfig` — sampling model, daily budget, max tokens
- `MCPServerExportConfig` extended: `approval_policy`, `sampling`, `webhook_url` fields
- New `[mcp.server.approval_policy]` and `[mcp.server.sampling]` TOML sections

#### PincerMCPServer extended (backward-compatible)
- `server.py` now accepts optional `ResourceRegistry`, `PromptRegistry`, `SamplingHandler`
- Resources and prompts registered on FastMCP instance during `start()`
- Existing constructor signature unchanged — all new params are optional

#### MCP Client (from previous sprints, consolidated here)
- `MCPClientManager` — parallel startup, 60s health loop, exponential-backoff reconnection
- `MCPClientSession` — wraps official `mcp>=1.8.0` SDK; stdio and streamable-http transports
- `MCPToolBridge` — converts MCP tool schemas to Pincer `ToolRegistry` entries at runtime
- `MCPSandbox` — sanitized env, isolated cwd, `RLIMIT_AS` memory limits
- `MCPAuditLogger` — connect/disconnect/tool-call events in Pincer audit log
- `MCPSecurityGate` — rate limiting, prompt injection detection, credential redaction
- `MCPAuthProvider` — OAuth 2.1 `client_credentials` with localhost bypass
- `MCPRegistryClient` — search and install MCP servers from public registries with AST scan
- Config: `pincer.toml` `[[mcp.servers]]` + `PINCER_MCP_SERVER_N_*` env vars
- CLI: `pincer mcp list/test/tools/call/status/serve/search/install/scan/uninstall`
- Doctor: 5 MCP security checks
- Optional dep: `uv pip install "pincer-agent[mcp]"` (mcp>=1.8.0)

#### Tests
- 94 new tests across 7 new test files (approval, resources, prompts, sampling, core, embedded, standalone)
- 879 total tests passing, 0 failures

## [0.7.3] — 2026-03-15

### Calendar Silent Failure Fix
- `calendar_create`: Strict response validation — success only when API returns both `id` and `htmlLink`; otherwise returns error
- IANA timezone handling: naive datetimes use `settings.timezone`; fixed offsets (e.g. UTC+01:00) fall back to IANA; ZoneInfo preserved
- Success message includes `calendar_id` when non-primary for auditability
- Tool description and system prompt updated so agent must include event link in response and pass errors through
- New tests: response validation, timezone edge cases, non-primary calendar

### Image Generation (Sprint 8)
- New `src/pincer/image/` module: `ImageProviderRouter`, `FalImageProvider`, `GeminiImageProvider`
- Primary provider: fal.ai `fal-ai/nano-banana-2` ($0.003/image); fallback: Gemini `gemini-2.5-flash-image` ($0.004/image)
- `generate_image` builtin tool registered in CLI when `PINCER_FAL_KEY` or `PINCER_GEMINI_API_KEY` is set
- Automatic provider fallback: fal → gemini on failure or unavailability
- New config fields: `image_provider`, `fal_key`, `fal_model`, `gemini_api_key`, `image_model_gemini`, `image_max_cost_per_request`, `image_daily_limit`
- `CostTracker.add_image_cost()` and `get_image_count_today()` — image costs tracked separately and included in daily spend
- New `image` optional dependency extra: `uv pip install "pincer-agent[image]"` (fal-client, google-genai)
- 21 new tests for image generation (router, providers, tool, cost tracking)

### Grok / xAI LLM Provider (Sprint 8)
- New `GrokProvider` — xAI Grok via OpenAI-compatible API (`api.x.ai`); supports streaming, function calling, vision
- New `_openai_common.py` — shared helpers for OpenAI-compatible providers (OpenAI, Grok)
- `GROK` added to `LLMProvider` enum; set `PINCER_DEFAULT_PROVIDER=grok` + `PINCER_GROK_API_KEY`
- Model mapping: claude-sonnet → grok-3, claude-haiku → grok-3-mini

### Signal Channel (Sprint 7.5)
- Signal channel: host-facing URL fix — use 127.0.0.1 instead of localhost for Safari compatibility (IPv6 resolution)
- Signal: pre-flight check before opening browser — verify signal-api is reachable; print clear error + docker command if not
- Signal: Docker build fix — set CI=true for pnpm to fix ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY
- Signal: latest-dev image, MODE=normal, configurable SIGNAL_API_IMAGE for better linking with recent Signal apps
- Signal: platform linux/amd64 for Apple Silicon (JVM ARM64 crash avoidance)
- Signal: device name "Pincer" for QR code pairing
- Security doctor: 3 new Signal checks (phone set, API local, allowlist); test updated for 31 checks
- Ruff: SIM105 (contextlib.suppress), F401 (unused imports), I001 (import order) fixes in signal channel and tests

## [0.7.2] — 2026-03-06

- Sprint 7: Voice Calling System — Twilio Voice + real-time AI agent integration
- Twilio infrastructure: inbound/outbound call handling, TwiML server, WebSocket media streams
- Dual engine architecture: ConversationRelay (Phase 1) and Media Streams (Phase 2)
- Speech pipeline: Deepgram streaming STT, ElevenLabs streaming TTS, audio codec conversion
- Barge-in controller: voice activity detection for interrupt handling (<500ms target)
- Call state machine: 11+ deterministic phases (greeting, intent, verify, execute, confirm, etc.)
- Voice-optimized system prompts for natural phone conversation
- Outbound calling: agent places calls on user's behalf via text command
- IVR navigation engine: DTMF tone generation, menu analysis, hold detection
- Warm transfer: conference bridge to patch user in after agent navigates to the right person
- Safety gates: mandatory verbal confirmation before all consequential actions
- PII guard: credit card, SSN, account number masking in transcripts and logs
- Recording compliance: jurisdiction-aware consent announcements (one-party/two-party)
- Call transcript logger with post-call summary generation
- Voice channel as first-class BaseChannel with cross-channel memory sharing
- Phone contacts skill: CRUD tools for managing contact directory
- Database migration 005: voice_calls, call_transcripts, call_actions, phone_contacts tables
- Identity resolver: phone_number column for caller ID matching
- Security doctor: 3 new voice-specific checks (Twilio credentials, webhook URL, recording consent)
- Audit logger: 4 new voice event types (call_start, call_end, tool_call, transfer)
- CLI integration: voice channel startup, outbound call tool registration, init wizard voice step
- 20+ new environment variables for voice configuration
- New `voice` optional dependency extra: twilio, websockets, deepgram-sdk, elevenlabs
- New `docs/Voice-calling-setup.md` — focused setup guide for voice calling
- README: fixed doc links to use actual docs folder paths; docs.pincer.dev → local docs

## [0.7.1] — 2026-03-04

- Feature work (PR #20), issue #17

## [0.7.0] — 2026-03-03

- Dashboard

## [0.6.x] — 2026-03-02

- Project documentation, README

## [0.6.0] — 2026-03-01

- Flattened repo structure (pincer/ → root)

## [0.5.x] — 2026-02-28

- Project docs, README for Pincer AI agent

## [0.5.0] — 2026-02-27

- Sprint 5: API server

## [0.4.x] — 2026-02-25

- Sprint 4: Discord voice (PyNaCl), neonize loop stop

## [0.4.0] — 2026-02-24

- Sprint 4: Discord channel, skills system, CLI polish

## [0.3.x] — 2026-02-23

- Sprint 3 completion, PROJECT_STRUCTURE docs

## [0.3.0] — 2026-02-22

- Sprint 3: WhatsApp channel, proactive agent, cross-channel identity

## [0.2.x] — 2026-02-21

- send_image tool, streaming truncation, agent reliability

## [0.2.0] — 2026-02-20

- Sprint 1+2: memory, browser, voice, streaming

## [0.1.0] — 2026-02-17

- Initial scaffold
