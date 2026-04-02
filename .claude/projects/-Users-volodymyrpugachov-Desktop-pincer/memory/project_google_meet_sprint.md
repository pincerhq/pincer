---
name: Google Meet Sprint — Sprint 8 Meet Extension
description: 27 Google Meet tools added to the Google Workspace MCP (branch: google_workspace)
type: project
---

Google Meet integration implemented on branch `google_workspace`, extending 85 → 112 tools.

**Why:** Full meeting lifecycle management via natural language on any Pincer channel.

**New files:**
- `src/pincer/integrations/google/tools_meet.py` — 27 Meet tools (ToolRegistry pattern)
- `src/pincer/integrations/google/meet_events.py` — MeetEventSubscriber (Pub/Sub + polling)
- `src/pincer/integrations/google/meet_artifacts.py` — MeetArtifactProcessor helpers
- `tests/integrations/google/test_tools_meet.py` — 46 tests
- `tests/integrations/google/test_meet_events.py` — 8 tests

**Modified files:**
- `auth.py` — Meet + Workspace Events scopes added to ALL_SCOPES and SERVICE_SCOPES["meet"]
- `service_factory.py` — `"meet": ("meet", "v2")` added to `_API_VERSIONS`
- `__init__.py` — `register_meet_tools` added to `register_all_tools()`; docstring updated to 112
- `config.py` — "meet" added to `_DEFAULT_SERVICES`
- `tools_calendar.py` — `google__create_event` now accepts `add_meet_link: bool` and `meet_link: str`
- `tests/integrations/google/conftest.py` — `mock_meet_service` fixture added
- `tests/integrations/google/test_server.py` — count assertion updated 85 → 112

**Implementation notes:**
- Uses `googleapiclient.discovery.build("meet", "v2")` via service_factory (NOT google-apps-meet SDK)
- Members API + moderation are Developer Preview — wrapped in try/except with graceful fallback
- No new PyPI deps needed
- Event subscriptions: Pub/Sub if `pubsub_topic` configured, else 60s polling fallback
- `google__summarize_meet_transcript` compiles full transcript + asks LLM to summarize in same turn
- Users must re-authorize (`pincer setup google`) after adding "meet" to services list

**How to apply:** When asked to create/modify Meet tools, events, or artifacts, read these files first.
