"""Key-prefix templates: `PINCER_CACHE_PREFIX` and `PINCER_TASK_PREFIX`.

The template rules live in config (not in `pincer.core.cache.keys`, which
builds the names) so that config never imports core: core depends on config,
not the other way round.

A template has two placeholders, `{environment}` and `{service}`; the default
`{environment}:{service}` renders to e.g. `production:pincer`. Braces are never
allowed in a rendered name: Redis Cluster (Azure `EnterpriseCluster`) treats
`{...}` as a hash tag, which would silently change slot placement.
"""

from __future__ import annotations

import string

SERVICE = "pincer"
DEFAULT_PREFIX_TEMPLATE = "{environment}:{service}"
_PLACEHOLDERS = frozenset({"environment", "service"})


def check_name(value: str, what: str) -> None:
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


def validate_prefix_template(template: str, setting: str = "cache_prefix") -> str:
    """Validate a prefix template without rendering real settings.

    Allowed placeholders are `{environment}` and `{service}` only — no
    positional `{}`, no conversions (`!r`) and no format specs (`:>10`).
    Literal braces (including escaped `{{`/`}}`) are rejected. *setting* names
    the setting in error messages.
    """
    if not template:
        raise ValueError(f"{setting} must not be empty")
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ValueError(f"{setting} is not a valid template: {exc}") from exc
    for literal, field, spec, conversion in parsed:
        if "{" in literal or "}" in literal:
            raise ValueError(f"{setting} must not contain literal braces (Redis Cluster hash tags): {template!r}")
        if field is None:
            continue
        if field == "":
            raise ValueError(f"{setting} must not use positional '{{}}' placeholders")
        if field not in _PLACEHOLDERS:
            raise ValueError(f"{setting} has unknown placeholder {{{field}}}; allowed: {{environment}}, {{service}}")
        if conversion or spec:
            raise ValueError(f"{setting} placeholder {{{field}}} must not use a conversion or format spec")
    # Render with stand-in values to catch empty segments / stray colons / whitespace.
    check_name(template.format(environment="environment", service=SERVICE), setting)
    return template


def render_prefix(template: str, environment: str, setting: str = "cache_prefix") -> str:
    """Render a validated template with the deployment environment (lowercased) and `SERVICE`."""
    validate_prefix_template(template, setting)
    rendered = template.format(environment=environment.strip().lower(), service=SERVICE)
    check_name(rendered, f"Rendered {setting}")
    return rendered
