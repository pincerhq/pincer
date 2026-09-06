# Background Tasks — repid + chat-driven scheduling

Pincer runs cron-style schedules and on-request background work (morning
briefings, custom prompts) through [repid](https://pypi.org/project/repid/),
a durable, ack'd, retryable task-actor library. This replaced the hand-rolled
`asyncio.create_task` scheduling loop that had no retry on failure and
couldn't be scaled beyond a single process. Webhook delivery has a
repid actor (`process_webhook`) defined but not yet wired to a production
trigger — see the `actors.py` row below — and `EventTriggerManager`'s
email/calendar polling loops are intentionally out of scope for this
migration; they remain single-process `asyncio.create_task` loops by design
(see [Split — horizontal scaling](#split--horizontal-scaling)).

Related: [CLI Reference](../reference/cli.md) · [CLI](cli.md)

---

## Why repid

repid gives Pincer durable execution — a message isn't dropped just because
the process crashed mid-handler — and retry/redelivery semantics, at the cost
of owning two things repid deliberately doesn't provide out of the box:

- **A scheduler.** repid has no concept of "run this at 8am daily" — it only
  knows how to enqueue and consume messages. Pincer supplies the "when" with
  its own SQLite-backed poll loop.
- **A retry policy.** repid acks or nacks a message; it doesn't retry a
  handler internally. Pincer wraps each actor body in a `tenacity` retry, then
  falls back to `on_error="nack"` if retries are exhausted — nack acks and
  discards the message (the Redis broker additionally writes it once to a
  `repid:{channel}:dlq` stream by default). This is *not* redelivery: repid's
  `reject()` is the requeue action, and Pincer's actors don't use it.

## The pieces

All in `src/pincer/tasks/`:

| Module | Role |
|---|---|
| `app.py` | Registers the active broker (`InMemoryServer` or `RedisServer`, selected by `task_broker`) as the repid app's default server, and owns the `Router` actors register against. |
| `dispatch.py` | `ScheduleDispatcher` — polls `CronScheduler`'s SQLite store every `task_poll_interval` seconds for due schedules, enqueues each as a repid message, and marks it fired. This is the "when" repid doesn't provide. |
| `actors.py` | The two actor bodies: `run_scheduled_action` (fires a due cron schedule — briefing or custom prompt) and `process_webhook` (delivers a webhook-triggered notification — defined and tested, but not yet wired to a production trigger; nothing enqueues it today). Both wrap their handler call in a bounded `tenacity` retry and use `on_error="nack"` to ack-and-discard (with a Redis DLQ) if retries are exhausted. |
| `context.py` | Actors are plain functions invoked by repid's worker loop — they have no access to a running process's local variables. `set_context()` stashes the deliverer, `ProactiveAgent`, and `EventTriggerManager` once at process startup; actor bodies fetch them via `get_deliverer()` / `get_proactive()` / `get_triggers()`. |
| `delivery.py` | The `Deliverer` abstraction and the `ResultEmitter`/`ResultRelay` pub-sub bridge — see [Result delivery](#result-delivery-and-the-standalone-worker) below. |

## Chat-driven scheduling

Schedules are created, listed, removed, and toggled from **chat**, not the
CLI — four tools registered by `src/pincer/tools/builtin/schedule_tool.py`
when `schedule_tool_enabled` is on (the default):

| Tool | What it does |
|---|---|
| `schedule_create` | Creates a schedule that runs `prompt` through the agent later. |
| `schedule_list` | Lists the calling user's schedules. |
| `schedule_remove` | Removes a schedule by name (or `schedule_id` if names collide). |
| `schedule_toggle` | Enables or disables a schedule without deleting it. |

Example: a user tells the bot *"send me a digest every day at 8am"* — the
agent calls `schedule_create(name="daily-digest", prompt="...", cron_expr="0 8 * * *")`.

**`schedule_create` parameters:**

- `prompt` — the instruction the agent will run later, with **no conversation
  context or memory carried over**. It executes via `Agent.run_headless`, a
  session-free execution path, so the prompt must be fully self-contained
  (e.g. "search the web for X and summarize", not "the thing we discussed").
- Exactly one of:
  - `cron_expr` — a recurring schedule (`"0 8 * * *"` for daily 8am), or
  - `run_in_minutes` — a one-time run, computed from the real current time.
- `tz` — defaults to `settings.timezone`.
- `channel` — which channel to deliver the result to.
- `allowed_tools` — optional per-schedule tool allow-list; validated against
  the live `ToolRegistry` at creation time so a typo'd tool name is rejected
  immediately rather than silently ignored later.

**Guardrails**, both configurable (see [Config reference](#config-reference)):

- `min_schedule_interval_minutes` (default 15) — rejects a recurring
  `cron_expr` that would fire more often than this, e.g. an accidental
  `* * * * *`. A one-time `run_in_minutes` schedule is naturally exempt.
- `max_schedules_per_user` (default 20) — caps active recurring schedules per
  user as a cost/abuse guard.
- **Identity resolution.** `schedule_create` rejects creation up front if the
  caller's `pincer_user_id` looks like `IdentityMiddleware`'s synthetic
  fallback ID (`"{channel}:{native_id}"`, handed out when `PINCER_IDENTITY_MAP`
  is configured but the sender isn't listed in it). That fallback is never
  persisted to the identity tables, so a schedule stored under it could never
  be delivered later — better to fail at creation time than fail silently at
  fire time.

## Result delivery and the standalone worker

Actors run wherever the repid worker consuming the task queue happens to be
— which is *not necessarily* the same process that owns live, started
channels (channels can't safely be started twice). So actors never call a
`ChannelRouter` directly. Instead:

- Every actor publishes its result through a `Deliverer` (the `ResultEmitter`
  implementation), which hands the result to a `DeliveryBackend` — in-process
  `asyncio.Queue` for `task_broker=memory`, Redis pub/sub for
  `task_broker=redis`.
- A `ResultRelay` running in the main `pincer run` process consumes results
  from that backend and hands them to the real `ChannelRouter`.

This is a deliberate simplification: even in the single-process default case,
results take one extra async hop through the backend instead of a direct
call, so the actor code path is identical regardless of deployment topology.
Redis pub/sub has no persistence — if a standalone worker publishes a result
while no `ResultRelay` is listening, that result is silently dropped. The
actor itself still succeeds (repid acks the message); only delivery is
best-effort.

## Deployment topologies

### Single-process (default)

`task_broker=memory` (the default, zero extra infrastructure). One
`pincer run` process owns everything: channels, the `ScheduleDispatcher`
poll loop, the repid worker consuming the task queue, and the `ResultRelay`.
Actors and channels share the same event loop; the `Deliverer` hop is
in-memory only.

### Split — horizontal scaling

Set `task_broker=redis` and `PINCER_TASK_BROKER_URL` to point every process
at the same Redis instance. `pincer run` still owns channels, the dispatcher,
and the `ResultRelay`; one or more standalone worker processes run:

```bash
pincer run tasks
```

`pincer run tasks` requires `task_broker=redis` — the in-memory broker can't
be shared across processes, and the command refuses to start otherwise. The
standalone worker builds the same LLM/tools/agent core as the main process
(`_build_core` in `cli/run.py`) but **does not** start channels, the MCP server
export, or live email/calendar polling loops (`EventTriggerManager.start()`
is never called there — only `ensure_table()`, since those loops must run in
exactly one process). It consumes the shared repid queue and publishes
results over Redis pub/sub for the main process's `ResultRelay` to deliver.

This is the topology `docker-compose.yml`'s `http` profile ships by default:

| Service | Role |
|---|---|
| `redis` | Shared broker + result pub/sub transport |
| `pincer-http` | Main process — channels, dispatcher, API, MCP export, result relay |
| `pincer-http-tasks` | `pincer run tasks` — standalone worker, scales independently of the main process |

Run it with:

```bash
docker compose --profile http up -d
```

## Config reference

All variables use the `PINCER_` prefix (source: `config/tasks.py`,
`config/tools.py`).

| Variable | Default | Description |
|---|---|---|
| `PINCER_TASK_BROKER` | `memory` | Broker for background task execution: in-process `memory` (zero infra) or `redis` |
| `PINCER_TASK_BROKER_URL` | (empty) | Broker connection URL, e.g. `redis://localhost:6379/0` (required when `task_broker=redis`) |
| `PINCER_TASK_MAX_RETRIES` | `3` | Max attempts for a background task actor before giving up (repid has no built-in retry) |
| `PINCER_TASK_POLL_INTERVAL` | `60` | Seconds between checks for due cron schedules |
| `PINCER_SCHEDULE_TOOL_ENABLED` | `true` | Enable the `schedule_create`/`list`/`remove`/`toggle` chat tools |
| `PINCER_MAX_SCHEDULES_PER_USER` | `20` | Max active recurring schedules per user |
| `PINCER_MIN_SCHEDULE_INTERVAL_MINUTES` | `15` | Reject schedules that would fire more often than this |

## Operational notes

- repid's own worker logs a CRITICAL-level traceback on `CancelledError` by
  design (it re-raises after logging). During Pincer's normal shutdown
  sequence this is expected noise, not a real failure, and is silenced.
- repid's default signal handling is disabled (`register_signals=[]`) on
  both the in-process worker and the standalone `pincer run tasks` worker —
  Pincer's own shutdown sequence owns `SIGINT`/`SIGTERM` end-to-end, and
  repid installing its own handlers previously swallowed shutdown signals.
