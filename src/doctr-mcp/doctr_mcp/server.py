"""doctr-mcp — FastMCP server exposing mindee/doctr OCR capabilities.

Transports:
    stdio:  python -m doctr_mcp.server --transport stdio
    HTTP (default for this server — see docs/core-components/mcp-servers.md):
            python -m doctr_mcp.server --transport http [--host 0.0.0.0] [--port 8000]
        MCP endpoint → http://localhost:8000/mcp

Environment:
    See doctr_mcp.config.DoctrSettings for DOCTR_* tuning variables, plus the
    standard TRANSPORT / HOST / PORT variables shared by all bundled MCP servers.
"""

from __future__ import annotations

import argparse
import logging
import os
from typing import TYPE_CHECKING

import torch
import uvicorn
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from starlette.middleware.cors import CORSMiddleware

from doctr_mcp import __version__
from doctr_mcp.config import get_settings, torch_threads_default
from doctr_mcp.tools_detection import register_detection_tools
from doctr_mcp.tools_kie import register_kie_tools
from doctr_mcp.tools_meta import register_meta_tools
from doctr_mcp.tools_ocr import register_ocr_tools
from doctr_mcp.tools_recognition import register_recognition_tools

if TYPE_CHECKING:
    from starlette.requests import Request

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="doctr_mcp",
    instructions=(
        # Critical directive first: this string is truncated to a per-server
        # character budget (Pincer's mcp_instructions_max_chars, 400 by
        # default) when surfaced into the caller's system prompt, so the
        # instruction that actually changes model behavior must survive
        # truncation — the descriptive tool catalog after it is a bonus.
        "OCR MCP server (mindee/doctr). ALWAYS hand PDFs/images to these tools — "
        "do not transcribe or describe their text content yourself via your own "
        "vision, and do not use file_read, shell_exec, or python_exec on them. "
        "Use ocr_document_text for a quick plain-text read of an image or PDF. "
        "Use ocr_document for the full block/line/word tree with geometry, "
        "orientation, and language detection. "
        "Use detect_text / recognize_text for the individual detection "
        "or recognition stages, extract_key_info for key-information extraction, "
        "and list_architectures to discover valid det_arch/reco_arch values. "
        "(Tool names may appear with an additional server-name prefix depending on "
        "how the connecting MCP client namespaces tools.)"
    ),
)

register_detection_tools(mcp)
register_recognition_tools(mcp)
register_ocr_tools(mcp)
register_kie_tools(mcp)
register_meta_tools(mcp)

_settings = get_settings()
if _settings.log_tool_requests:
    from fastmcp.server.middleware.logging import LoggingMiddleware

    mcp.add_middleware(
        LoggingMiddleware(
            logger=logging.getLogger("doctr_mcp.requests"),
            include_payloads=True,
            max_payload_length=_settings.log_max_payload_length,
            methods=["tools/call"],
        )
    )


@mcp.custom_route("/", methods=["GET"])
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy", "service": "doctr-mcp", "version": __version__})


def _configure_torch() -> None:
    settings = get_settings()
    threads = settings.torch_threads or torch_threads_default()
    torch.set_num_threads(threads)
    logger.info("torch.set_num_threads(%d)", threads)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="doctr-mcp OCR server (fastmcp)")
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default=os.environ.get("TRANSPORT", "http"),
        help="Transport: 'http' (streamable HTTP, default for this server) or 'stdio'",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Bind host for HTTP transport (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=os.environ.get("PORT", 8000),
        help="Bind port for HTTP transport (default: 8000)",
    )
    return parser.parse_args()


def _init_telemetry_from_env() -> None:
    """Start telemetry from ``TELEMETRY_UPTRACE_DSN`` / ``TELEMETRY_POSTHOG_DSN``; off when neither is set.

    Other ``TELEMETRY_*`` variables are ignored. A misconfiguration (both set, a
    malformed DSN) raises ``TelemetryConfigError`` and stops the server before it
    serves anything.
    """
    dsn_env = {k: os.environ[k] for k in ("TELEMETRY_UPTRACE_DSN", "TELEMETRY_POSTHOG_DSN") if k in os.environ}
    if os.environ.get("OTEL_DSN"):
        logger.warning("OTEL_DSN is no longer read — rename it to TELEMETRY_UPTRACE_DSN")
    try:
        from pincer_telemetry import init as _init_telemetry
        from pincer_telemetry import provider_from_prefixed_env
    except ImportError:
        if any(v.strip() for v in dsn_env.values()):
            logger.warning('TELEMETRY_* is set but pincer-telemetry is not installed — install "doctr-mcp[telemetry]"')
        return
    provider = provider_from_prefixed_env("TELEMETRY_", dsn_env)  # config errors propagate: the server exits
    if provider is None:
        return
    try:
        _init_telemetry(provider, service_name="doctr_mcp", version=__version__)
        logger.info("Telemetry init success (%s)", provider.name)
    except Exception as _tel_err:
        logger.error("Telemetry init failed (non-fatal): %s", _tel_err)


def main() -> None:
    _init_telemetry_from_env()

    _configure_torch()
    args = _parse_args()

    if args.transport == "http":
        logger.info("Starting doctr_mcp · HTTP transport · %s:%s/mcp", args.host, args.port)
        app = mcp.http_app()
        cors_app = CORSMiddleware(app=app, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
        uvicorn.run(cors_app, host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
