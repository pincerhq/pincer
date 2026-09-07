"""`pincer signal` — manage Signal messenger integration."""

from __future__ import annotations

import typer
from async_typer import AsyncTyper

from pincer.cli._shared import console

signal_app = AsyncTyper(name="signal", help="Manage Signal messenger integration")


@signal_app.command(name="setup")
def signal_setup() -> None:
    """Open the Signal QR-code link in your browser to register/link a device."""
    import urllib.error
    import urllib.request
    import webbrowser

    from pincer.config import get_settings_relaxed

    settings = get_settings_relaxed()
    pair_url = settings.signal_pair_url.rstrip("/")
    qr_url = f"{pair_url}/v1/qrcodelink?device_name=Pincer"

    # Pre-flight: verify signal-api is reachable before opening browser
    try:
        req = urllib.request.Request(f"{pair_url}/v1/about", method="GET")
        with urllib.request.urlopen(req, timeout=3) as _:
            pass
    except (OSError, urllib.error.URLError) as e:
        console.print("[bold red]Cannot reach signal-api[/bold red]")
        console.print(f"  {pair_url} — {e}")
        console.print(
            "\n[dim]Start signal-api first (no build required):[/dim]\n"
            "  [bold]docker compose -f docker-compose.yml -f docker-compose.signal.yml "
            "up -d signal-api[/bold]\n"
        )
        raise typer.Exit(1) from e

    console.print(f"[bold]Signal Pairing[/bold]\n\nOpening QR link: {qr_url}")
    console.print("\nScan the QR code with Signal: Settings → Linked Devices → Link New Device")
    webbrowser.open(qr_url)


@signal_app.command(name="status")
async def signal_status() -> None:
    """Check Signal API health and registered accounts."""
    await _signal_status()


async def _signal_status() -> None:
    from pincer.channels.signal_client import SignalAPIError, SignalClient
    from pincer.config import get_settings_relaxed

    settings = get_settings_relaxed()
    console.print("[bold]Signal Status[/bold]\n")
    console.print(f"  API URL:    {settings.signal_api_url}")
    console.print(f"  Phone:      {settings.signal_phone_number or '(not set)'}")
    console.print(f"  Enabled:    {settings.signal_enabled}")

    client = SignalClient(settings.signal_api_url, settings.signal_phone_number)
    try:
        await client.connect()
        try:
            health = await client.health()
            console.print(f"  Health:     [green]OK[/green] {health}")
        except SignalAPIError as e:
            console.print(f"  Health:     [red]FAIL[/red] {e}")

        accounts = await client.list_accounts()
        if accounts:
            console.print(f"  Accounts:   {', '.join(accounts)}")
        else:
            console.print("  Accounts:   (none registered yet — run `pincer signal setup`)")

        about = await client.about()
        console.print(f"  About:      {about}")
    finally:
        await client.disconnect()


@signal_app.command(name="test")
async def signal_test(
    recipient: str = typer.Argument(..., help="Recipient phone number (E.164)"),
) -> None:
    """Send a test message via Signal."""
    await _signal_test(recipient)


async def _signal_test(recipient: str) -> None:
    from pincer.channels.signal_client import SignalAPIError, SignalClient
    from pincer.config import get_settings_relaxed

    settings = get_settings_relaxed()
    if not settings.signal_phone_number:
        console.print("[red]PINCER_SIGNAL_PHONE_NUMBER not set.[/red]")
        raise typer.Exit(1)

    client = SignalClient(settings.signal_api_url, settings.signal_phone_number)
    try:
        await client.connect()
        await client.send_message(recipient, "Hello from Pincer! Signal channel is working.")
        console.print(f"[green]Test message sent to {recipient}[/green]")
    except SignalAPIError as e:
        console.print(f"[red]Send failed: {e}[/red]")
    finally:
        await client.disconnect()
