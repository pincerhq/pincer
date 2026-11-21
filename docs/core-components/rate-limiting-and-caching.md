# Rate limiting and caching

The HTTP API (`/api/*`) limits how many requests a client may send, and can
cache responses that are the same for every caller. Both store their state in
the cache backend, `PINCER_CACHE_BACKEND_URL`:

- `memory://` (the default) keeps everything in the process. Nothing to run,
  but each API process limits and caches on its own.
- `redis://host:6379/0` shares limits and cached responses between processes.
  Install the `redis` extra (`pip install 'pincer-agent[redis]'`).

Every key lives under `PINCER_CACHE_PREFIX`; see
[Redis key layout](background-tasks.md#redis-key-layout).

## Rate limiting

The IP ceiling is a middleware before authentication. The user and anonymous
limits need to know the route, so they run after routing, as an app-level
dependency (`pincer.api.rate_limits.enforce_rate_limits`):

```
CORS → IP ceiling → auth → HTTP cache → routing → user | anon or route limits → @cache_response → endpoint
```

| Limit | Checked | Keyed by | Default | Variable |
|---|---|---|---|---|
| IP ceiling | before auth, every request | client IP | 3000/min | `PINCER_RATE_API_IP_PER_MIN` |
| User | after auth, requests with an identity | `pincer_user_id` | 300/min | `PINCER_RATE_API_USER_PER_MIN` |
| Anonymous | after auth, requests without one (login, the public demo call) | client IP | 120/min | `PINCER_RATE_API_ANON_PER_MIN` |

- **The IP ceiling is a flood backstop, not a quota.** Many users behind one
  company NAT address all count against it, so keep it far above what all of
  them send together. It runs before auth, so a flooding host is refused
  before any token lookup.
- **The user limit** gives each user their own budget, whichever IP they come
  from. A request with a bad token is answered `401` by auth and never charged
  to a user. Repeated bad tokens lock the IP out (`PINCER_AUTH_MAX_FAILURES`).
- **Only FastAPI routes run the post-auth limits.** The built-in `/api/docs`
  and `/openapi.json` pages have the IP ceiling only.
- FastAPI reads a request's body before it runs dependencies, so a request
  refused by the user/anon limit has had its body read. The IP ceiling refuses
  floods before that.
- **Exempt paths:** `/api/health`, everything outside `/api/` (dashboard
  assets), and the self-authenticated webhooks under `/api/apps/twilio/`,
  `/api/apps/teams/` and `/api/storage/signed/`, whose callers share platform
  IPs.
- **WebSockets** (voice listen-in, the Twilio relay/stream/monitor sockets)
  are not rate limited by either layer: each authenticates itself (a ticket or
  a Twilio signature) when it opens.

A refused request gets:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 12
X-RateLimit-Limit: 300
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 12

{"detail": "Rate limit exceeded"}
```

Allowed responses carry the same `X-RateLimit-*` headers for the user (or
anonymous) limit. The algorithm is a sliding window counter: the current
minute's count plus the previous minute's, weighted by how much of it is still
in the window.

If the backend fails (Redis unreachable), requests are **allowed**. A warning
is logged once per outage, and recovery is logged at INFO.

`PINCER_RATE_API_DISABLED=true` switches both limits off; `pincer doctor`
warns about that in production.

!!! warning "X-Forwarded-For"
    The client IP is the first `X-Forwarded-For` entry, which is not verified.
    Run the API behind a proxy that overwrites that header; otherwise a client
    can choose its own IP bucket.

The limiter lives in `pincer.core.ratelimit`, a reimplementation of the design
of [FastAPI-RateLimiter](https://github.com/awais7012/FastAPI-RateLimiter). It
provides `SlidingWindowStrategy` and `TokenBucketStrategy`, memory and Redis
storage (one atomic Lua script per check), sync and async limiters (see
[Sync and async](#sync-and-async)) and the ASGI `RateLimitMiddleware`.

## HTTP response cache

Endpoints opt in. A route marked with `cache_control` has its `GET`/`HEAD`
responses served through [hishel](https://hishel.com)'s RFC 9111 cache:

```python
from pincer.core.cache.middleware import cache_control


@router.get("/slo", dependencies=[cache_control(max_age=60, public=True)])
async def slo_report() -> SLOReportOut: ...
```

- The cache is **shared** and keyed by method and URL only. It never sees who
  asked, so mark only responses that are the same for every caller, with
  `public=True`. A response without `public` (or with `private=True`) is not
  stored.
- It sits inside auth and the IP ceiling, so a cached answer still needs a
  valid token and counts against the ceiling. It sits before routing, so a
  hit is answered before the user/anon limit runs and doesn't count against
  it. Use `@cache_response` when cached answers must count.
- Routes that are not marked never pass through it. hishel reads a whole
  response before sending it, which would break streaming endpoints.
- Entries live at most `PINCER_CACHE_DEFAULT_TTL` seconds (default 300), or
  less if `max-age` says so.

No route uses it today: the shared caches moved to `@cache_response(scope="public")`
(below), whose hits count against the rate limits.

## Per-route limits

A decorator on an endpoint replaces the default user/anon limit for that
route:

```python
from pincer.core.ratelimit import no_rate_limit, rate_limit


@router.post("/chat")
@rate_limit("20/minute")  # scope="user": per caller
@rate_limit("500/day", strategy="token_bucket")  # stacked: every limit must allow; a refused request spends none
async def chat(...): ...


@router.get("/ops/digest")
@rate_limit("5/minute", scope="ip")
def digest(...): ...


@router.get("/status")
@no_rate_limit  # no post-auth limit; the IP ceiling still applies
async def status(...): ...
```

- **Rates:** `"<n>/second|minute|hour|day"` or `"<n>/<seconds>s"`, or a `Rate`, or a
  function returning one. A function is read on each request, so a limit can follow a
  setting.
- **`scope`** names who is counted:
  - `"user"` (default): the caller's identity, or their IP if there is none;
  - `"ip"`: everyone on one address together;
  - `"anon"`: by IP;
  - `"global"`: one bucket for everyone;
  - a function `request -> str | None` that returns the identifier, where None means not limited.
- **`detail`:** replaces the 429 message, with a string or a function of the refused result
  (e.g. to say how long to wait).
- **`strategy`:** `"sliding_window"` (default) or `"token_bucket"`. `cost` is how many units one request uses.
- **Buckets:** each declared limit has its own:
  `<prefix>:kv:ratelimit:<scope>:<route>.<n>per<period>s[.<strategy>]:<id>`. A tight route limit
  doesn't spend the default budget, and stacked limits don't share state. The
  route part is the route's name (the function name), or `name=`; routes with the
  same `name` and rate share a budget.
- **Stacked limits** are all checked before any is charged, so a request one of them
  refuses spends nothing on the others.
- **Headers:** a refusal is the usual `429` with `Retry-After` and `X-RateLimit-*`. On success,
  `X-RateLimit-*` reports the tightest of the route's limits.
- **Decorators only change the post-auth limits.** The IP ceiling is the same for every route.

## Per-route response cache

`@cache_response` caches an endpoint's return value, per user by default.
It runs inside the route, after auth and the rate limits:

```python
from pincer.core.cache.decorators import cache_response, response_cache


@router.get("/costs/today")
@cache_response(ttl=60)  # scope="user": one entry per caller
async def get_today_costs(...): ...


@router.get("/integrations")
@cache_response(ttl=300, scope="public", vary_headers=("accept-language",))
def list_integrations(): ...  # sync endpoints work too


# after a change, from the route that makes it:
await response_cache(request.app).ainvalidate(get_today_costs, user=identity.pincer_user_id)
```

- **What is stored:** the return value, as JSON, under
  `<prefix>:kv:apicache:<route>:<owner>:<hash>`. `<route>` is the endpoint's
  module-qualified name (`pincer.api.costs.get_today_costs`), or `name=`, so two
  endpoints with the same function name never share entries. The hash covers method,
  path, the query string (`vary_query=True`) and `vary_headers`.
- **On a hit,** the stored value goes back through FastAPI, so `response_model` filtering
  applies exactly as on a miss.
- **Responses are never stored:** an endpoint that returns a `Response` (file, stream,
  redirect) is passed through.
- **`scope`:**
  - `"user"` (default): a request without an identity is not cached;
  - `"public"`: everyone shares the entry;
  - a function `request -> str | None`.
- **Headers:** `X-Cache: HIT | MISS | BYPASS`, and `Cache-Control: private, max-age=<ttl>`.
  `public` is sent only for `scope="public"` on a request without an `Authorization` header,
  so a proxy or CDN never shares an authenticated answer. The TTL defaults to
  `PINCER_CACHE_DEFAULT_TTL`.
- **Hits still count against the route's rate limits**, whichever order the two
  decorators are in. A refused request never reaches the cache.
- **The decorator wins over `cache_control`:** a route with both is cached only by the
  decorator, never by the shared hishel cache.
- **Invalidation:** `invalidate(endpoint, user=None)` and `ainvalidate(...)` (the endpoint function or its cache name) drop a route's
  entries, for one owner or all. Keep TTLs short on user data: nothing else expires an
  entry when the data changes.
- **Failures:** if the store fails, the route is served uncached and a warning is logged
  once.

## Where pincer uses them

| Route | Rule |
|---|---|
| `POST /api/chat/message`, `/stream` | `PINCER_RATE_MESSAGES_PER_MIN` per user (token bucket), one budget for both routes; the same limit and wording as the messaging channels |
| `POST /api/voice/calls`, `/calls/scheduled` | 10 per hour per user, in front of the outbound safety gates |
| `POST /api/ops/canary` | 6 per hour for the whole deployment |
| `POST /api/voice/listen/{call_sid}/ticket` | 30 per minute per user |
| Storage writes (`PUT /text`, `POST /upload`, `/folders`, `/move`, `DELETE /object`) | 60 per minute per user, shared |
| `POST /api/skills/rebuild` | 2 per minute per user |
| `PUT /api/identity/me/password`, `POST /me/api-key` | 5 per hour per user, each |
| `GET /api/ops/slo`, `failures`, `digest`, `ga-gate`, `ga-gate/report` | Cached 60 s, shared |
| `GET /api/integrations`, `/{slug}` | Cached 60 s, shared |
| `GET /api/skills`, `/{name}` | Cached 30 s, shared; dropped by `POST /api/skills/rebuild` |
| `GET /api/costs/today`, `history`, `by-tool`, `by-model`; `GET /api/audit/stats` | Cached 30 s, shared |

Outside the HTTP routes:

- The messaging channels' per-user message and tool-call limits
  (`PINCER_RATE_MESSAGES_PER_MIN`, `PINCER_RATE_TOOLS_PER_MIN`) use the same limiter
  and backend, so with Redis they hold across `pincer run` and `pincer run tasks`.
- The website's demo call (`POST /api/public/demo-call`) keeps its three caps (a
  30-minute cooldown per number, 3 per client per day, 25 per day overall) on the same
  backend. They fail closed: if the backend can't be read, the endpoint answers 503
  instead of dialling.

## Sync and async

Both packages work from sync code (CLI commands, threads, sync httpx) and from
async code. The plain name is sync and the `Async` prefix is async, as in
hishel (`BaseStorage` / `AsyncBaseStorage`) and httpx (`Client` / `AsyncClient`).

| | Sync | Async |
|---|---|---|
| Limiter | `Limiter` | `AsyncLimiter` |
| Rate-limit storage base | `RateLimitStorage` | `AsyncRateLimitStorage` |
| In-memory buckets | `MemoryStorage` (thread-safe) | `AsyncMemoryStorage` |
| Redis buckets | `RedisStorage` (`redis.Redis`) | `AsyncRedisStorage` (`redis.asyncio.Redis`) |
| Storage for a backend URL | `create_storage(url)` | `create_async_storage(url)` |
| Shared Redis client | `pincer.core.cache.backend.get_redis(url)` | `get_async_redis(url)` |
| hishel storage, in memory | `pincer.core.cache.storage.MemoryStorage` | `AsyncMemoryStorage` |
| hishel storage for the backend | `create_http_cache_storage(settings)` | `create_async_http_cache_storage(settings)` |
| Cached outbound client | `pincer.core.cache.client.cached_client(...)` | `async_cached_client(...)` |
| Key-value store | `pincer.core.cache.kv.MemoryKeyValueStore`, `RedisKeyValueStore` | `AsyncMemoryKeyValueStore`, `AsyncRedisKeyValueStore` |
| Response cache | `ResponseCache.invalidate(...)`; sync endpoints | `ainvalidate(...)`; async endpoints |

`check(identifier)` reports whether a request would be allowed without charging it;
`hit` charges. A limiter built with `fail_open=False` refuses (`failed=True` on the
result) instead of allowing when the backend can't be asked.

Both modes use the same strategies, bucket names and prefix. A sync and an
async limiter with the same scope on one Redis count the same requests.

```python
from pincer.core.cache.keys import KeySpace
from pincer.core.ratelimit import Limiter, Rate, create_storage

limiter = Limiter(
    create_storage(settings.cache_backend_url), Rate.per_minute(10), KeySpace.for_cache(settings), "export"
)
if not limiter.hit(user_id).allowed:
    ...
```

### Cached outbound HTTP

`cached_client` / `async_cached_client` return httpx clients whose `GET`/`HEAD`
`2xx` responses are cached for `PINCER_CACHE_DEFAULT_TTL` seconds, under
`<prefix>:kv:httpclient:<namespace>`. Other methods and statuses always go to
the network. With `memory://` the storage lasts for the whole process, so
short-lived clients still share it; with `redis://` every process does.
`key_headers` adds a hash of those header values (the credential) to the cache
key, so two accounts never share an entry:

```python
from pincer.core.cache.client import cached_client

with cached_client(settings, "elevenlabs", key_headers=("xi-api-key",), base_url=API_BASE) as client:
    client.get("/v2/voices", headers={"xi-api-key": api_key})
```

The ElevenLabs voice lookups (`pincer voice list`, the doctor voice checks and
the startup validation) use it.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PINCER_CACHE_BACKEND_URL` | `memory://` | `memory://` (or `memory://localhost`) or `redis://…` / `rediss://…`. Also the default task broker |
| `PINCER_CACHE_PREFIX` | `{environment}:{service}` | Prefix template for every stored name |
| `PINCER_CACHE_DEFAULT_TTL` | `300` | Longest a cached response is kept, in seconds |
| `PINCER_RATE_API_DISABLED` | `false` | Switch HTTP API rate limiting off |
| `PINCER_RATE_API_IP_PER_MIN` | `3000` | Per-IP ceiling, before auth |
| `PINCER_RATE_API_USER_PER_MIN` | `300` | Per-user limit, after auth |
| `PINCER_RATE_API_ANON_PER_MIN` | `120` | Per-IP limit for requests without an identity |
