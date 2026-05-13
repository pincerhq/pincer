"""Pincer API Server — RESTful API for dashboard and external consumers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from pincer.api.audit import router as audit_router
from pincer.api.chat import router as chat_router
from pincer.api.conversations import router as conversations_router
from pincer.api.costs import router as costs_router
from pincer.api.integrations import router as integrations_router
from pincer.api.skills import router as skills_router
from pincer.config import get_settings_relaxed

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _dashboard_dist() -> Path:
    """Dashboard static files path: settings override (Docker) or project-relative."""
    override = get_settings_relaxed().dashboard_dist
    if override:
        return Path(override)
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    return project_root / "dashboard" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    import logging

    from pincer.api._deps import build_agent_from_settings
    from pincer.config import get_settings_relaxed
    from pincer.security.audit import get_audit_logger

    try:
        settings = get_settings_relaxed()
        audit_db = settings.data_dir / "audit.db"
    except Exception:
        audit_db = Path("data/audit.db")
    audit = await get_audit_logger(audit_db)

    try:
        app.state.agent = await build_agent_from_settings()
    except Exception as e:
        # Allow the server to start without the agent (e.g. in tests) — chat
        # endpoints will return 503 until the agent is attached.
        logging.getLogger(__name__).warning("Agent not built at startup: %s", e)
        app.state.agent = None

    yield
    await audit.shutdown()


def create_app() -> FastAPI:
    cfg = get_settings_relaxed()
    app = FastAPI(
        title="Pincer API",
        version="0.5.0",
        docs_url="/api/docs" if cfg.debug else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://localhost:5173",
            "http://localhost:8080",  # 3Days.ai dev
            cfg.dashboard_url,
            cfg.web_chat_url,
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _dashboard_token = cfg.dashboard_token.get_secret_value()
    _web_chat_token = cfg.web_chat_token.get_secret_value()

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        public_paths = ("/api/health", "/api/docs", "/api/openapi.json")
        if request.url.path in public_paths:
            return await call_next(request)
        if not request.url.path.startswith("/api/"):
            return await call_next(request)  # dashboard static files
        if not _dashboard_token and not _web_chat_token:
            return await call_next(request)  # no token configured: allow (dev/tests)
        auth = request.headers.get("Authorization", "")
        allowed = {f"Bearer {t}" for t in (_dashboard_token, _web_chat_token) if t}
        if auth in allowed:
            return await call_next(request)
        return JSONResponse(status_code=401, content={"error": "Invalid token"})

    app.include_router(costs_router)
    app.include_router(audit_router)
    app.include_router(conversations_router)
    app.include_router(skills_router)
    app.include_router(integrations_router)
    app.include_router(chat_router)

    # Voice routes (Sprint 7) — mounted unconditionally; handlers check engine state
    from pincer.voice.twiml_server import voice_router

    app.include_router(voice_router)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.5.0"}

    @app.get("/api/status")
    async def status() -> dict[str, object]:
        _cfg = get_settings_relaxed()
        return {
            "agent_running": True,
            "version": "0.7.6",
            "channels": {
                "telegram": bool(_cfg.telegram_bot_token.get_secret_value()),
                "whatsapp": _cfg.whatsapp_enabled,
                "discord": bool(_cfg.discord_bot_token.get_secret_value()),
                "voice": _cfg.voice_enabled,
                "signal": _cfg.signal_enabled,
            },
        }

    @app.get("/api/doctor")
    async def run_doctor() -> dict[str, object]:
        from pincer.security.doctor import SecurityDoctor

        doc = SecurityDoctor()
        return doc.run_all().to_dict()

    dist = _dashboard_dist()
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True))

    return app
