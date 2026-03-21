"""Tests for the FastAPI server."""

import os

import pytest

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")
os.environ.setdefault("PINCER_TELEGRAM_BOT_TOKEN", "123456:TEST")
os.environ.setdefault("PINCER_DATA_DIR", "/tmp/pincer-test")

from fastapi.testclient import TestClient

from pincer.api.server import create_app


@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "version" in data


def test_status(client):
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert "agent_running" in data
    assert "channels" in data


def test_doctor_endpoint(client):
    response = client.get("/api/doctor")
    assert response.status_code == 200
    data = response.json()
    assert "score" in data
    assert "checks" in data
    assert isinstance(data["checks"], list)


def test_auth_required_with_token():
    os.environ["PINCER_DASHBOARD_TOKEN"] = "test-secret-token-1234"
    try:
        # Need to reimport to pick up new token
        from pincer.api import server

        old_token = server.DASHBOARD_TOKEN
        server.DASHBOARD_TOKEN = "test-secret-token-1234"

        app = create_app()
        client = TestClient(app)

        # Health is always public
        assert client.get("/api/health").status_code == 200

        # Protected endpoint without token
        resp = client.get("/api/status")
        assert resp.status_code == 401

        # With correct token
        resp = client.get(
            "/api/status",
            headers={"Authorization": "Bearer test-secret-token-1234"},
        )
        assert resp.status_code == 200

        # With wrong token
        resp = client.get(
            "/api/status",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

        server.DASHBOARD_TOKEN = old_token
    finally:
        os.environ.pop("PINCER_DASHBOARD_TOKEN", None)


def test_costs_today(client):
    response = client.get("/api/costs/today")
    assert response.status_code == 200
    data = response.json()
    assert "date" in data
    assert "total_usd" in data
    assert "budget" in data


def test_costs_history(client):
    response = client.get("/api/costs/history?days=7")
    assert response.status_code == 200
    data = response.json()
    assert "period_days" in data
    assert data["period_days"] == 7
    assert "data" in data
    assert "totals" in data


def test_costs_by_model(client):
    response = client.get("/api/costs/by-model?days=7")
    assert response.status_code == 200
    data = response.json()
    assert "period_days" in data
    assert "models" in data


def test_costs_by_tool(client):
    response = client.get("/api/costs/by-tool?days=7")
    assert response.status_code == 200
    data = response.json()
    assert "period_days" in data
    assert "tools" in data
