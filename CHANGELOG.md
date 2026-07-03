# Changelog

All notable changes to **Pincer** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

#### ms365-mcp: encrypted token cache at rest

- Per-identity Microsoft 365 token caches (`<MS365_TOKEN_CACHE_DIR>/<identity>_token_cache.json`) are now encrypted at rest with a shared Fernet key, auto-generated on first run at `MS365_TOKEN_ENCRYPTION_KEY_PATH` (default `~/.pincer/ms365_mcp/token_encryption.key`, mode `0600`), or overridden with `MS365_TOKEN_ENCRYPTION_KEY` (raw key, e.g. injected by a secrets manager). A cache file that fails to decrypt (wrong/rotated key, corruption, or a stray pre-encryption plaintext file) is treated as "no cached token" — the identity re-authenticates rather than crashing the server.

#### Multi-provider LLM architecture with random failover

- **`LLMRouter`** — a single construction entry point (`pincer.llm.router`) that also *is* a `BaseLLMProvider`. `cli.py` and `api/_deps.py` now call `LLMRouter().get_llm()` / `.get_summarizer()`; adding a provider touches only `pincer/llm/`.
- **1 + N failover** — configure a primary plus up to 3 failovers via `PINCER_FALLBACK_PROVIDERS`. On any `LLMError`/`LLMRateLimitError` (after a provider's own retries) the router falls over to a randomly-ordered failover, re-sampled per failure, and re-raises the primary's error if all fail. `stream()` fails over only before the first token. `LLMResponse.model` reflects the provider that actually served the response.
- **Compatible endpoints** — any OpenAI- or Anthropic-wire endpoint (Grok, Ollama, OpenRouter, LiteLLM, local proxies, gateways) is configured as a named compatible provider: `PINCER_OPENAI_COMPATIBLE_PROVIDER`/`_BASE_URL`/`_API_KEY` and the `PINCER_ANTHROPIC_COMPATIBLE_*` equivalents. A custom OpenAI-wire and a custom Anthropic-wire endpoint can run side by side.
- **Per-provider models** — each provider carries its own model id (`PINCER_ANTHROPIC_MODEL`, `PINCER_OPENAI_MODEL`, `PINCER_OPENAI_COMPATIBLE_MODEL`, `PINCER_ANTHROPIC_COMPATIBLE_MODEL`), each defaulting to `PINCER_DEFAULT_MODEL` when unset. This makes failover correct: e.g. an `anthropic` primary on `claude-sonnet-4-5` can fall over to an Ollama endpoint on `llama3.2` instead of sending the Claude model id to Ollama.
- **Cost attributed to the serving provider** — `LLMResponse.provider` carries the provider that actually served the response (the failover, when one is used), and cost/`is_free` are recorded against it rather than the configured primary. Fixes mis-billing on the failover path.
- **Fail-fast provider validation** — building the provider pool raises a clear error instead of a runtime 401/404 when a well-known `openai`/`anthropic` provider is missing its API key, or when an OpenAI-wire provider resolves to a `claude-*` model (the Anthropic default leaking onto a non-Anthropic endpoint — set that provider's `*_MODEL`).
- **Configurable Summarizer provider** — `PINCER_SUMMARY_PROVIDER` selects which pool provider summarizes (defaults to the primary).
- **Free-provider cost handling** — keyless endpoints (e.g. local Ollama) are billed at `$0` via `LLMRouter.is_free()`, replacing the hardcoded provider allowlist.

### Changed

- **Provider names are now free-form.** `openai`/`anthropic` are well-known (key only, built-in base URL); any other `PINCER_DEFAULT_PROVIDER` value must match a configured `*_COMPATIBLE_PROVIDER`.
- Provider classes renamed/generalised: `AnthropicProvider` → `AnthropicCompatibleProvider` (`llm/anthropic_common.py`), `_openai_common.py` → `llm/openai_common.py`. Both select well-known vs compatible config from the provider name.

### Removed

- **`grok`/`ollama` as named providers**, their dedicated env vars (`PINCER_GROK_*`, `PINCER_OLLAMA_BASE_URL`), and the claude→model maps. **Migration:** point them at a compatible endpoint instead, e.g. `PINCER_DEFAULT_PROVIDER=grok` + `PINCER_OPENAI_COMPATIBLE_PROVIDER=grok` + `PINCER_OPENAI_COMPATIBLE_BASE_URL=https://api.x.ai/v1` + `PINCER_DEFAULT_MODEL=grok-3` (Ollama: base URL `http://localhost:11434/v1`, model `llama3.2`, no key).
- Empty provider stub modules (`deep_seek_provider.py`, `google_gemini_provider.py`, `groq_provider.py`, `mistral_provider.py`).

#### Cross-channel identity overhaul

- **N-channel identity format** — `PINCER_IDENTITY_MAP` now supports any number of channels per identity: `john@telegram:johnDoe=whatsapp:491234567890=signal:491234567890`. Old two-channel format (`telegram:12345=whatsapp:491234567890`) is fully backward-compatible.
- **Named canonical IDs** — The optional `name@` prefix sets the `pincer_user_id` used for memory tagging (e.g. `user:john`). Named IDs survive DB rebuilds; hash-based IDs (`usr_abc123...`) auto-generated when no name is given.
- **New schema** — `identity_meta` (one row per user) + `channel_identities` (many-to-many) replaces the old monolithic `identity_map` table. Automatic migration on first boot preserves all existing rows.
- **Per-channel ID resolution** — `BaseChannel` gains `resolve_internal_user_id(identifier)` (default: passthrough). Channel implementations translate config values to real internal IDs at startup before storing them:
  - **Slack** — email address → Slack user ID via `users_lookupByEmail` (requires `users:read.email` scope)
  - **Telegram** — `@username` → numeric user ID via `bot.get_chat()`; numeric IDs passed through
  - **WhatsApp** — phone number → LID (Linked Device ID) when available
- **Conflict merging** — when `seed_from_config` finds multiple existing identities that the config maps together, they are automatically merged into one.

#### CLI memory backend detection

- **`pincer memory` subcommands** now detect and use the configured memory backend (`sqlite` or `mcp`) instead of always connecting to SQLite.
- Each command prints `Backend: sqlite  path=…` or `Backend: mcp  server=…` so the active engine is always visible.
- When `PINCER_MEMORY_BACKEND=mcp`, commands connect directly to the configured MCP server (same as the agent's runtime connection). Clear error output if the server is missing from `pincer.toml` or unreachable.
- `memory stats` with MCP backend computes user/category breakdown from listed records (client-side), since raw SQL is unavailable.

#### First-session onboarding

- **One-question onboarding flow** on a brand-new session: the bot answers the user's first real message normally, then appends a single warm question — *"By the way — what's your name, and what will you mainly use me for? Feel free to also mention your preferred language."*
- The user's next message — whatever it is — closes the gate (`session.metadata["onboarding_complete"] = "true"`); a low-token LLM extraction call pulls `name`, `use_case`, and `language` out of the reply.
- Captured fields are written to **both** `session.metadata` (structured, fast lookup) and `MemoryStore` as a single `profile` category entry (semantic, auto-surfaces via the existing memory injection on later turns).
- Works identically on **all channels** (Telegram, WhatsApp, Signal, Slack, Discord, web, voice) because it lives at the agent layer beneath them.
- **Backward compatible** — pre-existing users (any session with prior messages) never see the prompt; no migration needed.
- **Idempotent under retries** — the `onboarding_prompt_sent` flag prevents the question being appended twice, and the gate always closes even on extraction-call failure so the user never sees the prompt a second time.
- New module `src/pincer/core/onboarding.py` centralizes the question text, the one-turn followup instruction, the always-on profile-usage instruction, and the JSON extraction prompt.
- New `MemoryStore.add_profile()` upserts the `profile` entry (deletes any prior one for the same user before inserting fresh).
- 10 new unit tests in `tests/test_onboarding.py` covering the full state machine + edge cases (gibberish reply, extraction failure, retry idempotency, pre-existing session no-op, streaming path).

---

## [0.8.0] — 2026-05-09

### Highlights

- **Skills deprecated** — Skills are disabled when any `[[mcp.servers]]` entry is configured in `pincer.toml`. They continue to load and run unchanged through all 0.8.x releases when MCP is not configured. **Removal in 0.9.0.**
- **MCP servers as Docker Compose services** — new `docker-compose` profiles (`stdio`, `http`) run each MCP server as an isolated container, independent of the Pincer agent process.
- **MCP stdio + HTTP transport** — MCP servers can now run as persistent HTTP services or per-request stdio subprocesses; `make up PROFILE=http` switches modes.
- **Three bundled MCP servers** — `pomodoro-mcp`, `pincer-mcps` (translate / summarize / stocks / weather / news), `sqlite-vec-memory-mcp` (semantic memory via sqlite-vec + ONNX embeddings) ship in the `src/` workspace, installable as uv workspace packages.
- **OpenTelemetry telemetry** — optional Uptrace integration for distributed traces and spans across the agent loop and MCP server calls.
- **Dashboard data improvements** — fixed data fetch, format errors, and endpoint consistency.

### Added

#### Bundled MCP servers (`src/` workspace)

| Package | What it does |
|---------|--------------|
| `pomodoro-mcp` | Pomodoro timer — sessions, stats, history |
| `pincer-mcps` | Translation, summarization, stock prices, weather, news (5 servers) |
| `sqlite-vec-memory-mcp` | Semantic memory via sqlite-vec + ONNX embeddings |

Each server supports both `stdio` (default) and `HTTP` transport and ships its own `Dockerfile`.

#### Docker Compose MCP integration

- `docker-compose.yml` extended with `stdio` and `http` profiles for running MCP servers as containers.
- `make up PROFILE=stdio` (default) / `make up PROFILE=http` switches transport mode.
- MCP servers run as independent services — they do not depend on the Pincer container lifecycle.

#### OpenTelemetry telemetry

- Optional `[telemetry]` dep group: `pincer-telemetry @ git+https://github.com/pincerhq/pincer-telemetry`
- Uptrace integration: distributed traces for agent loop iterations, LLM calls, MCP tool invocations.
- Enable via `PINCER_TELEMETRY_ENABLED=true` + `PINCER_UPTRACE_DSN=<dsn>`.
- Install: `uv pip install "pincer-agent[telemetry]"`.

#### MCP server arg env-var interpolation (PR #109, contributor: @looooown2006)

- `pincer.toml` `[[mcp.servers]] args` values now support `${ENV_VAR}` references.
- Env vars are interpolated at server start time from the process environment.

#### `pincer.toml` local config override (PR #105)

- `pincer.local.toml` is loaded after `pincer.toml` and its values take precedence.
- `make setup` creates `pincer.local.toml` from the template automatically.
- Useful for per-developer overrides without touching the committed `pincer.toml`.

#### Documentation

- `docs/core-components/mcp-servers.md` — new guide: stdio vs HTTP transport, Docker Compose deployment, sandboxing.
- `docs/changelog/v0.8.0.md` — full migration guide from Skills to MCP with before/after code examples.
- `docs/guides/mcp-guide.md` — updated with local-config and env-var interpolation sections.
- `mkdocs.yml` added; `docs/` restructured for mkdocs build.

### Changed

- **Skills disabled when MCP configured** — skill loading is skipped at startup when any `[[mcp.servers]]` entry is present and MCP is enabled. Skills and MCP are mutually exclusive starting in 0.8.0.
- **`pincer.toml`** — updated with MCP server transport profiles and Docker Compose service names.
- `pyproject.toml` version bumped `0.7.6 → 0.8.0`; `telemetry` optional dep group added; `[all]` extended to include `telemetry`.

### Fixed

- Dashboard: fixed data fetch endpoint returning stale data; corrected format errors in API response serialization (PRs #111, #112, #113).

### Deprecated

- **Skills system** — deprecated in 0.8.0, removed in 0.9.0. See [migration guide](docs/changelog/v0.8.0.md) for converting skills to FastMCP servers.

---



## [0.7.6] — 2026-04-19

### Highlights

- **Slack Native integration — 71 tools** across messages, channels, users, files, reactions, pins/bookmarks/reminders/search
- **MCP OAuth 2.0 Authorization Server** — expose Pincer tools to remote MCP clients with PKCE, JWT, scopes, `/authorize` · `/token` · `/introspect` · `/revoke`, RFC 8414 metadata
- **Google Workspace Meet v2 REST surface** — Google Workspace tool count corrected 85 → **113** (spaces, conference records, participants, sessions, recordings, transcripts, smart notes, event subscriptions)
- **Tools Catalog** — authoritative inventory: **304 native tools** (24 core + 27 bundled-skill functions + 113 Google Workspace + 69 Microsoft 365 + 71 Slack native) plus unlimited via MCP clients
- **Documentation refresh** — README, `docs/PROJECT_STRUCTURE.md`, `docs/TOOLS_CATALOG.md` updated to reflect the 600+ tool surface

### Added

#### Slack Native Integration — `src/pincer/integrations/slack/`

Separate from the Slack *channel* (inbound messaging). This is the native **tool surface** Pincer exposes to the agent when `PINCER_SLACK_BOT_TOKEN` is set. Search tools additionally require `PINCER_SLACK_USER_TOKEN`.

- `tools_messages.py` — **18 tools**: channel history, threads, post, schedule, Block Kit, DM, broadcast replies, summarize channel
- `tools_channels.py` — **16 tools**: list / create / archive / rename / set topic / set purpose / join / leave / invite / kick / open DM / open group DM
- `tools_users.py` — **10 tools**: list users, lookup by email, presence, user groups CRUD
- `tools_files.py` — **10 tools**: list / upload / download / share / delete + remote-file references
- `tools_misc.py` — **12 tools**: pins, bookmarks, reminders, search messages + files (requires user token)
- `tools_reactions.py` — **5 tools**: add / remove reactions, list emoji

New optional dependency: `uv pip install "pincer-agent[slack]"` (pulls `slack-sdk>=3.27`, `slack-bolt>=1.18`).

#### MCP OAuth 2.0 — `src/pincer/mcp/auth/`

- `endpoints.py` — FastAPI routes: `/authorize`, `/token`, `/introspect`, `/revoke`
- `metadata.py` — RFC 8414 authorization server metadata
- `pkce.py` — PKCE verifier/challenge helpers
- `tokens.py` — JWT issuance + validation (PyJWT)
- `scopes.py` — scope definitions + validation
- `middleware.py` — Bearer-token validation middleware for protected routes
- `client_flow.py` — OAuth **client** (PKCE, token refresh, redirect handling) so Pincer can also consume OAuth-protected MCP servers
- `consent.py` — user consent UI/flow
- `clients.py` · `token_store.py` — SQLite persistence for registered clients and issued tokens
- `errors.py` · `models.py` — typed OAuth error hierarchy + Pydantic models

`PyJWT[crypto]>=2.8.0` is now part of the `[mcp]` optional dependency group.

#### Google Workspace Meet v2 — 27 new tools

Extends Sprint 9's Google Workspace surface from 85 → **113** tools. Added surfaces:

- Spaces: create / get / update / end / moderation / artifacts / members
- Conference records + participants + sessions
- Recordings: list / get / download / status
- Transcripts + entries + summarization
- Smart notes
- Event subscriptions

#### Documentation

- `docs/TOOLS_CATALOG.md` — new, authoritative catalog of every tool Pincer can call
- `README.md` — refreshed headline ("600+ tools"), design trade-offs table, roadmap, architecture diagram, CLI reference, and doc index
- `docs/PROJECT_STRUCTURE.md` — sprint history updated (Sprint 9.5 Google Meet, Sprint 11 Slack Native, Sprint 12 MCP OAuth, Sprint 13 Tools Catalog); architecture diagram reflects 304 native tools

### Changed

- Google Workspace total corrected **85 → 113** (Meet v2 REST surface was added in Sprint 9.5 but undercounted in prior docs)
- MCP `[mcp]` extra now also pulls `PyJWT[crypto]>=2.8.0`
- `pyproject.toml` version bumped `0.7.5 → 0.7.6`

### Fixed

- Slack Native: CI stabilisation across several iterations (see commits `3ad6dd6` → `f9dc7d3`)

### Security

- MCP OAuth 2.0 server uses PKCE for public clients, short-lived JWT access tokens, and scope-based access control
- Bearer-token middleware wraps protected MCP routes
- Issued tokens persisted in SQLite with OS-keyring-backed signing keys

---

## [0.7.5] — 2026-04-06

### Added — Microsoft 365 Integration (69 tools)

New module `src/pincer/integrations/ms365/` with native Microsoft Graph REST coverage for:

- **Outlook email** (17 tools — `outlook__*`)
- **Calendar** (12 tools — `calendar__*`)
- **OneDrive** (14 tools — `onedrive__*`)
- **Microsoft To Do** (8 tools — `ms_todo__*`)
- **Teams** (7 tools — `teams__*`)
- **Contacts** (6 tools — `contacts__*`)
- **OneNote** (5 tools — `onenote__*`)

Auth uses **MSAL** device-code or interactive browser flow; token cached at `~/.pincer/ms365_token_cache.json` (`0600`) with automatic refresh. Rate-limited `httpx` async client with 429 + `Retry-After` handling and `@odata.nextLink` pagination.

### Added — CLI

- `pincer setup-ms365` — one-time device-code auth; writes `[integrations.ms365]` to `pincer.toml`

### Changed

- MS365 tools auto-registered at startup when `[integrations.ms365]` is configured and a cached token exists
- Grok/xAI provider wired into `_run_agent()` when `settings.default_provider == "grok"`
- `python-multipart>=0.0.9` added to core deps
- New optional extras: `voice`, `image`, `mcp`; `[all]` extended
- `bandit`, `types-croniter`, `types-qrcode` added to `dev`

---

## [0.7.4] — 2026-03-21

### Added — MCP Architecture Overhaul (Sprint A0)

Replaces Sprints 8–9 from the original roadmap. Delivers a layered architecture that supports both **embedded** and **standalone** MCP operation from a single shared core.

- **`MCPServiceCore`** — agent-independent coordination layer (tools, resources, prompts, sampling, security, audit)
- **`ApprovalBackend`** — pluggable approval interface with `ChannelApprovalBackend`, `PolicyApprovalBackend`, `CLIApprovalBackend`, `WebhookApprovalBackend`; `classify_risk()` for read / write / destructive / unknown
- **MCP Resources** — `pincer://memory/recent`, `pincer://agent/status` live resources (embedded); static/file-backed (standalone)
- **MCP Prompts** — built-in `pincer_summarize`, `pincer_explain_code`
- **MCP Sampling** — daily-budget-enforced LLM backend, disabled by default
- **`EmbeddedMCPShell`** — agent tools exported to MCP clients (filtered by `expose_tools`); `pincer_ask_user` always available
- **`StandaloneMCPShell` + `pincer mcp serve`** — run Pincer's MCP server without a full agent
- **MCP Client** consolidated: sandboxing (`RLIMIT_AS`), audit log, rate limiting, prompt-injection detection, credential redaction, OAuth 2.1 `client_credentials`, registry search/install with AST scan
- Config: `[mcp.server.approval_policy]`, `[mcp.server.sampling]`, per-server env/argv

94 new tests; 879 total tests passing.

---

## [0.7.3] — 2026-03-15

### Added — Image Generation (Sprint 8)

- New `src/pincer/image/` module: `ImageProviderRouter`, `FalImageProvider`, `GeminiImageProvider`
- Primary: fal.ai `fal-ai/nano-banana-2` ($0.003/image); fallback: Gemini `gemini-2.5-flash-image` ($0.004/image)
- `generate_image` builtin tool registered when `PINCER_FAL_KEY` or `PINCER_GEMINI_API_KEY` is set
- Automatic provider fallback: fal → gemini on failure
- `CostTracker.add_image_cost()` / `get_image_count_today()` — image costs tracked separately and included in daily spend
- New `image` optional extra

### Added — Grok / xAI LLM Provider (Sprint 8)

- `GrokProvider` via OpenAI-compatible API (`api.x.ai`); streaming, function calling, vision
- Shared `_openai_common.py` for OpenAI-compatible providers
- Model mapping: claude-sonnet → grok-3, claude-haiku → grok-3-mini

### Fixed — Calendar Silent Failure

- `calendar_create`: strict response validation — success only when API returns both `id` and `htmlLink`; otherwise returns error
- IANA timezone handling: naive datetimes use `settings.timezone`; fixed offsets fall back to IANA; `ZoneInfo` preserved
- Success message includes `calendar_id` when non-primary
- Agent system prompt updated so errors pass through

### Added — Signal Channel polish (Sprint 7.5)

- Host-facing URL uses `127.0.0.1` (Safari IPv6 fix)
- Pre-flight Docker reachability check with clear error + command
- Docker build: `CI=true` for pnpm; `linux/amd64` on Apple Silicon; configurable `SIGNAL_API_IMAGE`
- Device name "Pincer" for pairing QR
- 3 new Signal checks in `pincer doctor`

---

## [0.7.2] — 2026-03-06

### Added — Voice Calling System (Sprint 7)

- Twilio Voice infrastructure: inbound/outbound, TwiML server, WebSocket media streams
- Dual engine: ConversationRelay (Phase 1) + Media Streams (Phase 2)
- Streaming STT (Deepgram), streaming TTS (ElevenLabs), codec conversion
- Barge-in VAD controller (<500ms target)
- Call state machine: 11+ deterministic phases
- Outbound calling tool; IVR navigation (DTMF, menu analysis, hold detection); warm transfer via conference bridge
- Safety gates, PII guard (credit card/SSN/account masking), recording consent (jurisdiction-aware)
- Migration 005: `voice_calls`, `call_transcripts`, `call_actions`, `phone_contacts`
- 20+ new env vars; new `voice` optional extra (`twilio`, `websockets`, `deepgram-sdk`, `elevenlabs`)

---

## [0.7.1] — 2026-03-04

- Feature work (PR #20); issue #17

## [0.7.0] — 2026-03-03

- Dashboard

## [0.6.x] — 2026-03-02

- Project documentation, README

## [0.6.0] — 2026-03-01

- Flattened repo structure (`pincer/` → root)

## [0.5.x] — 2026-02-28

- Project docs, README for Pincer AI agent

## [0.5.0] — 2026-02-27

- Sprint 5: API server

## [0.4.x] — 2026-02-25

- Sprint 4: Discord voice (PyNaCl), neonize loop stop

## [0.4.0] — 2026-02-24

- Sprint 4: Discord channel, skills system, CLI polish

## [0.3.x] — 2026-02-23

- Sprint 3 completion, `PROJECT_STRUCTURE` docs

## [0.3.0] — 2026-02-22

- Sprint 3: WhatsApp channel, proactive agent, cross-channel identity

## [0.2.x] — 2026-02-21

- `send_image` tool, streaming truncation, agent reliability

## [0.2.0] — 2026-02-20

- Sprint 1+2: memory, browser, voice, streaming

## [0.1.0] — 2026-02-17

- Initial scaffold

[0.8.0]: https://github.com/pincerhq/pincer/releases/tag/v0.8.0
[0.7.6]: https://github.com/pincerhq/pincer/releases/tag/v0.7.6
[0.7.5]: https://github.com/pincerhq/pincer/releases/tag/v0.7.5
[0.7.4]: https://github.com/pincerhq/pincer/releases/tag/v0.7.4
[0.7.3]: https://github.com/pincerhq/pincer/releases/tag/v0.7.3
[0.7.2]: https://github.com/pincerhq/pincer/releases/tag/v0.7.2
[0.7.1]: https://github.com/pincerhq/pincer/releases/tag/v0.7.1
[0.7.0]: https://github.com/pincerhq/pincer/releases/tag/v0.7.0
