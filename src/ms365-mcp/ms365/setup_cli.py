"""One-time Microsoft 365 setup wizard (device code auth flow)."""

from __future__ import annotations

import sys
from pathlib import Path


def _save_config(client_id: str, tenant_id: str) -> None:
    toml_path = Path.cwd() / "pincer.toml"
    if toml_path.exists():
        content = toml_path.read_text()
        if "[integrations.ms365]" in content:
            return
    else:
        content = ""
    section = f'\n[integrations.ms365]\nenabled = true\nclient_id = "{client_id}"\ntenant_id = "{tenant_id}"\n'
    toml_path.write_text(content + section)
    print(f"  Config saved:  {toml_path}")


def main() -> None:
    """CLI entry point: ms365-mcp-setup"""
    print("Microsoft 365 Setup\n")
    print("This sets up all Microsoft 365 tools: Outlook email, Calendar, OneDrive,")
    print("To Do, Contacts, and OneNote (61 tools total).\n")

    print("Step 1: Azure App Registration\n")
    print("If you haven't registered an app yet:")
    print("  1. Go to https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade")
    print("  2. Click 'New registration'")
    print("  3. Name: 'MS365 MCP'")
    print("  4. Supported account types: choose based on your account type:")
    print("     - 'Personal Microsoft accounts only' for personal Outlook/OneDrive")
    print("     - 'Accounts in any org directory + personal' for work + personal")
    print("  5. Redirect URI: leave empty (device code flow)")
    print("  6. Click 'Register'")
    print("  7. Under 'Authentication', enable 'Allow public client flows'")
    print("  8. Copy the Application (client) ID\n")

    client_id = input("Paste your Application (client) ID: ").strip()
    if not client_id or len(client_id) < 10:
        print("Invalid client ID.")
        sys.exit(1)

    tenant_id = input("Tenant ID (press Enter for 'common'): ").strip() or "common"

    from ms365.auth import MS365Auth
    from ms365.config import get_settings

    cache_path = get_settings().cache_path

    if cache_path.exists():
        print(f"Token cache already exists: {cache_path}")
        answer = input("Re-authenticate (overwrites existing token)? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            sys.exit(0)

    auth = MS365Auth(client_id=client_id, tenant_id=tenant_id, cache_path=str(cache_path))

    print(f"\nRequesting {len(auth.scopes)} permission scope(s)...")
    print("Starting device code authentication...")
    print("(The sign-in URL and code will appear below)\n")

    try:
        result = auth.device_code_flow_sync()
    except Exception as exc:
        print(f"\nAuthentication failed: {exc}")
        sys.exit(1)

    account = auth.authenticated_account() or result.get("id_token_claims", {}).get("preferred_username", "unknown")
    print("\nMicrosoft 365 authenticated!")
    print(f"  Account:      {account}")
    print(f"  Token cached: {cache_path}")
    print(f"  Scopes:       {len(auth.scopes)}")

    _save_config(client_id, tenant_id)

    print("\n69 Microsoft 365 tools are now available.")
    print("Start the server:  pincer-ms365-mcp --transport http")


if __name__ == "__main__":
    main()
