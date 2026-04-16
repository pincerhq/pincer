# Microsoft 365 Setup Guide

Connect Pincer to your Microsoft 365 account to unlock 69 tools across Outlook, Calendar, OneDrive, To Do, Teams, Contacts, and OneNote.

---

## Prerequisites

- A Microsoft 365 or personal Microsoft account (Outlook.com, Hotmail, Live)
- `msal` Python package (installed automatically with the `ms365` optional dep)

---

## Step 1: Install the MS365 dependency

```bash
uv pip install msal
```

Or install with the full optional bundle:

```bash
uv pip install "pincer-agent[ms365]"
```

---

## Step 2: Register an Azure App

Pincer uses the **device code flow** — no redirect URI or web server required.

1. Open [Azure App Registrations](https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
2. Click **New registration**
3. **Name:** `Pincer Agent` (or any name you like)
4. **Supported account types:** choose based on your situation:
   - *Personal Microsoft accounts only* — for personal Outlook / OneDrive (outlook.com, hotmail.com)
   - *Accounts in any organizational directory and personal Microsoft accounts* — for work + personal
5. **Redirect URI:** leave empty
6. Click **Register**
7. Under **Authentication** → **Advanced settings** → enable **Allow public client flows**
8. Copy the **Application (client) ID** — you'll need it in the next step

---

## Step 3: Run the setup wizard

```bash
pincer setup-ms365
```

The wizard will:

1. Prompt for your **Application (client) ID** and optional **Tenant ID** (default: `common`)
2. Display a device code and URL: `https://microsoft.com/devicelogin`
3. Wait while you open the URL in a browser, enter the code, and sign in
4. Cache the token to `~/.pincer/ms365_token_cache.json` (mode `0600`)
5. Append the configuration to `pincer.toml`
6. Print a confirmation: `Microsoft 365 authenticated!`

---

## Step 4: Start the agent

```bash
pincer run
```

You should see:

```
Microsoft 365 tools enabled (69 tools)
```

All 69 `outlook__*`, `calendar__*`, `onedrive__*`, `ms_todo__*`, `teams__*`, and `onenote__*` tools are now available automatically on every `pincer run`.

---

## Tool inventory

| Service | Prefix | Count | Capabilities |
|---------|--------|-------|-------------|
| Outlook Email | `outlook__` | 17 | List/read/search/move/delete emails, send, reply, flag, manage folders, create drafts |
| Calendar | `calendar__` | 12 | List/create/update/delete events, find free slots, respond to invites, list calendars |
| OneDrive | `onedrive__` | 14 | List/read/upload/download/move/copy/delete files, create folders, share links |
| To Do | `ms_todo__` | 8 | List task lists, create/list/complete/delete tasks |
| Teams | `teams__` | 7 | List teams/channels, send channel/chat messages, create meetings |
| Contacts | `contacts__` | 6 | List/search/create/update/delete contacts |
| OneNote | `onenote__` | 5 | List notebooks/sections, read/create pages |
| **Total** | | **69** | |

---

## Configuration reference

Pincer reads MS365 config from `pincer.toml` under `[integrations.ms365]`:

```toml
[integrations.ms365]
enabled = true
client_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
tenant_id = "common"          # "common", "organizations", or your tenant GUID
auth_method = "device_code"   # "device_code" (headless) or "interactive" (opens browser)
token_cache_path = ""         # empty = ~/.pincer/ms365_token_cache.json
services = [                  # omit to enable all services
  "email", "calendar", "onedrive", "todo", "teams", "contacts", "onenote"
]
```

You can also use an environment variable reference for `client_id`:

```toml
client_id = "${PINCER_MS365_CLIENT_ID}"
```

And set the variable in your `.env`:

```
PINCER_MS365_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
PINCER_MS365_TENANT_ID=common
```

---

## Permissions (OAuth scopes)

The wizard requests exactly the scopes needed for the enabled services:

| Scope | Required by |
|-------|------------|
| `User.Read` | Always (user profile) |
| `Mail.ReadWrite`, `Mail.Send` | email |
| `Calendars.ReadWrite`, `OnlineMeetings.ReadWrite` | calendar |
| `Files.ReadWrite.All` | onedrive |
| `Tasks.ReadWrite` | todo |
| `Team.ReadBasic.All`, `Channel.ReadBasic.All`, `ChannelMessage.Send`, `Chat.ReadWrite` | teams |
| `Contacts.ReadWrite` | contacts |
| `Notes.ReadWrite.All` | onenote |

To request only a subset of permissions, restrict the `services` list in `pincer.toml` before running `pincer setup-ms365`.

---

## Re-authenticating

To re-authenticate (e.g. after token expiry or scope change):

```bash
pincer setup-ms365
```

The wizard will detect the existing token cache and ask for confirmation before overwriting it.

---

## Troubleshooting

### `msal not installed`

```bash
uv pip install msal
```

### `Microsoft 365 token not found — tools will be disabled`

Run `pincer setup-ms365` to perform the one-time authentication.

### `No valid Microsoft 365 token found`

The refresh token has expired (typically after 90 days of inactivity). Re-run:

```bash
pincer setup-ms365
```

### Tools not appearing after setup

1. Verify `pincer.toml` contains `[integrations.ms365]` with `enabled = true` and a valid `client_id`
2. Run `pincer run` — look for `Microsoft 365 tools enabled (69 tools)`
3. If you see a yellow warning, check the error message and re-run `pincer setup-ms365`

### Work account — admin consent required

For organizational accounts, an Azure AD admin may need to grant tenant-wide consent for some scopes. Contact your IT admin and point them to the app's **API Permissions** page in the Azure portal.
