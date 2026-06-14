# Microsoft Teams Channel Setup

Pincer's Teams channel lets users chat with your agent from Microsoft Teams — in a
personal (DM) chat with the bot, by @mentioning it in a channel, or inside a group chat.

Unlike Slack (Socket Mode) or Discord (an internal gateway connection), Teams uses
**inbound HTTP push**: Microsoft's servers POST each message activity to a public
endpoint on your bot. Pincer therefore runs a small HTTP server (uvicorn) that exposes
`/messages`, built on the official [`microsoft-teams-apps`](https://pypi.org/project/microsoft-teams-apps/)
Python SDK and mounted as app to `/api/apps/teams` mount point.

```
Teams user ──HTTP POST /api/apps/teams/messages──▶ Pincer (uvicorn + Teams SDK) ──▶ Pincer agent
        ◀────────────── ctx.send(reply) ──────────────────────────────────┘
```

## 1. Install the optional dependency

```bash
uv pip install "pincer-agent[teams]"
# or:  pip install "pincer-agent[teams]"
```

## 2. Register an Azure Bot (one-time)

1. Sign in to the [Azure portal](https://portal.azure.com).
2. Create a resource → search for **Azure Bot** → **Create**.
   - **Type of App:** *Multi Tenant* (simplest for getting started).
   - Let Azure create a new **Microsoft App ID**, or reuse an existing App Registration.
3. Once created, open the bot resource:
   - Under **Configuration**, set the **Messaging endpoint** to
     `https://<public-host>/api/apps/teams/messages` (see step 4 for local dev).
   - Note the **Microsoft App ID** — this is `PINCER_TEAMS_APP_ID`.
   - Click **Manage Password** (next to the App ID) → in the App Registration,
     go to **Certificates & secrets** → **New client secret** → copy the secret
     **Value** (not the ID). This is `PINCER_TEAMS_APP_PASSWORD`.
4. Under **Channels**, add and enable the **Microsoft Teams** channel.
5. Configure OAuth for Graph API access (required for real user ID resolution for crosschannel memory identification):
   - In the bot resource, go to **Settings → Configuration → Add OAuth Connection Settings**.
   - Fill in the fields:
     - **Name:** `graph` (must match exactly — Pincer looks up this connection by name)
     - **Service Provider:** *Azure Active Directory v2*
     - **Client ID:** same Microsoft App ID as above
     - **Client Secret:** same client secret value as above
     - **Tenant ID:** your tenant ID (or `common` for multi-tenant)
     - **Scopes:** `User.Read openid profile`
   - Save the connection.
   - Open the **App Registration** for this bot (Azure portal → App Registrations → find by App ID):
     - Go to **API Permissions → Add a permission → Microsoft Graph**.
     - Add **`User.Read.All`** under both **Application permissions** and **Delegated permissions**.
     - Click **Grant admin consent** for your tenant on both entries (requires a Global Administrator or Privileged Role Administrator account).

> The App Password / client secret is shown only once. Store it immediately.

## 3. Configure Pincer

Set the credentials via environment variables (or `.env`):

```bash
PINCER_TEAMS_APP_ID=<Microsoft App ID>
PINCER_TEAMS_APP_TENANT_ID=<Microsoft App Tenant ID> #optional for single-tenant deployments
PINCER_TEAMS_APP_PASSWORD=<client secret value>
PINCER_TEAMS_USER_ALLOWLIST=           # optional: comma-separated AAD object IDs / UPNs
```

When both `PINCER_TEAMS_APP_ID` and `PINCER_TEAMS_APP_PASSWORD` are set, `pincer run`
starts the Teams channel automatically:

```
Teams connected
```

## 4. Local development with ngrok

Microsoft's servers must reach your bot over HTTPS. Pincer has a built-in ngrok
integration that opens a tunnel automatically on startup — no manual `ngrok` command or
separate port needed. The Teams channel is served through the main API port, so one
tunnel covers everything.

Set the bot's **Messaging endpoint** in the Azure portal to:

```
https://<tunnel-url>/api/apps/teams/messages
```

For the full tunnel setup (auth token, static domain, `.env` entries, and step-by-step
Azure portal instructions) see **[Local Tunnel (ngrok)](../getting-started/local-tunnel.md)**.

For production, point the endpoint at your real public host (behind a reverse proxy
that terminates TLS) and remove `PINCER_NGROK_AUTHTOKEN` from `.env`.

## 5. Install the bot in Teams

You can test the bot directly from the Azure portal (**Channels → Microsoft Teams →
Open in Teams**), or package an app manifest (via the
[Developer Portal for Teams](https://dev.teams.microsoft.com)) referencing your bot's
App ID and side-load it into your tenant.

## How conversations map to sessions

Each Teams context gets its own session key so conversations stay isolated:

| Teams context           | Session key                  | Reply behaviour                  |
| ----------------------- | ---------------------------- | -------------------------------- |
| Personal chat (DM)      | `teams-dm-{user_aad_id}`     | Direct reply in the same chat    |
| Channel @mention        | `teams-thread-{activity_id}` | Reply in a thread from that msg  |
| Existing thread reply   | `teams-thread-{thread_root}` | Continue the same thread         |
| Group chat              | `teams-chat-{chat_id}`       | Reply in that chat               |

## Proactive messages

Teams does not allow sending a message to an arbitrary user out of the blue — the user
must have messaged the bot first. Pincer stores a conversation reference for every
incoming activity, so `channel.send(user_aad_id, text)` can proactively deliver a
message (e.g. a scheduled briefing) to anyone who has previously interacted with the bot.

## Troubleshooting

- **No replies / 401 from Azure:** double-check `PINCER_TEAMS_APP_ID` and
  `PINCER_TEAMS_APP_PASSWORD`, and that the messaging endpoint exactly matches your
  tunnel/host plus `/api/apps/teams/messages`.
- **`microsoft-teams-apps not installed`:** run `pip install "pincer-agent[teams]"`.
- **Endpoint unreachable:** confirm the built-in tunnel is active (`Ngrok tunnel: https://...`
  in the console) and the Azure endpoint matches. See [Local Tunnel (ngrok)](../getting-started/local-tunnel.md).
- **Allowlist blocks everyone:** `PINCER_TEAMS_USER_ALLOWLIST` matches the sender's AAD
  object ID; leave it empty to allow all users while testing.
- **User ID falls back to `from.id` instead of real AAD object ID:** the `graph` OAuth connection is missing or misconfigured, or admin consent for `User.Read.All` has not been granted. Verify step 2.5 above — check the App Registration's API Permissions page and confirm both application and delegated `User.Read.All` show a green **Granted** status.
