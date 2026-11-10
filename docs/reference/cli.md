# Pincer CLI Reference

Complete command reference across all sprints (1–13). For the full list of tools the agent itself can call, see `docs/TOOLS_CATALOG.md` in the repository — **304 first-party tools + unlimited via MCP**.

## Global

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer --help` | Show all available commands | 1 |
| `pincer --version` | Show Pincer version | 1 |

## Core Commands

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer init` | Interactive setup wizard (provider, keys, channels, .env) | 4 |
| `pincer run` | Start agent (all configured channels + API server). If a background Pincer is running, attach to it instead ([Background mode](#background-mode-daemon)) | 1 |
| `pincer run -d` | Start agent in the background; returns once all channels are up (`--wait-timeout`, default 60 s) | — |
| `pincer logs` | Print the background agent's log, raw (`-n/--tail N`, `-f/--follow`) | — |
| `pincer status` | Show whether the background agent is running or starting, with its pid, uptime, port, channels, version and working directory | — |
| `pincer stop` | Stop the background agent gracefully and wait for it to exit (`--timeout`, default 90 s; `--force` kills) | — |
| `pincer restart` | Stop the background agent, then start a new one from the current directory and environment | — |
| `pincer run tasks` | Standalone background-task worker only — no channels/API/MCP export (requires `PINCER_TASK_BROKER=redis`) | 14 |
| `pincer chat` | CLI chat interface (no messaging app needed) | 4 |
| `pincer config` | Show current configuration (masked secrets) | 1 |
| `pincer whatsapp setup` | Pair WhatsApp via QR code | 3 |
| `pincer google auth` | Google Calendar OAuth consent flow (legacy calendar-only token: the 12 calendar tools) | 3 |
| `pincer google setup` | Google Workspace OAuth consent flow (113 tools incl. Meet v2) | 9 |
| `ms365-mcp-setup` | Microsoft 365 device code auth flow, default identity (62 tools) | 10 |
| `pincer signal setup` | Open the Signal QR-code link to register/link a device (signal-cli sidecar) | 7.5 |
| `pincer signal status` | Check Signal API health and registered accounts | 7.5 |
| `pincer signal test <recipient>` | Send a test message via Signal | 7.5 |
| `pincer slack setup` | Interactive Slack bot token setup (71 tools) | 11 |

## Background mode (daemon)

`pincer run -d` runs Pincer in the background, without `nohup` or Docker. Everything below is
scoped to the data directory (`PINCER_DATA_DIR`, default `~/.pincer`). Two Pincers with different
data directories don't see each other, and a `pincer run tasks` worker is never touched.

```console
$ pincer run -d
…startup log…
Pincer started (pid 4182) — channels: signal  'pincer logs -f' to follow

$ pincer status
Pincer is running (pid 4182, up 3m 07s) — port 8080, channels: signal, version 0.8.0, cwd /home/me/pincer

$ pincer stop
Stopping Pincer (pid 4182)…
Pincer stopped (pid 4182)
```

**`pincer run -d`**
- Starts Pincer in a session of its own, so it keeps running after the terminal closes or the SSH
  session ends.
- It runs in your current directory with your environment, so it loads the same `.env` a
  foreground `pincer run` from there would.
- It shows the startup log as it streams and returns once every channel has started.
- If Pincer exits before that, you get `Pincer exited before it was ready (exit code N). Last 20
  lines of …` followed by those lines, and exit code 1.
- If `--wait-timeout` (default 60 s) passes first, you get `Pincer is still starting after 60 s — it
  keeps running; check 'pincer logs -f'` and exit code 1. Pincer is left starting, not killed.
- If it is already running, you get `Pincer is already running (pid 4182, up 1s). Use 'pincer
  restart' to apply config changes.` and exit code 0.

**`pincer logs`** prints the log raw, with no colours, so `pincer logs | grep ERROR` works.

| Command | Prints |
|---------|--------|
| `pincer logs` | The whole current log |
| `pincer logs -n 100` / `--tail 100` | The last 100 lines |
| `pincer logs -f` | The last 50 lines, then new lines as they are written. It ends by itself once Pincer has exited, after its last lines. Ctrl+C only stops following. |
| `pincer logs -f -n 0` | Only new lines |

- The log is `<data_dir>/logs/pincer.log`. It holds everything the process prints: log records, the
  startup banner and crash tracebacks. Phone numbers are masked, as they are on the terminal.
- Each `run -d` rotates the previous log to `pincer.log.1`, and keeps five previous sessions
  (`.1` newest … `.5` oldest). There is no rotation while Pincer runs.
- When Pincer isn't running, `logs` still prints the last session's log, with `Pincer is not
  running — showing the last session's log` on stderr. With no log at all it says `No logs yet —
  start with 'pincer run -d'`. Both exit 0.

**`pincer stop`**
- Sends SIGTERM, the same graceful shutdown as Ctrl+C or `docker stop`: active calls drain with
  their spoken ending.
- It waits until the process has exited, up to `--timeout`. The 90 s default is sized to the voice
  channel's 60 s call drain, plus time for the worker and the API to stop.
- It never kills on its own. On timeout it prints `Pincer is still shutting down after 90 s —
  'pincer stop --force' to kill it` and exits 1.
- `pincer stop --force` kills the daemon at once, together with its process group (the MCP servers
  and skills it started), and prints `Pincer killed (pid 4182)`.
- It signals the pid in Pincer's record only once it has checked that the process with that pid
  is the one that wrote the record: on Linux and macOS, by the exact start time the kernel keeps
  for it. If the record can't be read or is gone, or the pid now belongs to another process, it
  signals nothing (`--force` included): it says why, waits, and points to
  `lsof <data dir>/pincer.lock` to find the process holding the lock. On other platforms, or
  when Pincer couldn't read its own start time, it signals the record's pid while the lock is held.
- With nothing running it prints `Pincer is not running` and exits 0, so it's safe in scripts. If a
  foreground Pincer holds the port, it also tells you to stop that one with Ctrl+C in its terminal.

**`pincer restart`**
- Runs `stop` with the same `--timeout`, then `run -d` with the same `--wait-timeout`.
- The new Pincer uses the directory and environment you run `restart` from, so this is how you
  apply `.env` and `pincer.toml` changes.
- With nothing running it just starts one (`Pincer is not running — starting it`).
- If the old one hasn't exited within the timeout, it exits 1 without starting a new one, so two
  never overlap.
- There is no `restart --force`. If the old one hangs, run `pincer stop --force`, then
  `pincer run -d`.

**`pincer run` while a background Pincer is up** attaches to it instead of starting a second
instance:

```console
$ pincer run
Pincer is already running in the background (pid 4182) — attaching
…the last 50 log lines, then new ones…
^C
Stopping Pincer (pid 4182)…
…the shutdown log…
Shutdown complete
Pincer stopped (pid 4182)
```

- The first Ctrl+C stops Pincer gracefully, as `pincer stop` does, and keeps showing the log until
  it has exited.
- A second Ctrl+C during that shutdown only detaches: `Detached — Pincer (pid 4182) keeps shutting
  down in the background`.
- Closing the terminal only detaches too. Pincer has a session of its own and never receives the
  hangup.
- If Pincer is stopped elsewhere, the attached `run` ends with `Pincer has exited (pid 4182)`.

**Where it isn't available**

| Situation | Message (exit code 1) |
|-----------|-----------------------|
| Windows (`run -d`, `logs`, `status`, `stop`, `restart`) | `Daemon mode isn't supported on Windows — use a service manager.` |
| Inside a container (`run -d`, `restart`) | `Pincer is running inside a container — use 'docker compose up -d' to run it in the background.` |
| `pincer run tasks -d` | `Daemon mode applies to the full agent only — run 'pincer run tasks' in the foreground.` |

## Cost & Budget

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer cost` | Show today's spending summary | 1 |
| `pincer cost --days 7` | Show spending for last N days | 5 |
| `pincer cost --by-model` | Breakdown by LLM model | 5 |
| `pincer cost --by-tool` | Breakdown by tool | 5 |
| `pincer cost --export costs.json` | Export cost data to JSON | 5 |

## Security & Audit

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer doctor` | Run 25+ security checks, traffic-light report | 5 |
| `pincer doctor --json` | Output doctor report as JSON | 5 |
| `pincer audit` | View last 50 audit log entries | 5 |
| `pincer audit --limit 100` | View last N entries | 5 |
| `pincer audit --action tool_call` | Filter by action type | 5 |
| `pincer audit --user USER_ID` | Filter by user | 5 |
| `pincer audit --since 2026-02-20` | Filter from date | 5 |
| `pincer audit --export audit.json` | Export audit log to JSON | 5 |

### Audit Action Types

`tool_call`, `llm_request`, `llm_response`, `file_read`, `file_write`,
`network_request`, `skill_execute`, `auth_attempt`, `config_change`,
`budget_alert`, `rate_limit_hit`, `message_received`, `message_sent`, `error`

## Identity & API access

The REST API and the dashboard authenticate identities: a password for the
dashboard login, an API key for headless consumers such as the web chat
widget. There are no roles — any identity with a password or a key has full
API access. See [REST API → Authentication](rest-api.md#authentication).

| Command | Description |
|---------|-------------|
| `pincer identity list` | Identities with status (active/inactive), source (config/cli/auto), email, channels, whether a password is set, and the masked API key |
| `pincer identity create <name> [password] [--api-key \| --no-password] [--channel CH:ID …] [--email EMAIL] [--display-name NAME] [--timezone TZ]` | Create an identity with its channels and what it signs in with: a password (prompted for, hidden and confirmed, when omitted), `--api-key` (printed once), or `--no-password` (chat only; needs a `--channel`). Names are letters, digits and underscores; `me` and names starting with `usr_` are reserved |
| `pincer identity add-channel <name> <channel> <id>` | Link a channel id. Telegram ids must be numeric; a WhatsApp phone is swapped for its LID on the first message. A channel id that belongs to another identity is refused |
| `pincer identity remove-channel <name> <channel> <id>` | Unlink a channel id |
| `pincer identity set <name> [--display-name] [--email] [--timezone] [--preferred-channel] [--add-phone +E164 …] [--remove-phone +E164 …]` | Set profile fields and confirmed phones |
| `pincer identity deactivate <name>` | Switch an identity off without deleting it: its messages are handled as a guest's, its phones no longer identify it on calls, it gets no proactive messages, and it cannot sign in. Its sessions end; its password and API key are kept. A running app sees it at once |
| `pincer identity activate <name>` | Switch it back on |
| `pincer identity set-password <name> [password]` | Reset a password, or give one to an identity that has none (8–256 characters). Prompts, hidden and confirmed, when the password is omitted — which keeps it out of the shell history. Signs the identity out everywhere |
| `pincer identity api-key <name> [--force]` | Generate an API key (`pnc_…`) and print it once; afterwards only a masked form is shown. `--force` replaces an existing key, which stops working immediately |
| `pincer identity revoke <name>` | Remove the identity's password and API key and end its sessions. The identity, its channels and its history stay |

```bash
pincer identity create alice --email alice@example.com   # prompts for the password; sign in as "alice" or the email
pincer identity api-key alice                            # for the web chat widget / scripts
pincer identity create widget --api-key                  # a key-only identity
pincer identity create bob --no-password --channel telegram:123456 --channel whatsapp:+4915100000001
pincer identity set bob --add-phone +4915100000001 --timezone Europe/Berlin
pincer identity deactivate bob                           # bob's next message is a guest's; `activate` restores him
```

A forgotten password is reset with `set-password`. An email signs in for one
identity only: `create` refuses an email that already belongs to another.

These commands work in every `PINCER_IDENTITY_MANAGEMENT` mode (see
[Identities](#identities)), and what they make is a `cli` identity, which no
sync of the `[identity]` TOML removes. For a person the TOML lists, the next
start puts back what the TOML states; the command warns when that applies.

In `sync` mode, config identities left with no channel link are deleted at
startup — unless they hold a password or an API key, so an identity made only
for the dashboard or for a script survives. Removing someone from the TOML
therefore does not take their API access away; `pincer identity revoke <name>`
or `deactivate` does.

## Skills

Skills are discovered from their roots: `src/pincer/skills/` (bundled, shipped
inside the package) and `skills_dir` (user — `~/.pincer/skills/` by default, or an
`s3://` / `az://` / `gs://` prefix). There is no install step.

| Command | What it does |
|---|---|
| `pincer skills rebuild` | Clear cached remote skill files, rescan both roots, and make a running `pincer run` reload: the dashboard on its next request, the agent within `skills_index_watch_interval_seconds` (5 min by default) |
| `pincer skills rebuild --keep-cache` | Same, but keep cloudpathlib's cached copies of remote files |

See the [Skills Guide](../guides/skills-guide.md) for the format and the
`load_skill` / `load_skill_reference` / `run_skill_script` tools the agent
uses to read and act on them.

## Memory

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer memory search <query>` | Search conversation memory | 5 |
| `pincer memory stats` | Show memory usage stats | 5 |
| `pincer memory clear --user USER_ID` | Clear memory for a user | 5 |
| `pincer memory export --user USER_ID` | Export user memories to JSON | 5 |

Each command prints a `Backend:` line showing which storage engine is active for the current run:

```
Backend: sqlite  path=/path/to/pincer.db
```
or
```
Backend: mcp  server=sqlite-vec-memory
```

When `PINCER_MEMORY_BACKEND=mcp`, the command connects directly to the configured MCP memory server (same process as `pincer run`). The MCP server must be reachable — if it is not found in `pincer.toml` or fails to connect, the command exits with an error.

### Memory backend configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PINCER_MEMORY_BACKEND` | `sqlite` | Storage engine: `sqlite` (local DB file) or `mcp` (external MCP server) |
| `PINCER_MEMORY_MCP_SERVER` | `sqlite-vec-memory` | MCP server name used when `PINCER_MEMORY_BACKEND=mcp` |

The `sqlite-vec-memory` MCP server ships bundled with Pincer (see `src/sqlite-vec-memory-mcp/`). To enable it, add it to your `pincer.toml`:

```toml
[[mcp.servers]]
name      = "sqlite-vec-memory"
transport = "stdio"
command   = "uv"
args      = ["run", "--project", "src/sqlite-vec-memory-mcp", "memory-server"]
```

Then switch the memory backend:

```bash
PINCER_MEMORY_BACKEND=mcp pincer run
```

## Proactive Agent / Background Tasks

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer schedule list` | List all scheduled tasks (read-only, all users) | 5 |
| `pincer run tasks` | Standalone background-task worker for horizontal scaling | 14 |

Scheduled tasks now run through a durable, retryable [repid](https://pypi.org/project/repid/)
actor system instead of an in-process fire-loop, and are created, removed,
and toggled entirely via chat (`schedule_create`/`list`/`remove`/`toggle`
tools) rather than the CLI. See [Background Tasks](../core-components/background-tasks.md)
for the full architecture, deployment topologies (single-process vs.
Redis-backed split worker), and config reference.

## Google Workspace

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer google setup` | One-time OAuth consent — opens browser, saves token to `~/.pincer/google_workspace_token.json` | 9 |

### What `pincer google setup` does

1. Finds the OAuth client: `google_credentials.json` in the Google OAuth directory (below), else `PINCER_GOOGLE_CREDENTIALS_JSON` — shows instructions if neither is there
2. Launches `InstalledAppFlow` — opens browser at Google consent page
3. Requests all scopes covering Gmail, Calendar, Drive, Docs, Sheets, Slides, Meet, Tasks, and Contacts/People API
4. Saves the token with `chmod 0o600`
5. Reports: `Google Workspace tools enabled (113 tools)`

After setup, the 113 `google__*` tools are auto-registered every time `pincer run` is started — no further configuration needed. Full catalog: see `docs/TOOLS_CATALOG.md` in the repository.

With `--print-env` nothing is written: the token is printed as `PINCER_GOOGLE_TOKEN_JSON=...` for `.env` or a secret store.

### Where the Google files live

The OAuth directory does not depend on the directory Pincer is started from:

1. `PINCER_GOOGLE_OAUTH_DIR`, when set — use an absolute path (a relative one depends on the working directory and is logged);
2. otherwise the first of `PINCER_DATA_DIR` (default `~/.pincer`) and `./data` that holds a token file;
3. otherwise the first of them that holds `google_credentials.json`;
4. otherwise the data directory (where `pincer google setup` writes).

`./data` in the working directory is **deprecated**: it is only used when it holds one of those files, with a warning at startup — move them to the data directory or set `PINCER_GOOGLE_OAUTH_DIR`.

The token and the OAuth client can each come from a file or an env var:

| Token | OAuth client | Result |
|-------|--------------|--------|
| `google_workspace_token.json` (or legacy `google_token.json`) | — | The token file carries its client id/secret; `google_credentials.json` is only needed to run `pincer google setup` again |
| `PINCER_GOOGLE_TOKEN_JSON` with `client_id`/`client_secret` | — | Used as is, no files needed. If `google_credentials.json` / `PINCER_GOOGLE_CREDENTIALS_JSON` holds a *different* client, a warning says so: the token keeps its own client |
| `PINCER_GOOGLE_TOKEN_JSON` without `client_id`/`client_secret` | `google_credentials.json` | The client is filled in from the file |
| `PINCER_GOOGLE_TOKEN_JSON` without `client_id`/`client_secret` | `PINCER_GOOGLE_CREDENTIALS_JSON` | The client is filled in from the env var (the file wins when both are set) |

`PINCER_GOOGLE_TOKEN_JSON` wins over the token files. If it cannot be decoded, a warning says so and the token files are used. A token without a client and no client anywhere turns the Google tools off at startup with a warning. `pincer doctor` (check `google_workspace_token`) shows the directory it looked in and where the token and client came from.

### Getting `google_credentials.json`

1. Open [Google Cloud Console](https://console.cloud.google.com)
2. Create/select a project → APIs & Services → Enable APIs (Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks, People)
3. Credentials → Create → OAuth 2.0 Client ID → Desktop App
4. Download JSON → save as `google_credentials.json` in the data directory (`~/.pincer/` by default, or `PINCER_GOOGLE_OAUTH_DIR`)

## Microsoft 365

| Command | Description |
|---------|-------------|
| `ms365-mcp-setup` | One-time device code auth for the `"default"` identity slot — reads credentials from `.env`, displays device code, caches token under `~/.pincer/ms365_mcp/` |

### What `ms365-mcp-setup` does

1. Reads `PINCER_MS365_CLIENT_ID` and `PINCER_MS365_TENANT_ID` from environment or `.env` file
2. Exits with instructions if `PINCER_MS365_CLIENT_ID` is not set
3. Starts MSAL device code flow — prints `https://microsoft.com/devicelogin` + one-time code
4. Waits while you complete sign-in in a browser
5. Caches the token at `~/.pincer/ms365_mcp/default_token_cache.json` (mode `0600`)
6. Reports: `Microsoft 365 authenticated! … 62 Microsoft 365 tools are now available under the 'default' identity`

This is optional — ms365-mcp authenticates every identity lazily on its own
first tool call, so this step isn't required for Pincer's normal multi-user
operation. After setup, start the MCP server with `ms365-mcp-run --transport http`.
See the [Microsoft 365 MCP Guide](../guides/ms365-mcp.md) for the full
tool catalog and multi-user auth flow.

## Slack Native

The Slack *channel* (bot that responds to @mentions and DMs) and the Slack *native tools* (71 tools for reading/posting messages, managing channels, users, files) are both enabled by setting `PINCER_SLACK_BOT_TOKEN`.

| Requirement | For |
|---|---|
| `PINCER_SLACK_BOT_TOKEN=xoxb-…` | All 71 Slack tools + channel |
| `PINCER_SLACK_USER_TOKEN=xoxp-…` (optional) | `slack__search_messages` and `slack__search_files` (full-text search requires a user token per Slack API) |

Full catalog: see `docs/TOOLS_CATALOG.md` in the repository.

## MCP

| Command | Description |
|---------|-------------|
| `pincer mcp list` | Connected MCP servers and status |
| `pincer mcp tools` | List every MCP tool currently registered |
| `pincer mcp test <server>` | Smoke-test a specific MCP server connection |
| `pincer mcp call <server> <tool> --arg v` | Invoke a tool directly |
| `pincer mcp status` | Show live connectivity status for all configured MCP servers |
| `pincer mcp search <query>` | Search MCP Registry and ClawHub for available MCP servers |
| `pincer mcp install <name>` | Install an MCP server: download, scan, and add to `pincer.toml` |
| `pincer mcp scan <path>` | Run an AST security scan on a local directory or installed package |
| `pincer mcp uninstall <name>` | Remove an MCP server from `pincer.toml` and clean up staging files |
| `pincer mcp serve` | Start a standalone Pincer MCP server (no full agent required) |
| `pincer mcp server status` | Show whether Pincer's outbound MCP export server is enabled and its config |
| `pincer mcp server config` | Print MCP client config JSON for Claude Desktop / Cursor |

See [MCP Guide](../guides/mcp-guide.md) for the full MCP client documentation.

### Getting an Azure App client ID

1. Open [Azure App Registrations](https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
2. **New registration** → name it, choose account type, leave redirect URI empty
3. Under **Authentication** → **Advanced settings** → enable **Allow public client flows**
4. Copy the **Application (client) ID**

See [Microsoft 365 MCP Guide](../guides/ms365-mcp.md) for the full walkthrough.

## Environment Variables

All environment variables use the `PINCER_` prefix. See `.env.example` for the
complete list. Key variables added in Sprint 5:

| Variable | Default | Description |
|----------|---------|-------------|
| `PINCER_JWT_SECRET` | (generated) | Signs session tokens (HS256), min 32 chars. Unset = generated once into `<data_dir>/jwt_secret`; set it in production |
| `PINCER_JWT_ACCESS_TTL_SECONDS` | `1800` | Access token lifetime |
| `PINCER_JWT_REFRESH_TTL_SECONDS` | `604800` | Refresh token lifetime |
| `PINCER_AUTH_DISABLED` | `false` | Serve `/api/*` without authentication — local dev/tests only |
| `PINCER_DASHBOARD_HOST` | `127.0.0.1` | API server bind host |
| `PINCER_DASHBOARD_PORT` | `8080` | API server port |
| `PINCER_AUDIT_DISABLED` | `false` | Disable audit logging |
| `PINCER_RATE_MESSAGES_PER_MIN` | `30` | Per-user message rate limit |
| `PINCER_RATE_TOOLS_PER_MIN` | `20` | Per-user tool call rate limit |
| `PINCER_MAX_CONCURRENT_LLM` | `5` | Max concurrent LLM requests |

### Background tasks

| Variable | Default | Description |
|----------|---------|-------------|
| `PINCER_TASK_BROKER` | `memory` | `memory` (single-process) or `redis` (required for `pincer run tasks`) |
| `PINCER_TASK_BROKER_URL` | (empty) | Broker URL, e.g. `redis://localhost:6379/0` (required when `task_broker=redis`) |
| `PINCER_TASK_MAX_RETRIES` | `3` | Max attempts for a background task actor before giving up |
| `PINCER_TASK_POLL_INTERVAL` | `60` | Seconds between checks for due cron schedules |
| `PINCER_SCHEDULE_TOOL_ENABLED` | `true` | Enable the `schedule_create`/`list`/`remove`/`toggle` chat tools |
| `PINCER_MAX_SCHEDULES_PER_USER` | `20` | Max active recurring schedules per user |
| `PINCER_MIN_SCHEDULE_INTERVAL_MINUTES` | `15` | Reject schedules that would fire more often than this |

See [Background Tasks](../core-components/background-tasks.md) for details.

### Identities

Identities are configured in an `[identity]` section of `pincer.toml` / `pincer.local.toml` — one table per person, keyed by their `pincer_user_id` — and with [`pincer identity …`](#identity-api-access):

```toml
[identity.johndoe]
display_name      = "John Doe"
email             = "john.doe@domain.tld"
timezone          = "Europe/Berlin"
preferred_channel = "telegram"
phones            = ["+4915112345678"]

[[identity.johndoe.channels]]
channel         = "telegram"
channel_user_id = 123456

[identity.jane]
display_name      = "Jane Austin"
preferred_channel = "whatsapp"

[[identity.jane.channels]]
channel         = "telegram"
channel_user_id = "janeAustin"

[[identity.jane.channels]]
channel         = "whatsapp"
channel_user_id = "491111111111"
```

The key sets the `pincer_user_id` used as the memory tag (e.g. `user:johndoe`), stable across DB rebuilds. Channel-specific identifiers (email addresses for Slack, phone numbers for WhatsApp, @usernames for Telegram) are resolved to internal IDs by each channel driver at startup before being stored. A person may have just one `[[...channels]]` entry — useful for pre-registering a roster incrementally as new channel IDs become known. `pincer.local.toml`'s `[identity]` section merges into `pincer.toml`'s per person, per field, appending new person keys and leaving unspecified fields inherited from the base entry — see [Local overrides — pincer.local.toml](../guides/mcp-guide.md#local-overrides--pincerlocaltoml) for the same merge pattern applied to `[[mcp.servers]]`. As with those entries, `channels` is replaced wholesale by an override rather than merged per-channel.

`phones` lists the person's own numbers in E.164 (spaces, dashes and brackets are dropped). They are stored as **confirmed** phones of the identity: a voice call with one of these numbers — inbound or outbound — is that person's call, with every calendar tool and the standard `PINCER_APPROVAL_POLICY`, instead of a guest's (see [Voice calling](../core-components/voice-calling.md#registered-users-and-guests)). A number may belong to one person only; listing it twice is a startup error. `/api/identity` returns each identity's `phones` as `[{"phone", "confirmed"}]`, along with `active` and `source`.

#### `PINCER_IDENTITY_MANAGEMENT`

| Value | Adds new people | Updates fields / channels | Removes people missing from the TOML |
|---|---|---|---|
| `update` (default) | yes | yes, for the people the TOML lists | no |
| `sync` | yes | yes | yes — the ones that came from the config (and auto `usr_` ones); identities made with the CLI are kept |
| `clionly` | — the `[identity]` section is ignored (a warning is logged) | — | no |

- Fields the TOML states overwrite the database; fields it leaves out keep what the database has.
- In `update`, a channel removed from a person's list is unlinked from that person only; a WhatsApp LID learned at runtime is kept. A phone removed from a listed person stops identifying them at the next start.
- A deactivated identity (`pincer identity deactivate`) stays off across restarts — seeding never switches it back on.

!!! warning "`PINCER_IDENTITY_MAP` was removed"
    The env-var string (`name@telegram:123=whatsapp:4912…`) is no longer read. `pincer run` refuses to start while it is set (in the environment or `.env`) and prints the equivalent `[identity.<name>]` TOML; `pincer doctor` reports it as critical. Move the printed TOML into `pincer.local.toml` and remove the variable. Unnamed entries come out as `person_1`, `person_2`, … — rename them.

#### Guest access control

The guest gate is on when the TOML `[identity]` section lists anyone, the database has an identity from the config or the CLI with a channel (a deactivated one counts — it is a guest, it does not open the gate), or `PINCER_IDENTITY_MANAGEMENT=clionly`. If the database cannot be read, the gate stays on. With it on, senders on Telegram, WhatsApp, and Signal who are not a known, active identity are treated as guests:

| Variable | Default | Description |
|----------|---------|-------------|
| `PINCER_TELEGRAM_GUESTS_ALLOWED` | `false` | Let unmapped Telegram senders through with a disposable identity instead of rejecting them |
| `PINCER_WHATSAPP_GUESTS_ALLOWED` | `false` | Same, for WhatsApp DMs |
| `PINCER_SIGNAL_GUESTS_ALLOWED` | `false` | Same, for Signal DMs |

For Telegram and Signal, these flags are a no-op — everyone is allowed — while the gate is off. WhatsApp group mentions behave the same way. WhatsApp DMs are the one exception: with the gate off, DMs are rejected by default regardless of `PINCER_WHATSAPP_GUESTS_ALLOWED` (matching the pre-#182 default of the removed `PINCER_WHATSAPP_DM_ALLOWLIST`, which rejected all DMs when empty) — set `PINCER_WHATSAPP_GUESTS_ALLOWED=true` to accept WhatsApp DMs from anyone. `pincer doctor` warns if a channel is configured (bot token set, or WhatsApp enabled without self-chat-only mode) but the gate is off, since that means the channel has no access control whatsoever.

Identities + these flags are now the *only* access-control mechanism for Telegram, WhatsApp, and Signal — the older `PINCER_TELEGRAM_ALLOWED_USERS`, `PINCER_WHATSAPP_DM_ALLOWLIST`, and `PINCER_SIGNAL_ALLOWLIST` settings have been removed. Slack's equivalent (`PINCER_SLACK_USER_ALLOWLIST`) has also been removed — Slack access relies entirely on native workspace/app-install membership. Microsoft Teams keeps its own `PINCER_TEAMS_USER_ALLOWLIST`, unaffected by this change.

## API Endpoints

The API server starts automatically with `pincer run` on the configured
`PINCER_DASHBOARD_HOST:PINCER_DASHBOARD_PORT`.

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /api/health` | No | Health check |
| `GET /api/status` | Yes | Agent and channel status |
| `GET /api/doctor` | Yes | Security doctor report |
| `GET /api/costs/today` | Yes | Today's spending + budget |
| `GET /api/costs/history?days=30` | Yes | Daily spend history |
| `GET /api/costs/by-model?days=7` | Yes | Per-model cost breakdown |
| `GET /api/costs/by-tool?days=7` | Yes | Per-tool cost breakdown |

Authentication: `Authorization: Bearer <access token or API key>` — an access
token from `POST /api/auth/login` or an identity's API key. See
[REST API → Authentication](rest-api.md#authentication).

## Docker

```bash
# Build and run
docker compose up -d

# View logs
docker compose logs -f pincer

# Check health
curl http://localhost:8080/api/health
```

## One-Click Deploy

- **DigitalOcean**: `.do/deploy.template.yaml`
- **Railway**: `railway.toml`
- **Render**: `render.yaml`
