"""One-time Microsoft 365 setup wizard (device code auth flow)."""

from __future__ import annotations

import sys
from pathlib import Path

from .config import get_settings


def _load_dotenv(env_path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines from a .env file. Ignores comments and blank lines."""
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _get_ms365_config() -> tuple[str, str]:
    """Return (client_id, tenant_id) from env or .env file fallback."""

    config = get_settings()

    client_id = config.client_id
    tenant_id = config.tenant_id

    if not client_id or not tenant_id:
        for candidate in [Path(".env"), Path("../.env")]:
            if candidate.exists():
                dotenv = _load_dotenv(candidate.resolve())
                client_id = dotenv.get("PINCER_MS365_CLIENT_ID", "")
                tenant_id = dotenv.get("PINCER_MS365_TENANT_ID", "")
                break

    return client_id, tenant_id or "consumers"


def main() -> None:
    """CLI entry point: ms365-mcp-setup"""
    client_id, tenant_id = _get_ms365_config()

    if not client_id:
        print("Error: PINCER_MS365_CLIENT_ID is not set.\n")
        print("Add these to your .env file:")
        print("  PINCER_MS365_CLIENT_ID=<your-azure-app-client-id>")
        print("  PINCER_MS365_TENANT_ID=consumers  # or 'common' / your org tenant GUID")
        print("\nThen re-run: ms365-mcp-setup")
        print("\nSee docs/guides/ms365-mcp.md for Azure app registration steps.")
        sys.exit(1)

    print("Microsoft 365 Setup")
    print(f"  Client ID:  {client_id}")
    print(f"  Tenant ID:  {tenant_id}\n")

    from ms365.auth import MS365Auth
    from ms365.config import get_settings

    cache_path = get_settings().cache_path

    if cache_path.exists():
        print(f"Token cache already exists: {cache_path}")
        answer = input("Re-authenticate (overwrites existing token)? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            sys.exit(0)

    auth = MS365Auth(client_id=client_id, tenant_id=tenant_id, cache_path=str(cache_path))

    print(f"Requesting {len(auth.scopes)} permission scope(s)...")
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
    print("\n69 Microsoft 365 tools are now available.")
    print("Start the server:  ms365-mcp-run --transport http")
    print("Or run pincer:     pincer run")


if __name__ == "__main__":
    main()
