"""`pincer init` — interactive setup wizard."""

from __future__ import annotations

import typer

from pincer.cli._shared import console


def init() -> None:
    """Interactive setup wizard — zero to running in 5 minutes."""
    from pathlib import Path as _P

    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt

    console.print(Panel("[bold]Pincer Setup Wizard[/bold]", expand=False))

    env_lines: list[str] = []

    # Step 1: LLM Provider
    console.print("\n[bold]Step 1: LLM Provider[/bold]")
    provider = Prompt.ask(
        "Choose provider",
        choices=["anthropic", "openai", "both", "compatible"],
        default="anthropic",
    )
    if provider in ("anthropic", "both"):
        key = Prompt.ask("Anthropic API key", password=True)
        env_lines.append(f"PINCER_ANTHROPIC_API_KEY={key}")
        if provider == "anthropic":
            env_lines.append("PINCER_DEFAULT_PROVIDER=anthropic")
            env_lines.append("PINCER_DEFAULT_MODEL=claude-sonnet-4-5-20250929")
    if provider in ("openai", "both"):
        key = Prompt.ask("OpenAI API key", password=True)
        env_lines.append(f"PINCER_OPENAI_API_KEY={key}")
        if provider == "openai":
            env_lines.append("PINCER_DEFAULT_PROVIDER=openai")
            env_lines.append("PINCER_DEFAULT_MODEL=gpt-4o")
    if provider == "compatible":
        # Any OpenAI-/Anthropic-wire endpoint (Grok, Ollama, local proxy, gateway).
        wire = Prompt.ask("Wire format", choices=["openai", "anthropic"], default="openai")
        name = Prompt.ask("Provider name (e.g. grok, ollama, my-claude)")
        base_url = Prompt.ask("Base URL")
        key = Prompt.ask("API key (empty if not required)", password=True, default="")
        model = Prompt.ask("Model id (e.g. grok-3, llama3.2)")
        prefix = "OPENAI" if wire == "openai" else "ANTHROPIC"
        env_lines.append(f"PINCER_DEFAULT_PROVIDER={name}")
        env_lines.append(f"PINCER_{prefix}_COMPATIBLE_PROVIDER={name}")
        env_lines.append(f"PINCER_{prefix}_COMPATIBLE_BASE_URL={base_url}")
        if key:
            env_lines.append(f"PINCER_{prefix}_COMPATIBLE_API_KEY={key}")
        env_lines.append(f"PINCER_{prefix}_COMPATIBLE_MODEL={model}")

    # Step 2: Channels
    console.print("\n[bold]Step 2: Channels[/bold]")
    if Confirm.ask("Enable Telegram?", default=False):
        token = Prompt.ask("Telegram bot token", password=True)
        env_lines.append(f"PINCER_TELEGRAM_BOT_TOKEN={token}")
        console.print(
            "  Access is controlled by PINCER_IDENTITY_MAP — set it after setup to restrict "
            "who can message the bot (unset = anyone can message it)."
        )

    if Confirm.ask("Enable Discord?", default=False):
        token = Prompt.ask("Discord bot token", password=True)
        env_lines.append(f"PINCER_DISCORD_BOT_TOKEN={token}")

    if Confirm.ask("Enable WhatsApp?", default=False):
        env_lines.append("PINCER_WHATSAPP_ENABLED=true")
        console.print("  Run [bold]pincer whatsapp setup[/bold] to pair after setup.")

    if Confirm.ask("Enable Signal?", default=False):
        env_lines.append("PINCER_SIGNAL_ENABLED=true")
        phone = Prompt.ask("Signal phone number (E.164, e.g. +491234567890)")
        env_lines.append(f"PINCER_SIGNAL_PHONE_NUMBER={phone}")
        console.print(
            "  Access is controlled by PINCER_IDENTITY_MAP — set it after setup to restrict "
            "who can DM the bot (unset = anyone can DM it)."
        )
        api_url = Prompt.ask(
            "signal-cli-rest-api URL (for Docker: http://signal-api:8080)",
            default="http://signal-api:8080",
        )
        env_lines.append(f"PINCER_SIGNAL_API_URL={api_url}")
        console.print(
            "  Start signal-api: [bold]docker compose -f docker-compose.yml -f docker-compose.signal.yml up -d[/bold]"
        )
        console.print("  Then pair: [bold]pincer signal setup[/bold] (opens 127.0.0.1:8081 in browser automatically)")

    # Step 3: Preferences
    console.print("\n[bold]Step 3: Preferences[/bold]")
    tz = Prompt.ask("Timezone", default="Europe/Berlin")
    env_lines.append(f"PINCER_TIMEZONE={tz}")
    budget = Prompt.ask("Daily budget (USD)", default="5.00")
    env_lines.append(f"PINCER_DAILY_BUDGET_USD={budget}")

    # Step 4: Voice Calling
    console.print("\n[bold]Step 4: Voice Calling[/bold]")
    if Confirm.ask("Enable voice calling (Twilio)?", default=False):
        env_lines.append("PINCER_VOICE_ENABLED=true")
        sid = Prompt.ask("Twilio Account SID")
        env_lines.append(f"PINCER_TWILIO_ACCOUNT_SID={sid}")
        token = Prompt.ask("Twilio Auth Token", password=True)
        env_lines.append(f"PINCER_TWILIO_AUTH_TOKEN={token}")
        phone = Prompt.ask("Twilio phone number (E.164, e.g. +14155551234)")
        env_lines.append(f"PINCER_TWILIO_PHONE_NUMBER={phone}")
        webhook = Prompt.ask("Public webhook URL (https://...)", default="")
        if webhook:
            env_lines.append(f"PINCER_VOICE_WEBHOOK_BASE_URL={webhook}")
        if Confirm.ask("Enable outbound calling?", default=False):
            env_lines.append("PINCER_VOICE_OUTBOUND_ENABLED=true")

    # Step 5: Optional Integrations
    console.print("\n[bold]Step 5: Optional Integrations[/bold]")
    if Confirm.ask("Configure email?", default=False):
        env_lines.append(f"PINCER_EMAIL_IMAP_HOST={Prompt.ask('IMAP host', default='imap.gmail.com')}")
        env_lines.append(f"PINCER_EMAIL_SMTP_HOST={Prompt.ask('SMTP host', default='smtp.gmail.com')}")
        env_lines.append(f"PINCER_EMAIL_USERNAME={Prompt.ask('Email username')}")
        env_lines.append(f"PINCER_EMAIL_PASSWORD={Prompt.ask('Email password', password=True)}")

    if Confirm.ask("Add OpenWeatherMap key?", default=False):
        key = Prompt.ask("OpenWeatherMap API key", password=True)
        env_lines.append(f"PINCER_OPENWEATHERMAP_API_KEY={key}")

    if Confirm.ask("Add NewsAPI key?", default=False):
        key = Prompt.ask("NewsAPI key", password=True)
        env_lines.append(f"PINCER_NEWSAPI_KEY={key}")

    # Write .env
    env_path = _P(".env")
    if env_path.exists() and not Confirm.ask(f"\n{env_path} already exists. Overwrite?", default=False):
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(0)

    env_path.write_text("\n".join(env_lines) + "\n")

    console.print(
        Panel(
            "[green]Setup complete![/green]\n\n"
            "Next steps:\n"
            "  1. [bold]pincer run[/bold]   — start the agent\n"
            "  2. [bold]pincer doctor[/bold] — verify configuration\n"
            "  3. [bold]pincer chat[/bold]  — test in the terminal",
            title="Done",
            expand=False,
        )
    )
