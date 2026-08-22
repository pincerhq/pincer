"""Tests for the [identity] TOML config section (pincer.config.identity)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pincer.config.identity import (
    IdentityTomlEntry,
    load_identity_config,
    resolve_identity_map_config,
)
from pincer.core.identity import IdentityProfile, IdentityResolver

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
    """Bare TOML keys allow hyphens, but the resulting string must round-trip
    through PINCER_IDENTITY_MAP's 'name@ch:id' grammar, which uses '-' fine but
    reserves '@'/'='/',' — reject anything outside alphanumeric/underscore,
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


def test_single_channel_entry_is_allowed(tmp_path: Path) -> None:
    """Unlike PINCER_IDENTITY_MAP's typical usage, a TOML person may have just one channel."""
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


# ── resolve_identity_map_config: env-var precedence + stringification ──────


def test_env_var_short_circuits_toml(tmp_path: Path) -> None:
    (tmp_path / "pincer.toml").write_text("""
[identity.johndoe]
[[identity.johndoe.channels]]
channel = "telegram"
channel_user_id = 1
""")
    config, profiles = resolve_identity_map_config("jane@telegram:2=whatsapp:3", tmp_path)
    assert config == "jane@telegram:2=whatsapp:3"
    assert profiles == {}


def test_no_env_var_stringifies_toml_entries(tmp_path: Path) -> None:
    (tmp_path / "pincer.toml").write_text("""
[identity.johndoe]
[[identity.johndoe.channels]]
channel = "telegram"
channel_user_id = 123456

[[identity.johndoe.channels]]
channel = "whatsapp"
channel_user_id = "491234567890"
""")
    config, _ = resolve_identity_map_config("", tmp_path)
    assert config == "johndoe@telegram:123456=whatsapp:491234567890"


def test_stringified_config_round_trips_through_parse_mapping(tmp_path: Path) -> None:
    """The stringified TOML output must be understood by IdentityResolver._parse_mapping unchanged."""
    (tmp_path / "pincer.toml").write_text("""
[identity.johndoe]
[[identity.johndoe.channels]]
channel = "telegram"
channel_user_id = 123456

[identity.jane]
[[identity.jane.channels]]
channel = "telegram"
channel_user_id = "janeAustin"

[[identity.jane.channels]]
channel = "whatsapp"
channel_user_id = "491111111111"
""")
    config, _ = resolve_identity_map_config("", tmp_path)
    entries = [IdentityResolver._parse_mapping(raw) for raw in config.split(",")]
    assert entries == [
        ("johndoe", [("telegram", "123456")]),
        ("jane", [("telegram", "janeAustin"), ("whatsapp", "491111111111")]),
    ]


def test_profiles_carry_display_name_preferred_channel_email_timezone(tmp_path: Path) -> None:
    (tmp_path / "pincer.toml").write_text("""
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
""")
    _, profiles = resolve_identity_map_config("", tmp_path)
    assert profiles["johndoe"] == IdentityProfile(
        display_name="John Doe",
        preferred_channel="whatsapp",
        email="john.doe@domain.tld",
        timezone="Europe/Berlin",
    )
    # A person with no profile fields set still gets an (all-None) entry —
    # resolve_identity_map_config doesn't filter people out, it's up to the
    # caller (IdentityResolver.seed_from_config) to treat that as a no-op.
    assert profiles["jane"] == IdentityProfile()


def test_no_config_no_env_returns_empty(tmp_path: Path) -> None:
    config, profiles = resolve_identity_map_config("", tmp_path)
    assert config == ""
    assert profiles == {}
