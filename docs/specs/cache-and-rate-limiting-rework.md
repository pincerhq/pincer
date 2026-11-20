# Cache and rate limiting rework (internal)

Internal record, not part of the published docs: it is deliberately not in
`mkdocs.yml`'s nav. Issue #404, branch `ratelimiter`, November 2026.

It records which parts of pincer moved onto the new subsystems
(`pincer.core.ratelimit`, `pincer.core.cache`), which did not and why, and what
changed in behaviour. User-facing configuration is in
[Rate limiting and caching](../core-components/rate-limiting-and-caching.md).

## Why

Before the rework, every limit and cache in pincer was a per-process dict:
- the channel message limiter;
- the demo-call counters;
- the auth lockout;
- the MCP call limiter;
- assorted TTL caches.

With several processes (`pincer run` plus `pincer run tasks` workers, or more
than one API replica) every limit multiplied by the number of processes, and a
restart forgave everything. Web chat had no message limit at all: the channel
limiter was CLI-only (`api/_deps.py`).

The new subsystems keep their state in the cache backend
(`PINCER_CACHE_BACKEND_URL`: `memory://` or `redis://`) under one prefix
(`PINCER_CACHE_PREFIX`). They offer sync and async APIs and per-route
decorators. Moving a component onto them makes its limits and caches hold
across processes when Redis is configured, and behave as before with the
default `memory://`.

## Additions made for the switch

The subsystems needed a few things the switched components rely on.

| Addition | Where | Needed by |
|---|---|---|
| `Limiter.check()` / `AsyncLimiter.check()`: the outcome without charging (`dry_run` through storages and the Lua scripts, ARGV[4]) | `core/ratelimit/limiter.py`, `storage/*`, `strategies.py` | Demo call (check all caps, charge together), `RateLimiter.get_status` |
| `fail_open=False` and `RateLimitResult.failed` / `.unavailable()` | `core/ratelimit/limiter.py`, `types.py` | Demo call: refuse rather than dial when the counters can't be read |
| `@rate_limit(detail=str \| fn(result))` | `core/ratelimit/decorators.py` | Chat (the channels' wording), canary, credentials, voice calls |
| `@rate_limit(scope="global")` | same | Canary |
| `@rate_limit(rate=fn)`: rate read per request | same | Chat (follows `PINCER_RATE_MESSAGES_PER_MIN`) |
| `@cache_response(scope="public")` sends `Cache-Control: public` only without `Authorization`, `private` otherwise | `core/cache/decorators.py` | Costs, audit stats, ops, integrations, skills: authenticated deployment-wide data must not be shared by a proxy or CDN |
| Middleware and dependency read `result.failed` instead of guessing from `reset_after` | `core/ratelimit/middleware.py`, `api/rate_limits.py` | Fail-open results carry no `X-RateLimit-*` |

## Rate limiting: what moved

| Component | Before | After | Bucket(s) |
|---|---|---|---|
| **Web chat** `POST /api/chat/message`, `/stream` | Only the daily budget (402/503) and the generic 300/min API limit | `@rate_limit(_message_rate, name="chat", strategy="token_bucket", detail=_too_fast)`. The channels' rate and wording, one budget for both routes. | `…:ratelimit:user:chat.<n>per60s.token_bucket:<user>` |
| **Channel limiter** `security/rate_limiter.RateLimiter` | In-process `TokenBucket` per user | `AsyncLimiter` + `TokenBucketStrategy` on the cache backend. Same API and messages; `get_status` reads via `check()`. | `…:ratelimit:channel_message:<user>`, `…:channel_tool:<user>` |
| **Demo call** `POST /api/public/demo-call` | In-process dicts, per worker, reset on restart | Three `AsyncLimiter`s, `fail_open=False`, checked then charged together in the endpoint | `…:ratelimit:demo_number:<e164>`, `demo_client:<ip>`, `demo_global:all` |
| **Outbound calls** `POST /api/voice/calls`, `/calls/scheduled` | Safety gates only | `@rate_limit("10/hour", name="outbound_calls")` in front of the gates; one budget for both routes | `…:ratelimit:user:outbound_calls.10per3600s:<user>` |
| **Canary** `POST /api/ops/canary` | Config gate only | `@rate_limit("6/hour", scope="global")` | `…:ratelimit:global:trigger_canary.6per3600s:all` |
| **Listen-in tickets** `POST /api/voice/listen/{sid}/ticket` | none | `@rate_limit("30/minute")` | per user |
| **Storage writes** `PUT /text`, `POST /upload`, `/folders`, `/move`, `DELETE /object` | none | `@rate_limit("60/minute", name="storage_write")`, one budget for all five | `…:user:storage_write.60per60s:<user>` |
| **Skills rebuild** `POST /api/skills/rebuild` | none | `@rate_limit("2/minute")` | per user |
| **Credentials** `PUT /api/identity/me/password`, `POST /me/api-key` | none | `@rate_limit("5/hour")` each | per user |

## Caching: what moved

| Route | Before | After | Invalidated by |
|---|---|---|---|
| `/api/ops/slo`, `failures`, `digest`, `ga-gate`, `ga-gate/report` | hishel `cache_control(public)`: hits skipped the user limit | `@cache_response(ttl=60, scope="public")`: hits count | TTL |
| `GET /api/integrations`, `/{slug}` | Rebuilt per request | `@cache_response(ttl=60, scope="public")` | TTL (an integration or MCP change shows within a minute) |
| `GET /api/skills`, `/{name}` | Skill index cache only | `@cache_response(ttl=30, scope="public")` | `POST /api/skills/rebuild` drops both (`ainvalidate`) |
| `GET /api/costs/today`, `history`, `by-tool`, `by-model` | Database aggregates per request | `@cache_response(ttl=30, scope="public")` | TTL |
| `GET /api/audit/stats` | Aggregates per request | `@cache_response(ttl=30, scope="public")` | TTL |

`HTTPCacheMiddleware` (hishel) now covers no route. It stays wired as the
mechanism for a future truly public, header-driven cache.
`tests/test_api_switched_routes.py::test_the_hishel_cache_has_no_routes_left`
pins this.

## Deliberately not switched

| Component | Why not |
|---|---|
| `AuthGuard` / login guard (`api/auth_guard.py`) | A failure budget with exponential lockout, `reserve`/`release` for in-flight attempts and reset on success. The strategies count requests and can't express that. Follow-up: a Redis-backed store for its state on `KeySpace`, so a lockout holds across workers. |
| `MCPSecurityGate` (`mcp/security.py`) | Limits the agent's own calls per `server::tool` inside one process; there is no user and nothing to share. |
| `voice/safety_gates.py` | Call policy (per-target cooldown, quiet hours, do-not-call), already database-backed and persistent. The new `outbound_calls` limit sits in front of it, not instead of it. |
| `GoogleServiceFactory` cache | Caches live client objects bound to credentials; they can't be serialised. |
| `CalendarTimezoneResolver` cache | Keyed by calendar id, and `primary` means a different calendar per Google account. A shared store would need the account in the key, which the resolver doesn't have. In-process is correct today. |
| `voices.py` invalid-voice flag | Rare, process-local signal from a failed call. Low value. |
| Telemetry statement cache | Compiled SQL, not data. |
| Per-user live data (`identity/me`, conversations, schedules, call reports, storage listings and downloads, audit log) | Changes on every action and is cheap to read. Per-user caching would need invalidation in many places for little gain. Files and streams aren't cacheable anyway. |
| MCP export server (port 18800), Slack MCP server (18910) | Separate ASGI apps outside `create_app`, localhost by default. They'd need their own middleware wiring; out of scope. |

## Behaviour changes to know about

- **Web chat answers 429** with the channels' text ("You're sending messages
  too fast! Please wait Ns…") once a user exceeds `PINCER_RATE_MESSAGES_PER_MIN`
  across `/message` and `/stream` together. The dashboard sees this as an HTTP
  error, not a chat reply.
- **Channel limiter:** with Redis, one user's budget is shared by all processes.
  `cleanup_user` is now `async`, and `TokenBucket` was removed from
  `pincer.security` (it had no other users).
- **Demo call:**
  - The number cooldown is exactly 30 minutes (a token bucket of one).
  - The client and global caps use the sliding-window approximation of a
    rolling day instead of an exact log, so a burst near the UTC day boundary
    is weighed slightly differently.
  - A backend outage now answers 503 "The demo line is not available right now"
    instead of dialling.
  - A request refused by any cap, or for consent or the number, still spends
    nothing.
- **Cached aggregates:**
  - Cost figures (including the dashboard's budget bar) may lag by up to 30 s.
  - Audit stats lag by up to 30 s.
  - Integrations lag by up to 60 s.
  - Skills lag by up to 30 s, but not after a rebuild.
- **Ops reports:** a cache hit on them now counts against the caller's user
  limit, as every other request does.
- **Storage uploads:** an upload refused by its limit has already had its body
  read (FastAPI reads bodies before dependencies). The pre-auth IP ceiling is
  the guard against upload floods.

## Key layout added by the switch

Under `<prefix>:kv:`:
- `ratelimit:user:chat.<n>per60s.token_bucket:<user>`
- `ratelimit:channel_message:<user>`, `ratelimit:channel_tool:<user>`
- `ratelimit:demo_number:<e164>`, `ratelimit:demo_client:<ip>`, `ratelimit:demo_global:all`
- `ratelimit:user:outbound_calls.10per3600s:<user>`, `ratelimit:global:trigger_canary.6per3600s:all`
- `ratelimit:user:storage_write.60per60s:<user>`, and one bucket per other decorated route
- `apicache:<route>:public:<hash>` for every cached route above

## Tests

- `tests/test_api_switched_routes.py`:
  - every switched route carries exactly the expected decorators, and no other
    route has any (a new decorator must be added there and here);
  - the hishel cache has no routes left;
  - chat shares one budget with the channels' wording;
  - costs are cached with a private `Cache-Control`;
  - storage writes share one bucket;
  - the demo limits hold across two "workers" on one (fake) Redis, spend
    nothing when refused, and fail closed;
  - the channel limiter is shared across processes.
- `tests/test_public_demo_call.py`: unchanged and still green on the new limiters.
- `tests/test_rate_limiter.py`: the channel limiter's API.
- `tests/test_skills_api.py::test_rebuild_drops_the_cached_skill_listing`.
- `tests/test_ratelimit_storage.py`: `check()` charges nothing, and fail-closed
  limiters, in all four mode and backend combinations.
- `tests/test_api_route_decorators.py`: `detail`, `scope="global"`, rate
  functions, and the private `Cache-Control` for authenticated `public` entries.

## Open follow-ups

1. Shared state for `AuthGuard` lockouts (see above).
2. Rate limits for the MCP export and Slack MCP servers, if they are ever exposed beyond localhost.
3. Invalidation hooks from the code that changes integrations and MCP config, if 60 s staleness becomes a problem.
4. Deployments outside this repo still set the removed `PINCER_TASK_BROKER` /
   `PINCER_REDIS_PREFIX`: `../docker-compose.yml`, `../deploy/azure` (`apps/locals.tf`,
   `README.md`, `setups/example.tfvars`). `pincer run` refuses to start with them.
