"""`pincer mcp` — manage MCP server connections and registry."""

from __future__ import annotations

import typer
from async_typer import AsyncTyper

from pincer.cli._shared import console
from pincer.cli.mcp_server import mcp_server_app

mcp_app = AsyncTyper(name="mcp", help="Manage MCP server connections and registry")
mcp_app.add_typer(mcp_server_app, name="server")


@mcp_app.command(name="list")
async def mcp_list() -> None:
    """List configured MCP servers and their status."""
    await _mcp_list()


async def _mcp_list() -> None:
    from rich.table import Table

    try:
        from pincer.mcp.config import load_mcp_config
    except ImportError:
        console.print("[red]MCP not installed. Run: uv pip install 'pincer-agent[mcp]'[/red]")
        return

    cfg = load_mcp_config()
    if not cfg.enabled:
        console.print("[dim]MCP disabled (PINCER_MCP_ENABLED=false)[/dim]")
        return
    if not cfg.servers:
        console.print("[dim]No MCP servers configured.[/dim]")
        console.print("  Add servers in pincer.toml [[mcp.servers]] or PINCER_MCP_SERVER_1_* env vars.")
        return

    table = Table(title="MCP Servers")
    table.add_column("Name", style="bold")
    table.add_column("Transport")
    table.add_column("Enabled")
    table.add_column("Sandbox")
    table.add_column("Approval")
    table.add_column("Timeout")

    for s in cfg.servers:
        table.add_row(
            s.name,
            s.transport.value,
            "[green]yes[/green]" if s.enabled else "[dim]no[/dim]",
            "[green]yes[/green]" if s.sandbox else "[yellow]no[/yellow]",
            ", ".join(s.approval_required),
            f"{s.timeout}s",
        )
    console.print(table)


@mcp_app.command(name="test")
async def mcp_test(
    server: str = typer.Argument(..., help="Server name to test"),
) -> None:
    """Connect to an MCP server, list its tools, then disconnect (dry-run)."""
    await _mcp_test(server)


async def _mcp_test(server_name: str) -> None:
    from rich.table import Table

    try:
        from pincer.mcp.client import MCPClientSession
        from pincer.mcp.config import load_mcp_config
    except ImportError:
        console.print("[red]MCP not installed. Run: uv pip install 'pincer-agent[mcp]'[/red]")
        return

    cfg = load_mcp_config()
    srv = next((s for s in cfg.servers if s.name == server_name), None)
    if not srv:
        console.print(f"[red]Server '{server_name}' not found in config.[/red]")
        console.print(f"  Configured: {[s.name for s in cfg.servers]}")
        raise typer.Exit(1)

    console.print(f"[bold]Testing MCP server: {server_name}[/bold]")
    console.print(f"  Transport: {srv.transport.value}")
    console.print(f"  Command:   {srv.command or srv.url}")

    session = MCPClientSession(srv)
    try:
        with console.status("[bold green]Connecting...[/bold green]"):
            await session.connect()

        console.print(f"[green]Connected! {len(session.tools)} tools discovered.[/green]\n")

        if session.tools:
            table = Table(title=f"Tools from '{server_name}'")
            table.add_column("Tool Name", style="bold")
            table.add_column("Description")
            for tool in session.tools:
                desc = (getattr(tool, "description", None) or "")[:80]
                table.add_row(tool.name, desc)
            console.print(table)

    except Exception as e:
        console.print(f"[red]Connection failed: {e}[/red]")
        raise typer.Exit(1) from e
    finally:
        await session.disconnect()
        console.print("\n[dim]Disconnected.[/dim]")


@mcp_app.command(name="tools")
async def mcp_tools(
    server: str = typer.Option("", "--server", help="Filter by server name"),
) -> None:
    """List all MCP tools (optionally filtered by server)."""
    await _mcp_tools(server)


async def _mcp_tools(server_filter: str) -> None:
    from rich.table import Table

    try:
        from pincer.mcp.client import MCPClientSession
        from pincer.mcp.config import load_mcp_config
    except ImportError:
        console.print("[red]MCP not installed.[/red]")
        return

    cfg = load_mcp_config()
    servers_to_check = [s for s in cfg.servers if s.enabled]
    if server_filter:
        servers_to_check = [s for s in servers_to_check if s.name == server_filter]
        if not servers_to_check:
            console.print(f"[red]Server '{server_filter}' not found.[/red]")
            raise typer.Exit(1)

    table = Table(title="MCP Tools")
    table.add_column("Server", style="bold")
    table.add_column("Tool")
    table.add_column("Description")
    table.add_column("Approval")

    for srv in servers_to_check:
        session = MCPClientSession(srv)
        try:
            await session.connect()
            from pincer.mcp.bridge import _requires_approval

            for tool in session.tools:
                approval = _requires_approval(tool, srv.approval_required)
                desc = (getattr(tool, "description", None) or "")[:60]
                table.add_row(
                    srv.name,
                    tool.name,
                    desc,
                    "[yellow]required[/yellow]" if approval else "[dim]none[/dim]",
                )
        except Exception as e:
            table.add_row(srv.name, "(failed)", str(e)[:60], "")
        finally:
            await session.disconnect()

    console.print(table)


@mcp_app.command(name="call")
async def mcp_call(
    server: str = typer.Argument(..., help="Server name"),
    tool: str = typer.Argument(..., help="Tool name"),
    args: list[str] = typer.Option([], "--arg", help="key=value argument (repeatable)"),  # noqa: B008
) -> None:
    """Manually call an MCP tool for debugging (e.g. --arg key=value)."""
    await _mcp_call(server, tool, args)


async def _mcp_call(server_name: str, tool_name: str, raw_args: list[str]) -> None:
    import json as _json

    try:
        from pincer.mcp.client import MCPClientSession
        from pincer.mcp.config import load_mcp_config
    except ImportError:
        console.print("[red]MCP not installed.[/red]")
        return

    cfg = load_mcp_config()
    srv = next((s for s in cfg.servers if s.name == server_name), None)
    if not srv:
        console.print(f"[red]Server '{server_name}' not found.[/red]")
        raise typer.Exit(1)

    # Parse --arg key=value
    arguments: dict = {}
    for raw in raw_args:
        if "=" not in raw:
            console.print(f"[red]Invalid argument format '{raw}' — use key=value[/red]")
            raise typer.Exit(1)
        k, v = raw.split("=", 1)
        # Try to parse JSON values (numbers, booleans, etc.)
        try:
            arguments[k.strip()] = _json.loads(v)
        except _json.JSONDecodeError:
            arguments[k.strip()] = v

    session = MCPClientSession(srv)
    try:
        await session.connect()
        console.print(f"[bold]Calling {server_name}.{tool_name}[/bold]")
        console.print(f"  Arguments: {arguments}")
        result = await session.call_tool(tool_name, arguments)
        from pincer.mcp.bridge import _format_result

        output = _format_result(result)
        console.print("\n[bold]Result:[/bold]")
        console.print(output)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from e
    finally:
        await session.disconnect()


@mcp_app.command(name="status")
async def mcp_status() -> None:
    """Show live connectivity status for all configured MCP servers."""
    await _mcp_status()


async def _mcp_status() -> None:
    import contextlib
    import time

    from rich.table import Table

    try:
        from pincer.mcp.client import MCPClientSession
        from pincer.mcp.config import load_mcp_config
    except ImportError:
        console.print("[red]MCP not installed. Run: uv pip install 'pincer-agent[mcp]'[/red]")
        return

    cfg = load_mcp_config()
    if not cfg.enabled:
        console.print("[dim]MCP disabled (PINCER_MCP_ENABLED=false)[/dim]")
        return
    if not cfg.servers:
        console.print("[dim]No MCP servers configured.[/dim]")
        return

    table = Table(title="MCP Server Status")
    table.add_column("Name", style="bold")
    table.add_column("Transport")
    table.add_column("Connected")
    table.add_column("Tools")
    table.add_column("Latency")

    for srv in cfg.servers:
        if not srv.enabled:
            table.add_row(srv.name, srv.transport.value, "[dim]disabled[/dim]", "-", "-")
            continue

        session = MCPClientSession(srv)
        latency_ms = "-"
        tool_count = "-"
        connected_cell = "[red]no[/red]"

        try:
            t0 = time.monotonic()
            await session.connect()
            latency_ms = f"{(time.monotonic() - t0) * 1000:.0f}ms"
            tool_count = str(len(session.tools))
            connected_cell = "[green]yes[/green]"
        except Exception as e:
            connected_cell = f"[red]no[/red] ({e})"
        finally:
            with contextlib.suppress(Exception):
                await session.disconnect()

        table.add_row(srv.name, srv.transport.value, connected_cell, tool_count, latency_ms)

    console.print(table)


@mcp_app.command(name="search")
async def mcp_search(
    query: str = typer.Argument(..., help="Search query"),
    registry: str = typer.Option("all", "--registry", "-r", help="Registry to search: mcp|clawhub|all"),
) -> None:
    """Search MCP Registry and ClawHub for available MCP servers."""
    await _mcp_search(query, registry)


async def _mcp_search(query: str, registry: str) -> None:
    try:
        from pincer.mcp.registry_client import MCPRegistryClient
    except ImportError:
        console.print("[red]MCP registry client not available[/red]")
        raise typer.Exit(1) from None

    console.print(f"[dim]Searching '{registry}' registry for '{query}'...[/dim]")
    client = MCPRegistryClient()
    try:
        results = await client.search(query, registry=registry)
    except Exception as e:
        console.print(f"[red]Search failed: {e}[/red]")
        raise typer.Exit(1) from e

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        return

    from rich.table import Table

    table = Table(title=f"MCP servers matching '{query}'", show_lines=False)
    table.add_column("Package", style="cyan", no_wrap=True)
    table.add_column("Name", style="green")
    table.add_column("Registry", style="dim")
    table.add_column("Description")
    table.add_column("Downloads", justify="right", style="dim")

    for entry in results:
        table.add_row(
            entry.package_name,
            entry.name,
            entry.registry,
            entry.description[:80] if entry.description else "",
            str(entry.downloads) if entry.downloads else "",
        )

    console.print(table)
    console.print(f"\n[dim]Found {len(results)} result(s). Install with:[/dim] pincer mcp install <package>")


@mcp_app.command(name="install")
async def mcp_install(
    package: str = typer.Argument(..., help="Package name, e.g. @modelcontextprotocol/server-github"),
    no_scan: bool = typer.Option(False, "--no-scan", help="Skip security scan (not recommended)"),
    name: str | None = typer.Option(None, "--name", "-n", help="Override config server name"),
) -> None:
    """Install an MCP server: download, scan, and add to pincer.toml."""
    await _mcp_install(package, scan=not no_scan, name_override=name)


async def _mcp_install(package: str, scan: bool, name_override: str | None) -> None:
    try:
        from pincer.mcp.registry_client import MCPRegistryClient
    except ImportError:
        console.print("[red]MCP registry client not available[/red]")
        raise typer.Exit(1) from None

    client = MCPRegistryClient()
    console.print(f"[dim]Installing '{package}'...[/dim]")

    if not scan:
        console.print("[yellow]⚠ Security scan disabled — install at your own risk[/yellow]")

    try:
        config, scan_info = await client.install(package, scan=scan, name_override=name_override)
    except ValueError as e:
        # Scan blocked
        console.print(f"[red]✗ Install blocked by security scan: {e}[/red]")
        raise typer.Exit(1) from e
    except Exception as e:
        console.print(f"[red]Install failed: {e}[/red]")
        raise typer.Exit(1) from e

    # Display scan result
    if not scan_info.get("skipped"):
        score = scan_info["score"]
        summary = scan_info.get("summary", f"Score {score}/100")
        color = "green" if score >= 80 else "yellow" if score >= 40 else "red"
        console.print(f"[{color}]Security scan: {summary}[/{color}]")
        if score < 80 and not typer.confirm("Score below 80. Install anyway?", default=False):
            console.print("[yellow]Installation cancelled.[/yellow]")
            raise typer.Exit(0)

    # Append to pincer.toml
    from pathlib import Path as _InstPath

    toml_path = _InstPath.cwd() / "pincer.toml"
    _append_server_to_toml(toml_path, config)

    console.print(f"[green]✓ Installed '{config.name}'[/green]")
    console.print(f"  Command: {config.command} {' '.join(config.args)}")
    if config.env:
        console.print(f"  Required env vars: {', '.join(config.env.keys())}")
    console.print(f"\n[dim]Config written to {toml_path}. Run 'pincer run' to connect.[/dim]")


def _append_server_to_toml(toml_path: object, config: object) -> None:
    """Append a new [[mcp.servers]] entry to pincer.toml."""
    existing = toml_path.read_text() if toml_path.exists() else ""
    lines: list[str] = []

    # Ensure [mcp] section exists
    if "[mcp]" not in existing:
        if existing and not existing.endswith("\n"):
            existing += "\n"
        existing += "\n[mcp]\nenabled = true\n"

    # Build the new server entry
    lines.append("\n[[mcp.servers]]")
    lines.append(f'name = "{config.name}"')
    lines.append(f'transport = "{config.transport.value}"')
    if config.command:
        lines.append(f'command = "{config.command}"')
    if config.args:
        args_toml = "[" + ", ".join(f'"{a}"' for a in config.args) + "]"
        lines.append(f"args = {args_toml}")
    if config.env:
        lines.append("[mcp.servers.env]")
        for k, v in config.env.items():
            lines.append(f'{k} = "{v}"')
    lines.append('approval_required = ["*"]')

    toml_path.write_text(existing + "\n".join(lines) + "\n")


@mcp_app.command(name="scan")
async def mcp_scan(
    path_or_package: str = typer.Argument(..., help="Path to directory or package name to scan"),
) -> None:
    """Run an AST security scan on a local directory or installed package."""
    from pathlib import Path as _Path

    target = _Path(path_or_package)
    if target.exists():
        _scan_local(target)
    else:
        await _scan_package_name(path_or_package)


def _scan_local(path: object) -> None:
    try:
        from pincer.skills.scanner import PackageScanner
    except ImportError:
        console.print("[red]Scanner not available[/red]")
        raise typer.Exit(1) from None

    console.print(f"[dim]Scanning {path}...[/dim]")
    scanner = PackageScanner()
    result = scanner.scan_path(path)
    _print_scan_result(result)


async def _scan_package_name(package: str) -> None:
    try:
        from pincer.mcp.registry_client import MCPRegistryClient, _infer_entry
    except ImportError:
        console.print("[red]Scanner not available[/red]")
        raise typer.Exit(1) from None

    entry = _infer_entry(package)
    client = MCPRegistryClient()
    console.print(f"[dim]Downloading and scanning '{package}'...[/dim]")
    try:
        result = await client._scan_package(entry)  # noqa: SLF001
    except Exception as e:
        console.print(f"[red]Scan failed: {e}[/red]")
        raise typer.Exit(1) from e
    _print_scan_result(result)


def _print_scan_result(result: object) -> None:
    score = result.score
    color = "green" if score >= 80 else "yellow" if score >= 40 else "red"
    icon = "✓" if score >= 80 else "⚠" if score >= 40 else "✗"
    console.print(f"\n[{color}]{icon} {result.summary()}[/{color}]")
    console.print(f"  Files scanned: {result.files_scanned}")

    if result.findings:
        from rich.table import Table

        table = Table(show_header=True, show_lines=False)
        table.add_column("Risk", style="bold", width=8)
        table.add_column("Rule", width=20)
        table.add_column("Message")
        table.add_column("File", style="dim")
        table.add_column("Line", justify="right", style="dim", width=5)

        risk_colors = {"critical": "red", "high": "yellow", "medium": "blue", "info": "dim"}
        for f in result.findings:
            c = risk_colors.get(str(f.risk), "white")
            table.add_row(f"[{c}]{f.risk}[/{c}]", f.rule, f.message, f.file, str(f.line) if f.line else "")
        console.print(table)

    if result.blocked:
        console.print("[red]⛔ BLOCKED — score below 40, install not recommended[/red]")
    elif score < 80:
        console.print("[yellow]⚠ Score below 80 — review findings before installing[/yellow]")
    else:
        console.print("[green]✓ Safe to install[/green]")


@mcp_app.command(name="uninstall")
def mcp_uninstall(
    server_name: str = typer.Argument(..., help="Server name as configured in pincer.toml"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Remove an MCP server from pincer.toml and clean up staging files."""
    try:
        from pincer.mcp.registry_client import MCPRegistryClient
    except ImportError:
        console.print("[red]MCP registry client not available[/red]")
        raise typer.Exit(1) from None

    if not yes and not typer.confirm(f"Remove MCP server '{server_name}' from config?"):
        console.print("[dim]Cancelled.[/dim]")
        raise typer.Exit(0)

    client = MCPRegistryClient()
    removed = client.uninstall(server_name)
    if removed:
        console.print(f"[green]✓ Removed '{server_name}' from pincer.toml[/green]")
    else:
        console.print(f"[yellow]Server '{server_name}' not found in pincer.toml[/yellow]")
        raise typer.Exit(1)


@mcp_app.command(name="serve")
async def mcp_serve(
    host: str = typer.Option("", "--host", "-H", help="Override server host (default from config)"),
    port: int = typer.Option(0, "--port", "-p", help="Override server port (default from config)"),
    approval: str = typer.Option(
        "policy",
        "--approval",
        "-a",
        help="Approval mode: policy | cli | webhook",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    """Start a standalone Pincer MCP server (no full agent required).

    External MCP clients (Claude Desktop, Cursor, VS Code) connect to this server
    and call Pincer tools. Approval is handled via the selected --approval mode:

    \b
      policy   Auto-approve/deny based on [mcp.server.approval_policy] config
      cli      Interactive terminal prompt (foreground, interactive)
      webhook  POST to mcp.server.webhook_url for external approval
    """
    await _mcp_serve(host=host, port=port, approval_mode=approval, verbose=verbose)


async def _mcp_serve(
    host: str,
    port: int,
    approval_mode: str,
    verbose: bool,
) -> None:
    import logging as _logging

    if verbose:
        _logging.basicConfig(level=_logging.DEBUG)

    try:
        from pincer.mcp.config import load_mcp_config
        from pincer.mcp.standalone import StandaloneMCPShell
    except ImportError:
        console.print("[red]MCP not installed. Run: uv pip install 'pincer-agent[mcp]'[/red]")
        raise typer.Exit(1) from None

    cfg = load_mcp_config()

    # Apply CLI host/port overrides
    if host or port:
        from dataclasses import replace as _replace

        srv = cfg.server
        srv_overridden = _replace(
            srv,
            host=host or srv.host,
            port=port or srv.port,
        )
        from dataclasses import replace as _r

        cfg = _r(cfg, server=srv_overridden)

    if not cfg.server.enabled:
        console.print(
            "[yellow]MCP server export is not enabled.[/yellow]\n"
            "  Set [bold]enabled = true[/bold] under [mcp.server] in pincer.toml, or:\n"
            "  [bold]PINCER_MCP_SERVER_EXPORT_ENABLED=true[/bold]"
        )
        raise typer.Exit(1)

    if approval_mode not in ("policy", "cli", "webhook"):
        console.print(f"[red]Unknown approval mode: '{approval_mode}'. Choose: policy | cli | webhook[/red]")
        raise typer.Exit(1)

    # Build approval policy from config
    policy_cfg = cfg.server.approval_policy
    approval_policy = policy_cfg.as_dict() if policy_cfg else {"default": "deny"}

    webhook_url = getattr(cfg.server, "webhook_url", None)
    if approval_mode == "webhook" and not webhook_url:
        console.print("[red]approval_mode='webhook' requires mcp.server.webhook_url in pincer.toml[/red]")
        raise typer.Exit(1)

    console.print("[bold]Starting Pincer MCP server[/bold]")
    console.print(f"  Endpoint:  http://{cfg.server.host}:{cfg.server.port}{cfg.server.path}")
    console.print(f"  Approval:  {approval_mode}")
    console.print(f"  Tools:     {', '.join(cfg.server.expose_tools) or '(none)'}")
    if cfg.servers:
        console.print(f"  Clients:   {len(cfg.servers)} external MCP server(s)")

    shell = StandaloneMCPShell(
        mcp_config=cfg,
        approval_mode=approval_mode,
        webhook_url=webhook_url,
        approval_policy=approval_policy,
    )

    try:
        await shell.run()
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
