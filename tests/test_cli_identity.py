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


def test_create_then_set_password_then_sign_in(tmp_path: Path) -> None:
    created = _run("create", "tamias", "--email", "tamias@example.com", "--display-name", "Tamias")
    assert created.exit_code == 0, created.output
    assert "Created identity 'tamias'" in created.output

    assert _run("set-password", "tamias", "correct horse battery").exit_code == 0

    import asyncio

    assert asyncio.run(_login("tamias", "correct horse battery")).pincer_user_id == "tamias"
    assert asyncio.run(_login("tamias@example.com", "correct horse battery")).pincer_user_id == "tamias"


def test_set_password_prompts_hidden_and_confirms_when_omitted() -> None:
    _run("create", "tamias")

    mismatch = _run("set-password", "tamias", input="first password\nsecond password\n")
    assert mismatch.exit_code != 0

    result = _run("set-password", "tamias", input="correct horse battery\ncorrect horse battery\n")
    assert result.exit_code == 0, result.output
    assert "correct horse battery" not in result.output  # hidden input is not echoed

    import asyncio

    assert asyncio.run(_login("tamias", "correct horse battery")).pincer_user_id == "tamias"


def test_set_password_refuses_an_unknown_identity_and_a_weak_password() -> None:
    unknown = _run("set-password", "ghost", "correct horse battery")
    assert unknown.exit_code == 1
    assert "No identity named 'ghost'" in unknown.output

    _run("create", "tamias")
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
    assert _run("create", "tamias").exit_code == 0
    duplicate = _run("create", "tamias")
    assert duplicate.exit_code == 1
    assert "already exists" in duplicate.output

    bad_email = _run("create", "other", "--email", "not-an-email")
    assert bad_email.exit_code == 1


def test_api_key_is_printed_once_and_replaced_only_with_force() -> None:
    _run("create", "widget")

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

    assert _run("api-key", "ghost").exit_code == 1


def test_list_shows_who_can_sign_in_without_showing_secrets(tmp_path: Path) -> None:
    empty = _run("list")
    assert empty.exit_code == 0
    assert "No identities" in empty.output

    _run("create", "tamias", "--email", "tamias@example.com")
    _run("create", "widget")
    _run("set-password", "tamias", "correct horse battery")
    key_output = _run("api-key", "widget").output
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

    _run("create", "tamias")

    async def channels():
        try:
            return await IdentityService(get_database_url(tmp_path / "pincer.db")).channels_for("tamias")
        finally:
            await dispose_engines()

    assert asyncio.run(channels()) == []
