"""Cross-channel identity configuration — loaded from pincer.toml / pincer.local.toml.

An alternative to `PINCER_IDENTITY_MAP` (see `pincer.core.identity` module
docstring) for rosters too large to comfortably manage as one env-var string.

Priority (highest to lowest):
1. PINCER_IDENTITY_MAP, if non-empty — TOML is not even read in that case.
2. pincer.local.toml's [identity] section (optional — overrides/extends pincer.toml)
3. pincer.toml's [identity] section

This is a deliberate divergence from `pincer.mcp.config.load_mcp_config`'s
field-level env > local.toml > toml merge: here the env var, when set, fully
replaces the TOML section rather than merging with it.

Schema — one person per `[identity.<key>]` table, `<key>` becomes the
`pincer_user_id`:

    [identity.johndoe]
    display_name      = "John Doe"
    email             = "john.doe@domain.tld"
    timezone          = "Europe/Berlin"
    preferred_channel = "telegram"

    [[identity.johndoe.channels]]
    channel         = "telegram"
    channel_user_id = 123456

Unlike `PINCER_IDENTITY_MAP`, a person may have just one `[[...channels]]`
entry — useful for pre-registering a roster incrementally as new channel IDs
become known.

Entries are converted into `PINCER_IDENTITY_MAP`'s own string grammar
(`name@ch1:id1=ch2:id2...`) so `IdentityResolver._parse_mapping` and its
callers need no changes. `email`/`timezone` have no slot in that grammar, so
they're returned separately as a `pincer_user_id -> (email, timezone)` dict —
see `resolve_identity_map_config`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pincer.channels.base import ChannelType
from pincer.config.toml_loader import read_toml_raw
from pincer.core.identity import IdentityProfile

_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")


@dataclass(frozen=True)
class IdentityTomlEntry:
    """One `[identity.<key>]` entry parsed from pincer.toml / pincer.local.toml."""

    pincer_user_id: str
    display_name: str | None = None
    preferred_channel: str | None = None
    email: str | None = None
    timezone: str | None = None
    channels: list[tuple[str, str]] = field(default_factory=list)


def _merge_identity_raw(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge two raw [identity] section dicts, keyed by person (TOML table key).

    An override person with the same key as a base person is merged field by
    field: override wins per key, fields absent from the override are
    inherited from the base entry. `channels` is one such field — supplying
    it in the override replaces the base person's whole channel list rather
    than merging per-channel. New person-keys not present in base are
    appended, in order.
    """
    merged: dict[str, Any] = dict(base)
    for person_key, override_person in override.items():
        base_person = merged.get(person_key, {})
        merged[person_key] = {**base_person, **override_person}
    return merged


def load_identity_config(config_dir: Path | None = None) -> dict[str, IdentityTomlEntry]:
    """Load and merge the [identity] section from pincer.toml + pincer.local.toml.

    Returns entries keyed by pincer_user_id, in TOML definition order.
    Raises ValueError on an invalid person key, an unknown channel, or a
    (channel, channel_user_id) pair reused across two different people —
    config mistakes should fail loudly at startup, not be silently absorbed.
    """
    base_dir = config_dir or Path.cwd()
    base_raw = read_toml_raw(base_dir / "pincer.toml")
    local_raw = read_toml_raw(base_dir / "pincer.local.toml")

    identity_raw: dict[str, Any] = dict((base_raw or {}).get("identity", {}))
    if local_raw:
        local_identity = local_raw.get("identity")
        if local_identity:
            identity_raw = _merge_identity_raw(identity_raw, local_identity)

    entries: dict[str, IdentityTomlEntry] = {}
    seen_pairs: dict[tuple[str, str], str] = {}

    for person_key, person_raw in identity_raw.items():
        if not _KEY_PATTERN.match(person_key):
            raise ValueError(f"identity key must be alphanumeric/underscore: '{person_key}'")

        preferred_channel = person_raw.get("preferred_channel")
        if preferred_channel is not None:
            try:
                ChannelType(preferred_channel)
            except ValueError as e:
                raise ValueError(f"identity.{person_key}: unknown preferred_channel '{preferred_channel}'") from e

        channels: list[tuple[str, str]] = []
        for ch_raw in person_raw.get("channels", []):
            try:
                channel = ch_raw["channel"]
                channel_user_id_raw = ch_raw["channel_user_id"]
            except KeyError as e:
                raise ValueError(f"identity.{person_key}: channel entry missing {e.args[0]!r}") from e

            try:
                ChannelType(channel)
            except ValueError as e:
                raise ValueError(f"identity.{person_key}: unknown channel '{channel}'") from e

            channel_user_id = str(channel_user_id_raw).strip()
            if not channel_user_id:
                raise ValueError(f"identity.{person_key}: channel '{channel}' has an empty channel_user_id")

            pair = (channel, channel_user_id)
            if pair in seen_pairs:
                raise ValueError(
                    f"identity.{person_key}: channel:id '{channel}:{channel_user_id}' "
                    f"is already used by identity.{seen_pairs[pair]}"
                )
            seen_pairs[pair] = person_key
            channels.append(pair)

        if not channels:
            raise ValueError(f"identity.{person_key}: needs at least one [[identity.{person_key}.channels]] entry")

        entries[person_key] = IdentityTomlEntry(
            pincer_user_id=person_key,
            display_name=person_raw.get("display_name"),
            preferred_channel=preferred_channel,
            email=person_raw.get("email"),
            timezone=person_raw.get("timezone"),
            channels=channels,
        )

    return entries


def _stringify_entry(entry: IdentityTomlEntry) -> str:
    pairs = "=".join(f"{ch}:{cid}" for ch, cid in entry.channels)
    return f"{entry.pincer_user_id}@{pairs}"


def resolve_identity_map_config(
    env_value: str,
    config_dir: Path | None = None,
) -> tuple[str, dict[str, IdentityProfile]]:
    """Resolve the effective identity-map config for `IdentityResolver`.

    Returns (identity_map_config, profiles):
    - identity_map_config: a string in PINCER_IDENTITY_MAP's own grammar,
      ready to pass straight to `IdentityResolver`.
    - profiles: pincer_user_id -> IdentityProfile, carrying the fields that
      grammar has no room for (display_name, preferred_channel, email,
      timezone). Always empty when PINCER_IDENTITY_MAP is used, since that
      grammar has no field for any of these.

    PINCER_IDENTITY_MAP, when non-empty, is returned as-is — the TOML
    [identity] section is not even read in that case.
    """
    if env_value:
        return env_value, {}

    entries = load_identity_config(config_dir)
    identity_map_config = ",".join(_stringify_entry(entry) for entry in entries.values())
    profiles = {
        entry.pincer_user_id: IdentityProfile(
            display_name=entry.display_name,
            preferred_channel=entry.preferred_channel,
            email=entry.email,
            timezone=entry.timezone,
        )
        for entry in entries.values()
    }
    return identity_map_config, profiles
