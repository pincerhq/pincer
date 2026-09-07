"""`pincer slack` — manage Slack integration."""

from __future__ import annotations

import typer
from async_typer import AsyncTyper

from pincer.cli._shared import _find_env_file, _upsert_env, console

slack_app = AsyncTyper(name="slack", help="Manage Slack integration")


@slack_app.command(name="setup")
async def slack_setup() -> None:
    """Interactive Slack bot token setup (71 tools: channels, messages, files, and more)."""
    from pincer.integrations.slack.auth import save_tokens, validate_bot_token

    console.print("[bold]Slack Setup[/bold]\n")
    console.print("This configures all 71 Slack tools: channels, messages, threads,")
    console.print("files, reactions, pins, bookmarks, reminders, search, and user management.\n")

    console.print("Steps to create a Slack App (~5 minutes):\n")
    console.print("  1. Go to [link]https://api.slack.com/apps[/link] → Create New App → From Scratch")
    console.print("  2. Name: 'Pincer Agent', select your workspace")
    console.print("  3. OAuth & Permissions → Bot Token Scopes → add these scopes:")
    console.print("     channels:read, channels:write, channels:history, channels:join,")
    console.print("     chat:write, files:read, files:write, groups:read, groups:write,")
    console.print("     groups:history, im:read, im:write, im:history, mpim:read, mpim:write,")
    console.print("     mpim:history, pins:read, pins:write, reactions:read, reactions:write,")
    console.print("     reminders:read, reminders:write, users:read, users:read.email,")
    console.print("     usergroups:read, usergroups:write, bookmarks:read, bookmarks:write,")
    console.print("     emoji:read, dnd:read")
    console.print("  4. Install to Workspace → Authorize")
    console.print("  5. Copy Bot Token (xoxb-...)")
    console.print("  6. Optional: User Token Scopes → search:read, then copy User Token (xoxp-...)")
    console.print("     (required for slack__search_messages and slack__search_files)\n")

    bot_token = typer.prompt("Paste Bot Token (xoxb-...)", hide_input=True)
    bot_token = bot_token.strip()
    if not bot_token.startswith("xoxb-"):
        console.print("[red]Error: bot token must start with 'xoxb-'[/red]")
        raise typer.Exit(1)

    console.print("\nValidating bot token...")
    try:
        info = await validate_bot_token(bot_token)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"[green]Authenticated![/green]  workspace: {info['workspace']}, bot: {info['bot_user']}")

    user_token = ""
    if typer.confirm("\nDo you have a User Token (xoxp-...) for search features?", default=False):
        user_token = typer.prompt("Paste User Token (xoxp-...)", hide_input=True).strip()
        if user_token and not user_token.startswith("xoxp-"):
            console.print("[yellow]Warning: user token should start with 'xoxp-' — saving anyway.[/yellow]")

    token_path = save_tokens(bot_token, user_token)
    console.print(f"\n[green]Tokens saved to:[/green] {token_path}")
    console.print("\n[bold]71 Slack tools are now available in Pincer.[/bold]")

    # ── Slack Channel (Socket Mode) ──────────────────────────────────────────
    console.print("\n[bold]Slack Channel Setup (optional — lets users DM Pincer)[/bold]")
    console.print("To enable the Slack channel, you need an App-Level Token (xapp-...).")
    console.print("Steps:\n")
    console.print("  1. Settings → Socket Mode → Enable Socket Mode")
    console.print("  2. Generate an App-Level Token with scope: [bold]connections:write[/bold]")
    console.print("  3. Copy the token (starts with xapp-)")
    console.print("  4. Subscribe to bot events (Event Subscriptions → Bot Events):")
    console.print("       app_mention, message.channels, message.groups,")
    console.print("       message.im, message.mpim")
    console.print("  5. Re-install to workspace if prompted\n")

    if typer.confirm("Do you want to configure the Slack channel now?", default=False):
        app_token = typer.prompt("Paste App-Level Token (xapp-...)", hide_input=True).strip()
        if not app_token.startswith("xapp-"):
            console.print("[yellow]Warning: app token should start with 'xapp-' — saving anyway.[/yellow]")

        # Persist tokens in .env (create or update)
        env_path = _find_env_file()
        _upsert_env(env_path, "PINCER_SLACK_BOT_TOKEN", bot_token)
        _upsert_env(env_path, "PINCER_SLACK_APP_TOKEN", app_token)
        console.print(f"[green]Channel tokens saved to:[/green] {env_path}")
        console.print("\n[bold green]Slack channel ready![/bold green]")
        console.print("Users can now DM @Pincer or @mention it in channels.")

    console.print("\nStart the agent:  [bold]pincer run[/bold]")
