# Changelog

All notable changes to Pincer. Format: [Version] — Date.

---

<<<<<<< HEAD
## [0.7.4.2] — 2026-03-26

### MCP OAuth 2.1 — Full Authorization Server

Replaces the `MCPAuthProvider` HS256 stub with a complete, spec-compliant OAuth 2.1 authorization server and matching client flow. Backward-compatible: existing code that passes `auth_provider=` to `MCPAuthMiddleware` continues to work unchanged.

#### Authorization server (`src/pincer/mcp/auth/`)

New package — 14 modules, ~2 000 LOC:

- **`tokens.py`** — `TokenService`: Ed25519 JWT access tokens (RFC 9068), opaque refresh tokens, key generation at `~/.pincer/mcp_signing_key.pem` (chmod 0o600), JWK Set endpoint
- **`clients.py`** — `ClientRegistry`: in-memory client store; static clients loaded from config; wildcard port matching (`http://127.0.0.1:*/callback`) for native apps
- **`endpoints.py`** — `mount_oauth_endpoints()`: registers all 8 OAuth routes on any Starlette app; PKCE authorization flow with HTML consent page; 10-minute pending request TTL
- **`middleware.py`** — `MCPAuthMiddleware`: `Bearer` token enforcement on all non-exempt paths; `WWW-Authenticate` header pointing to RFC 9728 metadata; localhost bypass; backward-compat `auth_provider=` pass-through
- **`pkce.py`** — PKCE helpers: S256 code verifier/challenge generation and verification (plain rejected)
- **`scopes.py`** — 9 scopes: `tools:read`, `tools:execute`, `tools:all`, `resources:read`, `prompts:read`, `admin:ask_user`, `offline_access`, plus `tools:all` expansion logic
- **`models.py`** — Frozen dataclasses: `OAuthClient`, `AuthorizationCode`, `RefreshTokenData`, `TokenClaims`, `AuthServerConfig`
- **`errors.py`** — `OAuthError` with `status_code` and `to_dict()`; factory functions for all RFC 6749 error codes
- **`metadata.py`** — RFC 8414 authorization server metadata + RFC 9728 protected resource metadata builders
- **`consent.py`** — Self-contained HTML consent page; plain-text channel consent formatter
- **`token_store.py`** — `TokenStore`: keyring → `~/.pincer/mcp_tokens.json` → memory; 60-second expiry buffer; `clear()` and `clear_all()`
- **`client_flow.py`** — `MCPOAuthClient`: full PKCE flow with local callback server (`asyncio.start_server`), metadata discovery, DCR, token refresh, client_credentials fallback

#### Specs implemented

| RFC | Description |
|-----|-------------|
| RFC 7636 | PKCE (S256 only; plain rejected) |
| RFC 7591 | Dynamic Client Registration |
| RFC 7009 | Token Revocation |
| RFC 8414 | Authorization Server Metadata |
| RFC 8707 | Resource Indicators (audience binding) |
| RFC 9068 | JWT Access Tokens |
| RFC 9728 | Protected Resource Metadata |
| OAuth 2.1 | PKCE mandatory, no implicit flow |

#### Config

```toml
[mcp.server.auth]
enabled          = true
localhost_bypass = true     # Localhost clients skip auth (default: true)
token_lifetime   = 3600
refresh_token_lifetime = 86400
dcr_enabled      = true
dcr_max_clients  = 20
default_scopes   = "tools:read resources:read"
max_scopes       = "tools:all resources:read prompts:read admin:ask_user offline_access"
```

#### Client config (HTTP MCP servers)

```toml
[[mcp.servers]]
transport          = "streamable-http"
url                = "https://example.com/mcp"
oauth_enabled      = true
oauth_client_id    = ""   # Optional — DCR used if omitted
oauth_client_secret = ""  # Optional — for client_credentials grant
```

#### Dependencies

- `PyJWT[crypto]>=2.8.0` — Ed25519 JWT signing (was `PyJWT>=2.8.0`)
- `cryptography>=42` — Ed25519 key generation (already a transitive dep)

#### Tests

127 tests in `tests/mcp/auth/test_all.py` — **93.9% line coverage** on `src/pincer/mcp/auth/`.
=======
## [0.7.5] — 2026-03-26

### MCP OAuth 2.0 Authorization Server

- Full OAuth 2.0 Authorization Server built into the MCP server (`src/pincer/mcp/auth/`)
- PKCE support (RFC 7636) for public clients
- Token endpoints: `/authorize`, `/token`, `/introspect`, `/revoke`
- RFC 8414 server metadata endpoint (`/.well-known/oauth-authorization-server`)
- Bearer token validation middleware for all MCP server routes
- Registered client storage and user consent flow
- JWT issuance and validation via `PyJWT[crypto]`
- SQLite-backed token persistence
- OAuth client flow integration in MCP client (`client.py`) — automatic token attachment
- OAuth middleware wiring in MCP server (`server.py`)
- New `oauth_*` config fields in `src/pincer/mcp/config.py`
- Tests: `tests/mcp/auth/test_all.py` — comprehensive OAuth flow coverage
>>>>>>> 1f33b37 (Docs changes to 0.7.5)

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
