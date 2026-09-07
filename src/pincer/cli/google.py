"""`pincer google` — manage Google integration."""

from __future__ import annotations

import typer
from async_typer import AsyncTyper

from pincer.cli._shared import console

google_app = AsyncTyper(name="google", help="Manage Google integration")


@google_app.command(name="setup")
def google_setup() -> None:
    """One-time Google Workspace OAuth setup (opens browser for consent)."""
    from pincer.config import get_settings_relaxed
    from pincer.integrations.google.auth import ALL_SCOPES, GoogleAuth

    settings = get_settings_relaxed()
    oauth_dir = settings.google_oauth_dir()
    credentials_path = oauth_dir / "google_credentials.json"
    token_path = oauth_dir / "google_workspace_token.json"

    console.print("[bold]Google Workspace Setup[/bold]\n")
    console.print("This sets up all Google Workspace tools: Gmail, Calendar, Drive,")
    console.print("Docs, Sheets, Slides, Tasks, and Contacts (85 tools total).\n")

    if not credentials_path.exists():
        console.print(f"[red]Missing: {credentials_path}[/red]\n")
        console.print("Complete these steps in Google Cloud Console (~5 minutes):\n")
        console.print("  1. Go to [link]https://console.cloud.google.com/apis/credentials[/link]")
        console.print("  2. Create or select a project")
        console.print("  3. Enable APIs & Services → Library:")
        console.print("     Gmail API, Google Calendar API, Google Drive API,")
        console.print("     Google Docs API, Google Sheets API, Google Slides API,")
        console.print("     Google Tasks API, People API")
        console.print("  4. OAuth consent screen → External → Test mode → add your email")
        console.print("  5. Credentials → Create → OAuth 2.0 Client ID → Desktop App")
        console.print(f"  6. Download JSON → save as:\n     {credentials_path}\n")
        raise typer.Exit(1)

    if token_path.exists():
        console.print(f"[yellow]Token already exists: {token_path}[/yellow]")
        if not typer.confirm("Re-authenticate (overwrites existing token)?"):
            raise typer.Exit(0)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
    except ImportError:
        console.print("[red]google-auth-oauthlib not installed.[/red]\nRun:  uv pip install google-auth-oauthlib")
        raise typer.Exit(1) from None

    console.print(f"Requesting {len(ALL_SCOPES)} scope(s) for all Workspace APIs...")
    console.print("Opening browser for Google consent...\n")

    auth = GoogleAuth(
        credentials_path=str(credentials_path),
        token_path=str(token_path),
    )
    try:
        creds = auth.run_auth_flow(open_browser=True)
    except Exception as exc:
        console.print(f"[red]Authentication failed: {exc}[/red]")
        raise typer.Exit(1) from exc

    console.print("\n[green]Google Workspace authenticated![/green]")
    console.print(f"  Token saved to: {token_path}")
    console.print(f"  Refresh token:  {'Yes' if creds.refresh_token else 'No'}")
    console.print(f"  Scopes granted: {len(ALL_SCOPES)}")
    console.print("\n85 Google Workspace tools are now available in Pincer.")
    console.print("Start the agent:  [bold]pincer run[/bold]")


@google_app.command(name="auth")
def google_auth() -> None:
    """Run Google Calendar OAuth consent flow (legacy, 3 tools)."""

    from pincer.config import get_settings
    from pincer.tools.builtin.calendar_tool import SCOPES

    settings = get_settings()
    oauth_dir = settings.google_oauth_dir()
    credentials_path = oauth_dir / "google_credentials.json"
    token_path = oauth_dir / "google_token.json"

    console.print("[bold]Google Calendar — OAuth Setup[/bold]\n")

    if not credentials_path.exists():
        console.print(f"[red]Missing: {credentials_path}[/red]")
        console.print(
            "\nDownload the OAuth client JSON from:\n"
            "  Google Cloud Console -> APIs & Services -> Credentials\n"
            "  -> OAuth 2.0 Client IDs -> Download JSON\n"
            f"\nSave it as: {credentials_path}"
        )
        raise typer.Exit(1)

    if token_path.exists():
        console.print(f"[yellow]Token already exists: {token_path}[/yellow]")
        if not typer.confirm("Overwrite existing token?"):
            raise typer.Exit(0)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        console.print("[red]google-auth-oauthlib is not installed.[/red]\nRun:  uv pip install google-auth-oauthlib")
        raise typer.Exit(1) from None

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)

    console.print("Opening browser for Google consent...\n")
    try:
        creds = flow.run_local_server(port=0)
    except Exception:
        console.print(
            "[yellow]Browser not available. Trying manual flow...[/yellow]\n"
            "Open the URL below in any browser, then paste the code back here.\n"
        )
        creds = flow.run_local_server(port=8080, open_browser=False)

    with open(token_path, "w") as f:
        f.write(creds.to_json())

    console.print("\n[green]Google Calendar authorized![/green]")
    console.print(f"  Token saved to: {token_path}")
    console.print(f"  Refresh token:  {'Yes' if creds.refresh_token else 'No'}")
    console.print(f"  Expires:        {creds.expiry}")
    console.print("\nYou can now use calendar tools in Pincer.")
