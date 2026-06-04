# Microsoft 365 MCP Server

`pincer-ms365-mcp` exposes all **69 Microsoft 365 tools** — Outlook email,
Calendar, OneDrive, Microsoft To Do, Teams, Contacts, and OneNote — as a
standalone [Model Context Protocol](https://modelcontextprotocol.io) (MCP)
server. Any MCP host can connect to it: Claude Desktop, Cursor, VS Code, or
Pincer's own MCP client.

It is built on Microsoft Graph and lives in `src/ms365-mcp/ms365/`.

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
        │  MCP protocol (stdio or streamable HTTP)
        ▼
pincer-ms365-mcp            ← mcp_server.py (low-level mcp SDK Server)
        │  reuses register_*_tools() via a collecting shim
        ▼
GraphClient                 ← httpx, bearer token, retry, pagination
        │  Authorization: Bearer <token>
        ▼
Microsoft Graph API (https://graph.microsoft.com/v1.0)
```

- **Tool collection.** `collect_tools()` calls the existing
  `register_email_tools()`, `register_calendar_tools()`, … functions through a
  `_CollectingRegistry` shim that matches `ToolRegistry.register`'s signature, so
  every tool's name, JSON schema, description, and approval flag is reused
  verbatim — no duplication.
- **Serving.** `build_server()` registers those specs on a low-level
  `mcp.server.Server`: `list_tools` returns explicit `inputSchema`s, and
  `call_tool` validates input against the schema, then dispatches to the original
  async handler.
- **Auth.** The server consumes a cached MSAL token; it does not perform the
  interactive login itself.

Source: [`src/ms365-mcp/ms365/ms365_server.py`](../../src/ms365-mcp/ms365/ms365_server.py)

---

## Prerequisites

| Requirement | Notes |
| --- | --- |
| Python ≥ 3.12 | Same as the rest of Pincer. |
| `mcp` SDK | Installed by the `[mcp]` extra. |
| `msal` | Installed by the `[ms365]` extra. |
| Azure app registration | A public client with delegated Graph permissions (below). |
| A signed-in token cache | Created once by `ms365-mcp-setup`. |

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
3. **Redirect URI:** none needed for device code. (For the interactive flow,
   add a "Mobile and desktop applications" platform with
   `http://localhost`.)
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
| Calendar | `Calendars.ReadWrite`, `OnlineMeetings.ReadWrite` |
| OneDrive | `Files.ReadWrite.All` |
| To Do | `Tasks.ReadWrite` |
| Teams | `Team.ReadBasic.All`, `Channel.ReadBasic.All`, `ChannelMessage.Send`, `Chat.ReadWrite` |
| Contacts | `Contacts.ReadWrite` |
| OneNote | `Notes.ReadWrite.All` |

If you only run a subset of services (`--services`), you only need that subset's
scopes plus `User.Read`.

---

## Authentication

### Step 1 — Set credentials in `.env`

Add these to your `.env` file before running the setup wizard:

```bash
PINCER_MS365_CLIENT_ID=<your-azure-app-client-id>
PINCER_MS365_TENANT_ID=consumers   # or "common" / your org tenant GUID
```

### Step 2 — Run the setup wizard

```bash
ms365-mcp-setup
```

The wizard reads the credentials from `.env` (or the shell environment) and
runs MSAL's **device-code flow**: it prints a code and a URL
(`https://microsoft.com/devicelogin`); after you sign in, the token is written to:

```
~/.pincer/ms365_token_cache.json   (file mode 0600)
```

- **Refresh** is automatic — MSAL uses the cached refresh token to mint new
  access tokens silently and rewrites the cache. The server never prompts.
- **The server only reads/refreshes** this cache. If it is missing or empty, the
  server exits with a message telling you to run `ms365-mcp-setup`.
- The cache path can be overridden with `MS365_TOKEN_CACHE_PATH` (standalone server) or
  `PINCER_MS365_TOKEN_CACHE_PATH` in `.env` (when running via `pincer run`).

---

## Configuration

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `PINCER_MS365_CLIENT_ID` | — | Azure app (client) ID. **Required.** |
| `PINCER_MS365_TENANT_ID` | `consumers` | `consumers` for personal accounts, `common`, `organizations`, or your tenant GUID. |
| `TRANSPORT` | `stdio` | `stdio` or `http` (overridden by `--transport`). |
| `HOST` | `127.0.0.1` | HTTP bind host (overridden by `--host`). |
| `PORT` | `18820` | HTTP bind port (overridden by `--port`). |
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
pincer-ms365-mcp

# streamable HTTP — endpoint at http://127.0.0.1:18820/mcp
pincer-ms365-mcp --transport http --host 127.0.0.1 --port 18820

# expose only a subset of services
pincer-ms365-mcp --services email,calendar

# read-only mode (hide every write/destructive tool)
pincer-ms365-mcp --read-only
```

### Command-line options

| Flag | Default | Description |
| --- | --- | --- |
| `--transport {stdio,http}` | `stdio` | Transport to serve. |
| `--host HOST` | `127.0.0.1` | Bind host (HTTP only). |
| `--port PORT` | `18820` | Bind port (HTTP only). |
| `--services LIST` | all | Comma-separated subset: `email,calendar,onedrive,todo,teams,contacts,onenote`. |
| `--tenant ID` | from config | Override the Microsoft tenant id. |
| `--read-only` | off | Expose only read tools; hide writes (send/create/update/delete/move/share). |

HTTP mode also serves `GET /health` (and `GET /`) returning
`{"status": "healthy", "service": "pincer-ms365-mcp"}`.

> **Port note:** the default `18820` deliberately avoids `18800`, which Pincer's
> embedded MCP server uses.

---

## Connecting MCP hosts

### Claude Desktop / Cursor (stdio)

Add to `claude_desktop_config.json` (Claude Desktop) or the equivalent MCP
config:

```json
{
  "mcpServers": {
    "ms365": {
      "command": "pincer-ms365-mcp",
      "args": ["--transport", "stdio"],
      "env": {
        "PINCER_MS365_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
        "PINCER_MS365_TENANT_ID": "consumers"
      }
    }
  }
}
```

### Pincer's own MCP client (stdio — default)

The `pincer.toml` shipped in the repo uses stdio transport. Pincer spawns the
server as a sandboxed subprocess, so credentials and the token cache path must
be declared explicitly in the `env` dict — the sandbox intentionally restricts
`HOME` and does not inherit your shell environment:

```toml
[[mcp.servers]]
name      = "ms365"
transport = "stdio"
command   = "python"
args      = ["${PINCER_SRC_DIR:-$PWD}/src/ms365-mcp/ms365/ms365_server.py"]
env       = {
  MS365_CLIENT_ID         = "${PINCER_MS365_CLIENT_ID}",
  MS365_CLIENT_SECRET     = "${PINCER_MS365_CLIENT_SECRET}",
  MS365_TENANT_ID         = "${PINCER_MS365_TENANT_ID}",
  MS365_TOKEN_CACHE_PATH  = "${HOME}/.pincer/ms365_token_cache.json"
}
approval_required = ["*"]
```

`${PINCER_MS365_*}` values are interpolated from your `.env` file at startup.
`${HOME}` resolves from the parent process so the subprocess can find the token
cache even though the sandbox sets `HOME=/tmp`.

### Pincer's own MCP client (HTTP)

Run the server over HTTP, then register it in `pincer.toml`:

```toml
[[mcp.servers]]
name = "ms365"
transport = "http"
url = "http://127.0.0.1:18820/mcp"
```

Tools then appear to the agent as `ms365__outlook__list_messages`, etc.
(the configurable server-name prefix plus the original tool name).

---

## Docker

The server runs from the **root image** — `Dockerfile` already installs the
`[mcp]` and `[ms365]` extras (`uv sync --all-extras`) and the `pincer-ms365-mcp`
entry point, so no separate image is needed. A ready-made compose file is
provided at
[`docker-compose.ms365-mcp.yml`](../../docker-compose.ms365-mcp.yml):

```bash
# 1. Set credentials in .env, then authenticate once on the host:
ms365-mcp-setup

# 2. Start the server (UID/GID let the container read/write the token cache):
UID=$(id -u) GID=$(id -g) \
  docker compose -f docker-compose.ms365-mcp.yml up --build
# MCP endpoint → http://localhost:18820/mcp
```

Notes:

- `ms365-mcp-setup` is interactive, so run it **on the host first**. The
  container only consumes the cached token.
- The compose file mounts `~/.pincer` and runs as your host UID/GID so MSAL can
  read the token **and persist refreshed tokens**. A token cache mounted
  read-only would break silent refresh.
- stdio also works in a container (`docker run -i … pincer-ms365-mcp`) for hosts
  that speak MCP over stdio, but HTTP is the better fit for a long-running
  service with a health check.

---

## Tool reference

All 69 tools keep their original names and parameters. Parameter notation:
`name` (type) — required params are **bold**; optional params show their default.
The **Write** column marks tools that modify data; those carry the MCP
`destructiveHint` annotation and are hidden by `--read-only`.

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

### Calendar (12)

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
| `outlook__create_online_meeting` | **`subject`**, **`start`**, **`end`** | ✅ |

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

### Teams (7)

| Tool | Parameters | Write |
| --- | --- | --- |
| `teams__list_teams` | — | |
| `teams__list_channels` | **`team_id`** | |
| `teams__list_channel_messages` | **`team_id`**, **`channel_id`**, `max_results`=`25` | |
| `teams__get_channel_message` | **`team_id`**, **`channel_id`**, **`message_id`** | |
| `teams__send_channel_message` | **`team_id`**, **`channel_id`**, **`body`**, `body_type`=`text` | ✅ |
| `teams__list_chats` | `max_results`=`25` | |
| `teams__send_chat_message` | **`chat_id`**, **`body`**, `body_type`=`text` | ✅ |

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
the 34 write tools are then neither listed nor callable (leaving 35 read tools).

---

## Error handling

- **Input validation.** The MCP SDK validates every call against the tool's
  `inputSchema` before dispatch; bad arguments return an MCP validation error.
- **Graph / auth errors** are caught and returned as readable tool text
  (`[Error] <tool>: <ExceptionType>: <message>`) rather than crashing the server.
- **Rate limits.** `GraphClient` retries `429` responses up to 3 times, honoring
  the `Retry-After` header.
- **Pagination.** List endpoints follow `@odata.nextLink` up to each tool's
  `max_results`.

---

## Security

- **Token storage.** `~/.pincer/ms365_token_cache.json` is written with mode
  `0600`. Treat it as a credential — it contains refresh tokens.
- **Bind locally.** The HTTP transport defaults to `127.0.0.1`. Only bind to
  `0.0.0.0` (e.g. in Docker) behind a trusted network or reverse proxy; the
  endpoint is unauthenticated.
- **Least privilege.** Grant only the Graph scopes for the services you run, and
  use `--services` / `--read-only` to narrow the exposed surface.
- **Delegated access.** All actions run as the signed-in user — the server can
  do nothing the user cannot already do.

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `No cached Microsoft 365 token…` on startup | Run `ms365-mcp-setup` on the host first. |
| `PINCER_MS365_CLIENT_ID is not set` | Add `PINCER_MS365_CLIENT_ID` to `.env`, then re-run `ms365-mcp-setup`. |
| `Microsoft 365 client_id is not configured` | Set `PINCER_MS365_CLIENT_ID` in `.env`. |
| `MCP 'ms365' connection failed (attempt N):` with blank message | The sandboxed subprocess started but timed out before the MCP protocol initialised. Most likely cause: token cache not found because `HOME=/tmp` in the sandbox. Ensure `MS365_TOKEN_CACHE_PATH="${HOME}/.pincer/ms365_token_cache.json"` is in the server's `env` dict in `pincer.toml` (already the default). |
| `Device flow failed` during setup | Enable **Allow public client flows** on the Azure app. |
| `AADSTS9002346` error during setup | Your app is registered for personal accounts only — use `PINCER_MS365_TENANT_ID=consumers`. |
| Graph `401` / `403` in tool output | Missing scope or unconsented permission — add it in Azure and re-run `ms365-mcp-setup`. |
| `429` errors | Rate limited; the client retries, but reduce call volume. |
| Docker: token "permission denied" | Run with `UID=$(id -u) GID=$(id -g)` so the container can read/write the mounted cache. |
| Tools missing in the host | Confirm the server lists them: `pincer-ms365-mcp --services <svc>` and check host logs; verify `--read-only` isn't hiding writes. |

---

## Development

Run the test suite:

```bash
cd src/ms365-mcp && uv run pytest tests/ -v
```

Key entry points in
[`ms365_server.py`](../../src/ms365-mcp/ms365/ms365_server.py):

| Function | Purpose |
| --- | --- |
| `collect_tools(client, services, read_only)` | Gather tool specs from `tools_*.py` modules. |
| `build_server(client, services, read_only)` | Build a low-level MCP `Server` from the specs. |
| `run_stdio(server)` / `run_http(server, host, port)` | Transports. |
| `build_http_app(server)` | Starlette ASGI app (`/mcp`, `/health`). |
| `main()` | CLI entry point (`pincer-ms365-mcp`). |

To add a tool, add it to the relevant `tools_*.py` file — it is automatically
picked up, no changes to `ms365_server.py` required.
