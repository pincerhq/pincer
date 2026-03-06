# Changelog

All notable changes to Pincer. Format: [Version] — Date.

---

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
