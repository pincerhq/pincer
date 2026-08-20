# 📚 API Reference

Pincer exposes a REST API via its dashboard server for monitoring and management.

---

## Base URL

```
http://localhost:8080/api
```

All endpoints require the dashboard auth token:

```
Authorization: Bearer <PINCER_DASHBOARD_TOKEN>
```

---

## Health

### `GET /health`

No auth required. Returns agent status.

```json
{
  "status": "ok",
  "version": "0.7.0",
  "uptime_seconds": 86400,
  "channels": {
    "telegram": "connected",
    "whatsapp": "connected",
    "discord": "disconnected"
  },
  "budget": {
    "daily_limit": 5.0,
    "spent_today": 1.58,
    "remaining": 3.42
  }
}
```

---

## Costs

### `GET /api/costs/today`

Today's spending breakdown.

```json
{
  "date": "2026-02-26",
  "total_usd": 1.58,
  "by_model": {
    "claude-sonnet-4-20250514": 1.20,
    "claude-haiku-4-5-20251001": 0.38
  },
  "by_tool": {
    "shell_exec": 0.45,
    "gmail_read": 0.32,
    "python_exec": 0.81
  },
  "by_channel": {
    "telegram": 1.10,
    "whatsapp": 0.48
  },
  "total_tokens": {
    "input": 45200,
    "output": 12800
  }
}
```

### `GET /api/costs/history?days=30`

Daily spending for the last N days.

```json
{
  "days": [
    {"date": "2026-02-26", "total_usd": 1.58},
    {"date": "2026-02-25", "total_usd": 3.21},
    ...
  ]
}
```

### `GET /api/costs/by-tool?period=7d`

Spending breakdown by tool.

### `GET /api/costs/by-model?period=7d`

Spending breakdown by model.

---

## Conversations

The conversations API is backed by the active memory backend (SQLite or MCP,
configured via `PINCER_MEMORY_BACKEND`). Records in this endpoint are scoped to
the `exchange` category — only exchanges stored by the agent during real
user interactions are returned.

### `GET /api/conversations`

List memory records for a user. `user_id` is required. All active filters are
AND-combined: the response contains only records that satisfy every provided
constraint simultaneously.

**Query parameters**

| Parameter | Required | Default | Description |
|---|---|---|---|
| `user_id` | Yes | — | Pincer user ID to filter by |
| `channel` | No | — | Channel type: `telegram`, `whatsapp`, `discord`, etc. |
| `channel_user_id` | No | — | Channel-specific user or chat identifier |
| `limit` | No | `20` | Records per page (1–200) |
| `offset` | No | `0` | Pagination offset |

When both `channel` and `channel_user_id` are provided, results are narrowed to
records tagged `user:{channel}:{channel_user_id}`. Providing only one of the two
disables the channel filter.

**Response**

```json
{
  "conversations": [
    {
      "id": "a1b2c3d4-...",
      "user_id": "123456789",
      "category": "exchange",
      "tags": ["user:123456789", "category:exchange", "user:telegram:123456789"],
      "preview": "What is the weather like today?",
      "messages": [
        { "role": "user",      "content": "What is the weather like today?" },
        { "role": "assistant", "content": "It's 22 °C and sunny in Kyiv." }
      ],
      "created_at": "2026-05-25T10:30:00+00:00"
    }
  ],
  "total": 12,
  "limit": 20,
  "offset": 0
}
```

Each item includes a `messages` array with the exchange already split into a
`user` message and an `assistant` message. The `preview` field contains the
first 200 characters of the user turn for quick display.

### `GET /api/conversations/:id`

Fetch a single memory record by ID.

Returns `404` if the record does not exist or does not belong to the `exchange`
category.

**Response**

```json
{
  "id": "a1b2c3d4-...",
  "user_id": "123456789",
  "category": "exchange",
  "tags": ["user:123456789", "category:exchange", "user:telegram:123456789"],
  "messages": [
    { "role": "user",      "content": "Remind me to call the dentist tomorrow." },
    { "role": "assistant", "content": "Reminder set for tomorrow at 09:00." }
  ],
  "created_at": "2026-05-25T11:15:00+00:00"
}
```

### Content format

The agent stores each exchange as a single memory record whose `content` field
holds the pair as JSON:

```json
{"user": "user message text", "assistant": "agent reply text"}
```

The API parser handles two formats automatically:

1. **JSON** — `{"user": "...", "assistant": "..."}` (primary format)
2. **Prefixed plain text** — lines starting with `User asked:` /
   `Assistant replied:` (legacy / plain-text fallback)

If neither format is detected the entire content is returned as a single
`user` message.

---

## Memory

### `GET /api/memory/stats`

```json
{
  "total_conversations": 342,
  "total_messages": 8451,
  "total_entities": 156,
  "total_summaries": 89,
  "database_size_mb": 24.5
}
```

### `GET /api/memory/search?q=dentist&limit=5`

Search memory by keyword (uses FTS5).

### `GET /api/memory/entities?type=person`

List extracted entities. Types: `person`, `place`, `project`, `organization`.

---

## Skills

### `GET /api/skills`

List discovered `SKILL.md` skills — bundled (shipped inside the package at `src/pincer/skills/`) and user (`~/.pincer/skills/`), listed uniformly. Read-only — there is no install/scan/delete API; skills are added by placing a directory under `~/.pincer/skills/` and restarting. See the [Skills Guide](../guides/skills-guide.md).

```json
{
  "skills": [
    {
      "name": "skill-authoring",
      "description": "How to write a new Pincer skill as a SKILL.md directory...",
      "status": "active",
      "source": "file",
      "root": "bundled"
    }
  ]
}
```

### `GET /api/integrations`

List the agent's built-in tools, configured Google Workspace / Slack integrations, and MCP servers — a separate list from `/api/skills`, which is `SKILL.md` skills only. (`load_skill`/`load_skill_reference`/`run_skill_script` are wired per-agent at runtime rather than in the shared default tool set this endpoint introspects, so they aren't listed here — see the [Skills Guide](../guides/skills-guide.md).)

```json
{
  "integrations": [
    {
      "name": "file_read",
      "description": "Read a file's content from the workspace.",
      "status": "active",
      "source": "builtin",
      "approval_required": false
    },
    {
      "name": "Google Workspace",
      "description": "Gmail · Calendar · Drive · Docs · Sheets · Slides · Tasks · Contacts · Meet",
      "status": "active",
      "source": "integration",
      "slug": "google"
    }
  ]
}
```

---

## Audit

### `GET /api/audit?limit=50&tool=shell_exec&after=2026-02-25`

Query audit log with filters.

```json
{
  "events": [
    {
      "timestamp": "2026-02-26T10:30:15Z",
      "event": "tool_call",
      "user_id": "123456789",
      "channel": "telegram",
      "tool": "shell_exec",
      "approved": true,
      "duration_ms": 342
    }
  ]
}
```

---

## Voice (Sprint 7)

### `GET /api/voice/calls?limit=20`

List recent voice calls.

### `GET /api/voice/calls/:id`

Get call details including transcript.

### `GET /api/voice/calls/:id/transcript`

Get just the transcript.

### `POST /api/voice/calls`

Place an outbound call. Passes through the same server-side gate as a
chat-initiated call — do-not-call list, quiet hours, daily cap, per-target
cooldown — so this endpoint is not a way around the limits.

Body:

| Field | Required | Meaning |
|---|---|---|
| `target_number` | yes | E.164 number to call |
| `purpose` | yes | Why the call is being made (≤ 2000 chars). This is the agent's **call briefing**: it opens the call by explaining the reason in its own words and works towards it |
| `instructions` | no | Extra guidance for the agent during the call — what to ask, accept, avoid (≤ 4000 chars) |
| `target_name` | no | Who is being called; lets the agent address the person/business by name |
| `language` | no | `en` / `de` / `uk`; empty = configured default |

Statuses:

| Status | Meaning |
|---|---|
| `201` | Call initiated |
| `403` | Outbound disabled, number on the do-not-call list, or quiet hours |
| `422` | Not a valid E.164 number |
| `429` | Daily call limit reached, or the target is in its cooldown window |

### `POST /api/voice/schedule`

Appointment-scheduling call (free/busy → candidate slots → call → calendar
event). Same gate and same status codes.

## Do-not-call list (Sprint 8)

A shared opt-out list: an entry blocks the number for **every** user of the
instance and every channel. Callees who ask not to be called again during a
call are added automatically by the post-call pipeline.

### `GET /api/voice/do-not-call`

List blocked numbers with the reason, source (`callee` | `dashboard` | `cli` |
`manual`), originating call SID, and timestamp. Numbers are returned unmasked —
this endpoint exists to audit and correct the list, and a masked entry could
not be removed.

### `POST /api/voice/do-not-call`

```json
{ "phone_number": "+4915112345678", "reason": "asked not to be called again" }
```

`201` on success, `422` for a non-E.164 number. Idempotent — re-posting an
existing number updates its reason.

### `DELETE /api/voice/do-not-call/:number`

Unblock a number. `204` on success, `404` if it was not listed. Only ever do
this with the callee's consent; the removal is logged.

### `GET /api/voice/messages?limit=50`

Messages taken by the inbound receptionist (Sprint 12), newest first,
PII-masked: `caller_name` (+ `caller_name_unverified`), `callback_number`
(+ `callback_unverified`), `matter`, `urgent`, `created_at`,
`delivered_to_owner_at` (null = the owner report has not been delivered —
an alert fires after three failed attempts). Call rows in `/api/voice/calls`
carry `inbound_intent` (`question|message|appointment|human|unknown|after_hours`).

### `GET /api/voice/approvals?call_sid=CA…`

Open in-call approval requests (Sprint 11, `PINCER_VOICE_TOOL_APPROVAL=user`).
While the call partner is on hold, the initiating user is asked whether a
Tier W action may run. Each entry is the §6.5 payload:

```json
{
  "type": "voice_call_action",
  "approval_id": "…",
  "call_sid": "CA…",
  "tool_name": "google__create_event",
  "summary_spoken_language": "de",
  "summary": "den Termin „Beratung“ am Dienstag, der achtzehnte August um vierzehn Uhr eintragen",
  "args_preview": { "start": "2026-08-18T14:00:00+02:00", "end": "…" },
  "expires_at": "…",
  "final_state": ""
}
```

### `POST /api/voice/approvals/:id`

```json
{ "approved": true }
```

Answer an open request. `200` with the final payload, `404` when the request
is unknown, already answered, expired, or the call ended. The wait is bounded
server-side (`PINCER_VOICE_APPROVAL_TIMEOUT_S`); an unanswered request is
spoken to the callee as "I'll sort that out afterwards" and becomes a
post-call follow-up suggestion.

---

## WebSocket

### `WS /ws`

Real-time event stream. Connect for live updates:

```javascript
const ws = new WebSocket("ws://localhost:8080/ws?token=YOUR_TOKEN");
ws.onmessage = (e) => {
  const event = JSON.parse(e.data);
  // event.type: "message", "tool_call", "cost_update", "channel_status"
};
```