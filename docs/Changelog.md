# Changelog

All notable changes to Pincer. Format: [Version] — Date.

---

## [0.8.0] — 2026-03-26

### Google Workspace Native MCP Server

**Replaces the third-party wrapper dependency with a Pincer-owned, first-party Google Workspace integration providing 85 tools across 8 services.**

#### 85 tools across 8 services

| Service | Tools | Count |
|---------|-------|------:|
| Gmail | `search_messages`, `get_message`, `get_thread`, `list_labels`, `send_message`, `reply_to_message`, `create_draft`, `send_draft`, `trash_message`, `mark_as_read`, `mark_as_unread`, `add_label`, `remove_label`, `create_label`, `delete_label`, `get_attachment`, `forward_message`, `batch_trash`, `batch_mark_read` | 19 |
| Calendar | `list_calendars`, `list_events`, `get_event`, `create_event`, `update_event`, `delete_event`, `search_events`, `list_upcoming_events`, `add_attendee`, `respond_to_event`, `create_recurring_event`, `move_event` | 12 |
| Drive | `list_drive_files`, `search_drive_files`, `get_file_metadata`, `download_file`, `export_google_doc`, `list_shared_drives`, `get_file_permissions`, `list_recent_files`, `upload_file`, `create_folder`, `move_file`, `rename_file`, `copy_file`, `trash_file`, `share_file` | 15 |
| Docs | `get_doc_content`, `create_doc`, `append_to_doc`, `insert_text`, `find_replace`, `get_doc_outline`, `export_doc`, `add_comment` | 8 |
| Sheets | `get_sheet_values`, `update_sheet_values`, `append_sheet_values`, `clear_sheet_range`, `create_spreadsheet`, `add_sheet`, `delete_sheet`, `get_spreadsheet_info`, `format_cells`, `list_sheets` | 10 |
| Slides | `list_slides`, `get_slide_content`, `create_presentation`, `add_slide`, `update_slide_text`, `add_image_to_slide` | 6 |
| Tasks | `list_task_lists`, `list_tasks`, `get_task`, `create_task`, `update_task`, `complete_task`, `delete_task`, `create_task_list` | 8 |
| Contacts | `list_contacts`, `search_contacts`, `get_contact`, `create_contact`, `update_contact`, `delete_contact`, `list_contact_groups` | 7 |

#### New modules (`src/pincer/integrations/google/`)

- `auth.py` — `GoogleAuth` (InstalledAppFlow; token cache at `~/.pincer/google_workspace_token.json` with `chmod 0o600`)
- `service_factory.py` — `GoogleServiceFactory` (30-min service cache per API; `asyncio.to_thread()` for all sync `.execute()` calls)
- `quota.py` — `with_backoff()` exponential retry on HTTP 429/503 and `rateLimitExceeded` with jitter
- `pagination.py` — `collect_pages()` handles `nextPageToken` iteration up to `max_pages`
- `tools_gmail.py` (19 tools), `tools_calendar.py` (12), `tools_drive.py` (15), `tools_docs.py` (8), `tools_sheets.py` (10), `tools_slides.py` (6), `tools_tasks.py` (8), `tools_contacts.py` (7)
- `server.py` — standalone FastMCP server (`stdio` or HTTP on port 18900)
- `__init__.py` — public API: `get_google_factory()` + `register_all_tools(registry, factory) → 85`

#### CLI

- `pincer setup-google` — one-time OAuth consent flow; opens browser, saves token, reports 85 tools enabled
- Auto-registration in `_run_agent()` when `google_credentials.json` + `google_workspace_token.json` exist

#### Tool design

- All tools use `google__` prefix
- All write/destructive tools have `require_approval=True`
- All sync Google API calls wrapped in `asyncio.to_thread()`
- No new pip dependencies — `google-api-python-client`, `google-auth-oauthlib`, `google-auth` were already in core deps

#### Tests

- 131 new tests in `tests/integrations/google/` — all mocked, 0 real API calls
- 1,016 total tests passing, 0 failures

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
