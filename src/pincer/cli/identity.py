"""`pincer identity` — identities and the credentials they sign in to the API with.

The REST API has no shared token: a caller is an identity with a password (the
dashboard) or an API key (headless consumers). On a fresh install nobody has
either, so these commands are how the first one is made — and how a forgotten
password is reset, since they talk to the database directly and ask for no
credentials of their own.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Annotated

import typer
from async_typer import AsyncTyper

from pincer.cli._shared import console

if TYPE_CHECKING:
    from pincer.services.auth import AuthService
    from pincer.services.identity import IdentityService

logger = logging.getLogger(__name__)

identity_app = AsyncTyper(name="identity", help="Manage identities, passwords and API keys", no_args_is_help=True)

# The same alphabet a `[identity.<key>]` TOML table allows, so a name made here
# can later be listed in the identity map unchanged.
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")


async def _services() -> tuple[IdentityService, AuthService]:
    from pincer.config import get_settings_relaxed
    from pincer.services.auth import build_auth_service
    from pincer.services.identity import IdentityService

    try:
        identities = await IdentityService.for_path(get_settings_relaxed().db_path)
        return identities, build_auth_service()
    except Exception as e:
        logger.exception("Opening the identity store failed")
        console.print(f"[red]Could not open the identity store: {e}[/red]")
        raise typer.Exit(code=1) from e


def _fail(message: str) -> typer.Exit:
    console.print(f"[red]{message}[/red]")
    return typer.Exit(code=1)


@identity_app.command(name="list")
async def identity_list() -> None:
    """List identities and what each can sign in with."""
    from rich.table import Table

    identities, auth = await _services()
    profiles = await identities.list_profiles(limit=1000)
    credentials = await auth.credential_summaries()
    if not profiles:
        console.print("[dim]No identities. Create one with: pincer identity create <name>[/dim]")
        return

    table = Table(title="Identities")
    for column in ("Name", "Email", "Display name", "Channels", "Password", "API key"):
        table.add_column(column)
    for profile in profiles:
        summary = credentials.get(profile["pincer_user_id"])
        table.add_row(
            profile["pincer_user_id"],
            profile["email"] or "",
            profile["display_name"] or "",
            ", ".join(sorted({link["channel"] for link in profile["channels"]})),
            "set" if summary and summary.has_password else "",
            summary.api_key.masked if summary and summary.api_key else "",
        )
    console.print(table)


@identity_app.command(name="create")
async def identity_create(
    name: Annotated[str, typer.Argument(help="Identity name (letters, digits, underscore)")],
    email: Annotated[str | None, typer.Option("--email", help="Email address; can be used to sign in")] = None,
    display_name: Annotated[str | None, typer.Option("--display-name", help="Human-readable name")] = None,
) -> None:
    """Create an identity that has no channel yet."""
    from pincer.core.identity import RESERVED_IDENTITY_NAMES

    if not _NAME_PATTERN.match(name):
        raise _fail("An identity name may only contain letters, digits and underscores.")
    if name.lower() in RESERVED_IDENTITY_NAMES:
        raise _fail(f"'{name}' is a reserved name.")
    if name.startswith("usr_"):
        raise _fail("Names starting with 'usr_' are reserved for auto-generated identities.")
    if email is not None and "@" not in email:
        raise _fail(f"'{email}' is not an email address.")

    identities, _ = await _services()
    if not await identities.create_profile(name, email=email, display_name=display_name):
        raise _fail(f"Identity '{name}' already exists.")
    console.print(f"[green]Created identity '{name}'.[/green]")
    console.print(f"[dim]Next: pincer identity set-password {name}[/dim]")


@identity_app.command(name="set-password")
async def identity_set_password(
    name: Annotated[str, typer.Argument(help="Identity name")],
    password: Annotated[
        str | None,
        typer.Argument(help="New password. Omit it to be prompted, which keeps it out of the shell history."),
    ] = None,
) -> None:
    """Set an identity's password. Signs the identity out of every session."""
    from pincer.services.auth import UnknownIdentityError

    _, auth = await _services()
    if password is None:
        password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    try:
        await auth.set_password(name, password)
    except UnknownIdentityError as e:
        raise _fail(f"No identity named '{name}'. Create it with: pincer identity create {name}") from e
    except ValueError as e:
        raise _fail(str(e)) from e
    console.print(f"[green]Password set for '{name}'.[/green] Existing sessions were signed out.")


@identity_app.command(name="api-key")
async def identity_api_key(
    name: Annotated[str, typer.Argument(help="Identity name")],
    force: Annotated[
        bool, typer.Option("--force", help="Replace an existing key; the old one stops working at once")
    ] = False,
) -> None:
    """Generate an identity's API key and print it — once."""
    from pincer.services.auth import ApiKeyExistsError, UnknownIdentityError

    _, auth = await _services()
    try:
        api_key, _info = await auth.generate_api_key(name, force=force)
    except UnknownIdentityError as e:
        raise _fail(f"No identity named '{name}'. Create it with: pincer identity create {name}") from e
    except ApiKeyExistsError as e:
        existing = await auth.api_key_info(name)
        masked = f" ({existing.masked})" if existing else ""
        raise _fail(f"'{name}' already has an API key{masked}. Use --force to replace it.") from e

    console.print(f"[green]API key for '{name}':[/green]")
    # Not through rich: it would wrap a long key and garble a copy-paste.
    typer.echo(api_key)
    console.print("[yellow]Store it now — it is not shown again.[/yellow]")
