"""Tests for pincer.core.cache.keys — the single builder for backend key names."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pincer.core.cache.keys import SERVICE, KeySpace, key_part, render_prefix, validate_prefix_template


def _settings(
    prefix: str = "{environment}:{service}", environment: str = "production", task_prefix: str | None = None
) -> SimpleNamespace:
    return SimpleNamespace(cache_prefix=prefix, task_prefix=task_prefix, environment=environment)


def test_for_cache_renders_default_template() -> None:
    assert KeySpace.for_cache(_settings()).prefix == f"production:{SERVICE}"


def test_for_cache_lowercases_environment() -> None:
    assert KeySpace.for_cache(_settings(environment=" Staging ")).prefix == "staging:pincer"


def test_custom_segments_flow_through() -> None:
    keys = KeySpace.for_cache(_settings("{environment}:{service}:custom"))
    assert keys.stream("tasks") == "production:pincer:custom:stream:tasks"


def test_literal_prefix_ignores_environment() -> None:
    assert KeySpace.for_cache(_settings("prod:pincer:custom", "development")).prefix == "prod:pincer:custom"


def test_for_tasks_defaults_to_cache_prefix() -> None:
    assert KeySpace.for_tasks(_settings("{environment}:shared")).prefix == "production:shared"


def test_for_tasks_uses_its_own_prefix_when_set() -> None:
    keys = KeySpace.for_tasks(_settings("{environment}:shared", task_prefix="{environment}:{service}:tasks"))
    assert keys.prefix == "production:pincer:tasks"


def test_render_rejects_environment_with_braces_or_spaces() -> None:
    with pytest.raises(ValueError, match="braces"):
        render_prefix("{environment}:{service}", "{x}")
    with pytest.raises(ValueError, match="whitespace"):
        render_prefix("{environment}:{service}", "my env")


def test_render_rejects_empty_environment() -> None:
    with pytest.raises(ValueError, match="empty segments|start or end"):
        render_prefix("{environment}:{service}", "")


def test_errors_name_the_setting() -> None:
    with pytest.raises(ValueError, match="task_prefix has unknown placeholder"):
        validate_prefix_template("{foo}", "task_prefix")


def test_builders() -> None:
    keys = KeySpace("production:pincer")
    assert keys.stream("tasks") == "production:pincer:stream:tasks"
    assert keys.stream_group("tasks") == "production:pincer:stream:tasks:group"
    assert keys.stream_dlq("tasks") == "production:pincer:stream:tasks:dlq"
    assert keys.pubsub("task_results") == "production:pincer:pubsub:task_results"
    assert keys.kv("sessions", "abc") == "production:pincer:kv:sessions:abc"
    assert keys.key("a", "b") == "production:pincer:a:b"
    assert keys.ratelimit("ip", "10.0.0.1") == "production:pincer:kv:ratelimit:ip:10.0.0.1"
    assert keys.http_cache() == "production:pincer:kv:httpcache"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2001:db8::1", "2001-db8--1"),
        ("2001:0db8:0000:0000:0000:0000:0000:0001", "2001-db8--1"),
        ("10.0.0.1", "10.0.0.1"),
        ("018f2c1e-7b7a-7c3e-9d2a-1f2e3d4c5b6a", "018f2c1e-7b7a-7c3e-9d2a-1f2e3d4c5b6a"),
        ("a b:{c}", "a-b-c"),
        ("", "-"),
    ],
)
def test_key_part_sanitises(value: str, expected: str) -> None:
    assert key_part(value) == expected


def test_ratelimit_accepts_ipv6() -> None:
    assert KeySpace("p:pincer").ratelimit("ip", "::1") == "p:pincer:kv:ratelimit:ip:--1"


@pytest.mark.parametrize("part", ["", "a:b", "{tag}", "a b", "}"])
def test_key_rejects_bad_parts(part: str) -> None:
    keys = KeySpace("production:pincer")
    with pytest.raises(ValueError):
        keys.key(part)
    with pytest.raises(ValueError):
        keys.kv("ns", part)


def test_key_requires_a_part() -> None:
    with pytest.raises(ValueError):
        KeySpace("production:pincer").key()


def test_prefix_itself_is_validated() -> None:
    with pytest.raises(ValueError):
        KeySpace("bad:{tag}")


def test_route_scoped_names() -> None:
    keys = KeySpace("production:pincer")
    assert (
        keys.ratelimit("user", "alice", route="chat.20per60s")
        == "production:pincer:kv:ratelimit:user:chat.20per60s:alice"
    )
    assert keys.api_cache("costs", "alice", "abc") == "production:pincer:kv:apicache:costs:alice:abc"
