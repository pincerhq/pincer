# Microsoft 365 MCP Server

`ms365-mcp` exposes **62 Microsoft 365 tools** (plus one always-on auth-status
tool) — Outlook email, Calendar, OneDrive, Microsoft To Do, Contacts, OneNote,
and organization directory lookups — as a standalone
[Model Context Protocol](https://modelcontextprotocol.io) (MCP) server built
on [FastMCP](https://gofastmcp.com). Any MCP host can connect to it: Claude
Desktop, Cursor, VS Code, or Pincer's own MCP client.

It supports **multiple Microsoft accounts from one running server** — each
caller authenticates independently, lazily, on its first tool call. It is
built on Microsoft Graph and lives in `src/ms365-mcp/ms365/`.

---

## Table of contents

- [How it works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Azure app registration](#azure-app-registration)
- [Authentication](#authentication)
- [Configuration](#configuration)
- [Running the server](#running-the-server)
- [Connecting MCP hosts](#connecting-mcp-hosts)
- [Docker](#docker)
- [Tool reference](#tool-reference)
- [Approvals and destructive operations](#approvals-and-destructive-operations)
- [Error handling](#error-handling)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

---

## How it works

```
MCP host (Claude Desktop / Cursor / Pincer MCP client)
        │  MCP protocol (stdio or streamable HTTP), tool call carries
        │  an opaque `identity` in its `_meta` envelope
        ▼
ms365_server.py            ← FastMCP app; register_*_tools() unchanged per service
        │  each tool call resolves `identity` → that caller's own GraphClient
        ▼
IdentitySessionManager      ← one MS365Auth/GraphClient pair per identity,
        │                      created lazily on first use (identity_session.py)
        ▼
GraphClient                 ← httpx, bearer token, retry, pagination
        │  Authorization: Bearer <token>
        ▼
Microsoft Graph API (https://graph.microsoft.com/v1.0)
```

- **Tool collection.** `collect_tools()` calls the existing
  `register_email_tools()`, `register_calendar_tools()`, … functions per
  service, reusing each tool's name, JSON schema, description, and approval
  flag verbatim.
- **Serving.** `build_server()` registers those specs on a `fastmcp.FastMCP`
  instance. Each tool handler takes a FastMCP `Context` (never exposed in the
  tool's public schema) to resolve the caller's `identity` and look up (or
  lazily create) that identity's `GraphClient`.
- **Auth is per-identity and lazy** — see [Authentication](#authentication).
  There is no boot-time sign-in anymore.

Source: [`src/ms365-mcp/ms365/ms365_server.py`](../../src/ms365-mcp/ms365/ms365_server.py),
[`src/ms365-mcp/ms365/identity_session.py`](../../src/ms365-mcp/ms365/identity_session.py)

---

## Prerequisites

| Requirement | Notes |
| --- | --- |
| Python ≥ 3.12 | Same as the rest of Pincer. |
| `fastmcp` | The MCP server framework this is built on. |
| `msal` | Microsoft's auth library (device-code flow). |
| Azure app registration | A public client with delegated Graph permissions (below). |

Install the extras:

```bash
uv pip install "pincer-agent[mcp,ms365]"
# or everything:
uv pip install "pincer-agent[all]"
# or
uv sync --all-extras
```

`starlette` and `uvicorn` (needed for the HTTP transport) ship with Pincer's
core dependencies.

---

## Azure app registration

The server uses **delegated** permissions via the device-code flow, so you need
a public client app registration. One-time setup in the
[Azure Portal](https://portal.azure.com) → **Microsoft Entra ID** → **App
registrations** → **New registration**:

1. **Name:** anything (e.g. `pincer-ms365`).
2. **Supported account types:** "Personal Microsoft accounts only" for
   `PINCER_MS365_TENANT_ID=consumers` (recommended), or "Accounts in any
   organizational directory and personal Microsoft accounts" for `common`,
   or single-tenant for a specific org tenant.
3. **Redirect URI:** none needed for device code.
4. After creating, copy the **Application (client) ID** → this is
   `PINCER_MS365_CLIENT_ID`.
5. Under **Authentication** → **Advanced settings**, set **Allow public client
   flows** to **Yes** (required for device code).
6. Under **API permissions** → **Add a permission** → **Microsoft Graph** →
   **Delegated permissions**, add the scopes below, then **Grant admin consent**
   if your tenant requires it.

### Delegated Graph permissions

| Service | Graph scopes |
| --- | --- |
| (always) | `User.Read` |
| Email | `Mail.ReadWrite`, `Mail.Send` |
| Calendar | `Calendars.ReadWrite` |
| OneDrive | `Files.ReadWrite.All` |
| To Do | `Tasks.ReadWrite` |
| Contacts | `Contacts.ReadWrite` |
| OneNote | `Notes.ReadWrite.All` |
| Directory | `User.ReadBasic.All` |

Every identity requests the **full configured scope set** (`--services` only
hides which tools are *exposed*; it doesn't shrink the consent each identity
is asked for).

---

## Authentication

Authentication is **per-identity and lazy** — there is no boot-time sign-in
step. `identity` is an opaque string carried in the MCP call's `_meta`
envelope (Pincer populates it with the caller's `pincer_user_id`; a bare MCP
client that sends none falls back to a fixed `"default"` identity slot).

### First use of a new identity

1. A tool call for an identity with no cached token starts a Microsoft
   device-code flow and **returns immediately** with the sign-in URL and code
   — it does not block the request while waiting for the user to sign in.
   Concretely, this surfaces as a tool error (`isError=true`) whose message
   *is* the sign-in instructions, e.g.:

   > Go to https://microsoft.com/devicelogin and enter code ABC-DEF. Once
   > signed in, retry your request (or call `ms365__check_auth_status`).

2. Sign-in completes in the background (polling Microsoft, same as MSAL's
   normal device-code flow, up to its `expires_in` window). A second tool
   call for the same identity while this is in flight gets the same
   instructions again rather than starting a competing device code.
3. Once complete, the *next* tool call for that identity just works. Call
   **`ms365__check_auth_status`** at any time to check "not signed in" /
   "sign-in in progress" / "signed in" without retrying a real Graph call —
   this is the reliable way to check status regardless of transport.
4. When running inside Pincer, a completion notification is also pushed back
   over the MCP connection and relayed to the user's chat (see
   `pincer/mcp/notifications.py`) — this is delivered reliably over stdio,
   but not guaranteed over streamable HTTP depending on how the client keeps
   its connection open, which is why step 3's polling tool exists as the
   transport-agnostic fallback.

### Token storage

Each identity's MSAL token cache is a separate file:

```
<MS365_TOKEN_CACHE_DIR>/<identity>_token_cache.json   (default dir: ~/.pincer/ms365_mcp/, file mode 0600)
```

`identity` is sanitized to a safe filename component before use. Refresh is
automatic — MSAL rewrites the cache using the cached refresh token. **The
cache is encrypted at rest** with a shared Fernet key: auto-generated on
first run at `MS365_TOKEN_ENCRYPTION_KEY_PATH` (default
`~/.pincer/ms365_mcp/token_encryption.key`, file mode `0600`), or overridden
entirely with `MS365_TOKEN_ENCRYPTION_KEY` (a raw Fernet key — useful when
injecting the key from an external secrets manager instead of a local file).
One key is shared across all identities on a given server; per-identity
isolation comes from separate cache files, not separate keys. A file that
fails to decrypt (wrong/rotated key, corruption, or a stray plaintext file
left over from before this was added) is treated as "no cached token" —
the affected identity is prompted to re-authenticate rather than crashing
the server.

### `ms365-mcp-setup` (optional, single-identity convenience)

```bash
ms365-mcp-setup
```

Runs the device-code flow once, synchronously, for the **`"default"`
identity slot only** — the same slot a bare MCP client without an `identity`
would use. This is convenient for standalone testing outside Pincer, or to
pre-authenticate before the first real tool call, but it is **not required**
for Pincer's normal multi-user operation — each real user authenticates
lazily on their own first tool call regardless.

---

## Configuration

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `MS365_CLIENT_ID` (or `PINCER_MS365_CLIENT_ID` via `pincer run`) | — | Azure app (client) ID. **Required.** |
| `MS365_TENANT_ID` | `common` | `consumers` for personal accounts, `common`, `organizations`, or your tenant GUID. |
| `MS365_SERVICES` | all 7 | Comma-separated services requested for the OAuth scope set. |
| `MS365_TOKEN_CACHE_DIR` | `~/.pincer/ms365_mcp/` | Directory holding each identity's `<identity>_token_cache.json`. |
| `TRANSPORT` | `stdio` | `stdio` or `http` (overridden by `--transport`). |
| `HOST` | `127.0.0.1` | HTTP bind host (overridden by `--host`). |
| `PORT` | `8000` | HTTP bind port (overridden by `--port`). |
| `LOG_LEVEL` | `INFO` | Python logging level. |

Set these in your `.env` file:

```bash
PINCER_MS365_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
PINCER_MS365_TENANT_ID=consumers
```

---

## Running the server

```bash
# stdio (default) — for Claude Desktop / Cursor / local hosts
ms365-mcp-run

# streamable HTTP — endpoint at http://127.0.0.1:8000/mcp
ms365-mcp-run --transport http --host 127.0.0.1 --port 8000

# expose only a subset of services
ms365-mcp-run --services email,calendar

# read-only mode (hide every write/destructive tool)
ms365-mcp-run --read-only
```

### Command-line options

| Flag | Default | Description |
| --- | --- | --- |
| `--transport {stdio,http}` | `stdio` | Transport to serve. |
| `--host HOST` | `127.0.0.1` | Bind host (HTTP only). |
| `--port PORT` | `8000` | Bind port (HTTP only). |
| `--services LIST` | all | Comma-separated subset: `email,calendar,onedrive,todo,contacts,onenote,directory`. Only filters which tools are exposed — see [Configuration](#configuration) for the OAuth scope set. |
| `--tenant ID` | from config | Override the Microsoft tenant id. |
| `--read-only` | off | Expose only read tools; hide writes (send/create/update/delete/move/share). |

HTTP mode also serves `GET /health` (and `GET /`) returning
`{"status": "healthy", "service": "ms365-mcp"}`.

---

## Connecting MCP hosts

### Claude Desktop / Cursor (stdio)

Add to `claude_desktop_config.json` (Claude Desktop) or the equivalent MCP
config:

```json
{
  "mcpServers": {
    "ms365": {
      "command": "ms365-mcp-run",
      "args": ["--transport", "stdio"],
      "env": {
        "MS365_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
        "MS365_TENANT_ID": "consumers"
      }
    }
  }
}
```

A host that doesn't send an `identity` in its tool calls transparently uses
the `"default"` identity slot (see [Authentication](#authentication)).

### Pincer's own MCP client (stdio — default)

The `pincer.toml` shipped in the repo uses stdio transport. Pincer spawns the
server as a sandboxed subprocess, so credentials must be declared explicitly
in the `env` dict — the sandbox intentionally restricts `HOME` and does not
inherit your shell environment:

```toml
[[mcp.servers]]
name      = "ms365"
transport = "stdio"
command   = "python"
args      = ["${PINCER_SRC_DIR:-$PWD}/src/ms365-mcp/ms365/ms365_server.py"]
env       = {
  MS365_CLIENT_ID        = "${PINCER_MS365_CLIENT_ID}",
  MS365_TENANT_ID        = "${PINCER_MS365_TENANT_ID}",
  MS365_TOKEN_CACHE_DIR  = "${HOME}/.pincer/ms365_mcp"
}
approval_required = ["*"]
```

`${PINCER_MS365_*}` values are interpolated from your `.env` file at startup.
`${HOME}` resolves from the parent process so the subprocess can find the
per-identity token caches even though the sandbox sets `HOME=/tmp`. Pincer's
agent loop populates `identity` on every tool call automatically — no extra
config needed for multi-user identity mapping to work (see
`src/pincer/core/identity.py`).

### Pincer's own MCP client (HTTP)

Run the server over HTTP, then register it in `pincer.toml`:

```toml
[[mcp.servers]]
name = "ms365"
transport = "http"
url = "http://127.0.0.1:8000/mcp"
```

Tools then appear to the agent as `ms365__outlook__list_messages`, etc.
(the configurable server-name prefix plus the original tool name).

---

## Docker

A ready-made compose file is provided at
[`src/ms365-mcp/docker-compose.yml`](../../src/ms365-mcp/docker-compose.yml):

```bash
cd src/ms365-mcp
# Set MS365_CLIENT_ID / MS365_TENANT_ID in .env, then:
docker compose up --build
# MCP endpoint → http://localhost:8000/mcp
```

Notes:

- No pre-authentication step is required — each identity (including the
  `"default"` slot for a bare MCP client) authenticates lazily on its first
  tool call, same as any other deployment.
- The compose file mounts a named volume at `/app/data` and sets
  `MS365_TOKEN_CACHE_DIR=/app/data`, so every identity's token cache
  persists across container restarts.
- stdio also works in a container (`docker run -i … ms365-mcp-run`) for hosts
  that speak MCP over stdio, but HTTP is the better fit for a long-running
  service with a health check.

---

## Tool reference

62 tools across 7 services, plus `ms365__check_auth_status` (always
available, not tied to any one service — see [Authentication](#authentication)).
Parameter notation: `name` (type) — required params are **bold**; optional
params show their default. The **Write** column marks tools that modify data;
those carry the MCP `destructiveHint` annotation and are hidden by
`--read-only`.

### Email — Outlook (17)

| Tool | Parameters | Write |
| --- | --- | --- |
| `outlook__list_mail_folders` | — | |
| `outlook__list_messages` | `folder`=`Inbox`, `max_results`=`25`, `unread_only`=`false`, `has_attachments`=`false`, `from_address`=`""` | |
| `outlook__search_messages` | **`query`**, `max_results`=`10` | |
| `outlook__get_message` | **`message_id`** | |
| `outlook__get_message_attachments` | **`message_id`** | |
| `outlook__download_attachment` | **`message_id`**, **`attachment_id`** | |
| `outlook__send_message` | **`to`**, **`subject`**, **`body`**, `cc`=`""`, `bcc`=`""`, `importance`=`normal`, `body_type`=`text` | ✅ |
| `outlook__reply_to_message` | **`message_id`**, **`body`**, `body_type`=`text` | ✅ |
| `outlook__reply_all_to_message` | **`message_id`**, **`body`**, `body_type`=`text` | ✅ |
| `outlook__forward_message` | **`message_id`**, **`to`**, `comment`=`""` | ✅ |
| `outlook__create_draft` | **`to`**, **`subject`**, **`body`**, `cc`=`""`, `body_type`=`text` | ✅ |
| `outlook__update_draft` | **`message_id`**, `subject`=`""`, `body`=`""`, `to`=`""`, `body_type`=`text` | ✅ |
| `outlook__send_draft` | **`message_id`** | ✅ |
| `outlook__move_message` | **`message_id`**, **`destination_folder`** | ✅ |
| `outlook__delete_message` | **`message_id`** | ✅ |
| `outlook__mark_as_read` | **`message_id`**, `is_read`=`true` | |
| `outlook__flag_message` | **`message_id`**, `flagged`=`true` | |

### Calendar (11)

| Tool | Parameters | Write |
| --- | --- | --- |
| `outlook__list_calendars` | — | |
| `outlook__list_events` | `calendar_id`=`""`, `time_min`=`""`, `time_max`=`""`, `max_results`=`50` | |
| `outlook__get_event` | **`event_id`** | |
| `outlook__search_events` | **`query`**, `max_results`=`20` | |
| `outlook__check_availability` | **`emails`**, `time_min`=`""`, `time_max`=`""` | |
| `outlook__create_event` | **`subject`**, **`start`**, **`end`**, `description`=`""`, `location`=`""`, `attendees`=`""`, `timezone`=`UTC`, `is_online_meeting`=`false` | ✅ |
| `outlook__update_event` | **`event_id`**, `subject`=`""`, `start`=`""`, `end`=`""`, `description`=`""`, `location`=`""`, `timezone`=`""` | ✅ |
| `outlook__delete_event` | **`event_id`** | ✅ |
| `outlook__accept_event` | **`event_id`**, `comment`=`""` | ✅ |
| `outlook__decline_event` | **`event_id`**, `comment`=`""` | ✅ |
| `outlook__tentative_event` | **`event_id`**, `comment`=`""` | ✅ |

### OneDrive (14)

| Tool | Parameters | Write |
| --- | --- | --- |
| `onedrive__list_drive_items` | `path`=`/`, `max_results`=`50` | |
| `onedrive__search_files` | **`query`**, `max_results`=`25` | |
| `onedrive__get_file_metadata` | **`item_id`** | |
| `onedrive__download_file` | **`item_id`** | |
| `onedrive__get_file_preview` | **`item_id`** | |
| `onedrive__list_shared_with_me` | `max_results`=`25` | |
| `onedrive__list_recent_files` | `max_results`=`25` | |
| `onedrive__upload_file` | **`path`**, **`content`**, `content_type`=`text/plain` | ✅ |
| `onedrive__create_folder` | **`name`**, `parent_path`=`/` | ✅ |
| `onedrive__move_file` | **`item_id`**, **`destination_path`** | ✅ |
| `onedrive__rename_file` | **`item_id`**, **`new_name`** | ✅ |
| `onedrive__copy_file` | **`item_id`**, **`destination_path`**, `new_name`=`""` | ✅ |
| `onedrive__delete_file` | **`item_id`** | ✅ |
| `onedrive__share_file` | **`item_id`**, `link_type`=`view`, `scope`=`anonymous` | ✅ |

### Microsoft To Do (8)

| Tool | Parameters | Write |
| --- | --- | --- |
| `ms_todo__list_task_lists` | — | |
| `ms_todo__list_tasks` | **`list_id`**, `max_results`=`50`, `include_completed`=`false` | |
| `ms_todo__get_task` | **`list_id`**, **`task_id`** | |
| `ms_todo__create_task` | **`list_id`**, **`title`**, `due_date`=`""`, `importance`=`normal`, `body`=`""` | ✅ |
| `ms_todo__update_task` | **`list_id`**, **`task_id`**, `title`=`""`, `due_date`=`""`, `importance`=`""`, `body`=`""` | ✅ |
| `ms_todo__complete_task` | **`list_id`**, **`task_id`** | ✅ |
| `ms_todo__delete_task` | **`list_id`**, **`task_id`** | ✅ |
| `ms_todo__create_task_list` | **`name`** | ✅ |

### Contacts (6)

| Tool | Parameters | Write |
| --- | --- | --- |
| `outlook__list_contacts` | `max_results`=`50` | |
| `outlook__search_contacts` | **`query`**, `max_results`=`20` | |
| `outlook__get_contact` | **`contact_id`** | |
| `outlook__create_contact` | **`given_name`**, `surname`=`""`, `email`=`""`, `mobile_phone`=`""`, `business_phone`=`""`, `company_name`=`""`, `job_title`=`""` | ✅ |
| `outlook__update_contact` | **`contact_id`**, `given_name`=`""`, `surname`=`""`, `email`=`""`, `mobile_phone`=`""`, `company_name`=`""`, `job_title`=`""` | ✅ |
| `outlook__delete_contact` | **`contact_id`** | ✅ |

### OneNote (5)

| Tool | Parameters | Write |
| --- | --- | --- |
| `onenote__list_notebooks` | `max_results`=`25` | |
| `onenote__list_sections` | **`notebook_id`** | |
| `onenote__list_pages` | **`section_id`**, `max_results`=`50` | |
| `onenote__get_page_content` | **`page_id`** | |
| `onenote__create_page` | **`section_id`**, **`title`**, **`content`** | ✅ |

### Directory (1)

| Tool | Parameters | Write |
| --- | --- | --- |
| `ms365__search_users` | `email`=`""`, `first_name`=`""`, `last_name`=`""` | |

> **`ms365__search_users`** looks up other users in the organization's
> directory (not just the signed-in user) by exact email, or by first/last
> name prefix. Returns a single user's basic info if there's exactly one
> match, otherwise a list capped at 5 matches.

### Auth status (always available, 1)

| Tool | Parameters | Write |
| --- | --- | --- |
| `ms365__check_auth_status` | — | |

> Not part of any `--services` subset or `--read-only` filtering — always
> registered, since it's about the caller's own auth state rather than a
> Graph resource. See [Authentication](#authentication).

> **Date/time params** (`start`, `end`, `time_min`, `time_max`, `due_date`) are
> ISO 8601 strings, e.g. `2026-06-01T09:00:00`. **HTML bodies** are sent by
> passing `body_type=html`.

---

## Approvals and destructive operations

Every tool that modifies data is registered with `require_approval=True` in the
underlying integration. The MCP server surfaces that as a
[`destructiveHint`](https://modelcontextprotocol.io/docs/concepts/tools)
annotation and sets `readOnlyHint` on the rest, so MCP hosts can prompt the user
before running them.

Because a standalone MCP server has no messaging channel of its own, it does not
block on its own approval prompt — **confirmation is the host's
responsibility** (Claude Desktop, Cursor, and Pincer's MCP client all prompt on
destructive tools). To remove the write tools entirely, run with `--read-only`;
the 31 write tools are then neither listed nor callable (leaving 31 read tools
plus `ms365__check_auth_status`).

---

## Error handling

- **Input validation.** FastMCP validates every call against the tool's schema
  before dispatch; bad arguments return an MCP validation error.
- **Graph / auth errors** are caught and raised as FastMCP `ToolError`s
  (`isError=true` on the wire) with a readable message, rather than crashing
  the server. This includes the "sign-in pending" case — see
  [Authentication](#authentication).
- **Rate limits.** `GraphClient` retries `429` responses up to 3 times, honoring
  the `Retry-After` header.
- **Pagination.** List endpoints follow `@odata.nextLink` up to each tool's
  `max_results`.

---

## Security

- **Token storage.** Each identity's cache file is written with mode `0600`
  under `MS365_TOKEN_CACHE_DIR` (default `~/.pincer/ms365_mcp/`). Treat these
  as credentials — they contain refresh tokens. They are **encrypted at rest**
  with a Fernet key (see [Token storage](#token-storage) above) — note the key
  file itself is also only protected by filesystem permissions, so this
  doesn't help against an attacker with full read access to your account, but
  it does protect against the cache file leaking independently of the key
  (backups, cloud sync, accidentally archived/committed, etc.). Losing or
  rotating the key invalidates all cached tokens; affected identities simply
  re-authenticate.
- **`identity` is untrusted input.** It's sanitized before being used as part
  of a filename, but never assume it has any particular shape — it's an
  opaque string from whatever MCP host set it.
- **Bind locally.** The HTTP transport defaults to `127.0.0.1`. Only bind to
  `0.0.0.0` (e.g. in Docker) behind a trusted network or reverse proxy; the
  endpoint is unauthenticated.
- **Least privilege.** Grant only the Graph scopes for the services you run, and
  use `--services` / `--read-only` to narrow the exposed surface.
- **Delegated access.** All actions run as the signed-in user — the server can
  do nothing the user cannot already do, and each identity can only ever act
  as its own Microsoft account.

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `Microsoft 365 client_id is not configured` on startup | Set `MS365_CLIENT_ID` (standalone) or `PINCER_MS365_CLIENT_ID` in `.env` (via `pincer run`). |
| A tool call returns a sign-in URL/code instead of a result | Expected on first use of a new identity — not an error. Sign in, then retry (or poll `ms365__check_auth_status`). |
| `Sign-in already in progress for this account…` | A device-code flow for that identity is already running — wait for it to complete (or expire) rather than retrying repeatedly. |
| Sign-in notification never reached my chat | Best-effort over streamable HTTP — poll `ms365__check_auth_status` instead of waiting for the push. |
| `Device flow failed` | Enable **Allow public client flows** on the Azure app. |
| `AADSTS9002346` error | Your app is registered for personal accounts only — use `MS365_TENANT_ID=consumers`. |
| Graph `401` / `403` in tool output | Missing scope or unconsented permission — add it in Azure; the identity will need to re-authenticate (its next tool call after `has_cached_token()` fails will start a fresh device-code flow automatically). |
| `429` errors | Rate limited; the client retries, but reduce call volume. |
| `MCP 'ms365' connection failed (attempt N):` with blank message (Pincer sandboxed stdio) | The sandboxed subprocess started but timed out before the MCP protocol initialized — check `MS365_CLIENT_ID` is present in the server's `env` dict in `pincer.toml`. |
| Docker: token "permission denied" | Ensure `MS365_TOKEN_CACHE_DIR` points at the mounted volume and the container can write to it. |
| Tools missing in the host | Confirm the server lists them: `ms365-mcp-run --services <svc>` and check host logs; verify `--read-only` isn't hiding writes. |

---

## Development

Run the test suite:

```bash
cd src/ms365-mcp && uv run pytest tests/ -v
```

Key entry points:

| Function | File | Purpose |
| --- | --- | --- |
| `collect_tools(resolve_client, services, read_only)` | `ms365_server.py` | Gather tool specs from `tools_*.py` modules. |
| `build_server(resolve_client, services, read_only)` | `ms365_server.py` | Build a FastMCP app from the specs. |
| `_register_auth_status_tool(mcp, manager)` | `ms365_server.py` | Register the always-on `ms365__check_auth_status` tool. |
| `run_stdio(mcp)` / `run_http(mcp, host, port)` | `ms365_server.py` | Transports. |
| `build_http_app(mcp)` | `ms365_server.py` | Starlette ASGI app (`/mcp`, `/health`). |
| `main()` | `ms365_server.py` | CLI entry point (`ms365-mcp-run`). |
| `IdentitySessionManager.get_or_create(identity, ctx)` | `identity_session.py` | Lazily resolve/authenticate an identity's `GraphClient`; raises `AuthPendingError` while sign-in is pending. |
| `IdentitySessionManager.status_for(identity)` | `identity_session.py` | Backing implementation of `ms365__check_auth_status`. |

To add a tool, add it to the relevant `tools_*.py` file — it is automatically
picked up, no changes to `ms365_server.py` required. Each `register_*_tools()`
function still takes `(registry, resolve_client)`; its inner `_h()` wrapper
resolves the caller's `GraphClient` via `resolve_client(ctx)` on every call
rather than closing over one fixed client.
