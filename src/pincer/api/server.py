"""Pincer API Server — RESTful API for dashboard and external consumers."""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from pincer.api.audit import router as audit_router
from pincer.api.auth_guard import (
    PUBLIC_PATHS,
    SELF_AUTHENTICATED_PREFIXES,
    AuthGuard,
    audit_auth_failure,
    client_ip,
    cors_origins,
)
from pincer.api.chat import router as chat_router
from pincer.api.conversations import router as conversations_router
from pincer.api.costs import router as costs_router
from pincer.api.identity import router as identity_router
from pincer.api.integrations import router as integrations_router
from pincer.api.ops import router as ops_router
from pincer.api.public_demo import router as public_demo_router
from pincer.api.schedules import router as schedules_router
from pincer.api.skills import router as skills_router
from pincer.api.voice import router as voice_api_router
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
    from pincer.voice.pii_guard import install_log_pii_filter

    # T8.5: uvicorn configures its own handlers, and `pincer serve` does not go
    # through cli._setup_logging — install the phone-number filter here too so
    # no log sink of a running API server can carry a raw E.164 number.
    install_log_pii_filter()

    try:
        settings = get_settings_relaxed()
        audit_db = settings.data_dir / "audit.db"
    except Exception:
        audit_db = Path("data/audit.db")
    audit = await get_audit_logger(audit_db)

    if not getattr(app.state, "agent", None):
        # When started via `pincer run` the CLI pre-injects the fully-built
        # agent (with MCP, memory backend, etc.) before uvicorn starts.
        # Only build a standalone agent when running the API server directly.
        try:
            app.state.agent = await build_agent_from_settings()
        except Exception as e:
            logging.getLogger(__name__).warning("Agent not built at startup: %s", e)
            app.state.agent = None

    teams_channel = getattr(app.state, "teams_channel", None)
    if teams_channel is not None:
        sub_app = teams_channel.get_sub_app()
        if sub_app is not None:
            app.mount("/api/apps/teams", sub_app)
            logging.getLogger(__name__).info("Teams sub-app mounted at /api/apps/teams")

    yield
    await audit.shutdown()


def create_app() -> FastAPI:
    settings = get_settings_relaxed()
    app = FastAPI(
        title="Pincer API",
        version="0.8.0",
        docs_url="/api/docs",  # if settings.debug else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # Add Bearer token security scheme so the Swagger UI shows an Authorize button.
    from fastapi.openapi.utils import get_openapi

    def _custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
        }
        schema["security"] = [{"BearerAuth": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = _custom_openapi  # type: ignore[method-assign]

    auth_guard = AuthGuard(
        max_failures=int(getattr(settings, "auth_max_failures", 10)),
        lockout_seconds=int(getattr(settings, "auth_lockout_seconds", 300)),
    )
    app.state.auth_guard = auth_guard

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        _dashboard_token = settings.dashboard_token.get_secret_value()
        _web_chat_token = settings.web_chat_token.get_secret_value()
        path = request.url.path
        if path in PUBLIC_PATHS:
            return await call_next(request)
        if path.startswith(SELF_AUTHENTICATED_PREFIXES):
            # Teams webhooks carry their own HMAC; Twilio webhooks are verified
            # by X-Twilio-Signature in voice/webhook_auth.py (T8.1). They cannot
            # send our Bearer token — blocking them 401s every status callback.
            return await call_next(request)
        if not path.startswith("/api/"):
            return await call_next(request)  # dashboard static files
        if not _dashboard_token and not _web_chat_token:
            # No token configured: allow (dev/tests). `pincer doctor
            # --production` reports this state CRITICAL, so it cannot ship.
            return await call_next(request)

        # T8.2 brute-force guard: an IP that keeps guessing the shared bearer
        # token is locked out with exponential backoff before the comparison.
        ip = client_ip(request)
        wait = auth_guard.retry_after(ip)
        if wait:
            await audit_auth_failure(ip, path, "locked_out", locked_for=wait)
            return JSONResponse(
                status_code=429,
                content={"error": "Too many failed authentication attempts"},
                headers={"Retry-After": str(wait)},
            )

        auth = request.headers.get("Authorization", "")
        allowed = {f"Bearer {t}" for t in (_dashboard_token, _web_chat_token) if t}
        if auth and any(secrets.compare_digest(auth, candidate) for candidate in allowed):
            auth_guard.record_success(ip)
            return await call_next(request)

        locked_for = auth_guard.record_failure(ip)
        await audit_auth_failure(ip, path, "invalid_token", locked_for=locked_for)
        return JSONResponse(status_code=401, content={"error": "Invalid token"})

    # Registered last on purpose. Starlette's add_middleware() inserts at index 0
    # and the stack is built in reverse, so the last-added middleware is the
    # outermost one. CORS must sit outside auth: a browser preflight carries no
    # Authorization header, so an inner CORS would let auth 401 the preflight --
    # and a 401 without Access-Control-Allow-Origin reads as an opaque CORS
    # failure in the browser rather than as the auth error it is.
    #
    # T8.2: `cors_origins` drops every localhost entry when
    # PINCER_ENVIRONMENT=production, so a page served from a developer machine
    # can never make credentialed calls against the production API.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins(settings),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(costs_router)
    app.include_router(audit_router)
    app.include_router(conversations_router)
    app.include_router(identity_router)
    app.include_router(schedules_router)
    app.include_router(skills_router)
    app.include_router(integrations_router)
    app.include_router(chat_router)
    app.include_router(voice_api_router)
    app.include_router(ops_router)
    # The website's demo call: unauthenticated on purpose, rate-limited hard.
    app.include_router(public_demo_router)

    if settings.voice_enabled:
        from pincer.voice.twiml_server import twilio_router, voice_router

        app.include_router(twilio_router)
        app.include_router(voice_router)  # deprecated /voice/* aliases

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.8.0"}

    @app.get("/api/status")
    async def status() -> dict[str, object]:
        settings = get_settings_relaxed()
        return {
            "agent_running": True,
            "version": "0.8.0",
            "channels": {
                "telegram": bool(settings.telegram_bot_token.get_secret_value()),
                "whatsapp": settings.whatsapp_enabled,
                "discord": bool(settings.discord_bot_token.get_secret_value()),
                "voice": settings.voice_enabled,
                "signal": settings.signal_enabled,
            },
        }

    @app.get("/api/doctor")
    async def run_doctor() -> dict[str, object]:
        from pincer.security.doctor import SecurityDoctor

        doc = SecurityDoctor()
        return doc.run_all().to_dict()

    dist = _dashboard_dist()
    if dist.is_dir():

        @app.get("/{full_path:path}")
        async def _spa(full_path: str) -> FileResponse:
            candidate = dist / full_path
            if candidate.is_file():
                # Vite content-hashes filenames under assets/, so they can be cached
                # forever; everything else (index.html, favicons) must revalidate or
                # a stale index.html keeps pointing at asset files a rebuild deleted.
                if full_path.startswith("assets/"):
                    headers = {"Cache-Control": "public, max-age=31536000, immutable"}
                else:
                    headers = {"Cache-Control": "no-cache"}
                return FileResponse(str(candidate), headers=headers)
            return FileResponse(str(dist / "index.html"), headers={"Cache-Control": "no-cache"})

    return app
