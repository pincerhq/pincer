"""Tests for pincer.redis_keys — the single builder for Redis names."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pincer.redis_keys import SERVICE, RedisKeys, render_prefix


def _settings(prefix: str = "{environment}:{service}", environment: str = "production") -> SimpleNamespace:
    return SimpleNamespace(redis_prefix=prefix, environment=environment)


def test_from_settings_renders_default_template() -> None:
    assert RedisKeys.from_settings(_settings()).prefix == f"production:{SERVICE}"


def test_from_settings_lowercases_environment() -> None:
    assert RedisKeys.from_settings(_settings(environment=" Staging ")).prefix == "staging:pincer"


def test_custom_segments_flow_through() -> None:
    keys = RedisKeys.from_settings(_settings("{environment}:{service}:custom"))
    assert keys.stream("tasks") == "production:pincer:custom:stream:tasks"


def test_literal_prefix_ignores_environment() -> None:
    assert RedisKeys.from_settings(_settings("prod:pincer:custom", "development")).prefix == "prod:pincer:custom"


def test_render_rejects_environment_with_braces_or_spaces() -> None:
    with pytest.raises(ValueError, match="braces"):
        render_prefix("{environment}:{service}", "{x}")
    with pytest.raises(ValueError, match="whitespace"):
        render_prefix("{environment}:{service}", "my env")


def test_render_rejects_empty_environment() -> None:
    with pytest.raises(ValueError, match="empty segments|start or end"):
        render_prefix("{environment}:{service}", "")


def test_builders() -> None:
    keys = RedisKeys("production:pincer")
    assert keys.stream("tasks") == "production:pincer:stream:tasks"
    assert keys.stream_group("tasks") == "production:pincer:stream:tasks:group"
    assert keys.stream_dlq("tasks") == "production:pincer:stream:tasks:dlq"
    assert keys.pubsub("task_results") == "production:pincer:pubsub:task_results"
    assert keys.kv("sessions", "abc") == "production:pincer:kv:sessions:abc"
    assert keys.key("a", "b") == "production:pincer:a:b"


@pytest.mark.parametrize("part", ["", "a:b", "{tag}", "a b", "}"])
def test_key_rejects_bad_parts(part: str) -> None:
    keys = RedisKeys("production:pincer")
    with pytest.raises(ValueError):
        keys.key(part)
    with pytest.raises(ValueError):
        keys.kv("ns", part)


def test_key_requires_a_part() -> None:
    with pytest.raises(ValueError):
        RedisKeys("production:pincer").key()


def test_prefix_itself_is_validated() -> None:
    with pytest.raises(ValueError):
        RedisKeys("bad:{tag}")
