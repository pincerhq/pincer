"""Tests for the [identity] TOML config section (pincer.config.identity)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pincer.config.identity import (
    IdentityTomlEntry,
    _parse_legacy_entry,
    legacy_identity_map,
    legacy_identity_map_toml,
    load_identity_config,
    load_identity_map,
)

# ── load_identity_config: basic parsing ─────────────────────────────────────


def test_no_config_files_returns_empty(tmp_path: Path) -> None:
    assert load_identity_config(tmp_path) == {}


def test_single_person_single_channel(tmp_path: Path) -> None:
    (tmp_path / "pincer.toml").write_text("""
[identity.johndoe]
display_name      = "John Doe"
email             = "john.doe@domain.tld"
timezone          = "Europe/Berlin"
preferred_channel = "telegram"

[[identity.johndoe.channels]]
channel         = "telegram"
channel_user_id = 123456
""")
    entries = load_identity_config(tmp_path)
    assert set(entries) == {"johndoe"}
    entry = entries["johndoe"]
    assert entry == IdentityTomlEntry(
        pincer_user_id="johndoe",
        display_name="John Doe",
        preferred_channel="telegram",
        email="john.doe@domain.tld",
        timezone="Europe/Berlin",
        channels=[("telegram", "123456")],
    )


def test_channel_user_id_coerced_to_string(tmp_path: Path) -> None:
    """TOML allows a bare int or a quoted string for channel_user_id; both normalize to str."""
    (tmp_path / "pincer.toml").write_text("""
[identity.jane]
[[identity.jane.channels]]
channel         = "telegram"
channel_user_id = "janeAustin"

[[identity.jane.channels]]
channel         = "whatsapp"
channel_user_id = 491111111111
""")
    entries = load_identity_config(tmp_path)
    assert entries["jane"].channels == [("telegram", "janeAustin"), ("whatsapp", "491111111111")]


def test_multiple_people(tmp_path: Path) -> None:
    (tmp_path / "pincer.toml").write_text("""
[identity.johndoe]
[[identity.johndoe.channels]]
channel = "telegram"
channel_user_id = 1

[identity.jane]
[[identity.jane.channels]]
channel = "whatsapp"
channel_user_id = 2
""")
    entries = load_identity_config(tmp_path)
    assert set(entries) == {"johndoe", "jane"}


# ── validation ───────────────────────────────────────────────────────────────


def test_unknown_channel_raises(tmp_path: Path) -> None:
    (tmp_path / "pincer.toml").write_text("""
[identity.johndoe]
[[identity.johndoe.channels]]
channel = "carrier_pigeon"
channel_user_id = 1
""")
    with pytest.raises(ValueError, match="unknown channel"):
        load_identity_config(tmp_path)


def test_unknown_preferred_channel_raises(tmp_path: Path) -> None:
    (tmp_path / "pincer.toml").write_text("""
[identity.johndoe]
preferred_channel = "carrier_pigeon"
[[identity.johndoe.channels]]
channel = "telegram"
channel_user_id = 1
""")
    with pytest.raises(ValueError, match="unknown preferred_channel"):
        load_identity_config(tmp_path)


def test_invalid_person_key_raises(tmp_path: Path) -> None:
    """Bare TOML keys allow hyphens, but the key becomes the pincer_user_id and
    the `pincer identity` name — reject anything outside alphanumeric/underscore,
    mirroring MCPServerConfig's name validation."""
    (tmp_path / "pincer.toml").write_text("""
[identity.john-doe]
[[identity.john-doe.channels]]
channel = "telegram"
channel_user_id = 1
""")
    with pytest.raises(ValueError, match="alphanumeric"):
        load_identity_config(tmp_path)


def test_duplicate_channel_pair_across_people_raises(tmp_path: Path) -> None:
    (tmp_path / "pincer.toml").write_text("""
[identity.johndoe]
[[identity.johndoe.channels]]
channel = "telegram"
channel_user_id = 1

[identity.jane]
[[identity.jane.channels]]
channel = "telegram"
channel_user_id = 1
""")
    with pytest.raises(ValueError, match="already used by identity.johndoe"):
        load_identity_config(tmp_path)


def test_channelless_entry_raises(tmp_path: Path) -> None:
    """A person with zero [[channels]] entries must be rejected: it would turn
    the guest gate on and give cleanup() a whitelist with nothing of theirs in it."""
    (tmp_path / "pincer.toml").write_text("""
[identity.jane]
display_name = "Jane Austin"
""")
    with pytest.raises(ValueError, match="needs at least one"):
        load_identity_config(tmp_path)


def test_channel_entry_missing_key_raises_with_context(tmp_path: Path) -> None:
    (tmp_path / "pincer.toml").write_text("""
[identity.johndoe]
[[identity.johndoe.channels]]
channel = "telegram"
""")
    with pytest.raises(ValueError, match="identity.johndoe: channel entry missing 'channel_user_id'"):
        load_identity_config(tmp_path)


def test_empty_channel_user_id_raises(tmp_path: Path) -> None:
    """An empty string channel_user_id must be rejected: str("") is still "",
    so it would otherwise pass the "needs at least one channel" guard while
    stringifying to 'jane@telegram:' — a segment that survives IdentityResolver's
    skip-guards but yields zero usable pairs, hitting the same empty-whitelist
    wipe as test_channelless_entry_raises."""
    (tmp_path / "pincer.toml").write_text("""
[identity.jane]
[[identity.jane.channels]]
channel         = "telegram"
channel_user_id = ""
""")
    with pytest.raises(ValueError, match="identity.jane: channel 'telegram' has an empty channel_user_id"):
        load_identity_config(tmp_path)


def test_whitespace_only_channel_user_id_raises(tmp_path: Path) -> None:
    """Whitespace-only channel_user_id is stripped to empty and rejected the same way."""
    (tmp_path / "pincer.toml").write_text("""
[identity.jane]
[[identity.jane.channels]]
channel         = "telegram"
channel_user_id = "   "
""")
    with pytest.raises(ValueError, match="identity.jane: channel 'telegram' has an empty channel_user_id"):
        load_identity_config(tmp_path)


def test_single_channel_entry_is_allowed(tmp_path: Path) -> None:
    """A TOML person may have just one channel."""
    (tmp_path / "pincer.toml").write_text("""
[identity.johndoe]
[[identity.johndoe.channels]]
channel = "telegram"
channel_user_id = 1
""")
    entries = load_identity_config(tmp_path)
    assert entries["johndoe"].channels == [("telegram", "1")]


# ── pincer.toml + pincer.local.toml per-field merge ─────────────────────────


def test_local_toml_partial_override_keeps_base_channels(tmp_path: Path) -> None:
    (tmp_path / "pincer.toml").write_text("""
[identity.johndoe]
display_name = "John"
[[identity.johndoe.channels]]
channel = "telegram"
channel_user_id = 1
""")
    (tmp_path / "pincer.local.toml").write_text("""
[identity.johndoe]
display_name = "Johnny"
""")
    entries = load_identity_config(tmp_path)
    entry = entries["johndoe"]
    assert entry.display_name == "Johnny"
    assert entry.channels == [("telegram", "1")]


def test_local_toml_replaces_channels_wholesale(tmp_path: Path) -> None:
    (tmp_path / "pincer.toml").write_text("""
[identity.johndoe]
[[identity.johndoe.channels]]
channel = "telegram"
channel_user_id = 1
""")
    (tmp_path / "pincer.local.toml").write_text("""
[identity.johndoe]
[[identity.johndoe.channels]]
channel = "whatsapp"
channel_user_id = 2
""")
    entries = load_identity_config(tmp_path)
    # Local's channels list fully replaces base's — not merged per-channel.
    assert entries["johndoe"].channels == [("whatsapp", "2")]


def test_local_toml_adds_new_person(tmp_path: Path) -> None:
    (tmp_path / "pincer.toml").write_text("""
[identity.johndoe]
[[identity.johndoe.channels]]
channel = "telegram"
channel_user_id = 1
""")
    (tmp_path / "pincer.local.toml").write_text("""
[identity.jane]
[[identity.jane.channels]]
channel = "whatsapp"
channel_user_id = 2
""")
    entries = load_identity_config(tmp_path)
    assert set(entries) == {"johndoe", "jane"}


def test_local_toml_only_no_base(tmp_path: Path) -> None:
    (tmp_path / "pincer.local.toml").write_text("""
[identity.johndoe]
[[identity.johndoe.channels]]
channel = "telegram"
channel_user_id = 1
""")
    entries = load_identity_config(tmp_path)
    assert set(entries) == {"johndoe"}


# ── load_identity_map: the mode decides what the resolver gets ────────────

_TWO_PEOPLE = """
[identity.johndoe]
display_name = "John Doe"
preferred_channel = "whatsapp"
email = "john.doe@domain.tld"
timezone = "Europe/Berlin"
[[identity.johndoe.channels]]
channel = "telegram"
channel_user_id = 1

[[identity.johndoe.channels]]
channel = "whatsapp"
channel_user_id = 2

[identity.jane]
[[identity.jane.channels]]
channel = "telegram"
channel_user_id = 3
"""


@pytest.mark.parametrize("mode", ["sync", "update"])
def test_sync_and_update_load_the_toml_entries(tmp_path: Path, mode: str) -> None:
    (tmp_path / "pincer.toml").write_text(_TWO_PEOPLE)
    entries = load_identity_map(tmp_path, mode)
    assert list(entries) == ["johndoe", "jane"]
    assert entries["johndoe"] == IdentityTomlEntry(
        pincer_user_id="johndoe",
        display_name="John Doe",
        preferred_channel="whatsapp",
        email="john.doe@domain.tld",
        timezone="Europe/Berlin",
        channels=[("telegram", "1"), ("whatsapp", "2")],
    )
    # No profile fields: they stay unset, which seeding treats as "keep what is stored".
    assert entries["jane"] == IdentityTomlEntry(pincer_user_id="jane", channels=[("telegram", "3")])


def test_clionly_ignores_the_toml_and_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "pincer.toml").write_text(_TWO_PEOPLE)
    with caplog.at_level("WARNING"):
        assert load_identity_map(tmp_path, "clionly") == {}
    assert "is ignored" in caplog.text


def test_clionly_does_not_even_validate_the_toml(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "pincer.toml").write_text("[identity.me]\n")
    assert load_identity_map(tmp_path, "clionly") == {}
    with pytest.raises(ValueError, match="reserved"):
        load_identity_map(tmp_path, "update")


def test_an_unknown_mode_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sync, update, clionly"):
        load_identity_map(tmp_path, "merge")


def test_no_config_returns_empty(tmp_path: Path) -> None:
    assert load_identity_map(tmp_path, "update") == {}


# ── the removed PINCER_IDENTITY_MAP ──────────────────────────────────────


def test_legacy_env_var_is_found_in_the_environment_and_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pincer.config.main import Settings

    monkeypatch.delenv("PINCER_IDENTITY_MAP", raising=False)
    # The .env files Settings reads (the test suite turns them off by default).
    monkeypatch.setitem(Settings.model_config, "env_file", ("../.env", ".env"))
    config_dir = tmp_path / "app"
    config_dir.mkdir()
    assert legacy_identity_map(config_dir) is None

    (config_dir / ".env").write_text("PINCER_IDENTITY_MAP=\n")
    assert legacy_identity_map(config_dir) is None  # empty is ignored, as Settings does

    (tmp_path / ".env").write_text("pincer_identity_map=john@telegram:1\n")  # ../.env, any case
    assert legacy_identity_map(config_dir) == "john@telegram:1"

    monkeypatch.setenv("PINCER_IDENTITY_MAP", "jane@telegram:2")
    assert legacy_identity_map(config_dir) == "jane@telegram:2"


def test_legacy_string_becomes_toml_that_loads_back(tmp_path: Path) -> None:
    value = "john@telegram:12345=whatsapp:+491234567890, telegram:999=signal:+4911,broken"
    toml = legacy_identity_map_toml(value)
    assert "# could not convert: broken" in toml
    (tmp_path / "pincer.toml").write_text(toml)
    entries = load_identity_config(tmp_path)
    assert entries["john"].channels == [("telegram", "12345"), ("whatsapp", "+491234567890")]
    assert entries["person_1"].channels == [("telegram", "999"), ("signal", "+4911")]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("john@telegram:1,john@whatsapp:2", {"john": [("telegram", "1"), ("whatsapp", "2")]}),
        ("john@telegram:1=whatsapp:2,john@whatsapp:2", {"john": [("telegram", "1"), ("whatsapp", "2")]}),
        ("person_1@telegram:1,telegram:2", {"person_1": [("telegram", "1")], "person_2": [("telegram", "2")]}),
        ("telegram:2,person_1@telegram:1", {"person_2": [("telegram", "2")], "person_1": [("telegram", "1")]}),
    ],
)
def test_legacy_toml_loads_when_names_repeat_or_collide(tmp_path: Path, value: str, expected: dict) -> None:
    """One table per name: TOML refuses `[identity.john]` declared twice."""
    (tmp_path / "pincer.toml").write_text(legacy_identity_map_toml(value))
    entries = load_identity_config(tmp_path)
    assert {name: entry.channels for name, entry in entries.items()} == expected


def test_parse_legacy_entry() -> None:
    assert _parse_legacy_entry("john@telegram:johnDoe=whatsapp:491234567890=signal:4912") == (
        "john",
        [("telegram", "johnDoe"), ("whatsapp", "491234567890"), ("signal", "4912")],
    )
    assert _parse_legacy_entry("telegram:1=email:me@example.com") == (
        None,
        [("telegram", "1"), ("email", "me@example.com")],
    )
    with pytest.raises(ValueError):
        _parse_legacy_entry("justaname@")


def test_run_refuses_to_start_while_the_legacy_env_var_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import typer

    from pincer.cli.run import _refuse_legacy_identity_map

    monkeypatch.delenv("PINCER_IDENTITY_MAP", raising=False)
    (tmp_path / "app").mkdir()
    monkeypatch.chdir(tmp_path / "app")
    _refuse_legacy_identity_map()  # not set: nothing happens

    monkeypatch.setenv("PINCER_IDENTITY_MAP", "john@telegram:1")
    with pytest.raises(typer.Exit):
        _refuse_legacy_identity_map()
    out = capsys.readouterr().out
    assert "PINCER_IDENTITY_MAP is no longer read" in out
    assert "[identity.john]" in out
    assert "[red]" not in out  # styled, not printed as raw markup
    assert out.startswith("Configuration error:")


def test_reserved_person_key_raises(tmp_path: Path) -> None:
    """`/api/identity/me` is the caller's own identity, so nobody may be keyed `me`."""
    (tmp_path / "pincer.toml").write_text("""
[identity.me]
[[identity.me.channels]]
channel = "telegram"
channel_user_id = 1
""")
    with pytest.raises(ValueError, match="reserved"):
        load_identity_config(tmp_path)
