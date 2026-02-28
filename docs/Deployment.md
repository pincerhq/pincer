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
    volumes:
      - pincer-data:/app/data
      - ./skills:/app/skills  # Optional: mount custom skills
    ports:
      - "8080:8080"  # Dashboard
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "pincer", "health"]
      interval: 30s
      timeout: 10s
      retries: 3

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
  -v pincer-data:/app/data \
  -p 8080:8080 \
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

## Resource Requirements

| Scale | CPU | RAM | Disk | Monthly Cost |
|-------|-----|-----|------|-------------|
| Personal (1 user) | 1 vCPU | 512MB | 1GB | ~$5/mo |
| Small team (5 users) | 1 vCPU | 1GB | 5GB | ~$10/mo |
| Heavy use (10+ users) | 2 vCPU | 2GB | 10GB | ~$20/mo |

The Docker image is ~150MB. Pincer itself uses very little compute — most of the work is done by the LLM API.

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