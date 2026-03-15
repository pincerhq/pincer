# Changelog

All notable changes to Pincer. Format: [Version] — Date.

---

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
