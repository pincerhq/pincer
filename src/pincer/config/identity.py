"""Cross-channel identity configuration — loaded from pincer.toml / pincer.local.toml.

pincer.local.toml's [identity] section overrides/extends pincer.toml's, per
person and per field (see `_merge_identity_raw`).

Schema — one person per `[identity.<key>]` table, `<key>` becomes the
`pincer_user_id`:

    [identity.johndoe]
    display_name      = "John Doe"
    email             = "john.doe@domain.tld"
    timezone          = "Europe/Berlin"
    preferred_channel = "telegram"
    phones            = ["+4915112345678"]

    [[identity.johndoe.channels]]
    channel         = "telegram"
    channel_user_id = 123456

A person may have just one `[[...channels]]` entry — useful for
pre-registering a roster incrementally as new channel IDs become known.

`phones` are the person's own numbers in E.164. They are stored as confirmed
phones of the identity: a voice call from or to one of them is that person's
call, with their tools and approval rules, not a guest's. A number may belong
to one person only.

How the section reaches the database is `PINCER_IDENTITY_MANAGEMENT`:

- `sync`: the TOML is the source of truth. New people are added, the fields
  and channels it states overwrite the database, and identities that came
  from the config but left it are removed (identities made with the CLI are
  kept).
- `update` (default): new people are added and listed people are updated;
  nobody is removed.
- `clionly`: the section is ignored; identities are managed with
  `pincer identity …` only.

`PINCER_IDENTITY_MAP`, the env-var string this section replaced, is no longer
read. `pincer run` refuses to start while it is set and prints the TOML it
corresponds to (`legacy_identity_map_toml`).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

from pincer.channels.base import ChannelType
from pincer.config.toml_loader import read_toml_raw
from pincer.core.identity import RESERVED_IDENTITY_NAMES
from pincer.core.phone import normalize_e164

logger = logging.getLogger(__name__)

IdentityManagement = Literal["sync", "update", "clionly"]
IDENTITY_MANAGEMENT_MODES: tuple[str, ...] = get_args(IdentityManagement)

#: The env var the [identity] section replaced; see `legacy_identity_map`.
LEGACY_IDENTITY_MAP_ENV = "PINCER_IDENTITY_MAP"

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
    phones: list[str] = field(default_factory=list)


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
    Raises ValueError on an invalid or reserved person key, an unknown channel, a
    (channel, channel_user_id) pair reused across two different people, or a
    phone that is not E.164 or is listed for two people —
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
    seen_phones: dict[str, str] = {}

    for person_key, person_raw in identity_raw.items():
        if not _KEY_PATTERN.match(person_key):
            raise ValueError(f"identity key must be alphanumeric/underscore: '{person_key}'")
        if person_key.lower() in RESERVED_IDENTITY_NAMES:
            raise ValueError(f"identity key '{person_key}' is reserved")

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

        phones_raw = person_raw.get("phones", [])
        if isinstance(phones_raw, str) or not isinstance(phones_raw, list):
            raise ValueError(f"identity.{person_key}: phones must be a list of E.164 numbers")
        phones: list[str] = []
        for phone_raw in phones_raw:
            phone = normalize_e164(str(phone_raw))
            if phone is None:
                raise ValueError(f"identity.{person_key}: phone '{phone_raw}' is not an E.164 number")
            if phone in seen_phones and seen_phones[phone] != person_key:
                raise ValueError(
                    f"identity.{person_key}: phone '{phone}' is already used by identity.{seen_phones[phone]}"
                )
            if phone not in phones:
                phones.append(phone)
            seen_phones[phone] = person_key

        entries[person_key] = IdentityTomlEntry(
            pincer_user_id=person_key,
            display_name=person_raw.get("display_name"),
            preferred_channel=preferred_channel,
            email=person_raw.get("email"),
            timezone=person_raw.get("timezone"),
            channels=channels,
            phones=phones,
        )

    return entries


def load_identity_map(config_dir: Path | None, mode: str) -> dict[str, IdentityTomlEntry]:
    """The [identity] entries the resolver applies under `mode`.

    In `clionly` that is none: the section is not merged at all, and a
    warning says so when it has entries. Raises ValueError like
    `load_identity_config` for an invalid section in the other modes.
    """
    if mode not in IDENTITY_MANAGEMENT_MODES:
        raise ValueError(f"PINCER_IDENTITY_MANAGEMENT must be one of {', '.join(IDENTITY_MANAGEMENT_MODES)}")
    if mode == "clionly":
        if toml_declares_identities(config_dir):
            logger.warning(
                "PINCER_IDENTITY_MANAGEMENT=clionly: the [identity] section of pincer.toml / pincer.local.toml "
                "is ignored; manage identities with `pincer identity …`"
            )
        return {}
    return load_identity_config(config_dir)


def toml_declares_identities(config_dir: Path | None) -> bool:
    """True when pincer.toml or pincer.local.toml has an [identity] entry, valid or not."""
    base_dir = config_dir or Path.cwd()
    return any(
        bool((read_toml_raw(base_dir / name) or {}).get("identity")) for name in ("pincer.toml", "pincer.local.toml")
    )


# ── the removed PINCER_IDENTITY_MAP ──────────────────────────────


def legacy_identity_map(config_dir: Path | None = None) -> str | None:
    """The value of the removed `PINCER_IDENTITY_MAP`, if it is still set in
    the environment or in a `.env` file Settings reads; None otherwise.

    Settings ignores unknown variables, so without this check a deployment
    that relied on the variable to gate its channels would silently open.
    """
    for key, value in os.environ.items():
        if key.upper() == LEGACY_IDENTITY_MAP_ENV and value.strip():
            return value
    from dotenv import dotenv_values

    base_dir = config_dir or Path.cwd()
    # The files and order `Settings.model_config.env_file` names.
    for env_file in (base_dir.parent / ".env", base_dir / ".env"):
        if not env_file.is_file():
            continue
        for key, env_value in dotenv_values(env_file).items():
            if key.upper() == LEGACY_IDENTITY_MAP_ENV and env_value and env_value.strip():
                return env_value
    return None


def _parse_legacy_entry(entry: str) -> tuple[str | None, list[tuple[str, str]]]:
    """One `name@ch1:id1=ch2:id2` (or unnamed `ch1:id1=ch2:id2`) entry of the
    old string, as (name, [(channel, id), ...]). Raises ValueError when it
    has no `channel:id` pair."""
    entry = entry.strip()
    name: str | None = None
    eq_pos = entry.find("=")
    at_pos = entry.find("@")
    if 0 < at_pos < (eq_pos if eq_pos >= 0 else len(entry)):
        name = entry[:at_pos].strip() or None
        entry = entry[at_pos + 1 :]
    pairs: list[tuple[str, str]] = []
    for part in entry.split("="):
        ch, colon, cid = part.strip().partition(":")
        if colon and ch.strip() and cid.strip():
            pairs.append((ch.strip(), cid.strip()))
    if not pairs:
        raise ValueError(f"entry has no channel:id pair: {entry!r}")
    return name, pairs


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def legacy_identity_map_toml(value: str) -> str:
    """The `[identity.<name>]` TOML that says what a `PINCER_IDENTITY_MAP`
    string said, ready to paste: it loads as written.

    Entries with the same name become one person with their channels merged,
    since TOML refuses a table declared twice. An unnamed entry (or one whose
    name is not a valid key) gets a placeholder (`person_1`, …) to rename,
    never one an entry already uses. An entry that does not parse is left as
    a comment.
    """
    parsed: list[tuple[str | None, list[tuple[str, str]]]] = []
    comments: list[str] = []
    for raw_entry in value.split(","):
        if not raw_entry.strip():
            continue
        try:
            name, pairs = _parse_legacy_entry(raw_entry)
        except ValueError:
            comments.append(f"# could not convert: {raw_entry.strip()}")
            continue
        parsed.append((name if name is not None and _KEY_PATTERN.match(name) else None, pairs))

    taken = {name for name, _pairs in parsed if name is not None}
    people: dict[str, list[tuple[str, str]]] = {}
    unnamed = 0
    for name, pairs in parsed:
        if name is None:
            unnamed += 1
            while f"person_{unnamed}" in taken:
                unnamed += 1
            name = f"person_{unnamed}"
            taken.add(name)
        channels = people.setdefault(name, [])
        channels.extend(pair for pair in pairs if pair not in channels)

    blocks: list[str] = []
    for name, channels in people.items():
        lines = [f"[identity.{name}]"]
        for channel, channel_user_id in channels:
            lines += [
                "",
                f"[[identity.{name}.channels]]",
                f"channel = {_toml_string(channel)}",
                f"channel_user_id = {_toml_string(channel_user_id)}",
            ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks + comments)


def legacy_identity_map_error(value: str) -> str:
    """What `pincer run` and `pincer doctor` say while the old variable is set."""
    return (
        f"{LEGACY_IDENTITY_MAP_ENV} is no longer read. Move it into the [identity] section of pincer.toml "
        f"(or pincer.local.toml) and remove it from the environment / .env. The equivalent TOML:\n\n"
        f"{legacy_identity_map_toml(value)}\n"
    )
