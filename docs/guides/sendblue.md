# iMessage and SMS with Sendblue

Connect a Sendblue line to Pincer's agent without a Mac relay. This adapter supports
one-to-one text conversations, session history and proactive text delivery. Sendblue
chooses iMessage or SMS according to the recipient and your account's capabilities.

## Install and configure

Install `pip install 'pincer-agent[sendblue]'` (or `uv sync --extra sendblue --extra dev`
from source). Configure an LLM provider using Pincer's normal setup first; Sendblue
credentials provide messaging, not model access.

For CLI account verification, install Node.js and run:

```sh
npx -y @sendblue/cli@0.10.0 setup --phone +YOUR_PERSONAL_PHONE
```

Send the displayed verification text from that phone, then run
`npx -y @sendblue/cli@0.10.0 setup --check`. Exit code 3 means verification is still
pending. Credentials are saved to `~/.sendblue/credentials.json` when it completes.
Your personal phone is the recipient; `assignedNumber` is the sending line. Existing
accounts can use their API credentials and an assigned Sendblue line instead.

Confirm that your account enables receive webhooks and agent replies before continuing.
Sendblue's public pricing and quickstart have described different free-plan entitlements;
phone verification alone does not prove this integration is enabled for your account.

Set these variables in the same shell that runs Pincer (the commands below use `jq`):

```sh
export PINCER_SENDBLUE_ENABLED=true
export PINCER_SENDBLUE_API_KEY="$(jq -r .apiKey ~/.sendblue/credentials.json)"
export PINCER_SENDBLUE_API_SECRET="$(jq -r .apiSecret ~/.sendblue/credentials.json)"
export PINCER_SENDBLUE_FROM_NUMBER="$(jq -r .assignedNumber ~/.sendblue/credentials.json)"
export PINCER_SENDBLUE_SIGNING_SECRET="$(openssl rand -hex 32)"
export PINCER_SENDBLUE_ALLOW_FROM='["+YOUR_PERSONAL_PHONE"]'
pincer run
```

Keep the same signing secret across restarts and store credentials in your secret
manager or Pincer's local `.env` (never commit it). All phone numbers must use E.164
format, such as `+15555550101`. `PINCER_SENDBLUE_ALLOW_FROM` accepts a JSON array,
comma-separated numbers, or a blank value. An empty allowlist denies all senders; `["*"]` explicitly
allows any sender. A configured Pincer identity map is an additional inbound gate:
add the sender as `sendblue:15555550101` (a leading `+` also works) using normal
identity setup. The provider always receives an E.164 number with `+`.

The webhook shares Pincer's existing API listener, normally `127.0.0.1:8080`.
Expose **only** `POST /api/apps/sendblue/webhook` through your HTTPS reverse proxy
or tunnel. Use `PINCER_DASHBOARD_HOST` and `PINCER_DASHBOARD_PORT` for the shared
listener. This route uses `sb-signing-secret` instead of a dashboard Bearer token;
other API routes retain their normal authentication. There is no second listener
or additional Sendblue SDK dependency. If upgrading this PR's earlier implementation,
replace the old port-8787 `/webhooks/sendblue` URL; the old
`PINCER_SENDBLUE_WEBHOOK_HOST/PORT` settings are no longer used.

Run `pincer doctor` to check credentials, the assigned line, sender permissions and
public binding. Authenticated `GET /api/status` reports whether the Sendblue channel
is running. Invalid channel configuration reports a specific safe startup warning.

## Register incoming messages

In the same configured shell, append a receive webhook using the account API.
Replace the URL with your public HTTPS endpoint. POST appends; do not replace
other account webhooks with PUT.

```sh
export SENDBLUE_WEBHOOK_URL='https://YOUR_HOST/api/apps/sendblue/webhook'
jq -n --arg url "$SENDBLUE_WEBHOOK_URL" \
  --arg secret "$PINCER_SENDBLUE_SIGNING_SECRET" \
  --arg line "$PINCER_SENDBLUE_FROM_NUMBER" \
  '{webhooks:{receive:[{url:$url,secret:$secret,sendblue_numbers:[$line]}]}}' |
curl --fail-with-body https://api.sendblue.com/api/account/webhooks \
  -H "sb-api-key-id: $PINCER_SENDBLUE_API_KEY" \
  -H "sb-api-secret-key: $PINCER_SENDBLUE_API_SECRET" \
  -H 'Content-Type: application/json' --data-binary @-
```

Register once and inspect existing webhooks before repeating. On the free plan,
additional contacts must complete Sendblue's contact verification (`npx -y
@sendblue/cli@0.10.0 add-contact +RECIPIENT`, then have that person text the assigned
line); adding a number to Pincer's allowlist does not complete provider verification.

## Verify the complete conversation

1. From an allowed, verified phone, text the assigned line: “Remember the word cobalt.”
2. Confirm a reply arrives on that phone, then ask “What word did I ask you to remember?”
3. Confirm the answer and check Pincer's conversation history.
4. Try an unlisted sender: it must not trigger an agent turn. A webhook with a wrong
   `sb-signing-secret` must return HTTP 401.

A Sendblue `QUEUED` response confirms provider acceptance, not handset delivery.
Check delivery in Sendblue and on the recipient device. If no reply arrives, check
HTTPS routing, the shared signing secret, `to_number` versus the assigned line,
allowlist/identity mapping, model credentials and free-plan contact verification.

## Delivery limits

- Text only. Attachments are represented to the agent by an explicit unsupported-media
  notice; files are not downloaded. Group messages, outbound echoes and status callbacks
  are ignored. File uploads fail explicitly. URLs can be sent as plain text.
- Tool approval and interactive `ask_user` prompts are not implemented for this channel;
  approval-required tools are denied on Sendblue. Enabling Sendblue preserves the
  existing approval policy for other channels and the embedded MCP server. Use a channel with an
  approval interface for those operations.
- At most 128 messages may be queued or in flight, with up to eight concurrent turns.
  Messages from one sender remain ordered; a slow sender does not block other senders.
  HTTP 200 means accepted into memory. A full inbox returns 503. Up to 4096 accepted message handles are deduplicated
  in memory; restart or eviction clears that protection. This is not a durable inbox.
- Replies split into 2000-character chunks. Ambiguous network failures are not retried
  automatically because a retry can send the same message twice. Earlier chunks can
  have been accepted. Inspect Sendblue before manually retrying.
- Processing failures log the stage, exception type and escaped incoming handle, without
  exception text, payloads or secrets. A handler failure before a reply is sent triggers
  one best-effort failure notice. A failed/ambiguous outbound send triggers no extra POST.
  Accepted messages are not replayed automatically. Shutdown closes admission and cancels
  in-flight/queued work before dependencies are stopped; in-memory messages may be lost.
- Responses with a valid provider handle and no explicit `ERROR`/`DECLINED` status or
  nonzero error code count as accepted, including `REGISTERED`, `PENDING`, and `ACCEPTED`.
  This allows later chunks to proceed, but does not claim delivery to the handset.

Provider reference: [credentials](https://docs.sendblue.com/getting-started/credentials),
[webhooks](https://docs.sendblue.com/api/resources/webhooks),
[sending messages](https://docs.sendblue.com/api/resources/messages/methods/send).
