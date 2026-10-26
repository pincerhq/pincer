"""`pincer google` — manage Google integration."""

from __future__ import annotations

import typer
from async_typer import AsyncTyper

from pincer.cli._shared import console

google_app = AsyncTyper(name="google", help="Manage Google integration")


@google_app.command(name="setup")
def google_setup(
    print_env: bool = typer.Option(
        False,
        "--print-env",
        help="Don't write a token file; print PINCER_GOOGLE_TOKEN_JSON=... for .env / a secret store.",
    ),
) -> None:
    """One-time Google Workspace OAuth setup (opens browser for consent)."""
    from pincer.config import get_settings_relaxed
    from pincer.integrations.google.auth import ALL_SCOPES, GoogleAuth
    from pincer.integrations.google.token_source import CREDENTIALS_ENV, env_client_config

    settings = get_settings_relaxed()
    oauth_dir = settings.google_oauth_dir()
    credentials_path = oauth_dir / "google_credentials.json"
    token_path = oauth_dir / "google_workspace_token.json"
    client_config = None if credentials_path.exists() else env_client_config(settings)

    console.print("[bold]Google Workspace Setup[/bold]\n")
    console.print("This sets up all Google Workspace tools: Gmail, Calendar, Drive,")
    console.print("Docs, Sheets, Slides, Tasks, Contacts, and Meet.\n")

    if not credentials_path.exists() and client_config is None:
        console.print(f"[red]Missing: {credentials_path}[/red] (or set {CREDENTIALS_ENV})\n")
        console.print("Complete these steps in Google Cloud Console (~5 minutes):\n")
        console.print("  1. Go to [link]https://console.cloud.google.com/apis/credentials[/link]")
        console.print("  2. Create or select a project")
        console.print("  3. Enable APIs & Services → Library:")
        console.print("     Gmail API, Google Calendar API, Google Drive API,")
        console.print("     Google Docs API, Google Sheets API, Google Slides API,")
        console.print("     Google Tasks API, People API, Google Meet REST API")
        console.print("  4. OAuth consent screen → External → add your email")
        console.print("     (publish it — tokens of apps in Testing expire after 7 days)")
        console.print("  5. Credentials → Create → OAuth 2.0 Client ID → Desktop App")
        console.print(f"  6. Download JSON → save as:\n     {credentials_path}\n")
        raise typer.Exit(1)

    if token_path.exists() and not print_env:
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
        creds = auth.run_auth_flow(open_browser=True, save=not print_env, client_config=client_config)
    except Exception as exc:
        console.print(f"[red]Authentication failed: {exc}[/red]")
        raise typer.Exit(1) from exc

    console.print("\n[green]Google Workspace authenticated![/green]")
    console.print(f"  Refresh token:  {'Yes' if creds.refresh_token else 'No'}")
    console.print(f"  Scopes granted: {len(ALL_SCOPES)}")
    if print_env:
        _print_env_line(creds.to_json())
        return
    console.print(f"  Token saved to: {token_path}")
    console.print("\nGoogle Workspace tools are now available in Pincer.")
    console.print("Start the agent:  [bold]pincer run[/bold]")


@google_app.command(name="export-token")
def google_export_token() -> None:
    """Print an existing token file as PINCER_GOOGLE_TOKEN_JSON=... (migrate off the data volume)."""
    from pincer.config import get_settings_relaxed
    from pincer.integrations.google.token_source import decode_json_blob

    oauth_dir = get_settings_relaxed().google_oauth_dir()
    for name in ("google_workspace_token.json", "google_token.json"):
        token_path = oauth_dir / name
        if token_path.exists():
            break
    else:
        console.print(f"[red]No Google token found in {oauth_dir}.[/red] Run:  pincer google setup --print-env")
        raise typer.Exit(1)

    raw = token_path.read_text()
    try:
        data = decode_json_blob(raw)
    except ValueError as exc:
        console.print(f"[red]{token_path} is not valid token JSON: {exc}[/red]")
        raise typer.Exit(1) from exc
    if not data.get("refresh_token"):
        console.print(f"[red]{token_path} has no refresh token — it can't be used from an env var.[/red]")
        raise typer.Exit(1)

    console.print(f"Exporting {token_path}")
    _print_env_line(raw)


def _print_env_line(token_json: str) -> None:
    from pincer.integrations.google.token_source import TOKEN_ENV, encode_json_blob

    console.print(
        "\n[bold]Add this to .env or your secret store[/bold] (e.g. Key Vault), then restart Pincer.\n"
        "[yellow]It grants access to the Google account — treat it as a password.[/yellow]\n"
    )
    # Plain print: no Rich wrapping/markup, so the value can be copied verbatim.
    print(f"{TOKEN_ENV}={encode_json_blob(token_json.strip())}")


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
