# Voice Operations Runbook

For whoever is on call — **including someone who did not build this**. Every
entry is symptom → where to look → what to do → when to escalate. If you follow
an entry and it does not resolve the incident, that is a bug in this page: fix
the entry as part of the incident.

---

## On-call quick card

**The five golden signals**

| Signal | Healthy | Where |
|---|---|---|
| Call success rate | ≥ 85% over 2h | `pincer voice ops status`, Grafana *Voice Ops* |
| Booking success rate | ≥ 70% over 24h | same |
| Turn latency p95 | ≤ 2.5s over 1h (SLO 2.0s) | same, + `pincer voice latency-report` |
| Stuck calls | 0, always | same — pages immediately |
| Cost per call | ≤ 2× the 7-day baseline | same |

**First three commands, in order**

```bash
pincer voice ops status          # 1. golden signals + what is firing right now
pincer doctor --production       # 2. config/security gate — RED here explains a lot
pincer voice latency-report      # 3. which turn stage regressed, if latency is the complaint
pincer voice latency-model       # 3b. same numbers per LLM model — did a model/provider switch cause it?
```

**Then, depending on what those show**

```bash
pincer voice ops failures --hours 6   # what broke, by stable failure code
pincer voice ops slo                  # error-budget burn, feature-freeze status
pincer voice ops canary --history     # was the media path healthy before this?
docker compose -f docker-compose.prod.yml logs --tail 200 pincer | grep -E 'ERROR|CRITICAL'
```

**Where things live**

| Thing | Location |
|---|---|
| App logs | `docker compose -f docker-compose.prod.yml logs pincer` |
| Turn latency records | `$PINCER_DATA_DIR/logs/voice_latency.jsonl` |
| SQLite (calls, costs, transcripts) | `$PINCER_DATA_DIR/pincer.db` |
| Audit log | `$PINCER_DATA_DIR/audit.db` |
| Dashboard | `https://api.<domain>` → Voice Ops |
| Metrics | Uptrace/Grafana → *Voice Ops* (`uid: pincer-voice-ops`) |

**Escalation.** Anything customer-visible and unresolved after **30 minutes**,
or any suspected data loss/leak, goes to the engineering owner immediately. Do
not sit on a page.

---

## Severity and what it means

| Severity | Definition | Response |
|---|---|---|
| **PAGE** | Calls are failing now, money is burning, or data is at risk | Act immediately, any hour |
| **NOTIFY** | A threshold drifted; customers may not have noticed yet | Same working day |

Alerts arrive on the ops channel (`PINCER_OPS_CHANNEL`/`PINCER_OPS_USER_ID`) —
Pincer delivers its own alerts. If the ops channel is the thing that is broken,
alerts fall back to `PINCER_OPS_ALERT_EMAIL` and, failing that, are logged at
ERROR with the text `OPS ALERT UNDELIVERED`. **Grep for that string** whenever
you suspect you have missed something.

---

# Top failure modes

## 1. "An application error has occurred" right after the greeting {#application-error}

*Seen in production as Hotfix 1.*

**Symptom.** The callee hears the greeting, then Twilio's canned error message.
The call ends. Transcripts show nothing after the greeting.

**Signature.** `failure_code=twiml_error`. Twilio error **64101** in the logs, or
in Twilio Console → Monitor → Errors. `pincer voice ops failures --hours 2` shows
`twiml_error` climbing.

**Cause.** Twilio could not execute the TwiML we returned. Historically:
`<ConversationRelay url="https://…">` — the attribute must be `wss://`, an HTTPS
URL is rejected at `<Connect>` time.

**Fix.**
```bash
# 1. Confirm what we are handing Twilio:
curl -s -X POST https://voice.<domain>/api/apps/twilio/webhook   # will 403 without a signature — expected
# 2. Check the builder's output directly:
pincer voice ops status                       # confirms the service is up
grep -n 'wss://' src/pincer/voice/twiml_builder.py
# 3. Verify the fallback handler is configured, so callers hear our apology, not Twilio's:
#    Twilio Console → Phone Numbers → your number → "Primary handler fails" →
#    https://voice.<domain>/api/apps/twilio/fallback
```
If `PINCER_VOICE_WEBHOOK_BASE_URL` is wrong or non-HTTPS, every call fails this
way. `pincer doctor --production` catches it (`prod_webhook_url`).

**Escalate** if the TwiML looks correct and Twilio still rejects it — that is a
Twilio-side change and needs their support with the error SID.

---

## 2. WebSocket never connects — silent call, no transcript {#ws-transport}

*Seen in production as Hotfix 2.*

**Symptom.** The call connects, then silence. No transcript lines at all.

**Signature.** `failure_code=ws_drop` or `no_audio`. No `ConversationRelay
connected` log line for the call SID.

**Diagnose.** Since Sprint 8 the relay authenticates the upgrade, so the check is
inverted — **403 is the healthy answer**:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  https://voice.<domain>/api/apps/twilio/relay
```

| Result | Meaning | Action |
|---|---|---|
| `403` | Healthy — proxy forwards the upgrade, app refuses unsigned connects | Look elsewhere |
| `404` / `502` | The proxy is not routing WebSockets to the app | Fix Caddy/nginx; see `deploy/Caddyfile` |
| timeout | Upgrade never reaches the app | Firewall or proxy; check `ufw status` and proxy logs |
| `101` / `200` | **Security failure** — unsigned sockets are being accepted | Check `PINCER_VOICE_WS_AUTH_REQUIRED` and `PINCER_TWILIO_AUTH_TOKEN`, page the owner |

`scripts/deploy.sh --verify` runs exactly this check and refuses a deploy on
101/200.

---

## 3. Silent agent — healthy transcript, callee heard nothing {#silent-agent}

*Seen in production as Hotfix 3.*

**Symptom.** The transcript shows a full, sensible conversation. The callee says
they heard nothing, or only the greeting.

**Signature.** `failure_code=no_audio`. Log lines with `state="undelivered"`.
Twilio error **64111** ("Error converting tokens to speech") repeated.

**Why it is classified as a failure even though the call "completed":** a call
whose agent turns never reached the caller is not a success, and
`_classify_call_failure` reclassifies it so it cannot score as one.

**Fix.**
```bash
pincer voice list                       # does the configured voice ID still exist?
pincer voice test --language de         # synthesize a sample; listen to the 8kHz mu-law variant
```
Two consecutive 64111 errors mark the voice unusable automatically: later calls
omit the `voice` attribute and use Twilio's per-language default ElevenLabs
voice. If that already happened you will see
`Configured ElevenLabs voice … is unusable` in the logs — the calls are
recovering on their own; fix the voice ID at leisure.

If TTS is failing for *every* voice, treat it as a provider outage (§7).

---

## 4. Language drift — agent answers in the wrong language {#language-drift}

**Symptom.** A German call gets English replies, or the agent mixes languages
mid-sentence.

**Signature.** `failure_code=language_drift`. Log line
`Language drift on streamed first sentence`. Callee confusion in the transcript.

**Fix.**
1. Confirm the call's language was resolved correctly:
   `sqlite3 $PINCER_DATA_DIR/pincer.db "SELECT call_sid, language, failure_code FROM voice_calls ORDER BY started_at DESC LIMIT 10"`
2. The guard buffers a drifting first sentence and regenerates once. Repeated
   drift on one model usually means the turn model was switched to a weaker one:
   check `PINCER_VOICE_TURN_MODEL` and the dashboard's telephony page (it
   persists to `data_dir/voice_runtime.json`).
3. Revert the turn model to the default and re-test with
   `pincer voice ops canary` if the canary language matches.

**Escalate** if drift persists on the default model — that is a prompt-pack
regression, not an ops issue.

---

## 5. Latency regression {#latency-regression}

**Symptom.** Callees talk over the agent, or complain about pauses. Alert:
`Voice latency p95 … over 1h`.

**Diagnose — find the stage, do not guess:**
```bash
pincer voice latency-report -n 30
```

| Stage that grew | Meaning | Action |
|---|---|---|
| `llm_first_token_ms` | The model is slow or overloaded | Switch `PINCER_VOICE_TURN_MODEL` to the fast tier (also possible live from the dashboard); check the provider's status page |
| `first_sentence_ms` | Model is writing long preambles | Prompt regression — check recent prompt-pack changes |
| `first_dispatch_ms` | TTS time-to-first-chunk | ElevenLabs slow; check `TTS first audio chunk took …ms` warnings |
| `prep_ms` | Our own pre-turn work (tools, memory) | A tool is slow; check tool timings in the audit log |
| `total_ms` only | Network between us and Twilio | Check host CPU and the proxy |

**Quick mitigation:** switch the turn model to the fast tier from the dashboard.
It applies to the very next turn — no restart.

---

## 6. Call success rate drop {#call-success-rate-drop}

**Symptom.** Alert: `Call success rate NN% over 2h`.

**First move — the alert already tells you the top failure codes. Get the full
picture:**
```bash
pincer voice ops failures --hours 2
```

Then follow the dominant code:

| Code | Go to |
|---|---|
| `no_answer`, `busy`, `voicemail` | **Not an outage.** Callees are unreachable; these are excluded from the SLO. Check whether calls are being placed at a sensible hour |
| `ws_drop`, `no_audio` | §2, §3 |
| `twiml_error` | §1 |
| `tts_error`, `stt_error` | §7 |
| `llm_error` | §7 (LLM provider), or budget — check `budget` too |
| `budget` | Daily spend limit reached: `pincer voice ops slo`, raise `PINCER_DAILY_BUDGET_USD` deliberately |
| `stuck` | §8 |
| `abuse_gate`, `do_not_call`, `quiet_hours` | **Working as designed.** The gate blocked calls. Confirm the limits are intentional |

---

## 7. Provider outage — Twilio / Deepgram / ElevenLabs / LLM {#provider-outage}

**Symptom.** Alert: `Synthetic canary call failed`, usually before customers
notice. Multiple failure codes at once.

**Confirm it is them, not us:**
```bash
pincer voice ops canary            # place one now
pincer voice ops canary --history  # was it healthy an hour ago?
pincer doctor --production         # rules out our own config
```
Then check the provider status pages (Twilio, Deepgram, ElevenLabs, and your LLM
provider).

**Mitigate by provider:**

| Provider | Mitigation |
|---|---|
| **LLM** | Failover is automatic between configured providers. Confirm a second provider key is set; if not, set one — this is the cheapest resilience available |
| **ElevenLabs** | Set `PINCER_CR_TTS_PROVIDER=google` to fall back to Twilio's Google voices. Quality drops, calls keep working |
| **Deepgram** | Switch `PINCER_VOICE_ENGINE=conversation_relay` — its STT is Twilio-side and does not touch Deepgram |
| **Twilio** | No mitigation; it *is* the phone line. Notify affected customers, pause outbound scheduling: `PINCER_VOICE_OUTBOUND_ENABLED=false` |

**Escalate** immediately for a Twilio outage — the product is down and customers
need telling.

---

## 8. Stuck call {#stuck-call}

**Symptom.** PAGE: `N stuck call(s) force-ended`.

**What already happened automatically:** the watchdog detected a call past
`voice_max_call_duration` + grace, forced its state machine terminal
(`stuck_max_duration_exceeded`), and ended it. The page is telling you it
*happened*, not that it is ongoing.

**Your job is the cause, not the cleanup:**
```bash
pincer voice ops status                                   # confirm zero stuck now
sqlite3 $PINCER_DATA_DIR/pincer.db \
  "SELECT call_sid, started_at, ended_at, failure_code FROM voice_calls WHERE failure_code='stuck' ORDER BY started_at DESC LIMIT 10"
docker compose -f docker-compose.prod.yml logs pincer | grep <call_sid>
```
Common causes: a hung tool call inside a turn, an engine that stopped delivering
`on_call_end`, or a Twilio status callback that never arrived (check the
signature-rejection log — a misconfigured auth token 403s every callback, so
calls never get their terminal event).

**Verify the money stopped:** cross-check Twilio Console → Monitor → Calls that
the call is actually terminated on their side too. Our reaper ends our state; a
Twilio-side call that survives keeps billing.

---

## 9. Disk full {#disk-full}

**Symptom.** PAGE: `Disk NN% free on the data volume`. Or: writes failing,
transcripts missing, backups absent.

**Why it is a PAGE:** SQLite and the backups share this volume. When it fills,
transcripts, cost records, and the do-not-call list all stop persisting *and* the
backup that would let you recover cannot be written.

```bash
df -h /var/lib/pincer
du -sh /var/lib/pincer/* | sort -h | tail
```

**Usual suspects, in order:**
1. **Backups accumulating** — `PINCER_BACKUP_RETENTION_DAYS`; prune old archives.
2. **`voice_latency.jsonl`** — append-only, never rotated. Safe to truncate;
   it only feeds the latency report and the latency SLI:
   `tail -n 50000 voice_latency.jsonl > tmp && mv tmp voice_latency.jsonl`
3. **Docker images/layers** — `docker system prune -a` (safe; the running image
   is retained).
4. **Transcripts** — should be aging out via the retention purge. If they are
   not, check that the `voice_retention_purge` schedule exists and
   `PINCER_VOICE_TRANSCRIPT_RETENTION_DAYS > 0`.

**Never** delete `pincer.db` or `audit.db` to free space.

---

## 10. Backup restore {#backup-restore}

**Symptom.** Data loss, corruption, or a bad migration.

```bash
scripts/backup-db.sh                 # take a fresh one FIRST, even now
ls -la /var/backups/pincer/          # find the target archive
scripts/restore-db.sh <archive>      # needs PINCER_BACKUP_PASSPHRASE
```

**Restore is not the whole job.** After restoring:
1. `pincer doctor --production` — confirm config still matches the data.
2. `pincer voice dnc list` — **verify the do-not-call list is intact.** Restoring
   an older backup can resurrect numbers that opted out in the meantime, which is
   a compliance problem, not just a data one. Re-add anything you know of.
3. `pincer voice ops status` — confirm signals compute.

**Drill this quarterly.** A restore procedure nobody has run is a hypothesis.

---

## 11. Certificate expiry {#cert-expiry}

**Symptom.** Twilio webhooks fail en masse; browsers warn on the dashboard.
`failure_code=twilio_api` or `call_setup` across the board.

```bash
echo | openssl s_client -servername voice.<domain> -connect voice.<domain>:443 2>/dev/null \
  | openssl x509 -noout -dates
docker compose -f docker-compose.prod.yml logs caddy | tail -50
```
Caddy renews automatically; failures are almost always **port 80 blocked**
(needed for the ACME HTTP-01 challenge) or a DNS record that moved.

```bash
ufw status | grep 80          # must be allowed
dig +short voice.<domain>     # must point at this host
docker compose -f docker-compose.prod.yml restart caddy
```

---

## 12. Twilio number / account problem {#twilio-number}

**Symptom.** Outbound calls rejected; inbound never arrives. `failure_code=twilio_api`.

| Cause | Check | Fix |
|---|---|---|
| Account balance / spending limit | Twilio Console → Billing | Top up; a spending limit silently stops calls |
| Regulatory bundle expired (DACH) | Console → Regulatory Compliance | Re-verify; this can take days — start now |
| Number released or reassigned | Console → Phone Numbers | Switch to the spare number, update `PINCER_TWILIO_PHONE_NUMBER` |
| Geo permissions | Console → Voice → Geo permissions | Enable the destination country |
| Auth token rotated | `pincer doctor --production` → `prod_voice_signatures` | Update `PINCER_TWILIO_AUTH_TOKEN`; a stale token 403s **every webhook**, so calls connect and then go nowhere |

That last row is worth internalising: a rotated Twilio auth token looks exactly
like a total voice outage, and the only clue is
`Rejected twilio_webhook:… signature mismatch` in the logs.

---

## 13. `doctor` RED after a deploy {#doctor-red-after-deploy}

**Symptom.** PAGE: `pincer doctor --production: N CRITICAL`.

```bash
docker compose -f docker-compose.prod.yml exec pincer pincer doctor --production
```

The deploy gate refuses to start on CRITICAL, so seeing this on a *running*
instance means either it was started around the gate or config changed
underneath it. Each check names its own fix in `fix_hint`. The security-relevant
ones are documented in [security-checklist.md](../guides/security-checklist.md).

**If the instance is live and RED, decide explicitly:** roll back
(`scripts/rollback.sh`) or fix forward. Do not leave it running unresolved —
several CRITICALs (missing tokens, disabled signature validation) mean the
surface is unauthenticated right now.

---

## 14. Booking success rate drop {#booking-success-rate-drop}

**Symptom.** NOTIFY: `Booking success rate NN% over 24h`.

This measures *negotiation*, not reachability — `unreachable` outcomes are
already excluded. A drop means calls are connecting and failing to close.

```bash
sqlite3 $PINCER_DATA_DIR/pincer.db \
  "SELECT result, COUNT(*) FROM appointment_outcomes WHERE recorded_at >= datetime('now','-1 day') GROUP BY result"
```

| Dominant result | Meaning |
|---|---|
| `calendar_failed` | **Our bug.** Agreed on the call but the calendar write failed — check Google auth. These are double-booking risks; the user was told honestly, but follow up |
| `out_of_slots` | Callees keep proposing times outside the offered candidates. Widen `PINCER_BUSINESS_HOURS` or offer more candidates |
| `declined` | Genuine negotiation failures. Read three transcripts before changing anything |

Read transcripts with `/transcript <call_sid>` or the dashboard.

---

## 15. Cost spike {#cost-spike}

**Symptom.** NOTIFY: `Cost per call N× the 7d baseline`.

```bash
sqlite3 $PINCER_DATA_DIR/pincer.db \
  "SELECT call_sid, duration_seconds, twilio_usd, stt_usd, tts_usd, llm_usd, total_usd
   FROM call_costs ORDER BY total_usd DESC LIMIT 10"
```
The component that dominates tells you the cause:

| Component | Cause | Action |
|---|---|---|
| `llm` | Model switched to an expensive tier, or turns got long | Check `PINCER_VOICE_TURN_MODEL` and `PINCER_VOICE_MAX_RESPONSE_TOKENS` |
| `twilio` | Calls running longer | Check `voice_max_call_duration`; look for calls near the cap (a near-stuck pattern) |
| `tts` | Agent talking more | Prompt regression — replies should be 1-2 sentences |
| `stt` | Long silences being transcribed | Check endpointing settings |

**Remember the unit prices are configuration** (`PINCER_PRICE_*`). If they were
never set to your real account rates, the alert may be precise and wrong — verify
against the Twilio bill before chasing a phantom.

---

# Operational procedures

## Deploy

```bash
cd /opt/pincer && git pull
scripts/deploy.sh
```
The script runs `pincer doctor --production` **inside the new image** and refuses
to start on any CRITICAL, then verifies both public endpoints (including the
inverted WSS check, §2). Active calls hear a spoken ending before the restart —
they are never cut to dead air.

**After every deploy:** `pincer voice ops status`, and confirm the next canary
run is green.

## Rollback

```bash
scripts/rollback.sh              # previous image tag
scripts/deploy.sh --verify       # confirm endpoints
```
Rollback does **not** roll back the database. Sprint 9 added columns
(`failure_code`, `engine`, `language`, `report_delivered_at`) and tables
(`call_costs`, `appointment_outcomes`, `canary_runs`); older code ignores them
harmlessly, so a rollback is safe in this direction. Check before assuming that
holds for a future migration.

## Secret rotation

See [production-deployment.md](../guides/production-deployment.md#secrets-rotation).
Order matters for the Twilio auth token: it signs both webhooks and WebSocket
tokens, so rotate it in Twilio Console and `.env.production` **together**, then
redeploy. Between the two, every webhook 403s.

## Restore drill

Quarterly, on staging, timed:
```bash
scripts/backup-db.sh && scripts/restore-db.sh <archive>
pincer doctor --production && pincer voice ops status && pincer voice dnc list
```
Record how long it took. An untimed restore procedure is not a recovery plan.

## Adding a customer

1. New instance (pilot = one instance per customer; there is no tenant isolation
   inside an instance).
2. Copy `.env.production.example`, fill secrets, `chmod 600`.
3. Own Twilio number + regulatory bundle for the country.
4. `PINCER_ENVIRONMENT=production`, distinct `PINCER_DASHBOARD_TOKEN` and
   `PINCER_WEB_CHAT_TOKEN`.
5. `scripts/deploy.sh` — the gate blocks a misconfigured launch.
6. AVV/DPA signed ([avv-annex.md](../guides/avv-annex.md)) **before** the first
   real call.
7. Verify: `pincer voice ops canary`, then one supervised live call.

## Decommissioning

1. `PINCER_VOICE_OUTBOUND_ENABLED=false`, redeploy — stop new calls first.
2. Drain: confirm `pincer voice ops status` shows zero active calls.
3. Final backup; hand it to the customer if the contract requires it.
4. Release the Twilio number (do not just stop paying — a released number gets
   reassigned to someone else, who will start receiving callbacks).
5. Delete personal data per the DPA; record what was deleted and when.
6. Destroy secrets: revoke Twilio, ElevenLabs, Deepgram, and LLM keys.

---

## Keeping this page honest

Every runbook entry above is reachable from an alert (each alert carries its own
`Runbook:` anchor). When you add an alert rule, add its section here in the same
change — an alert that points nowhere is an alert that gets ignored.

The acceptance test for this page (T9.4) is that **a developer who did not build
the system resolves three simulated incidents using only this document**. If you
find yourself explaining something verbally during an incident, that explanation
belongs here.
