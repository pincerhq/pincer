"""The single place Redis names (streams, consumer groups, pub/sub channels, keys) are built.

Every Redis name pincer uses starts with one configurable prefix,
`PINCER_REDIS_PREFIX` (`settings.redis_prefix`), so several deployments can
share one Redis without seeing each other's data. The prefix is a template
with two placeholders, `{environment}` and `{service}`; the default
`{environment}:{service}` renders to e.g. `production:pincer`.

After the prefix comes a type segment — `stream`, `pubsub` or `kv` — so a
`SCAN MATCH production:pincer:kv:*` or an ACL pattern like
`~production:pincer:*` / `&production:pincer:*` stays simple.

Braces are never allowed in a rendered name: Redis Cluster (Azure
`EnterpriseCluster`) treats `{...}` as a hash tag, which would silently change
slot placement.

No redis import here, so the in-memory broker keeps working without the
`tasks-redis` extra installed.
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from typing import Any

SERVICE = "pincer"
DEFAULT_PREFIX_TEMPLATE = "{environment}:{service}"
_PLACEHOLDERS = frozenset({"environment", "service"})


def _check_name(value: str, what: str) -> None:
    """Shared rules for a rendered prefix or a key segment."""
    if not value:
        raise ValueError(f"{what} must not be empty")
    if "{" in value or "}" in value:
        raise ValueError(f"{what} must not contain braces (Redis Cluster hash tags): {value!r}")
    if any(ch.isspace() for ch in value):
        raise ValueError(f"{what} must not contain whitespace: {value!r}")
    if value.startswith(":") or value.endswith(":"):
        raise ValueError(f"{what} must not start or end with ':': {value!r}")
    if "::" in value:
        raise ValueError(f"{what} must not contain empty segments ('::'): {value!r}")


def validate_prefix_template(template: str) -> str:
    """Validate a `redis_prefix` template without rendering real settings.

    Allowed placeholders are `{environment}` and `{service}` only — no
    positional `{}`, no conversions (`!r`) and no format specs (`:>10`).
    Literal braces (including escaped `{{`/`}}`) are rejected.
    """
    if not template:
        raise ValueError("redis_prefix must not be empty")
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ValueError(f"redis_prefix is not a valid template: {exc}") from exc
    for literal, field, spec, conversion in parsed:
        if "{" in literal or "}" in literal:
            raise ValueError(f"redis_prefix must not contain literal braces (Redis Cluster hash tags): {template!r}")
        if field is None:
            continue
        if field == "":
            raise ValueError("redis_prefix must not use positional '{}' placeholders")
        if field not in _PLACEHOLDERS:
            raise ValueError(f"redis_prefix has unknown placeholder {{{field}}}; allowed: {{environment}}, {{service}}")
        if conversion or spec:
            raise ValueError(f"redis_prefix placeholder {{{field}}} must not use a conversion or format spec")
    # Render with stand-in values to catch empty segments / stray colons / whitespace.
    _check_name(template.format(environment="environment", service=SERVICE), "redis_prefix")
    return template


def render_prefix(template: str, environment: str) -> str:
    """Render a validated template with the deployment environment (lowercased) and `SERVICE`."""
    validate_prefix_template(template)
    rendered = template.format(environment=environment.strip().lower(), service=SERVICE)
    _check_name(rendered, "Rendered redis_prefix")
    return rendered


@dataclass(frozen=True)
class RedisKeys:
    """Builds fully-qualified Redis names under one rendered prefix."""

    prefix: str

    def __post_init__(self) -> None:
        _check_name(self.prefix, "Redis prefix")

    @classmethod
    def from_settings(cls, settings: Any) -> RedisKeys:
        return cls(render_prefix(settings.redis_prefix, settings.environment))

    def key(self, *parts: str) -> str:
        """Generic name: `<prefix>:<part>:<part>...`."""
        if not parts:
            raise ValueError("RedisKeys.key() needs at least one part")
        for part in parts:
            _check_name(part, "Redis key part")
            if ":" in part:
                raise ValueError(f"Redis key part must not contain ':' (pass separate parts): {part!r}")
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
