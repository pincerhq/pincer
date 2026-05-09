# Signal Messenger Setup

This guide explains how to connect Pincer to Signal via **signal-cli-rest-api**.

## Requirements

- Docker + Docker Compose
- A phone number registered with Signal (can be a virtual number)
- Pincer with `PINCER_SIGNAL_ENABLED=true`

---

## Quick Start

### 1. Start the Signal API sidecar

**Start signal-api before pairing.** The pair command checks that signal-api is reachable before opening the browser.

We use `bbernhard/signal-cli-rest-api:latest-dev` for better linking compatibility with recent Signal app versions (iOS/Android 7.31+).

```bash
docker compose -f docker-compose.yml -f docker-compose.signal.yml up -d signal-api
```

This starts only the Signal API (pre-built image, no build required). For the full stack including Pincer, omit the service name to start everything.

### 2. Pair your device (QR code)

```bash
pincer signal pair
```

Scan the QR code with the Signal app:
**Settings → Linked Devices → Link New Device**

Alternatively, open the printed URL in your browser directly.

### 3. Set environment variables

```env
PINCER_SIGNAL_ENABLED=true
PINCER_SIGNAL_PHONE_NUMBER=+491234567890
PINCER_SIGNAL_API_URL=http://signal-api:8080   # Docker internal URL
PINCER_SIGNAL_ALLOWLIST=                       # Empty = allow all DMs
```

### 4. Verify

```bash
pincer signal status    # check API health + registered accounts
pincer signal test +491234567890   # send test message to yourself
```

### 5. Run Pincer

```bash
pincer run
```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `PINCER_SIGNAL_ENABLED` | `false` | Enable Signal channel |
| `PINCER_SIGNAL_API_URL` | `http://signal-api:8080` | signal-cli-rest-api base URL (inter-container) |
| `PINCER_SIGNAL_PAIR_URL` | `http://127.0.0.1:8081` | URL for `pincer signal pair` browser (host-facing) |
| `PINCER_SIGNAL_PHONE_NUMBER` | *(required)* | Your Signal E.164 phone number |
| `PINCER_SIGNAL_ALLOWLIST` | `""` | Comma-separated allowed DM numbers; empty = allow all |
| `PINCER_SIGNAL_GROUP_REPLY` | `mention_only` | Group reply mode: `mention_only` \| `all` \| `disabled` |
| `PINCER_SIGNAL_RECEIVE_MODE` | `websocket` | `websocket` (recommended) or `poll` |
| `PINCER_SIGNAL_POLL_INTERVAL` | `2` | Poll interval in seconds (poll mode only) |

---

## Host vs Container URL

When signal-api runs in Docker, two different URLs apply:

- **`PINCER_SIGNAL_API_URL`** — Used by the Pincer agent (inside Docker) to talk to signal-api. Use the Docker service name: `http://signal-api:8080`.
- **`PINCER_SIGNAL_PAIR_URL`** — Used by `pincer signal pair` to open the QR link in your browser. The browser runs on your host, so it must use 127.0.0.1: `http://127.0.0.1:8081` (default, matches docker-compose port mapping). Using 127.0.0.1 instead of localhost avoids Safari IPv6 resolution issues on macOS.

If you use a different port mapping for signal-api, set `PINCER_SIGNAL_PAIR_URL` accordingly (e.g. `http://127.0.0.1:8082`).

---

## Security Notes

- **Never expose signal-api to the internet.** The REST API has no authentication — keep it on localhost or the internal Docker network.
- The security doctor (`pincer doctor`) checks:
  - `signal_phone_set` — critical if Signal is enabled but phone is not configured
  - `signal_api_local` — critical if the API URL is not a local/internal address
  - `signal_allowlist` — warning if no DM allowlist is configured

---

## Receive Modes

**WebSocket (default):** Real-time via `ws://.../v1/receive/{number}`. Recommended for low latency.

**Poll:** Periodic HTTP GET every `signal_poll_interval` seconds. Use if WebSocket is unreliable.

```env
PINCER_SIGNAL_RECEIVE_MODE=poll
PINCER_SIGNAL_POLL_INTERVAL=5
```

---

## Group Chats

By default (`mention_only`), Pincer only replies when its name is mentioned:

```
Hey Pincer, what's the weather tomorrow?
```

To reply to all messages in a group:
```env
PINCER_SIGNAL_GROUP_REPLY=all
```

To disable group chat entirely:
```env
PINCER_SIGNAL_GROUP_REPLY=disabled
```

---

## Voice Notes

Signal voice notes (AAC format) are transcribed automatically via OpenAI Whisper.
Requires `PINCER_OPENAI_API_KEY` to be set.

---

## Cross-Channel Identity

Link a Signal number to a Telegram user:

```env
PINCER_IDENTITY_MAP=telegram:12345=signal:+491234567890
```

---

## Troubleshooting

**"Cannot reach signal-api" when running `pincer signal pair`**
- The pair command checks that signal-api is reachable before opening the browser. If you see this error, start signal-api first:
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.signal.yml up -d signal-api
  ```

**signal-api container not healthy**
- Check logs: `docker logs pincer-signal-api`
- Ensure port 8080 is not occupied by another service

**"Server cannot be found" / "Safari can't connect to localhost" when opening QR link**
- The pair command opens a URL in your browser. If you see "server cannot be found" or "can't connect to localhost", ensure signal-api is running and use `PINCER_SIGNAL_PAIR_URL=http://127.0.0.1:8081` (127.0.0.1 avoids Safari IPv6 resolution issues on macOS). Verify: `curl http://127.0.0.1:8081/v1/about` returns JSON. If not, check `docker ps | grep signal` and `docker logs pincer-signal-api`.

**"Unacceptable response from the service" / "Failed to link device" / silent failure when scanning QR code**
- We use `MODE=normal` and `latest-dev` for better linking. If linking still fails (silent or rejected):
  1. **Clear volume and start fresh:** `docker compose -f docker-compose.yml -f docker-compose.signal.yml down` then `docker volume rm pincer-signal-data`, then `up -d signal-api` again. Get a new QR code with `pincer signal pair`.
  2. **Apple Silicon (M1/M2/M4):** JVM in the image can crash on ARM64. Add under the signal-api service in docker-compose.signal.yml:
     ```yaml
     platform: linux/amd64
     ```
  3. **Try json-rpc mode:** If normal mode fails, switch to `MODE=json-rpc` in docker-compose.signal.yml and retry.
  4. Ensure your Signal app is updated; some versions have known compatibility issues.

**"QR code not found" / pairing fails**
- The QR link expires quickly. Re-run `pincer signal pair` for a fresh code.

**Messages not arriving**
- Run `pincer signal status` to confirm health and account registration
- Try switching to poll mode: `PINCER_SIGNAL_RECEIVE_MODE=poll`

**Voice transcription returns an error**
- Ensure `PINCER_OPENAI_API_KEY` is set and valid
