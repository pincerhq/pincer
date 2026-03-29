---
name: Google Workspace Sprint
description: Native Google Workspace MCP integration with 85 tools, completed on branch google_workspace
type: project
---

All 85 Google Workspace tools implemented and tested (branch: google_workspace).

**Why:** Replace third-party wrapper dependency with Pincer-owned first-party integration for reliability and control.

**Files:**
- `src/pincer/integrations/google/` — full implementation
  - `auth.py` — GoogleAuth (InstalledAppFlow, token cache at `~/.pincer/google_workspace_token.json`)
  - `service_factory.py` — GoogleServiceFactory (30-min service cache)
  - `tools_gmail.py` (19), `tools_calendar.py` (12), `tools_drive.py` (15), `tools_docs.py` (8), `tools_sheets.py` (10), `tools_slides.py` (6), `tools_tasks.py` (8), `tools_contacts.py` (7)
  - `server.py` — standalone FastMCP server (stdio or HTTP, port 18900)
- `tests/integrations/google/` — 131 tests, all passing

**CLI:** `pincer setup-google` for one-time OAuth consent. Credentials at `~/.pincer/google_credentials.json`.

**Tool registration:** Auto-registers in `_run_agent()` if `google_credentials.json` + `google_workspace_token.json` exist.

**How to apply:** When working on Google tools, all tools use `google__` prefix and `asyncio.to_thread()` for sync API calls. Write tools have `require_approval=True`. Each service file has a `register_*_tools(registry, factory) → int` function.
