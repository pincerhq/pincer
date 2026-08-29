# Production Deployment (Sprint 7)

One-command deploy of a supervised, TLS-terminated, EU-hosted Pincer with
tested backups. Local development is untouched: `uv run pincer run` and
`docker-compose.yml` behave exactly as before — production lives entirely in
`docker-compose.prod.yml` + `scripts/`.

## Topology

```
Internet ──443──▶ Caddy (TLS, Let's Encrypt, WSS passthrough)
                    ├── https://api.<domain>    → pincer:8080  (dashboard + REST)
                    └── https://voice.<domain>  → pincer:8080  (Twilio webhooks + WSS)
                  pincer ── redis ── pincer-tasks (worker)
                  volume: pincer-prod-data (SQLite, WAL mode)
```

## First deploy (T7.1–T7.3)

1. **Host**: EU VM (Hetzner/OVH, 2 vCPU / 4 GB), Docker + compose plugin,
   `git`, `gpg`, optionally `rclone` (backups) and `wscat` (WSS verification).
2. **DNS**: A records `api.<domain>` and `voice.<domain>` → the VM. Ports 80 +
   443 open; nothing else public (the app has no host-published ports).
3. **Config**:
   ```bash
   git clone <repo> /opt/pincer && cd /opt/pincer
   cp .env.production.example .env.production
   chmod 600 .env.production   # deploy.sh refuses otherwise
   $EDITOR .env.production     # secrets, domains, DACH defaults are pre-filled
   ```
4. **Deploy**:
   ```bash
   scripts/deploy.sh
   ```
   The script builds the image tagged with the git SHA, runs
   **`pincer doctor --production`** inside it and *refuses to start on any
   CRITICAL* (tunnel configured, non-HTTPS webhook URL, missing dashboard
   token, weakened DACH compliance), then deploys, waits for container
   health, and verifies both public endpoints.
5. **Verify WSS** — since Sprint 8 (T8.1) this check is inverted. The relay
   authenticates the upgrade, so an unsigned connect **must be refused with
   403**; a 403 proves *both* that the proxy forwards the WebSocket upgrade
   (the Hotfix-2 lesson — Twilio error 64101 = broken upgrade) *and* that the
   app rejects a socket with no signed token:
   ```bash
   curl -sS -o /dev/null -w '%{http_code}\n' \
     -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
     -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
     https://voice.<domain>/api/apps/twilio/relay
   # 403 → correct.  101/200 → SECURITY FAILURE.  404/502/timeout → routing broken.
   ```
   `deploy.sh --verify` runs exactly this and **dies** on 101/200.
6. **Twilio console** (T7.6): point the +49 number's voice webhook to
   `https://voice.<domain>/api/apps/twilio/webhook` and the fallback URL to
   `.../api/apps/twilio/fallback`; confirm the regulatory bundle is approved;
   set a spending limit + alert; reserve a spare number.

## Host hardening (T8.6)

Run once per host, before the first deploy. These are the **[manual]** items in
the [security checklist](security-checklist.md) §T8.6 and must be verified on
the production host itself.

```bash
# 1. Unattended security upgrades
apt-get update && apt-get install -y unattended-upgrades
dpkg-reconfigure --priority=low unattended-upgrades
# Verify: /etc/apt/apt.conf.d/20auto-upgrades has APT::Periodic::Unattended-Upgrade "1";

# 2. SSH: keys only
#    /etc/ssh/sshd_config —
#      PasswordAuthentication no
#      PermitRootLogin prohibit-password
#      KbdInteractiveAuthentication no
sshd -t && systemctl reload ssh
# Verify from a second terminal BEFORE closing the current session.

# 3. Firewall allowlist — 443 and SSH only
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp    # ACME HTTP-01 challenge only; Caddy redirects to 443
ufw allow 443/tcp
ufw enable
# Verify no app port is published to the host:
ss -ltnp | grep -E ':(8080|8765)' && echo "FAIL: app port exposed" || echo "OK"

# 4. fail2ban on sshd
apt-get install -y fail2ban
systemctl enable --now fail2ban
fail2ban-client status sshd
```

**Docker note.** Docker writes its own iptables rules and can punch through
`ufw`. `docker-compose.prod.yml` publishes **no host ports** — only Caddy binds
80/443 — so the app is reachable only through the proxy. If you ever add a
`ports:` entry, bind it to `127.0.0.1` explicitly (`127.0.0.1:8080:8080`), or it
becomes internet-facing regardless of `ufw`.

**Secrets on disk.** `.env.production` must be `chmod 600` and owned by the
deploy user; `scripts/deploy.sh` refuses to run otherwise.

## Observability (T9.1–T9.2)

Two independent layers, on purpose. The second one exists because the first one
can be the thing that breaks.

**1. Metrics + dashboard (needs a collector).**

```bash
# .env.production
PINCER_TELEMETRY_DSN=https://<token>@otlp.uptrace.dev
```
`pincer run` initialises OpenTelemetry from this DSN; without it every metric
call is a cheap no-op and nothing else changes. Import
`deploy/grafana/voice-ops-dashboard.json` into Grafana (or the equivalent
Uptrace view) and point it at the Prometheus-compatible datasource fed by your
collector. Panel queries use the normalised metric names
(`pincer.voice.calls.ended` → `pincer_voice_calls_ended_total`).

**2. Alerts + CLI (needs nothing).**

The golden signals are computed from SQLite, not queried back out of the metrics
backend, so alerting and `pincer voice ops status` keep working when the
collector is down — which is exactly when you need to be told something.

```bash
PINCER_OPS_USER_ID=<your telegram user id>
PINCER_OPS_CHANNEL=telegram
PINCER_OPS_ALERT_EMAIL=ops@example.com     # fallback when the channel is broken
```
`pincer run` creates three schedules automatically on first start
(idempotent by name):

| Schedule | Default | What it does |
|---|---|---|
| `ops_alert_scan` | every 5 min | Evaluates the golden-signal rules, delivers what fires |
| `voice_canary` | every 6 h | Places a synthetic call; failure pages |
| `voice_weekly_digest` | Mon 09:00 | Failure digest with week-over-week deltas |

**Canary setup.** It places *real* calls, so it needs a staging responder you
own:

```bash
PINCER_VOICE_CANARY_ENABLED=true
PINCER_VOICE_CANARY_NUMBER=+49...            # never a customer number
PINCER_VOICE_QUIET_HOURS_OVERRIDE_USERS=pincer-canary   # or overnight runs are skipped
```

Verify the whole chain before trusting it:

```bash
pincer doctor --production      # ops_alert_routing + voice_canary checks
pincer voice ops status         # golden signals compute
pincer voice ops canary         # a real call goes out and comes back healthy
pincer voice ops digest         # the weekly digest renders
```

See [runbook.md](../operations/runbook.md) for what to do when one of these
starts complaining, and [slo.md](../operations/slo.md) for the SLO targets and
the error-budget freeze rule.

## Graceful deploys (T7.2)

`docker stop` / a redeploy sends SIGTERM. The runtime routes it into the same
path as Ctrl+C: every **active call hears the spoken ERROR_RECOVERY ending in
its call language** before the hangup (60 s drain, `stop_grace_period: 75s`),
the state machine lands in FAILED, and the initiating user gets the failure
report from the post-call pipeline.

**Drill (per release cycle, not assumed):** start a test call to your own
phone, run `scripts/deploy.sh` mid-conversation, confirm (a) the callee hears
the polite ending, (b) the initiator receives the report.

## Backups & restore (T7.4)

- `PRAGMA journal_mode=WAL` + `busy_timeout` are set on the main DB at
  startup. Single-writer discipline: only the `pincer` and `pincer-tasks`
  containers may write; ad-hoc inspection of the volume must be read-only
  (`sqlite3 'file:pincer.db?mode=ro'`).
- Nightly backup — add to the deploy user's crontab:
  ```
  15 3 * * * /opt/pincer/scripts/backup-db.sh >> /var/log/pincer-backup.log 2>&1
  ```
  Online WAL-safe snapshot → AES256 gpg → `backups/` → optional S3-compatible
  EU bucket (`PINCER_BACKUP_RCLONE_REMOTE`), 30-day retention both sides.
- **Restore drill — monthly, an untested backup is not a backup:**
  ```bash
  scripts/restore-db.sh --drill
  ```
  Decrypts the latest backup to a scratch dir, runs `PRAGMA integrity_check`,
  prints voice-table row counts, and runs the test suite against the restored
  data dir. Record the timing here:

  | Date | Backup | Drill result | Duration |
  |------|--------|--------------|----------|
  | _(fill on first drill)_ | | | |

- Real restore: `scripts/restore-db.sh --restore backups/<file>.gpg`
  (stops the stack, keeps the old DB as `pincer.db.pre-restore`, restarts).
- Retention purge: confirm the Sprint 0 purge is live in prod — the audit log
  must contain `retention_purge` entries after the first 03:30 run.

## Rollback (T7.5)

Every deploy appends its image tag to `.deploy_history`.

```bash
scripts/rollback.sh                # previous tag
scripts/rollback.sh sha-abc1234    # specific tag
```

Drill this once end-to-end after the first deploy: deploy → roll back →
`scripts/deploy.sh --verify` → deploy again.

## CI/CD pipeline (T7.5)

CI (`.github/workflows/ci.yml`) already blocks merges on pytest (including
the voice harness in both languages), mypy, ruff, and bandit. The deploy
pipeline on top:

1. merge to `main` → CI green
2. `scripts/deploy.sh` on the **staging** host
3. staging smoke: one automated PSTN loop call staging→staging
   (`schedule_appointment_call` against the spare number), plus
   `scripts/deploy.sh --verify`
4. manual promote: `scripts/deploy.sh` on prod; release checklist file
   (`docs/changelog/`) carries the manual 5-call protocol from Sprint 1.

## Secrets rotation

`.env.production` is chmod 600, git-ignored, and lives only on the host.
To rotate: edit the value, `scripts/deploy.sh` (containers get the new env).

| Secret | Where to rotate | Notes |
|--------|-----------------|-------|
| `PINCER_TWILIO_AUTH_TOKEN` | Twilio console → secondary token | promote secondary, then delete primary — no downtime |
| `PINCER_ELEVENLABS_API_KEY` | ElevenLabs dashboard | issue new, deploy, revoke old |
| `PINCER_DEEPGRAM_API_KEY` | Deepgram console | same pattern |
| `PINCER_ANTHROPIC_API_KEY` / OpenAI | provider console | same pattern |
| `PINCER_DASHBOARD_TOKEN` | `secrets.token_urlsafe(32)` | update dashboard users after deploy |
| Google OAuth | `pincer setup-google` re-run | token cache lives in the data volume |
| `PINCER_BACKUP_PASSPHRASE` | new value + re-encrypt kept backups | old backups need the old passphrase — store both in the password manager |

## Top failure modes

**Superseded by [operations/runbook.md](../operations/runbook.md)** (Sprint 9),
which covers 15 failure modes with symptom → dashboard/log signature → fix →
escalation, plus deploy/rollback/restore/onboarding procedures and the on-call
quick card. Every alert links to its section there.

Two entries that changed since this page was written:

- The relay WSS check is **inverted** — an unsigned `wscat` connect must now be
  refused with 403 (Sprint 8, T8.1). See
  [runbook §2](../operations/runbook.md#ws-transport).
- SQLite `database is locked` is still "a third writer, and never add one" —
  WAL + busy_timeout tolerate readers, not concurrent writers.
