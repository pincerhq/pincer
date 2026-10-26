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
| `POST /api/auth/logout` | any | End the caller's session |
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

Returns a new token pair in the same shape as login. The refresh token is
single-use: store the new one. Presenting a refresh token that was already
replaced ends the session (`401 token_expired`). The exception is the 30
seconds after a refresh: the token it replaced is then still answered, with
the same refresh token the first caller received, so two tabs refreshing at
once both end up holding the current one.

### `POST /api/auth/logout`

`204`. Ends the session the access token belongs to: its access and refresh
tokens stop working immediately. Other sessions of the same identity and its
API key are unaffected (a no-op when called with an API key).

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
| `token_expired` | 401 | The token expired, its session was signed out, or it was issued before a password change; refresh or sign in again. Does not count toward the lockout |
| `locked_out` | 429 | Too many failures; retry after the `Retry-After` header |

Failures are counted per IP, and on login also per account
(`PINCER_AUTH_MAX_FAILURES`, `PINCER_AUTH_LOCKOUT_SECONDS`). An identity has
one account budget whether it is named by id or by email. The per-IP lockout
doubles with each further failure; the per-account one is a flat window, since
anyone can spend another person's budget.

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

List discovered `SKILL.md` skills — bundled (shipped inside the package at `src/pincer/skills/`) and user (`skills_dir`: `~/.pincer/skills/` or an `s3://` / `az://` / `gs://` prefix), listed uniformly. There is no install/delete API; add a skill directory to the user root, then call `POST /api/skills/rebuild` (or `pincer skills rebuild`). See the [Skills Guide](../guides/skills-guide.md).

```json
{
  "skills": [
    {
      "name": "skill-authoring",
      "description": "How to write a new Pincer skill as a SKILL.md directory...",
      "status": "active",
      "source": "file",
      "root": "bundled",
      "dir": "skill-authoring"
    }
  ]
}
```

### `POST /api/skills/rebuild`

Rebuild the skill index — the API counterpart of `pincer skills rebuild`. Query parameter `keep_cache` (default `false`) keeps cloudpathlib's cached copies of remote skill files instead of re-fetching them.

1. Rebuilds the index of the process serving the request first, so the agent and dashboard it serves already see the result when the response returns.
2. Queues one `rebuild_skill_index` background job carrying the new `generation`.

The job is consumed by **one** process: with `task_broker=redis` that is any `pincer run tasks` worker, or the main `pincer run` itself, where it is a no-op. Workers that share the main process's data volume (the default `docker-compose.yml` setup) also follow the generation stamp the rebuild wrote; the job is what reaches a worker that doesn't. The job is idempotent and never moves a process to an older generation: a process already at that generation (or a newer one) skips it. The job carries the sender's disk ID (a file in `skills_cache_dir`), so a worker on the same data volume only catches up with the stamp instead of clearing the shared cache a second time.

A failed enqueue doesn't fail the request — the in-app rebuild has already happened — and is reported in `job`. If a skill root can't be listed (an outage, expired credentials) the response is `503`, and nothing changes: the index keeps its previous skills and no generation is announced. A single skill whose SKILL.md can't be read doesn't fail the rebuild: it's skipped (local root), or keeps its previous version (cloud root), with a warning in the log. Any other failed rebuild is a `500`.

`generation` is opaque, but it sorts by creation time: it has the form `<time_ns, 20 digits>-<uuid hex>`. A process never moves to a generation that sorts before its current one.

```json
{
  "generation": "01760000000000000000-3f1c9e4a7b2d4e0f9a8c6b5d4e3f2a1b",
  "cache_cleared": true,
  "skills": [{"name": "skill-authoring", "root": "bundled", "dir": "skill-authoring", "...": "..."}],
  "job": {"queued": true, "topic": "rebuild_skill_index"}
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

## Storage

Browse and manage files under the **storage root**: `PINCER_STORAGE_ROOT` (a local path or an `s3://` / `az://` / `gs://` URI). When it is unset and `skills_dir` is a cloud URI, the root is that bucket or container; otherwise it is `<data_dir>/storage`. Cloud roots use the same `PINCER_STORAGE_*` credentials as remote skill roots. A local root that contains the data dir (`PINCER_STORAGE_ROOT=/app` with the data dir in `/app/data`) is refused with `503` on every route, since the data dir holds the JWT secret and the database; `pincer doctor` flags it too.

Keys are POSIX paths relative to the root (`reports/q1.pdf`; `""` is the root). Absolute keys, `~`, `..` segments and scheme-qualified keys are refused with `400`.

**Protected prefixes.** When the skills dir lies under the root, its prefix is protected: deleting or moving it, anything inside it, or any of its parent folders is a `403`, and so is moving something into it. Files inside it can still be read, uploaded and edited. On a local root the check follows the folder on disk, not the key: a different-case key on a case-insensitive filesystem, a Unicode variant of a name, or a symlink leading into the skills dir is refused the same way. A symlink that points into it can itself be deleted or moved; that leaves the skills dir alone.

Every route except `/api/storage/signed/{token}` needs a bearer token. Reading needs any authenticated identity. The routes that change something (`PUT /text`, `POST /upload`, `POST /folders`, `POST /move`, `DELETE /object`) need a signed-in session: an API key gets `403` there. With `PINCER_AUTH_DISABLED`, reads need no token, but the routes that change something still need a signed-in session (`401` without one).

| Status | Meaning |
|---|---|
| `400` | Invalid key (traversal, absolute path, scheme), or a folder moved into itself |
| `403` | Protected prefix, an API key on a route that changes files, or an invalid signed link |
| `404` | No such file or folder |
| `409` | Already exists (upload, move, mkdir without `overwrite`), the same name twice in one upload, a file where a folder is needed, a move onto a folder that contains the source, or the file changed since it was read (`PUT /text` with a stale `etag`) |
| `410` | The signed link has expired |
| `413` | Text larger than 2 MB, or an upload larger than `PINCER_STORAGE_UPLOAD_FILE_MAX_SIZE` |
| `415` | Not a UTF-8 text file |
| `503` | Storage unreachable or misconfigured; the detail names the `PINCER_STORAGE_*` settings to check |

### `GET /api/storage`

```json
{"backend": "s3", "root": "s3://acme/", "protected": ["team/skills"], "presign_ttl": 900, "max_text_bytes": 2097152,
 "max_upload_bytes": 20971520}
```

`backend` is `file`, `s3`, `az` or `gs`.

### `GET /api/storage/list?prefix=reports&limit=50&cursor=…`

One page of the folders and files directly inside `prefix`: folders first, then files, each sorted by name (case-insensitive). `limit` is the page size, 50 by default and at most 100 (more is a `422`). Pass the response's `next_cursor` as `cursor` to get the next page; it is `null` on the last one. A cursor names the last entry served, not a position, so entries added or deleted between pages are neither repeated nor skipped. A cursor that cannot be read is a `400`.

A folder with more than 100,000 entries can only be paged through up to that point; `truncated` is then `true`. Empty folders on object stores are a zero-byte `.keep` marker, which is not listed.

```json
{
  "prefix": "reports",
  "folders": [{"key": "reports/2026", "name": "2026", "protected": false}],
  "files": [
    {"key": "reports/q1.pdf", "name": "q1.pdf", "size": 48213, "last_modified": "2026-10-01T09:12:00+00:00",
     "content_type": "application/pdf", "protected": false}
  ],
  "next_cursor": "WzEsInExLnBkZiJd",
  "truncated": false
}
```

### `GET /api/storage/presign?key=…&disposition=inline|attachment`

A link that serves the file without a bearer token, valid for `PINCER_STORAGE_PRESIGN_TTL` seconds (default 900; 60 to 604800, i.e. 1 minute to 7 days), for `<img src>`, downloads and "open in new tab".

```json
{"url": "https://acme.s3.amazonaws.com/reports/q1.pdf?X-Amz-Expires=900&…", "expires_at": "2026-10-18T16:23:36+00:00"}
```

Cloud roots return the provider's presigned URL; S3 links also carry the requested `Content-Disposition`, but only files that are safe to show there are served inline: raster images, PDF, plain text, Markdown, CSV, audio and video. Anything else, HTML, SVG and every XML type (`*+xml`, `*/xml`) included, always downloads (`attachment`), because the provider serves it without the headers below, from the bucket's origin. Azure and GCS links are served with the content type the object was stored with. Local roots return a path on this API, `/api/storage/signed/<token>`, which the client resolves against the API's base URL.

### `GET /api/storage/signed/{token}`

Public. Serves a file of a **local** root from a link minted by `/presign`. The token is an HMAC over the key, the expiry and the disposition, keyed with the JWT secret. A tampered token is `403`, an expired one `410`. Cloud roots answer `404` here: their links point at the provider.

### `GET /api/storage/download?key=…`

The file as an attachment, with the bearer token. A fallback for clients that cannot use `/presign`.

Files served by this API (here and through `/signed/` links) carry `X-Content-Type-Options: nosniff` and a sandboxing `Content-Security-Policy`, so an uploaded HTML or SVG file cannot run script on the API's origin. A cloud root's presigned links are served by the provider instead; see `/presign`.

### `GET /api/storage/text?key=…`

A UTF-8 text file of at most 2 MB, for the editor. The `etag` is a hash of the content.

```json
{"key": "notes/plan.md", "content": "# Plan\n…", "etag": "8a5d8b28d66ae90b5876fe0337b963d9", "size": 812}
```

### `PUT /api/storage/text`

```json
{"key": "notes/plan.md", "content": "# Plan v2\n…", "etag": "8a5d8b28d66ae90b5876fe0337b963d9"}
```

Saves the file and returns `{"key", "etag", "size"}`. With `etag`, the save is refused with `409` if the file changed (or was deleted) since it was read. Omit `etag` to overwrite unconditionally, or to create a new file.

Two saves with the same `etag` cannot both succeed. On S3, Azure and GCS the write is conditional on the version that was checked (`If-Match` / generation match), so this holds across replicas. On a local root, saves of one file are serialised within the Pincer process. An S3-compatible server that rejects conditional writes (`501 NotImplemented`) is logged once; from then on each save or create-only upload checks the object (`HEAD`) before writing, which is serialised within the process only. A server that silently ignores the headers gets no protection across replicas either.

### `POST /api/storage/upload?prefix=…&overwrite=false`

Multipart form with one or more `files` parts. A file name may carry subfolders (`2026/q1.pdf`, from a folder pick); they are created. Returns `{"files": [<file entry>, …]}`.

Every file is checked before any is written, so a refusal stores none of them: a bad name (`400`); an existing file without `overwrite=true`, the same name twice, or a file where a folder is needed — an existing file (`a.txt/b.txt` when `a.txt` is a file) or another file of the same upload (`x` and `x/y`) — (`409`); or a file over `PINCER_STORAGE_UPLOAD_FILE_MAX_SIZE` (default 20 MB; `413`). On S3 roots the limit is at most 5 GiB, the most one PUT can store; `GET /api/storage` reports the limit in force as `max_upload_bytes`. Without `overwrite`, the write itself only creates: a file someone else stores under the same name in the meantime is a `409`, not overwritten. Only a failure while writing (that `409`, or an unavailable store) can leave earlier files of the request stored; the error's detail then names them.

### `POST /api/storage/folders`

`{"prefix": "reports/2026"}` → `201` with the folder entry. `409` if it exists.

### `POST /api/storage/move`

`{"from": "reports/q1.pdf", "to": "archive/q1.pdf", "overwrite": false}` moves or renames a file or a folder. Object stores copy and then delete, object by object. `409` if the target exists and `overwrite` is `false`, or if the target is a folder that contains the source (replacing `a` with `a/b` would delete `a/b` too); `403` if either side is protected. On a case-insensitive disk, a rename that only changes case (`readme.md` → `README.md`) is a rename, with or without `overwrite`.

### `DELETE /api/storage/object?key=…` / `?prefix=…`

Deletes a file (`key`) or a folder and everything in it (`prefix`). Pass exactly one. Returns `{"key": "reports", "kind": "folder"}`.

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
60 seconds, only for that call and for one connection, and stops working when
the session that requested it signs out:

```javascript
const res = await fetch(`/api/voice/listen/${callSid}/ticket`, {
  method: "POST",
  headers: { Authorization: `Bearer ${accessToken}` },
});
const { ticket } = await res.json(); // { "ticket": "…", "expires_in": 60 }
const ws = new WebSocket(`wss://api.example.com/api/voice/listen/${callSid}?token=${ticket}`);
```

Non-browser clients may instead send `Authorization: Bearer <access token>`
on the upgrade request. An API key is refused with 403: listening in needs a
signed-in session.
