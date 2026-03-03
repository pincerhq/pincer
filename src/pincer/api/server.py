"""Pincer API Server — RESTful API for dashboard and external consumers."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

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
    from pincer.config import get_settings_relaxed
    from pincer.security.audit import get_audit_logger

    try:
        settings = get_settings_relaxed()
        audit_db = settings.data_dir / "audit.db"
    except Exception:
        audit_db = Path("data/audit.db")
    audit = await get_audit_logger(audit_db)
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
            return JSONResponse(
                status_code=401,
                content={
                    "error": "PINCER_DASHBOARD_TOKEN not set. Add it to .env to enable dashboard auth.",
                },
            )
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
        return {
            "agent_running": True,
            "version": "0.5.0",
            "channels": {
                "telegram": bool(os.environ.get("PINCER_TELEGRAM_BOT_TOKEN")),
                "whatsapp": os.environ.get("PINCER_WHATSAPP_ENABLED", "").lower()
                == "true",
                "discord": bool(os.environ.get("PINCER_DISCORD_BOT_TOKEN")),
            },
        }

    @app.get("/api/doctor")
    async def run_doctor() -> dict[str, object]:
        from pincer.security.doctor import SecurityDoctor

        doc = SecurityDoctor()
        return doc.run_all().to_dict()

    if _DASHBOARD_DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(_DASHBOARD_DIST), html=True))

    return app
