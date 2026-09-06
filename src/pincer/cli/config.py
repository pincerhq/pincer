"""`pincer config` — show current configuration."""

from __future__ import annotations

from pincer.cli._shared import console


def config() -> None:
    """Show current configuration."""
    from pincer.config import get_settings

    try:
        settings = get_settings()
        console.print("[bold]Pincer Configuration[/bold]\n")
        console.print(f"  Provider:     {settings.default_provider}")
        console.print(f"  Model:        {settings.default_model}")
        console.print(f"  Anthropic:    {'set' if settings.anthropic_api_key.get_secret_value() else 'not set'}")
        console.print(f"  OpenAI:       {'set' if settings.openai_api_key.get_secret_value() else 'not set'}")
        console.print(f"  Telegram:     {'set' if settings.telegram_bot_token.get_secret_value() else 'not set'}")
        console.print(f"  Budget:       ${settings.daily_budget_usd:.2f}/day")
        console.print(f"  Data dir:     {settings.data_dir}")
        console.print(f"  Shell:        {'enabled' if settings.shell_enabled else 'disabled'}")
        console.print(f"  Log level:    {settings.log_level.value}")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
