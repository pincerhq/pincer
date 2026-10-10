# 📚 API Reference

Pincer exposes a REST API via its dashboard server for monitoring and management.

---

## Base URL

```
http://localhost:8080/api
```

Every `/api/*` route except `GET /api/health`, `POST /api/auth/login` and
`POST /api/auth/refresh` requires an authenticated identity — see
[Authentication](#authentication).

---

## Authentication

The API authenticates **identities**. There is no shared token and there are
no roles: any identity with a password or an API key has full API access.
Create identities and credentials with the
[`pincer identity`](cli.md#identity-api-access) commands.

Send one of two credentials as a Bearer value:

```
Authorization: Bearer <access token or API key>
```

| Credential | For | How to get it |
|---|---|---|
| Access token (JWT) | The dashboard and other interactive sessions | `POST /api/auth/login`; lives 30 min (`PINCER_JWT_ACCESS_TTL_SECONDS`), renewed with the refresh token (7 days, `PINCER_JWT_REFRESH_TTL_SECONDS`) |
| API key (`pnc_…`) | Headless consumers, e.g. the web chat widget | `pincer identity api-key <name>` or `POST /api/identity/me/api-key`; shown in full once |

| Route | Auth | Purpose |
|---|---|---|
| `POST /api/auth/login` | public | Exchange name or email + password for a token pair |
| `POST /api/auth/refresh` | public | Exchange a refresh token for a new token pair |
| `GET /api/identity/me` | any | The caller's identity |
| `PUT /api/identity/me/password` | session (JWT) | Change the caller's password |
| `GET /api/identity/me/api-key` | any | Whether the caller has an API key (masked) |
| `POST /api/identity/me/api-key?force=` | session (JWT) | Generate or replace the caller's API key |
| `POST /api/voice/listen/{call_sid}/ticket` | session (JWT) | One-minute ticket for the [listen-in WebSocket](#websocket) |

"session (JWT)" routes need an access token; an API-key caller gets `403`.

`PINCER_AUTH_DISABLED=true` serves `/api/*` without authentication. It is for
local development and tests only — `pincer doctor --production` reports it
CRITICAL.

### `POST /api/auth/login`

```json
{ "identifier": "alice", "password": "…" }
```

`identifier` is matched against the exact identity name first, then against
the email address (case-insensitive). An email shared by several identities
cannot be used to sign in.

```json
{
  "access_token": "eyJ…",
  "refresh_token": "eyJ…",
  "token_type": "bearer",
  "expires_in": 1800,
  "pincer_user_id": "alice"
}
```

### `POST /api/auth/refresh`

```json
{ "refresh_token": "eyJ…" }
```

Returns a new token pair in the same shape as login.

### `GET /api/identity/me`

The caller's identity, plus `auth_method`: `jwt` or `api_key`.

### `PUT /api/identity/me/password`

```json
{ "current_password": "…", "new_password": "…" }
```

Passwords are 8–256 characters. Returns a fresh token pair; `403` if the
current password is wrong. Changing a password invalidates every token issued
before it. API keys are unaffected.

### `GET /api/identity/me/api-key`

```json
{ "exists": true, "masked": "pnc_Ab3d…wxyz", "created_at": "2026-02-26T10:00:00Z" }
```

### `POST /api/identity/me/api-key?force=true`

`201` with the key — the only time it is returned in full (only its SHA-256 is
stored):

```json
{ "api_key": "pnc_…", "masked": "pnc_Ab3d…wxyz", "created_at": "2026-02-26T10:00:00Z" }
```

`409` if a key already exists and `force` is not set. Replacing a key stops
the old one working immediately.

### Errors

Authentication failures return `{"error": "<code>", "detail": "…"}`:

| Code | Status | Meaning |
|---|---|---|
| `invalid_credentials` | 401 | Login failed — the same response for an unknown user and a wrong password |
| `invalid_token` | 401 | The Bearer value is not a valid access token or API key |
| `token_expired` | 401 | The token expired or was issued before a password change; refresh or sign in again. Does not count toward the lockout |
| `locked_out` | 429 | Too many failures; retry after the `Retry-After` header |

Failures are counted per IP, and on login also per account
(`PINCER_AUTH_MAX_FAILURES`, `PINCER_AUTH_LOCKOUT_SECONDS`).

### Chat routes and `X-Pincer-User`

On `/api/chat/*` the user depends on the credential:

- **Access token** — the signed-in identity; `X-Pincer-User` is ignored.
- **API key** — `X-Pincer-User` (a UUID) is the visitor-session id. It is
  optional: without it the key's own identity is used.
- **Auth disabled** — `X-Pincer-User` is required.

---

## Health

### `GET /health`

No auth required. Returns agent status. `auth_required` is `false` only when
`PINCER_AUTH_DISABLED` is set.

```json
{
  "status": "ok",
  "version": "0.7.0",
  "auth_required": true,
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

### `WS /api/voice/listen/{call_sid}`

Live listen-in audio for an active call (requires
`PINCER_LISTEN_IN_ENABLED=true`; see
[Voice calling → Live listen-in](../core-components/voice-calling.md#live-listen-in-sprint-15)
for the wire protocol).

Browsers cannot set a header on a WebSocket, so they first request a ticket
with their access token and pass it in the query string. A ticket is valid for
60 seconds and only for that call:

```javascript
const res = await fetch(`/api/voice/listen/${callSid}/ticket`, {
  method: "POST",
  headers: { Authorization: `Bearer ${accessToken}` },
});
const { ticket } = await res.json(); // { "ticket": "…", "expires_in": 60 }
const ws = new WebSocket(`wss://api.example.com/api/voice/listen/${callSid}?token=${ticket}`);
```

Non-browser clients may instead send `Authorization: Bearer <access token or
API key>` on the upgrade request.
