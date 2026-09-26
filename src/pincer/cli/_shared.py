"""Helpers shared across multiple CLI command modules."""

from __future__ import annotations

import logging
import socket
from typing import TYPE_CHECKING, Any

from rich.console import Console

if TYPE_CHECKING:
    from pincer.config import Settings

console = Console()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        # NOTE: rich formatting temporarily disabled
        # handlers=[RichHandler(console=console, show_path=False, markup=True)],
    )
    # format="%(message)s",

    # T8.5: no raw E.164 number reaches any log sink. Installed on the root
    # handlers right after basicConfig so it covers every module's logger,
    # including third-party ones (twilio, httpx) that echo call metadata.
    from pincer.voice.pii_guard import install_log_pii_filter

    install_log_pii_filter()


def _find_env_file() -> str:
    """Return the path to the .env file (project root preferred, else home dir)."""
    from pathlib import Path

    candidates = [Path(".env"), Path("../.env"), Path.home() / ".pincer" / ".env"]
    for p in candidates:
        if p.exists():
            return str(p.resolve())
    return str(Path(".env").resolve())


def _upsert_env(env_path: str, key: str, value: str) -> None:
    """Set or update a KEY=VALUE pair in an .env file."""
    from pathlib import Path

    path = Path(env_path)
    lines: list[str] = []
    found = False
    if path.exists():
        lines = path.read_text().splitlines(keepends=True)
    new_lines: list[str] = []
    for line in lines:
        if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}\n")
    path.write_text("".join(new_lines))


def _port_in_use(host: str, port: int) -> bool:
    """Return True if something is already listening on the given host:port.

    Defensive: unresolvable inputs (e.g. mocked settings in tests) count as
    "not in use" rather than crashing startup.
    """
    try:
        check_host = "127.0.0.1" if host == "0.0.0.0" else host
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex((check_host, int(port))) == 0
    except (TypeError, ValueError, OSError):
        return False


def _print_voice_webhook_urls(settings: Any, console: Any) -> None:  # type: ignore[no-untyped-def]
    base = (settings.voice_webhook_base_url or "").rstrip("/")
    if not base:
        return
    lines = [
        "[bold]Voice webhook URLs (configure in Twilio Console):[/bold]",
        f"  Inbound:           {base}/api/apps/twilio/webhook",
        f"  Status callback:   {base}/api/apps/twilio/status",
        f"  Fallback:          {base}/api/apps/twilio/fallback",
    ]
    engine = getattr(settings, "voice_engine", "conversation_relay").lower().strip()
    host = base
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    if engine == "media_streams":
        lines.append(f"  Media stream WS:   wss://{host}/api/apps/twilio/stream/{{CallSid}}")
    else:
        # ConversationRelay is WS-only (the relay-webhook HTTP path caused Twilio 64101).
        lines.append(f"  ConversationRelay: wss://{host}/api/apps/twilio/relay")
    for line in lines:
        console.print(line)


def _create_memory_backend(settings: Settings):  # type: ignore[return]
    """Factory: select and construct the configured memory backend."""
    if settings.memory_backend == "mcp":
        from pincer.memory.mcp import MCPMemoryBackend

        return MCPMemoryBackend(server_name=settings.memory_mcp_server)
    from pincer.memory.sqlite import SqlMemoryBackend

    return SqlMemoryBackend(settings.db_path)
