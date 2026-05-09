"""Pincer API Server — RESTful API for dashboard and external consumers."""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

_SERVER_START = time.time()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from pincer.api.audit import router as audit_router
from pincer.api.conversations import router as conversations_router
from pincer.api.costs import router as costs_router
from pincer.api.skills import router as skills_router

# Dashboard static files: use env override (Docker) or project-relative path
_DASHBOARD_DIST_ENV = os.environ.get("PINCER_DASHBOARD_DIST")
if _DASHBOARD_DIST_ENV:
    _DASHBOARD_DIST = Path(_DASHBOARD_DIST_ENV)
else:
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
    _DASHBOARD_DIST = _PROJECT_ROOT / "dashboard" / "dist"

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

DASHBOARD_TOKEN = os.environ.get("PINCER_DASHBOARD_TOKEN", "")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from pincer.security.audit import get_audit_logger

    audit = await get_audit_logger()
    yield
    await audit.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Pincer API",
        version="0.5.0",
        docs_url="/api/docs" if os.environ.get("PINCER_DEBUG") else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://localhost:5173",
            os.environ.get("PINCER_DASHBOARD_URL", ""),
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        public_paths = ("/api/health", "/api/docs", "/api/openapi.json")
        if request.url.path in public_paths:
            return await call_next(request)
        if not request.url.path.startswith("/api/"):
            return await call_next(request)  # dashboard static files
        if not DASHBOARD_TOKEN:
            return await call_next(request)  # no token configured: allow (dev/tests)
        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {DASHBOARD_TOKEN}":
            return await call_next(request)
        return JSONResponse(status_code=401, content={"error": "Invalid token"})

    app.include_router(costs_router)
    app.include_router(audit_router)
    app.include_router(conversations_router)
    app.include_router(skills_router)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.5.0"}

    @app.get("/api/status")
    async def status() -> dict[str, object]:
        channels = [
            {
                "name": "telegram",
                "type": "telegram",
                "connected": bool(os.environ.get("PINCER_TELEGRAM_BOT_TOKEN")),
            },
            {
                "name": "whatsapp",
                "type": "whatsapp",
                "connected": os.environ.get("PINCER_WHATSAPP_ENABLED", "").lower() == "true",
            },
            {
                "name": "discord",
                "type": "discord",
                "connected": bool(os.environ.get("PINCER_DISCORD_BOT_TOKEN")),
            },
        ]
        return {
            "agent_running": True,
            "version": "0.5.0",
            "uptime_seconds": int(time.time() - _SERVER_START),
            "active_sessions": 0,
            "channels": channels,
        }

    @app.get("/api/settings")
    async def get_settings_api() -> dict[str, object]:
        from pincer.config import get_settings_relaxed
        try:
            s = get_settings_relaxed()
        except Exception:
            from fastapi import HTTPException
            raise HTTPException(status_code=503, detail="Settings unavailable")
        telegram_set = bool(s.telegram_bot_token.get_secret_value())
        discord_set = bool(s.discord_bot_token.get_secret_value())
        api_key_set = bool(
            s.anthropic_api_key.get_secret_value()
            or s.openai_api_key.get_secret_value()
        )
        return {
            "llm": {
                "provider": s.default_provider.value,
                "model": s.default_model,
                "api_key_set": api_key_set,
                "max_tokens": s.max_tokens,
                "temperature": s.temperature,
            },
            "channels": {
                "telegram_enabled": telegram_set,
                "telegram_token_set": telegram_set,
                "whatsapp_enabled": s.whatsapp_enabled,
                "discord_enabled": discord_set,
                "discord_token_set": discord_set,
                "web_enabled": False,
            },
            "budget": {
                "daily_limit": s.daily_budget_usd,
                "per_conversation_limit": 0,
                "per_tool_limit": 0,
                "auto_downgrade": False,
            },
            "security": {
                "allowed_users": [str(u) for u in s.telegram_allowed_users],
                "require_approval_for": (
                    ["shell"] if getattr(s, "shell_require_approval", False) else []
                ),
                "audit_enabled": True,
                "rate_limit_messages": 0,
                "rate_limit_tools": 0,
            },
            "system_prompt": s.system_prompt,
            "timezone": "UTC",
        }

    @app.get("/api/doctor")
    async def run_doctor() -> dict[str, object]:
        from pincer.security.doctor import SecurityDoctor

        doc = SecurityDoctor()
        return doc.run_all().to_dict()

    if _DASHBOARD_DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(_DASHBOARD_DIST), html=True))

    return app
