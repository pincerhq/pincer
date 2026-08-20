# Capacity & Scale Triggers

What one instance can carry, and the point at which the answer stops being "add
another instance".

> **Status: modelled, not load-tested.** The numbers below are derived from the
> architecture and the pilot's measured per-call cost, not from a benchmark
> against a saturated instance. They are conservative on purpose. Replace them
> with measurements once a pilot instance has a busy day — and mark that day in
> this document.

---

## One instance

**Deployment shape:** a single container per customer, SQLite on local disk,
Caddy in front. Sizing baseline from Sprint 7: an EU VM with 2 vCPU / 4 GB.

| Dimension | Comfortable | Hard limit | What actually binds |
|---|---|---|---|
| Concurrent calls | **3–5** | ~10 | One asyncio task per turn plus a WebSocket per call. The LLM concurrency semaphore (`PINCER_MAX_CONCURRENT_LLM`, default 5) is the real ceiling — beyond it, turns queue and latency leaves the SLO |
| Calls per day | **~150** | ~400 | `PINCER_VOICE_DAILY_CALL_LIMIT` caps this deliberately (default 20 — raise it consciously). Above ~400 the SQLite write path and the retention purge want attention |
| Turns per call | 10–20 | — | Phase timeouts end a call long before this matters |
| Transcript storage | ~2 KB/call | — | 90-day retention at 150 calls/day ≈ 27 MB. Not a constraint; the disk alert exists for backups, not transcripts |
| Customers per instance | **1** | 1 | Not a performance limit — there is no tenant isolation inside an instance |

**The binding constraint is concurrency, not volume.** 150 calls spread across a
working day is comfortable; 15 calls that all land in the same five minutes is
not. A customer whose traffic is spiky needs headroom, not a bigger daily cap.

### Raising the concurrency ceiling

In rough order of effort:

1. `PINCER_MAX_CONCURRENT_LLM` — raise it and watch `turn_latency_p95`. Free,
   and the first thing to try.
2. `PINCER_VOICE_TURN_MODEL` on the fast tier — cuts the dominant latency stage,
   which raises effective concurrency without more hardware.
3. More vCPU. Audio handling is I/O-bound, but TTS/STT framing on
   `media_streams` is not free.
4. Split the API server from the voice worker (`pincer run` already supports
   component selection). Untested at scale.

---

## Scale triggers

Each one is a decision point with a named alternative, so nobody has to invent
architecture under pressure.

| Trigger | Threshold | Decision to make |
|---|---|---|
| **Customer count** | > 50 instances | One-container-per-customer stops being operable by hand. Evaluate multi-tenancy (needs tenant isolation the codebase does not have) or fleet automation |
| **Write contention** | SQLite `database is locked` in the logs | Evaluate Postgres. The retention purge and the cost writer are the loudest writers |
| **Concurrent calls** | `turn_latency_p95` breaches during peaks while p50 is fine | Concurrency, not model speed. Raise the LLM semaphore first |
| **Daily volume** | > 400 calls/day on one instance | Split the voice worker from the API, or split the customer across instances |
| **Storage** | Disk alert fires (< 10% free) | Almost always backups, not transcripts — see [runbook §disk-full](runbook.md#disk-full) |
| **Cost** | Cost-per-call alert at 2× baseline | Not a capacity problem. [runbook §cost-spike](runbook.md#cost-spike) |

### The multi-tenancy decision, stated early

Multi-tenancy is on the post-GA backlog and is deliberately *not* a small
change: the current model assumes one customer's data per database, so
identity, memory, the do-not-call list, and every observability query are
single-tenant by construction. Adding tenants is a data-model change, not a
config flag. Fleet automation (many small instances, managed by a script) is the
cheaper answer up to a few dozen customers, and should be exhausted first.

---

## What to measure before trusting any of this

```bash
pincer voice ops status          # p95 during the customer's busiest hour
pincer voice ops slo             # is the latency budget burning at peak?
sqlite3 $PINCER_DATA_DIR/pincer.db \
  "SELECT strftime('%Y-%m-%d %H', started_at) AS hour, COUNT(*)
   FROM voice_calls GROUP BY hour ORDER BY 2 DESC LIMIT 10"   # actual peak concurrency proxy
```

If the busiest hour shows more calls than the "comfortable" row above and the
latency SLO held anyway, this document is too conservative — update it with the
evidence rather than leaving a number nobody believes.
