"""`pincer mcp server` — manage the Pincer MCP export server."""

from __future__ import annotations

from async_typer import AsyncTyper

from pincer.cli._shared import console

mcp_server_app = AsyncTyper(name="server", help="Manage the Pincer MCP export server")


@mcp_server_app.command(name="status")
def mcp_server_status_cmd() -> None:
    """Show whether the Pincer MCP export server is enabled and its config."""
    try:
        from pincer.mcp.config import load_mcp_config
    except ImportError:
        console.print("[red]MCP not installed.[/red]")
        return
    cfg = load_mcp_config()
    srv = cfg.server
    if not srv.enabled:
        console.print("[dim]MCP server export is disabled.[/dim]")
        console.print("  Enable with: [bold]PINCER_MCP_SERVER_EXPORT_ENABLED=true[/bold]")
        console.print("  Or set [mcp.server] enabled = true in pincer.toml")
        return
    console.print("[green]MCP server export ENABLED[/green]")
    console.print(f"  Endpoint:      {srv.host}:{srv.port}{srv.path}")
    console.print(f"  Exposed tools: {', '.join(srv.expose_tools) or '(none)'}")
    console.print("  + pincer_ask_user (always)")


@mcp_server_app.command(name="config")
def mcp_server_config_cmd() -> None:
    """Print MCP client config JSON for Claude Desktop / Cursor."""
    import json

    try:
        from pincer.mcp.config import load_mcp_config
    except ImportError:
        console.print("[red]MCP not installed.[/red]")
        return
    cfg = load_mcp_config()
    srv = cfg.server
    url = f"http://{srv.host}:{srv.port}{srv.path}"
    client_cfg = {
        "mcpServers": {
            "pincer": {
                "type": "streamable-http",
                "url": url,
            }
        }
    }
    console.print(json.dumps(client_cfg, indent=2))
    console.print(
        "\n[dim]Add the above to Claude Desktop's MCP config, then start Pincer with MCP server enabled.[/dim]"
    )
