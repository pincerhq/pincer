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
    assert "not both" in both.output

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
            await identities.create_profile("bare")  # what `create` used to leave behind
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
