"""Tests for pincer.tasks.app broker registration."""

import sys
from unittest.mock import MagicMock

from repid import InMemoryServer, RedisServer

from pincer.tasks.app import app, register_default_server

# `pincer.tasks/__init__.py` re-exports the `app` Repid instance under the
# name `app`, shadowing the `pincer.tasks.app` submodule on the package
# object — so `import pincer.tasks.app as X` resolves to the Repid instance,
# not the module. Go through sys.modules to get the actual module.
task_app_module = sys.modules["pincer.tasks.app"]


def test_register_default_server_uses_redis_when_configured(monkeypatch):
    mock_settings = MagicMock()
    mock_settings.task_broker = "redis"
    mock_settings.task_broker_url = "redis://localhost:6379/0"
    monkeypatch.setattr(task_app_module, "get_settings", lambda: mock_settings)

    register_default_server()

    server = app.servers.get_default_server()
    assert isinstance(server, RedisServer)


def test_register_default_server_defaults_to_memory(monkeypatch):
    mock_settings = MagicMock()
    mock_settings.task_broker = "memory"
    monkeypatch.setattr(task_app_module, "get_settings", lambda: mock_settings)

    register_default_server()

    server = app.servers.get_default_server()
    assert isinstance(server, InMemoryServer)
