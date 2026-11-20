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

Two middlewares sit on either side of authentication:

```
CORS → IP ceiling → auth → user | anon limit → HTTP cache → routes
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
- **Exempt paths:** `/api/health`, everything outside `/api/` (dashboard
  assets), and the self-authenticated webhooks under `/api/apps/twilio/`,
  `/api/apps/teams/` and `/api/storage/signed/`, whose callers share platform
  IPs.

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

The limiter lives in `pincer.core.ratelimit`, an async reimplementation of the
design of [FastAPI-RateLimiter](https://github.com/awais7012/FastAPI-RateLimiter).
It provides `SlidingWindowCounter` and `TokenBucket` strategies, memory and
Redis storage (one atomic Lua script per check), `Limiter` and the ASGI
`RateLimitMiddleware`.

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
- It sits inside auth and the rate limits, so a cached answer still needs a
  valid token and still counts against the limits.
- Routes that are not marked never pass through it. hishel reads a whole
  response before sending it, which would break streaming endpoints.
- Entries live at most `PINCER_CACHE_DEFAULT_TTL` seconds (default 300), or
  less if `max-age` says so.

Cached today: `/api/ops/slo`, `/api/ops/failures`, `/api/ops/digest`,
`/api/ops/ga-gate` and `/api/ops/ga-gate/report`, for 60 seconds.

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
