# Changelog

All notable changes to Pincer are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.7.0] — 2026-02-28

### Added
- 📞 **Voice calling** via Twilio — make and receive phone calls through your agent
- Speech-to-text support (Deepgram, Whisper, Google)
- Text-to-speech support (ElevenLabs, OpenAI, Google)
- Call state machine with barge-in detection
- Recording consent and PII masking for compliance
- Outbound call approval flow (agent asks permission before dialing)
- Call transcript storage and retrieval
- Voice cost tracking integrated with existing budget system

## [0.6.0] — 2026-02-26

### Added
- 🚀 **Public launch release**
- Hero demo recordings and GIF
- "You Could've Built Pincer" blog post
- Comprehensive documentation (quickstart, architecture, skills guide, security)
- Migration guide from OpenClaw
- Pre-configured deploy buttons (Railway, DigitalOcean, Render)

### Fixed
- Various bug fixes from beta tester feedback

## [0.5.0] — 2026-02-21

### Added
- 🔒 **Security layer** — user allowlist, tool approval, audit logging
- Security Doctor — 25+ automated health checks via `pincer doctor`
- Rate limiting (per-user and global)
- 💰 **Cost controls** — daily/session/tool budgets with auto-model-downgrade
- Cost dashboard API endpoints
- 🐳 **Docker** — multi-stage Dockerfile, docker-compose.yml
- One-click deploy templates (Railway, DigitalOcean, Render)
- 🖥️ **Web dashboard** — FastAPI + HTMX, status overview, cost charts, skill management

## [0.4.0] — 2026-02-14

### Added
- 🎮 **Discord channel** — full Discord bot integration
- 🧩 **Skill system** — skill loader, sandbox, security scanner, skill signing
- 10 bundled skills (weather, habit tracker, stock prices, etc.)
- CLI: `pincer skills install/remove/list/scan/verify`
- Skill YAML manifest format

## [0.3.0] — 2026-02-07

### Added
- 💬 **WhatsApp channel** via neonize (multi-device protocol)
- 📧 Email tools — Gmail read/send via OAuth
- 📅 Calendar tools — Google Calendar read/create
- ⏰ Scheduler — cron jobs, morning briefings, reminders
- `pincer google setup` OAuth wizard

## [0.2.0] — 2026-01-31

### Added
- 🧠 **Memory system** — SQLite + FTS5 full-text search
- Entity extraction (people, places, projects)
- Auto-summarization of old conversations
- 🌐 Browser tool (Playwright)
- 🐍 Python code execution tool
- 🎤 Voice message transcription (Telegram)
- Streaming LLM responses with typing indicators

## [0.1.0] — 2026-01-24

### Added
- 🦀 **Initial release**
- ReAct agent loop with tool use
- LLM providers: Anthropic (Claude), OpenAI, Ollama, OpenRouter
- Telegram channel (aiogram 3.x)
- Core tools: web_search, shell_exec, file_read, file_write
- CLI: `pincer init`, `pincer run`
- Pydantic-based configuration from `.env`
- Cost tracking with per-model pricing

---

[0.7.0]: https://github.com/pincerhq/pincer/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/pincerhq/pincer/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/pincerhq/pincer/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/pincerhq/pincer/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/pincerhq/pincer/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/pincerhq/pincer/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/pincerhq/pincer/releases/tag/v0.1.0