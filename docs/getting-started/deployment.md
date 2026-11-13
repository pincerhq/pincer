# 🐳 Deployment Guide

Run Pincer 24/7 so your agent is always available. This guide covers Docker, Docker Compose, and one-click cloud deployments.

---

## Option 1: Docker Compose (Recommended)

The simplest way to run Pincer in production.

### 1. Create your project directory

```bash
mkdir pincer && cd pincer
```

### 2. Create `docker-compose.yml`

```yaml
services:
  pincer:
    image: ghcr.io/pincerhq/pincer:latest
    env_file: .env
    environment:
      - PINCER_DATA_DIR=/app/data
      - PINCER_DASHBOARD_HOST=0.0.0.0
      - PINCER_DASHBOARD_PORT=8080
      # MCP server — on by default so Claude Desktop, Cursor, etc. can connect
      - PINCER_MCP_SERVER_EXPORT_ENABLED=true
      - PINCER_MCP_SERVER_EXPORT_HOST=0.0.0.0
      - PINCER_MCP_SERVER_EXPORT_PORT=18800
    volumes:
      - pincer-data:/app/data
      - ./skills:/app/skills:ro       # Optional: mount custom skills
      - ./pincer.toml:/app/pincer.toml:ro  # Optional: MCP server config
    ports:
      - "8080:8080"    # Dashboard
      - "18800:18800"  # MCP endpoint (Claude Desktop, Cursor, VS Code, etc.)
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 1G   # Node.js MCP subprocesses need headroom
          cpus: "1.5"

volumes:
  pincer-data:
```

### 3. Create your `.env` file

```bash
# Copy the example and edit
curl -sL https://raw.githubusercontent.com/pincerhq/pincer/main/.env.example > .env
nano .env  # Fill in your keys
```

### 4. Launch

```bash
docker compose up -d
```

### 5. Check status

```bash
docker compose logs -f pincer
```

### Updating

```bash
docker compose pull
docker compose up -d
```

### WhatsApp + Docker

WhatsApp requires a one-time QR pairing before it can receive messages. Unlike Telegram (API token), WhatsApp uses the multi-device protocol and stores session data on disk.

**To enable WhatsApp with Docker:**

1. Set in `.env`:
   ```env
   PINCER_WHATSAPP_ENABLED=true
   ```

2. Rebuild and start (if building from source, the image includes `libmagic1` for neonize):
   ```bash
   docker compose build --no-cache
   docker compose up -d
   ```

3. Pair WhatsApp (one-time, run interactively):
   ```bash
   docker compose run --rm -it pincer pincer whatsapp setup
   ```
   A QR code appears in the terminal. Scan it with WhatsApp on your phone (Settings → Linked Devices → Link a Device).

4. Restart the stack so it picks up the paired session:
   ```bash
   docker compose up -d
   ```

5. Test by sending a message to yourself (self-chat) or in a group with the trigger word (`pincer` by default). Picking the bot in WhatsApp's @-picker only works with `PINCER_WHATSAPP_GROUP_TRIGGER_ON_MENTION=true`; replying to one of Pincer's messages works by default (`PINCER_WHATSAPP_GROUP_TRIGGER_ON_REPLY`).

**Troubleshooting:** If you see `WhatsApp failed: neonize is required for WhatsApp support. Install it with: pip install neonize (requires libmagic)`, the Docker image needs `libmagic1`. Rebuild with `docker compose build --no-cache` — the project Dockerfile already includes it.

The container uses `working_dir: /app/data` so the WhatsApp session is stored in the persistent volume and survives restarts. If WhatsApp stops working after a restart, re-run `whatsapp setup` to re-pair.

`pincer doctor` warns (`whatsapp_session_store`) when the data dir is not writable by the user Pincer runs as. In that case neonize cannot create `pincer-wa.db`, and the channel used to time out after 120 seconds asking you to scan a QR code. The check answers for the user that runs `pincer doctor` and names its uid (`euid=…`). Run as root (`docker exec` defaults to it), it warns when the data dir is owned by root, because the image runs as `pincer`.

#### WhatsApp session in Postgres

To keep the session out of the volume, store it in Postgres:

```env
PINCER_WHATSAPP_SESSION_DB="postgres://pincer_wa:secret@db:5432/pincer?sslmode=disable&search_path=whatsapp"
```

- The value must be a `postgres://` or `postgresql://` URL. `search_path` picks the schema for the `whatsmeow_*` tables, and whatsmeow creates and migrates those tables itself. Pincer's Alembic migrations ignore any table named `whatsmeow_*`, so sharing Pincer's database (even its schema) is safe. `PINCER_DATABASE_URL` takes the same `&search_path=<schema>` for Pincer's own tables.
- Create the schema once, and preferably a dedicated role that can only touch it:
  ```sql
  CREATE ROLE pincer_wa LOGIN PASSWORD 'secret';
  CREATE SCHEMA whatsapp AUTHORIZATION pincer_wa;
  ```
  An existing role instead needs `GRANT USAGE, CREATE ON SCHEMA whatsapp TO <role>;`.
- The URL holds a password, so Pincer never logs it. The startup log only says `session store: postgres (schema=whatsapp)`.
- A bad URL, a missing schema or missing rights now fail at startup with the database error instead of the 120-second QR timeout.
- Switching stores (SQLite → Postgres, or another schema) starts with an empty session, so pair again with `pincer whatsapp setup`.
- Pincer reads the store with `psycopg` when `PINCER_WHATSAPP_JID` is set, so the image needs the `postgres` extra. URL parameters libpq doesn't know (`search_path`, `statement_timeout`, …) are passed to the server as `options=-c key=value`, as lib/pq does.
- The URL must name a host, in the URL or as `?host=` for a Unix socket. A malformed URL fails at startup without echoing it.

#### Choosing the account

A store can hold more than one paired account. `PINCER_WHATSAPP_JID=+491511234567` picks one by phone number, written in E.164: country code first, no leading `00` and no trunk `0` (`+49 (0) 151…` is fine, the `(0)` is dropped; `0049…` and `0151…` are rejected). If the store holds several devices for that number (a re-pair without logging out), Pincer warns and uses the most recent one (highest device number). If that number isn't stored yet, Pincer shows a QR code to pair it rather than resuming another account. If a different phone scans the QR code, Pincer logs an error naming the number that actually paired. Unset, Pincer uses the first stored account, as before.

---

## Option 2: Docker (Manual)

```bash
docker run -d \
  --name pincer \
  --env-file .env \
  -e PINCER_MCP_SERVER_EXPORT_ENABLED=true \
  -e PINCER_MCP_SERVER_EXPORT_HOST=0.0.0.0 \
  -e PINCER_MCP_SERVER_EXPORT_PORT=18800 \
  -v pincer-data:/app/data \
  -p 8080:8080 \
  -p 18800:18800 \
  --memory 1g \
  --restart unless-stopped \
  ghcr.io/pincerhq/pincer:latest
```

---

## Option 3: One-Click Cloud Deploy

### Railway

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/template/pincer)

1. Click the button
2. Set environment variables in Railway dashboard
3. Deploy — Railway handles the rest

### DigitalOcean App Platform

[![Deploy to DO](https://www.deploytodo.com/do-btn-blue.svg)](https://cloud.digitalocean.com/apps/new?repo=https://github.com/pincerhq/pincer/tree/main)

### Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/pincerhq/pincer)

---

## Option 4: Bare Metal / VPS

For a basic Ubuntu/Debian server:

```bash
# Install Python 3.14
sudo apt update && sudo apt install -y python3.14 python3.14-venv

# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install Pincer
uv pip install pincer-agent

# Create working directory
mkdir -p ~/pincer && cd ~/pincer

# Initialize config
pincer init

# Run in background with systemd
sudo tee /etc/systemd/system/pincer.service << 'EOF'
[Unit]
Description=Pincer AI Agent
After=network.target

[Service]
Type=simple
User=pincer
WorkingDirectory=/home/pincer/pincer
ExecStart=/home/pincer/.local/bin/pincer run
Restart=always
RestartSec=10
Environment=PATH=/home/pincer/.local/bin:/usr/bin

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable pincer
sudo systemctl start pincer
sudo systemctl status pincer
```

---

## MCP server endpoint

When running in Docker, the MCP server is **enabled by default** on port `18800`. This lets Claude Desktop, Cursor, VS Code, and other MCP clients connect to Pincer's tools directly.

### Connecting Claude Desktop

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or the equivalent on your OS:

```json
{
  "mcpServers": {
    "pincer": {
      "url": "http://localhost:18800/mcp"
    }
  }
}
```

Restart Claude Desktop. Pincer tools (`pincer_email_check`, `pincer_calendar_today`, `pincer_memory_search`, etc.) appear in the tool list.

### Connecting Cursor

In Cursor settings → MCP → Add server, enter `http://localhost:18800/mcp`.

### Configuring which tools are exposed

Edit `pincer.toml` (mounted into the container) and set `expose_tools` under `[mcp.server]`:

```toml
[mcp.server]
expose_tools = [
    "email_check",
    "calendar_today",          # today's events (alias over google__list_events)
    "memory_search",
    # "google__list_events",   # opt-in: full calendar read (any range)
    # "shell_exec",   # requires user approval before executing
    # "file_read",
]
```

### Adding MCP client servers

Add `[[mcp.servers]]` blocks to `pincer.toml` to connect Pincer to external MCP servers (GitHub, Slack, Postgres, etc.). See [MCP Guide](../guides/mcp-guide.md) for the full reference.

### Changing the MCP port

```yaml
# docker-compose.yml
environment:
  - PINCER_MCP_SERVER_EXPORT_PORT=19000
ports:
  - "19000:19000"
```

Or set `PINCER_MCP_PORT=19000` to remap the host port only.

---

## Resource Requirements

| Scale | CPU | RAM | Disk | Monthly Cost |
|-------|-----|-----|------|-------------|
| Personal (1 user, no MCP clients) | 1 vCPU | 512MB | 1GB | ~$5/mo |
| Personal (with MCP client servers) | 1 vCPU | 1GB | 2GB | ~$10/mo |
| Small team (5 users) | 2 vCPU | 2GB | 5GB | ~$20/mo |

The Docker image is ~250MB (includes Node.js for MCP stdio servers). Pincer itself uses very little compute — most of the work is done by the LLM API. Each Node.js MCP subprocess (e.g. GitHub MCP, filesystem MCP) uses ~150-250MB of additional RAM.

---

## Backups

Back up your data directory regularly:

```bash
# Manual backup
tar czf pincer-backup-$(date +%Y%m%d).tar.gz data/

# Automated daily backup (cron)
echo "0 3 * * * cd /home/pincer/pincer && tar czf /backups/pincer-$(date +\%Y\%m\%d).tar.gz data/" | crontab -
```

Key files to back up:
- `data/pincer.db` — conversations, memories, entities
- `data/google_tokens.json` — OAuth tokens (re-auth needed if lost)
- `data/` — WhatsApp session (neonize store); re-pair if lost
- `.env` — your configuration
- the storage root (`PINCER_STORAGE_ROOT`, default `<data_dir>/storage`) — custom skills in `skills/` and the agent's files in `workspace/`

---

## HTTPS / Reverse Proxy

If you need HTTPS for webhooks (Telegram webhook mode, Twilio voice):

### Caddy (simplest)

```
pincer.yourdomain.com {
    reverse_proxy localhost:8080
}
```

### Nginx

```nginx
server {
    listen 443 ssl;
    server_name pincer.yourdomain.com;
    
    ssl_certificate /etc/letsencrypt/live/pincer.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pincer.yourdomain.com/privkey.pem;
    
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## Monitoring

### Health Check Endpoint

```bash
curl http://localhost:8080/health
# {"status": "ok", "uptime": 86400, "channels": 2, "budget_remaining": 3.42}
```

### Prometheus Metrics (Optional)

Enable with:

```env
PINCER_METRICS_ENABLED=true
PINCER_METRICS_PORT=9090
```

Available metrics:
- `pincer_messages_total` — messages processed (by channel)
- `pincer_tool_calls_total` — tool calls (by tool name)
- `pincer_llm_tokens_total` — tokens used (by model)
- `pincer_cost_usd_total` — total spend
- `pincer_response_time_seconds` — agent response latency

### Uptime Monitoring

Point [UptimeRobot](https://uptimerobot.com) or similar at `http://your-server:8080/health` for free uptime monitoring.