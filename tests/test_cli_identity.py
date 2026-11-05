"""`pincer identity` — create identities and give them credentials (issue #228)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from argon2 import PasswordHasher
from typer.testing import CliRunner

from pincer.cli.main import app
from pincer.config import get_settings_relaxed
from pincer.db.engine import dispose_engines, get_database_url
from pincer.security import credentials
from pincer.services.auth import build_auth_service
from pincer.services.identity import IdentityService

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def _scratch_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINCER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PINCER_JWT_SECRET", "j" * 48)
    monkeypatch.setattr(credentials, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    get_settings_relaxed.cache_clear()
    yield
    get_settings_relaxed.cache_clear()


def _run(*args: str, input: str | None = None):
    # Wide enough that rich does not wrap the table cells under test.
    return runner.invoke(app, ["identity", *args], input=input, env={"COLUMNS": "200"})


async def _login(name: str, password: str):
    try:
        return await build_auth_service().login(name, password)
    finally:
        await dispose_engines()


def test_create_with_a_password_then_sign_in(tmp_path: Path) -> None:
    created = _run("create", "tamias", PASSWORD, "--email", "tamias@example.com", "--display-name", "Tamias")
    assert created.exit_code == 0, created.output
    assert "Created identity 'tamias'" in created.output
    assert "set-password" not in created.output  # nothing left to do

    import asyncio

    assert asyncio.run(_login("tamias", PASSWORD)).pincer_user_id == "tamias"
    assert asyncio.run(_login("tamias@example.com", PASSWORD)).pincer_user_id == "tamias"


def test_create_prompts_hidden_and_confirms_when_the_password_is_omitted() -> None:
    mismatch = _run("create", "tamias", input="first password\nsecond password\n")
    assert mismatch.exit_code != 0
    assert "tamias" not in _run("list").output

    result = _run("create", "tamias", input=f"{PASSWORD}\n{PASSWORD}\n")
    assert result.exit_code == 0, result.output
    assert PASSWORD not in result.output  # hidden input is not echoed

    import asyncio

    assert asyncio.run(_login("tamias", PASSWORD)).pincer_user_id == "tamias"


def test_create_with_an_api_key_prints_a_key_that_works() -> None:
    import asyncio

    created = _run("create", "widget", "--api-key")
    assert created.exit_code == 0, created.output
    key = next(line for line in created.output.splitlines() if line.startswith("pnc_"))
    assert "not shown again" in created.output

    async def who() -> tuple[str, str]:
        try:
            identity = await build_auth_service().authenticate_bearer(key)
            return identity.pincer_user_id, identity.method
        finally:
            await dispose_engines()

    assert asyncio.run(who()) == ("widget", "api_key")
    listed = _run("list").output
    assert key not in listed
    assert key[-4:] in listed


def test_create_makes_nothing_when_the_credential_is_refused() -> None:
    """Profile and credential are one step: a profile left behind without
    one would be an identity that exists until the next restart."""
    weak = _run("create", "tamias", "short")
    assert weak.exit_code == 1
    assert "at least 8 characters" in weak.output

    both = _run("create", "tamias", PASSWORD, "--api-key")
    assert both.exit_code == 1
    assert "Give one of a password, --api-key or --no-password" in both.output

    assert "No identities" in _run("list").output


def test_a_created_identity_survives_the_startup_pruning(tmp_path: Path) -> None:
    """With an identity map, identities without a channel are swept at every
    start — except the ones that can sign in. `create` must make one of those."""
    import asyncio

    assert _run("create", "tamias", PASSWORD).exit_code == 0
    assert _run("create", "widget", "--api-key").exit_code == 0

    async def restart() -> list[str]:
        try:
            identities = IdentityService(get_database_url(tmp_path / "pincer.db"))
            # what `create` used to leave behind, had it come from the config
            await identities.create_profile("bare", source="config")
            await identities.create_identity("mapped", channel="telegram", channel_user_id="1")
            await identities.prune({("telegram", "1")})
            return sorted(row["pincer_user_id"] for row in await identities.list_profiles())
        finally:
            await dispose_engines()

    assert asyncio.run(restart()) == ["mapped", "tamias", "widget"]
    assert asyncio.run(_login("tamias", PASSWORD)).pincer_user_id == "tamias"


def test_set_password_resets_and_prompts_hidden_when_omitted() -> None:
    _run("create", "tamias", PASSWORD)

    mismatch = _run("set-password", "tamias", input="first password\nsecond password\n")
    assert mismatch.exit_code != 0

    result = _run("set-password", "tamias", input="a brand new password\na brand new password\n")
    assert result.exit_code == 0, result.output
    assert "a brand new password" not in result.output  # hidden input is not echoed

    import asyncio

    from pincer.services.auth import InvalidCredentialsError

    assert asyncio.run(_login("tamias", "a brand new password")).pincer_user_id == "tamias"
    with pytest.raises(InvalidCredentialsError):
        asyncio.run(_login("tamias", PASSWORD))


def test_set_password_refuses_an_unknown_identity_and_a_weak_password() -> None:
    unknown = _run("set-password", "ghost", PASSWORD)
    assert unknown.exit_code == 1
    assert "No identity named 'ghost'" in unknown.output

    _run("create", "tamias", PASSWORD)
    weak = _run("set-password", "tamias", "short")
    assert weak.exit_code == 1
    assert "at least 8 characters" in weak.output


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("me", "reserved"),
        ("ME", "reserved"),
        ("usr_abc", "reserved for auto-generated"),
        ("john-doe", "letters, digits and underscores"),
        ("john@example.com", "letters, digits and underscores"),
    ],
)
def test_create_refuses_names_that_cannot_be_identities(name: str, message: str) -> None:
    result = _run("create", name)
    assert result.exit_code == 1
    assert message in result.output


def test_create_refuses_a_duplicate_and_a_bad_email() -> None:
    assert _run("create", "tamias", PASSWORD).exit_code == 0
    duplicate = _run("create", "tamias", PASSWORD)
    assert duplicate.exit_code == 1
    assert "already exists" in duplicate.output

    bad_email = _run("create", "other", PASSWORD, "--email", "not-an-email")
    assert bad_email.exit_code == 1


def test_api_key_is_printed_once_and_replaced_only_with_force() -> None:
    _run("create", "widget", PASSWORD)

    first = _run("api-key", "widget")
    assert first.exit_code == 0, first.output
    key = next(line for line in first.output.splitlines() if line.startswith("pnc_"))
    assert "not shown again" in first.output

    again = _run("api-key", "widget")
    assert again.exit_code == 1
    assert "--force" in again.output
    assert key not in again.output
    assert key[-4:] in again.output  # the masked form says which key is meant

    forced = _run("api-key", "widget", "--force")
    assert forced.exit_code == 0
    replacement = next(line for line in forced.output.splitlines() if line.startswith("pnc_"))
    assert replacement != key

    unknown = _run("api-key", "ghost")
    assert unknown.exit_code == 1
    assert "pincer identity create ghost --api-key" in unknown.output


def test_list_shows_who_can_sign_in_without_showing_secrets(tmp_path: Path) -> None:
    empty = _run("list")
    assert empty.exit_code == 0
    assert "No identities" in empty.output

    _run("create", "tamias", PASSWORD, "--email", "tamias@example.com")
    key_output = _run("create", "widget", "--api-key").output
    key = next(line for line in key_output.splitlines() if line.startswith("pnc_"))

    listed = _run("list")
    assert listed.exit_code == 0
    assert "tamias" in listed.output
    assert "tamias@example.com" in listed.output
    assert "widget" in listed.output
    assert "set" in listed.output
    assert key not in listed.output
    assert "argon2" not in listed.output


def test_an_identity_made_by_hand_has_no_channel(tmp_path: Path) -> None:
    import asyncio

    _run("create", "tamias", PASSWORD)

    async def channels():
        try:
            return await IdentityService(get_database_url(tmp_path / "pincer.db")).channels_for("tamias")
        finally:
            await dispose_engines()

    assert asyncio.run(channels()) == []


def test_create_refuses_an_email_that_already_signs_someone_in() -> None:
    """Two identities sharing an email could neither of them sign in with it."""
    assert _run("create", "tamias", PASSWORD, "--email", "Tamias@Example.com").exit_code == 0
    clash = _run("create", "other", PASSWORD, "--email", "tamias@example.com")
    assert clash.exit_code == 1
    assert "already belongs to 'tamias'" in clash.output
    assert "other" not in _run("list").output


def test_set_password_warns_when_the_email_cannot_sign_in(tmp_path: Path) -> None:
    import asyncio

    _run("create", "tamias", PASSWORD, "--email", "shared@example.com")

    async def add_duplicate() -> None:
        try:
            # e.g. seeded from the identity map, which the CLI check never sees
            await IdentityService(get_database_url(tmp_path / "pincer.db")).create_profile(
                "other", email="SHARED@example.com"
            )
        finally:
            await dispose_engines()

    asyncio.run(add_duplicate())

    result = _run("set-password", "tamias", PASSWORD)
    assert result.exit_code == 0
    assert "cannot be used to sign in" in result.output
    assert "Sign in as 'tamias'" in result.output


def test_revoke_removes_password_key_and_sessions() -> None:
    import asyncio

    from pincer.services.auth import InvalidCredentialsError

    _run("create", "tamias", PASSWORD)
    _run("api-key", "tamias")

    revoked = _run("revoke", "tamias")
    assert revoked.exit_code == 0, revoked.output
    assert "Revoked API access for 'tamias'" in revoked.output

    with pytest.raises(InvalidCredentialsError):
        asyncio.run(_login("tamias", PASSWORD))
    listed = _run("list").output
    assert "tamias" in listed  # the identity itself stays
    assert "pnc_" not in listed

    again = _run("revoke", "tamias")
    assert again.exit_code == 1
    assert "no password or API key" in again.output


# ── roster management (#348) ─────────────────────────────────────────


def _service(tmp_path: Path) -> IdentityService:
    return IdentityService(get_database_url(tmp_path / "pincer.db"))


def _query(tmp_path: Path, call):
    import asyncio

    async def run():
        try:
            return await call(_service(tmp_path))
        finally:
            await dispose_engines()

    return asyncio.run(run())


def test_create_with_a_channel_and_no_password(tmp_path: Path) -> None:
    created = _run(
        "create",
        "alice",
        "--no-password",
        "--channel",
        "telegram:123",
        "--channel",
        "whatsapp:+4915100000001",
        "--timezone",
        "Europe/Berlin",
    )
    assert created.exit_code == 0, created.output
    assert _query(tmp_path, lambda s: s.channels_for("alice")) == [
        ("telegram", "123"),
        ("whatsapp", "4915100000001"),  # stored the way a message names it
    ]
    profile = _query(tmp_path, lambda s: s.profile("alice"))
    assert (profile["source"], profile["active"], profile["timezone"]) == ("cli", True, "Europe/Berlin")
    assert profile["preferred_channel"] == "telegram"

    listed = _run("list").output
    assert "active" in listed
    assert "cli" in listed


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--no-password"], "needs a --channel"),
        (["--no-password", "--channel", "fax:1"], "Unknown channel 'fax'"),
        (["--no-password", "--channel", "telegram"], "is not CHANNEL:ID"),
        (["--no-password", "--channel", "telegram:@alice"], "numeric user id"),
        (["--no-password", "--channel", "telegram:1", "--timezone", "Mars/Base"], "not an IANA timezone"),
        (["--api-key", "--no-password"], "Give one of"),
    ],
)
def test_create_refuses_bad_channels_and_options(args: list[str], message: str) -> None:
    result = _run("create", "alice", *args)
    assert result.exit_code == 1
    assert message in result.output
    assert "alice" not in _run("list").output


def test_create_refuses_a_channel_someone_else_has() -> None:
    assert _run("create", "alice", "--no-password", "--channel", "telegram:1").exit_code == 0
    clash = _run("create", "bob", "--no-password", "--channel", "telegram:1")
    assert clash.exit_code == 1
    assert "already belongs to 'alice'" in clash.output
    assert "bob" not in _run("list").output


def test_add_and_remove_channel(tmp_path: Path) -> None:
    _run("create", "alice", "--no-password", "--channel", "telegram:1")
    _run("create", "bob", "--no-password", "--channel", "telegram:2")

    added = _run("add-channel", "alice", "signal", "+4915100000001")
    assert added.exit_code == 0, added.output
    assert "Linked signal:4915100000001 to 'alice'" in added.output
    assert "already linked" in _run("add-channel", "alice", "signal", "4915100000001").output

    taken = _run("add-channel", "alice", "telegram", "2")
    assert taken.exit_code == 1
    assert "already belongs to 'bob'" in taken.output
    assert _run("add-channel", "alice", "telegram", "@bob").exit_code == 1
    assert _run("add-channel", "ghost", "telegram", "9").exit_code == 1

    removed = _run("remove-channel", "alice", "signal", "4915100000001")
    assert removed.exit_code == 0, removed.output
    not_linked = _run("remove-channel", "alice", "telegram", "2")
    assert not_linked.exit_code == 1
    assert "is not linked to 'alice'" in not_linked.output
    assert _query(tmp_path, lambda s: s.channels_for("alice")) == [("telegram", "1")]


def test_set_profile_fields_and_phones(tmp_path: Path) -> None:
    _run("create", "alice", "--no-password", "--channel", "telegram:1")
    _run("create", "bob", "--no-password", "--channel", "telegram:2", "--email", "bob@example.com")

    result = _run(
        "set",
        "alice",
        "--display-name",
        "Alice A.",
        "--email",
        "alice@example.com",
        "--timezone",
        "Europe/Kyiv",
        "--preferred-channel",
        "telegram",
        "--add-phone",
        "+49 151 00000001",
    )
    assert result.exit_code == 0, result.output
    profile = _query(tmp_path, lambda s: s.profile_with_channels("alice"))
    assert (profile["display_name"], profile["email"], profile["timezone"]) == (
        "Alice A.",
        "alice@example.com",
        "Europe/Kyiv",
    )
    assert profile["phones"] == [{"phone": "+4915100000001", "confirmed": True}]
    assert _query(tmp_path, lambda s: s.user_for_confirmed_phone("+4915100000001")) == "alice"

    assert "already belongs to 'bob'" in _run("set", "alice", "--email", "BOB@example.com").output
    assert "already belongs to 'alice'" in _run("set", "bob", "--add-phone", "+4915100000001").output
    assert _run("set", "alice", "--add-phone", "12").exit_code == 1
    assert "Nothing to set" in _run("set", "alice").output
    assert _run("set", "alice", "--email", "alice@example.com").exit_code == 0  # its own email

    assert _run("set", "alice", "--remove-phone", "+4915100000001").exit_code == 0
    assert _query(tmp_path, lambda s: s.user_for_confirmed_phone("+4915100000001")) is None


def test_deactivate_blocks_sign_in_and_activate_restores_it(tmp_path: Path) -> None:
    import asyncio

    from pincer.services.auth import InvalidCredentialsError

    _run("create", "tamias", PASSWORD, "--channel", "telegram:1")
    assert asyncio.run(_login("tamias", PASSWORD)).pincer_user_id == "tamias"

    off = _run("deactivate", "tamias")
    assert off.exit_code == 0, off.output
    assert "Deactivated 'tamias'" in off.output
    assert "already inactive" in _run("deactivate", "tamias").output
    assert "inactive" in _run("list").output
    with pytest.raises(InvalidCredentialsError):
        asyncio.run(_login("tamias", PASSWORD))
    assert _query(tmp_path, lambda s: s.user_for_channel("telegram", "1")) is None

    on = _run("activate", "tamias")
    assert on.exit_code == 0, on.output
    assert "already active" in _run("activate", "tamias").output
    assert asyncio.run(_login("tamias", PASSWORD)).pincer_user_id == "tamias"
    assert _run("deactivate", "ghost").exit_code == 1


def test_a_change_to_a_toml_listed_identity_is_warned_about(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pincer.toml").write_text(
        '[identity.alice]\n\n[[identity.alice.channels]]\nchannel = "telegram"\nchannel_user_id = "1"\n'
    )
    monkeypatch.setenv("PINCER_IDENTITY_MANAGEMENT", "sync")
    get_settings_relaxed.cache_clear()
    created = _run("create", "alice", "--no-password", "--channel", "telegram:1")
    assert created.exit_code == 0, created.output
    assert "the next start resets what the TOML states" in created.output

    monkeypatch.setenv("PINCER_IDENTITY_MANAGEMENT", "clionly")
    get_settings_relaxed.cache_clear()
    quiet = _run("set", "alice", "--display-name", "Alice")
    assert quiet.exit_code == 0, quiet.output
    assert "next start" not in quiet.output
