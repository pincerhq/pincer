"""The single place backend key names (streams, consumer groups, pub/sub channels, keys) are built.

Every name pincer stores starts with one configurable prefix, so several
deployments can share one Redis without seeing each other's data. The same
names are used by the in-memory backends, which keeps both behaving alike.

Two settings carry a prefix template:

- `PINCER_CACHE_PREFIX` (`settings.cache_prefix`): rate limiting and the HTTP cache;
- `PINCER_TASK_PREFIX` (`settings.task_prefix`): task streams and result
  channels, defaulting to `cache_prefix`.

A template has two placeholders, `{environment}` and `{service}`; the default
`{environment}:{service}` renders to e.g. `production:pincer`.

After the prefix comes a type segment — `stream`, `pubsub` or `kv` — so a
`SCAN MATCH production:pincer:kv:*` or an ACL pattern like
`~production:pincer:*` / `&production:pincer:*` stays simple.

Braces are never allowed in a rendered name: Redis Cluster (Azure
`EnterpriseCluster`) treats `{...}` as a hash tag, which would silently change
slot placement.

No redis import here, so the in-memory backends keep working without the
`redis` extra installed.
"""

from __future__ import annotations

import contextlib
import ipaddress
from dataclasses import dataclass

from pincer.config.prefix import DEFAULT_PREFIX_TEMPLATE, SERVICE, check_name, render_prefix, validate_prefix_template

# The template rules are config's (`pincer.config.prefix`); re-exported here
# for the code that builds names.
__all__ = [
    "DEFAULT_PREFIX_TEMPLATE",
    "SERVICE",
    "KeySpace",
    "key_part",
    "render_prefix",
    "validate_prefix_template",
]


def key_part(value: str) -> str:
    """Make an arbitrary identifier usable as one key part.

    IP addresses are normalised (`ipaddress`) and IPv6 colons become `-`, so
    `2001:db8::1` and `2001:0db8:0:0::1` share one part. Any other `:` or
    whitespace is replaced too; braces are dropped (Redis Cluster hash tags).
    """
    with contextlib.suppress(ValueError):
        value = ipaddress.ip_address(value).compressed
    cleaned = "".join("-" if ch == ":" or ch.isspace() else ch for ch in value if ch not in "{}")
    return cleaned or "-"


@dataclass(frozen=True)
class KeySpace:
    """Builds fully-qualified key names under one rendered prefix."""

    prefix: str

    def __post_init__(self) -> None:
        check_name(self.prefix, "Key prefix")

    @classmethod
    def from_template(cls, template: str, environment: str, setting: str = "cache_prefix") -> KeySpace:
        return cls(render_prefix(template, environment, setting))

    @classmethod
    def for_cache(cls, settings: object) -> KeySpace:
        """Names for rate limiting and the HTTP cache (`cache_prefix`)."""
        return cls.from_template(settings.cache_prefix, settings.environment)  # type: ignore[attr-defined]

    @classmethod
    def for_tasks(cls, settings: object) -> KeySpace:
        """Names for task streams and result channels (`task_prefix`, defaulting to `cache_prefix`)."""
        template = settings.task_prefix or settings.cache_prefix  # type: ignore[attr-defined]
        return cls.from_template(template, settings.environment, "task_prefix")  # type: ignore[attr-defined]

    def key(self, *parts: str) -> str:
        """Generic name: `<prefix>:<part>:<part>...`."""
        if not parts:
            raise ValueError("KeySpace.key() needs at least one part")
        for part in parts:
            check_name(part, "Key part")
            if ":" in part:
                raise ValueError(f"Key part must not contain ':' (pass separate parts): {part!r}")
        return ":".join((self.prefix, *parts))

    def kv(self, namespace: str, *parts: str) -> str:
        return self.key("kv", namespace, *parts)

    def stream(self, channel: str) -> str:
        return self.key("stream", channel)

    def stream_group(self, channel: str) -> str:
        return self.key("stream", channel, "group")

    def stream_dlq(self, channel: str) -> str:
        return self.key("stream", channel, "dlq")

    def pubsub(self, channel: str) -> str:
        return self.key("pubsub", channel)

    def ratelimit(self, scope: str, identifier: str, *, route: str | None = None) -> str:
        """A rate-limit bucket: `<prefix>:kv:ratelimit:<scope>[:<route>]:<identifier>` (route, identifier sanitised).

        `route` keeps a per-route limit (`@rate_limit`) apart from the default bucket of the same scope.
        """
        if route is None:
            return self.kv("ratelimit", scope, key_part(identifier))
        return self.kv("ratelimit", scope, key_part(route), key_part(identifier))

    def api_cache(self, route: str, owner: str, *parts: str) -> str:
        """A cached endpoint result (`@cache_response`): `<prefix>:kv:apicache:<route>:<owner>[:<part>...]`."""
        return self.kv("apicache", key_part(route), key_part(owner), *parts)

    def http_cache(self) -> str:
        """Prefix of the HTTP response cache entries: `<prefix>:kv:httpcache`."""
        return self.kv("httpcache")
