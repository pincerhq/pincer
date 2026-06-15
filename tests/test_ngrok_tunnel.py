"""Tests for NgrokTunnel."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pincer.tunnel.ngrok import NgrokTunnel


def _make_fake_ngrok(public_url: str = "http://abc123.ngrok-free.app") -> MagicMock:
    """Return a mock pyngrok.ngrok module."""
    fake_tunnel = MagicMock()
    fake_tunnel.public_url = public_url

    fake = MagicMock()
    fake.set_auth_token = MagicMock()
    fake.connect = MagicMock(return_value=fake_tunnel)
    fake.disconnect = MagicMock()
    return fake


def _pyngrok_patch(fake: MagicMock) -> Any:
    """Return a patch.dict that makes `from pyngrok import ngrok` resolve to *fake*.

    `from pyngrok import ngrok` looks up the `ngrok` attribute on the top-level
    `pyngrok` module object, not on `sys.modules["pyngrok.ngrok"]`. We must
    wire both so both `start()` and `stop()` get the same fake.
    """
    pyngrok_top = MagicMock()
    pyngrok_top.ngrok = fake
    return patch.dict(sys.modules, {"pyngrok": pyngrok_top, "pyngrok.ngrok": fake})


# ── start() — disabled path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_empty_authtoken_returns_empty_string() -> None:
    t = NgrokTunnel(authtoken="", domain="", target_port=8080)
    result = await t.start()
    assert result == ""
    assert t._tunnel is None


@pytest.mark.asyncio
async def test_start_empty_authtoken_does_not_import_pyngrok(monkeypatch: Any) -> None:
    monkeypatch.setitem(sys.modules, "pyngrok", None)  # would raise on import
    t = NgrokTunnel(authtoken="", domain="", target_port=8080)
    result = await t.start()
    assert result == ""


# ── start() — ImportError ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_raises_runtime_error_when_pyngrok_missing() -> None:
    with patch.dict(sys.modules, {"pyngrok": None, "pyngrok.ngrok": None}):
        t = NgrokTunnel(authtoken="tok", domain="", target_port=8080)
        with pytest.raises(RuntimeError, match="pyngrok is not installed"):
            await t.start()


# ── start() — happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_sets_auth_token() -> None:
    fake = _make_fake_ngrok()
    with _pyngrok_patch(fake):
        t = NgrokTunnel(authtoken="my-token", domain="", target_port=8080)
        await t.start()
    fake.set_auth_token.assert_called_once_with("my-token")


@pytest.mark.asyncio
async def test_start_connects_to_target_port() -> None:
    fake = _make_fake_ngrok()
    with _pyngrok_patch(fake):
        t = NgrokTunnel(authtoken="tok", domain="", target_port=9000)
        await t.start()
    fake.connect.assert_called_once_with(9000, proto="http")


@pytest.mark.asyncio
async def test_start_passes_domain_kwarg_when_set() -> None:
    fake = _make_fake_ngrok()
    with _pyngrok_patch(fake):
        t = NgrokTunnel(authtoken="tok", domain="my-bot.ngrok-free.app", target_port=8080)
        await t.start()
    fake.connect.assert_called_once_with(8080, proto="http", domain="my-bot.ngrok-free.app")


@pytest.mark.asyncio
async def test_start_omits_domain_kwarg_when_empty() -> None:
    fake = _make_fake_ngrok()
    with _pyngrok_patch(fake):
        t = NgrokTunnel(authtoken="tok", domain="", target_port=8080)
        await t.start()
    _, kwargs = fake.connect.call_args
    assert "domain" not in kwargs


@pytest.mark.asyncio
async def test_start_upgrades_http_url_to_https() -> None:
    fake = _make_fake_ngrok(public_url="http://abc.ngrok-free.app")
    with _pyngrok_patch(fake):
        t = NgrokTunnel(authtoken="tok", domain="", target_port=8080)
        result = await t.start()
    assert result == "https://abc.ngrok-free.app"


@pytest.mark.asyncio
async def test_start_leaves_https_url_unchanged() -> None:
    fake = _make_fake_ngrok(public_url="https://abc.ngrok-free.app")
    with _pyngrok_patch(fake):
        t = NgrokTunnel(authtoken="tok", domain="", target_port=8080)
        result = await t.start()
    assert result == "https://abc.ngrok-free.app"


@pytest.mark.asyncio
async def test_start_stores_tunnel_object() -> None:
    fake = _make_fake_ngrok()
    with _pyngrok_patch(fake):
        t = NgrokTunnel(authtoken="tok", domain="", target_port=8080)
        await t.start()
    assert t._tunnel is fake.connect.return_value


# ── stop() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_no_tunnel_is_noop() -> None:
    t = NgrokTunnel(authtoken="tok", domain="", target_port=8080)
    await t.stop()  # must not raise
    assert t._tunnel is None


@pytest.mark.asyncio
async def test_stop_calls_disconnect() -> None:
    fake = _make_fake_ngrok(public_url="https://x.ngrok-free.app")
    with _pyngrok_patch(fake):
        t = NgrokTunnel(authtoken="tok", domain="", target_port=8080)
        await t.start()
        await t.stop()
    fake.disconnect.assert_called_once_with("https://x.ngrok-free.app")


@pytest.mark.asyncio
async def test_stop_clears_tunnel_reference() -> None:
    fake = _make_fake_ngrok()
    with _pyngrok_patch(fake):
        t = NgrokTunnel(authtoken="tok", domain="", target_port=8080)
        await t.start()
        assert t._tunnel is not None
        await t.stop()
    assert t._tunnel is None


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    fake = _make_fake_ngrok()
    with _pyngrok_patch(fake):
        t = NgrokTunnel(authtoken="tok", domain="", target_port=8080)
        await t.start()
        await t.stop()
        await t.stop()  # second call must not raise
    assert t._tunnel is None


@pytest.mark.asyncio
async def test_stop_swallows_disconnect_exception() -> None:
    fake = _make_fake_ngrok()
    fake.disconnect.side_effect = Exception("network gone")
    with _pyngrok_patch(fake):
        t = NgrokTunnel(authtoken="tok", domain="", target_port=8080)
        await t.start()
        await t.stop()  # must not propagate
    assert t._tunnel is None


@pytest.mark.asyncio
async def test_start_raises_when_connect_returns_none() -> None:
    fake = _make_fake_ngrok()
    fake.connect.return_value = None
    with _pyngrok_patch(fake), pytest.raises(RuntimeError, match="ngrok.connect returned None"):
        t = NgrokTunnel(authtoken="tok", domain="", target_port=8080)
        await t.start()
