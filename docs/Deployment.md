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
# Install Python 3.12
sudo apt update && sudo apt install -y python3.12 python3.12-venv

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

Restart Claude Desktop. Pincer tools (`pincer_web_search`, `pincer_email_check`, `pincer_memory_search`, etc.) appear in the tool list.

### Connecting Cursor

In Cursor settings → MCP → Add server, enter `http://localhost:18800/mcp`.

### Configuring which tools are exposed

Edit `pincer.toml` (mounted into the container) and set `expose_tools` under `[mcp.server]`:

```toml
[mcp.server]
expose_tools = [
    "web_search",
    "email_check",
    "calendar_today",
    "memory_search",
    # "shell_exec",   # requires user approval before executing
    # "file_read",
]
```

### Adding MCP client servers

Add `[[mcp.servers]]` blocks to `pincer.toml` to connect Pincer to external MCP servers (GitHub, Slack, Postgres, etc.). See [mcp-guide.md](mcp-guide.md) for the full reference.

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
- `.env` — your configuration
- `skills/` — custom skills

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