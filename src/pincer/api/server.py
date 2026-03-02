"""Pincer API Server — RESTful API for dashboard and external consumers."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pincer.api.costs import router as costs_router

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
        if not DASHBOARD_TOKEN:
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {DASHBOARD_TOKEN}":
            return await call_next(request)
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    app.include_router(costs_router)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.5.0"}

    @app.get("/api/status")
    async def status() -> dict[str, object]:
        return {
            "agent_running": True,
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

    return app
