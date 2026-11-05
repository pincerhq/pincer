"""`pincer identity` — identities, their channels, and the credentials they sign in to the API with.

The REST API has no shared token: a caller is an identity with a password (the
dashboard) or an API key (headless consumers). On a fresh install nobody has
either, so these commands are how the first one is made — and how a forgotten
password is reset, since they talk to the database directly and ask for no
credentials of their own.

They also manage the roster itself — create, link and unlink channels, set
profile fields and phones, activate and deactivate — in every
`PINCER_IDENTITY_MANAGEMENT` mode. In `sync` (and for the people the TOML lists
in `update`), the next start puts back what the [identity] TOML states, so a
change to such an identity is warned about.
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


def _parse_channel(channel: str, channel_user_id: str) -> tuple[str, str]:
    """A (channel, normalized id) pair as the resolver stores it, or a failure to print."""
    from pincer.channels.base import ChannelType
    from pincer.core.identity import IdentityResolver

    try:
        channel_type = ChannelType(channel.strip().lower())
    except ValueError as e:
        known = ", ".join(c.value for c in ChannelType)
        raise _fail(f"Unknown channel '{channel}'. Known channels: {known}.") from e
    cid = channel_user_id.strip()
    if not cid:
        raise _fail("The channel user id is empty.")
    if channel_type is ChannelType.TELEGRAM and not cid.lstrip("-").isdigit():
        # A running app could translate @username, but nothing does that for a
        # link made here, and the link would never match a message.
        raise _fail("A Telegram id must be the numeric user id, not a @username.")
    return channel_type.value, IdentityResolver._normalize_id(channel_type, cid)


def _parse_channel_spec(spec: str) -> tuple[str, str]:
    channel, colon, cid = spec.partition(":")
    if not colon:
        raise _fail(f"'{spec}' is not CHANNEL:ID (e.g. telegram:123456).")
    return _parse_channel(channel, cid)


def _warn_if_managed_by_toml(name: str, source: str | None) -> None:
    """Say when the next start will put back what the [identity] TOML states.

    That is any identity the TOML lists (sync and update both apply it), and
    in sync a config identity it no longer lists, which the next start removes.
    """
    from pincer.config import get_settings_relaxed
    from pincer.config.identity import load_identity_map

    mode = get_settings_relaxed().identity_management
    if mode == "clionly":
        return
    try:
        listed = name in load_identity_map(None, mode)
    except ValueError:
        listed = False
    if listed:
        console.print(
            f"[yellow]'{name}' is listed in the \\[identity] TOML and PINCER_IDENTITY_MANAGEMENT={mode}: "
            "the next start resets what the TOML states for it.[/yellow]"
        )
    elif mode == "sync" and source == "config":
        console.print(
            f"[yellow]'{name}' came from the \\[identity] TOML and PINCER_IDENTITY_MANAGEMENT=sync: "
            "the next start removes it unless the TOML lists it again.[/yellow]"
        )


def _toml_channels(name: str) -> set[tuple[str, str]]:
    """The channels the [identity] TOML lists for `name`, as stored, under the configured mode."""
    from pincer.config import get_settings_relaxed
    from pincer.config.identity import load_identity_map
    from pincer.core.identity import IdentityResolver

    try:
        entry = load_identity_map(None, get_settings_relaxed().identity_management).get(name)
    except ValueError:
        return set()
    if entry is None:
        return set()
    return {(ch, IdentityResolver._normalize_id(ch, cid)) for ch, cid in entry.channels}


async def _require(identities: IdentityService, name: str) -> dict[str, object]:
    profile = await identities.profile(name)
    if profile is None:
        raise _fail(f"No identity named '{name}'. Create it with: pincer identity create {name}")
    return profile


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
    for column in ("Name", "Status", "Source", "Email", "Display name", "Channels", "Password", "API key"):
        table.add_column(column)
    for profile in profiles:
        summary = credentials.get(profile["pincer_user_id"])
        table.add_row(
            profile["pincer_user_id"],
            "active" if profile["active"] else "[red]inactive[/red]",
            profile["source"],
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
    password: Annotated[
        str | None,
        typer.Argument(help="Password. Omit it to be prompted, which keeps it out of the shell history."),
    ] = None,
    email: Annotated[str | None, typer.Option("--email", help="Email address; can be used to sign in")] = None,
    display_name: Annotated[str | None, typer.Option("--display-name", help="Human-readable name")] = None,
    timezone: Annotated[str | None, typer.Option("--timezone", help="IANA timezone, e.g. Europe/Berlin")] = None,
    channel: Annotated[
        list[str] | None,
        typer.Option("--channel", help="CHANNEL:ID to link, e.g. telegram:123456 (repeatable)"),
    ] = None,
    api_key: Annotated[
        bool, typer.Option("--api-key", help="Give it an API key instead of a password, and print the key once")
    ] = False,
    no_password: Annotated[
        bool, typer.Option("--no-password", help="No credential: it chats on its channels but cannot sign in")
    ] = False,
) -> None:
    """Create an identity: with a password (prompted), --api-key, or --no-password, and its --channel links.

    It needs a channel or a credential. Made here, it is a CLI identity: no
    sync of the [identity] TOML removes it.
    """
    from pincer.core.identity import RESERVED_IDENTITY_NAMES
    from pincer.security import credentials as creds
    from pincer.services.identity import ChannelTakenError, NewApiKey

    if not _NAME_PATTERN.match(name):
        raise _fail("An identity name may only contain letters, digits and underscores.")
    if name.lower() in RESERVED_IDENTITY_NAMES:
        raise _fail(f"'{name}' is a reserved name.")
    if name.startswith("usr_"):
        raise _fail("Names starting with 'usr_' are reserved for auto-generated identities.")
    if email is not None and "@" not in email:
        raise _fail(f"'{email}' is not an email address.")
    if sum((api_key, no_password, password is not None)) > 1:
        raise _fail("Give one of a password, --api-key or --no-password.")
    channels = list(dict.fromkeys(_parse_channel_spec(spec) for spec in channel or []))
    if no_password and not channels:
        raise _fail("An identity with no password and no API key needs a --channel.")
    _check_timezone(timezone)

    identities, _ = await _services()
    if await identities.profile(name) is not None:
        raise _fail(f"Identity '{name}' already exists.")
    if email is not None and (owners := await identities.email_owners(email)):
        # Two identities with one email could neither of them sign in with it.
        raise _fail(f"'{email}' already belongs to '{owners[0]}'. An email can sign in for one identity only.")

    new_key: str | None = None
    password_hash: str | None = None
    if api_key:
        new_key = creds.new_api_key()
    elif not no_password:
        if password is None:
            password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
        try:
            creds.validate_password(password)
        except ValueError as e:
            raise _fail(str(e)) from e
        password_hash = await creds.hash_password(password)

    try:
        created = await identities.create_profile(
            name,
            email=email,
            display_name=display_name,
            timezone=timezone,
            password_hash=password_hash,
            api_key=NewApiKey(creds.hash_api_key(new_key), *creds.api_key_display_parts(new_key)) if new_key else None,
            channels=channels,
        )
    except ChannelTakenError as e:
        raise _fail(f"{e}. A channel id can belong to one identity only.") from e
    if not created:
        raise _fail(f"Identity '{name}' already exists.")
    console.print(f"[green]Created identity '{name}'.[/green]")
    _warn_if_managed_by_toml(name, "cli")
    if new_key:
        console.print(f"[green]API key for '{name}':[/green]")
        # Not through rich: it would wrap a long key and garble a copy-paste.
        typer.echo(new_key)
        console.print("[yellow]Store it now — it is not shown again.[/yellow]")


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

    identities, auth = await _services()
    if password is None:
        password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    try:
        await auth.set_password(name, password)
    except UnknownIdentityError as e:
        raise _fail(f"No identity named '{name}'. Create it with: pincer identity create {name}") from e
    except ValueError as e:
        raise _fail(str(e)) from e
    console.print(f"[green]Password set for '{name}'.[/green] Existing sessions were signed out.")
    profile = await identities.profile(name)
    email = profile.get("email") if profile else None
    if email and len(await identities.email_owners(email)) > 1:
        console.print(
            f"[yellow]'{email}' is shared with another identity, so it cannot be used to sign in. "
            f"Sign in as '{name}'.[/yellow]"
        )


@identity_app.command(name="revoke")
async def identity_revoke(name: Annotated[str, typer.Argument(help="Identity name")]) -> None:
    """Remove an identity's password and API key and end its sessions.

    The identity itself, its channels and its history stay; it just cannot
    sign in to the API or the dashboard any more.
    """
    _, auth = await _services()
    if not await auth.revoke(name):
        raise _fail(f"'{name}' has no password or API key to revoke.")
    console.print(f"[green]Revoked API access for '{name}'.[/green] Its password, API key and sessions are gone.")


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
        raise _fail(f"No identity named '{name}'. Create it with: pincer identity create {name} --api-key") from e
    except ApiKeyExistsError as e:
        existing = await auth.api_key_info(name)
        masked = f" ({existing.masked})" if existing else ""
        raise _fail(f"'{name}' already has an API key{masked}. Use --force to replace it.") from e

    console.print(f"[green]API key for '{name}':[/green]")
    # Not through rich: it would wrap a long key and garble a copy-paste.
    typer.echo(api_key)
    console.print("[yellow]Store it now — it is not shown again.[/yellow]")


def _check_timezone(timezone: str | None) -> None:
    if timezone is None:
        return
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise _fail(f"'{timezone}' is not an IANA timezone (e.g. Europe/Berlin).") from e


@identity_app.command(name="add-channel")
async def identity_add_channel(
    name: Annotated[str, typer.Argument(help="Identity name")],
    channel: Annotated[str, typer.Argument(help="Channel, e.g. telegram, whatsapp, signal")],
    channel_user_id: Annotated[
        str, typer.Argument(help="The user's id on that channel (numeric for Telegram, phone for WhatsApp)")
    ],
) -> None:
    """Link a channel id to an identity. A WhatsApp phone is swapped for its LID on the first message."""
    from pincer.services.identity import ChannelTakenError

    pair = _parse_channel(channel, channel_user_id)
    identities, _ = await _services()
    profile = await _require(identities, name)
    try:
        added = await identities.add_channel(name, *pair)
    except ChannelTakenError as e:
        raise _fail(f"{e}. Remove it there first: pincer identity remove-channel {e.owner} {pair[0]} {pair[1]}") from e
    if not added:
        console.print(f"[dim]{pair[0]}:{pair[1]} is already linked to '{name}'.[/dim]")
        return
    console.print(f"[green]Linked {pair[0]}:{pair[1]} to '{name}'.[/green]")
    _warn_if_managed_by_toml(name, str(profile.get("source")))


@identity_app.command(name="remove-channel")
async def identity_remove_channel(
    name: Annotated[str, typer.Argument(help="Identity name")],
    channel: Annotated[str, typer.Argument(help="Channel, e.g. telegram")],
    channel_user_id: Annotated[str, typer.Argument(help="The user's id on that channel")],
) -> None:
    """Unlink a channel id from an identity.

    Refused for a channel the [identity] TOML lists for that person: the
    next message from it would link it again. Remove it from the TOML.
    """
    pair = _parse_channel(channel, channel_user_id)
    if pair in _toml_channels(name):
        raise _fail(
            f"{pair[0]}:{pair[1]} is listed for '{name}' in the \\[identity] TOML, which links it again on the "
            "next message. Remove it from pincer.toml / pincer.local.toml instead."
        )
    identities, _ = await _services()
    profile = await _require(identities, name)
    if not await identities.unlink_channel(name, *pair):
        raise _fail(f"{pair[0]}:{pair[1]} is not linked to '{name}'.")
    console.print(f"[green]Unlinked {pair[0]}:{pair[1]} from '{name}'.[/green]")
    _warn_if_managed_by_toml(name, str(profile.get("source")))


@identity_app.command(name="set")
async def identity_set(
    name: Annotated[str, typer.Argument(help="Identity name")],
    display_name: Annotated[str | None, typer.Option("--display-name", help="Human-readable name")] = None,
    email: Annotated[str | None, typer.Option("--email", help="Email address; can be used to sign in")] = None,
    timezone: Annotated[str | None, typer.Option("--timezone", help="IANA timezone, e.g. Europe/Berlin")] = None,
    preferred_channel: Annotated[
        str | None, typer.Option("--preferred-channel", help="Channel for proactive messages, e.g. telegram")
    ] = None,
    add_phone: Annotated[
        list[str] | None, typer.Option("--add-phone", help="A confirmed phone in E.164 (repeatable)")
    ] = None,
    remove_phone: Annotated[
        list[str] | None, typer.Option("--remove-phone", help="A phone to drop (repeatable)")
    ] = None,
) -> None:
    """Set an identity's profile fields and phones."""
    from pincer.channels.base import ChannelType
    from pincer.core.phone import normalize_e164
    from pincer.services.identity import PhoneTakenError

    if email is not None and "@" not in email:
        raise _fail(f"'{email}' is not an email address.")
    _check_timezone(timezone)
    if preferred_channel is not None:
        try:
            preferred_channel = ChannelType(preferred_channel.strip().lower()).value
        except ValueError as e:
            raise _fail(f"Unknown channel '{preferred_channel}'.") from e
    added: list[str] = []
    for raw in add_phone or []:
        phone = normalize_e164(raw)
        if phone is None:
            raise _fail(f"'{raw}' is not an E.164 phone number (e.g. +4915112345678).")
        added.append(phone)
    removed: list[str] = []
    for raw in remove_phone or []:
        phone = normalize_e164(raw)
        if phone is None:
            raise _fail(f"'{raw}' is not an E.164 phone number.")
        removed.append(phone)
    fields = {
        "display_name": display_name,
        "email": email,
        "timezone": timezone,
        "preferred_channel": preferred_channel,
    }
    if all(value is None for value in fields.values()) and not added and not removed:
        raise _fail("Nothing to set. See: pincer identity set --help")

    identities, _ = await _services()
    profile = await _require(identities, name)
    if email is not None and (owners := [o for o in await identities.email_owners(email) if o != name]):
        raise _fail(f"'{email}' already belongs to '{owners[0]}'. An email can sign in for one identity only.")
    try:
        missing = await identities.update_identity(name, fields, add_phones=added, remove_phones=removed)
    except PhoneTakenError as e:
        raise _fail(f"{e}. A number can belong to one identity only. Nothing was changed.") from e
    for phone in missing:
        console.print(f"[yellow]'{name}' has no phone {phone}.[/yellow]")
    console.print(f"[green]Updated '{name}'.[/green]")
    _warn_if_managed_by_toml(name, str(profile.get("source")))


@identity_app.command(name="activate")
async def identity_activate(name: Annotated[str, typer.Argument(help="Identity name")]) -> None:
    """Switch an identity back on: its channels, phones and credentials work again."""
    identities, _ = await _services()
    profile = await _require(identities, name)
    if profile.get("active"):
        console.print(f"[dim]'{name}' is already active.[/dim]")
        return
    await identities.set_active(name, True)
    console.print(f"[green]Activated '{name}'.[/green]")


@identity_app.command(name="deactivate")
async def identity_deactivate(name: Annotated[str, typer.Argument(help="Identity name")]) -> None:
    """Switch an identity off without deleting it.

    Its messages are handled as a guest's, its phones no longer identify it on
    calls, it gets no proactive messages, and it cannot sign in; its sessions
    end now. A running app sees this at once. The [identity] TOML never
    switches it back on: use `pincer identity activate`.
    """
    identities, _ = await _services()
    profile = await _require(identities, name)
    if not profile.get("active"):
        console.print(f"[dim]'{name}' is already inactive.[/dim]")
        return
    await identities.set_active(name, False)
    console.print(f"[green]Deactivated '{name}'.[/green] Its sessions were signed out; its credentials are kept.")
