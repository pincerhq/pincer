"""`pincer whatsapp` — manage WhatsApp integration."""

from __future__ import annotations

from async_typer import AsyncTyper

from pincer.cli._shared import console

whatsapp_app = AsyncTyper(name="whatsapp", help="Manage WhatsApp integration")


@whatsapp_app.command(name="setup")
async def whatsapp_setup() -> None:
    """Pair WhatsApp via QR code (run once to link your device)."""
    await _whatsapp_setup()


async def _whatsapp_setup() -> None:
    from pincer.config import get_settings

    settings = get_settings()
    console.print("[bold]WhatsApp Pairing[/bold]\n")
    console.print("This will display a QR code. Scan it with:")
    console.print("  WhatsApp -> Settings -> Linked Devices -> Link a Device\n")

    try:
        from pincer.channels.whatsapp import WhatsAppChannel

        wa = WhatsAppChannel(settings)

        async def noop_handler(msg):  # type: ignore[no-untyped-def]
            return "Pairing mode — send messages after running `pincer run`."

        await wa.start(noop_handler)
        console.print("\n[green]WhatsApp paired successfully![/green]")
        console.print("Session saved. Run `pincer run` with PINCER_WHATSAPP_ENABLED=true.")
        await wa.stop()
    except Exception as e:
        console.print(f"[red]Pairing failed: {e}[/red]")
