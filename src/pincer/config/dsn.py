"""Backend connection URLs: `redis://` for a shared Redis, `memory://` for in-process state.

The scheme picks the engine, so no setting needs a separate `memory | redis`
switch: `is_redis(url)` is the one check callers make.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, AnyUrl, RedisDsn, UrlConstraints

MEMORY_HOSTS = frozenset({"localhost"})
DEFAULT_BACKEND_URL = "memory://"
REDIS_SCHEMES = frozenset({"redis", "rediss"})


def _check_memory_url(url: AnyUrl) -> AnyUrl:
    """`memory://` names the current process only: no host but `localhost`, nothing else."""
    if url.host is not None and url.host not in MEMORY_HOSTS:
        raise ValueError(f"memory:// URL host must be empty or 'localhost', got {url.host!r}")
    if url.port is not None or url.username is not None or url.password is not None:
        raise ValueError("memory:// URL must not carry a port or credentials")
    if url.path not in (None, "", "/") or url.query or url.fragment:
        raise ValueError("memory:// URL must not carry a path, query or fragment")
    return url


MemoryDsn = Annotated[
    AnyUrl,
    UrlConstraints(allowed_schemes=["memory"], host_required=False),
    AfterValidator(_check_memory_url),
]
"""In-process backend: `memory://` or `memory://localhost`."""

BackendDsn = RedisDsn | MemoryDsn
"""A Redis server (`redis://`, `rediss://`) or the in-process backend (`memory://`)."""


def is_redis(url: AnyUrl | str | None) -> bool:
    """Whether *url* points at a Redis server (as opposed to `memory://`)."""
    if url is None:
        return False
    scheme = url.scheme if isinstance(url, AnyUrl) else str(url).partition("://")[0]
    return scheme.lower() in REDIS_SCHEMES


def backend_name(url: AnyUrl | str | None) -> str:
    """`redis` or `memory`, for logs and status lines."""
    return "redis" if is_redis(url) else "memory"


def redact_url(url: AnyUrl | str | None) -> str:
    """*url* with any password replaced by `***`, safe for logs and console output."""
    if url is None:
        return ""
    parsed = url if isinstance(url, AnyUrl) else AnyUrl(str(url))
    if parsed.password is None:
        return str(parsed)
    user = parsed.username or ""
    host = parsed.host or ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{user}:***@{host}{port}{parsed.path or ''}{query}"
