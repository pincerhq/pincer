"""One-time import of legacy identity_map rows into identity_meta/channel_identities.

Ports the former `core/identity.py:IdentityResolver._migrate_legacy()`
method. Only ever applicable to very old SQLite installs that still have an
`identity_map` table — superseded by `identity_meta`/`channel_identities`
well before this migration was written. Postgres installs never have this
table, so this revision is a no-op there.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-22
"""

from __future__ import annotations

import logging

from alembic import op
from sqlalchemy import text

logger = logging.getLogger(__name__)

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# (channel name in channel_identities, source column in identity_map)
_CHANNEL_COLUMNS = (
    ("telegram", "telegram_user_id"),
    ("whatsapp", "whatsapp_phone"),
    ("discord", "discord_user_id"),
    ("voice", "phone_number"),
    ("signal", "signal_phone"),
    ("slack", "slack_user_id"),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return

    exists = bind.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='identity_map'")).fetchone()
    if not exists:
        return

    # identity_map's shape drifted across old installs (columns added by
    # 004_sprint4.sql's ALTER TABLE were never guaranteed to have run) — mirror
    # the original method's defensive column detection instead of assuming
    # every column is present.
    columns = {row[1] for row in bind.execute(text("PRAGMA table_info(identity_map)")).fetchall()}

    def _sel(col: str, default: str = "NULL") -> str:
        return col if col in columns else default

    default_preferred = "'telegram'"
    rows = bind.execute(
        text(
            "SELECT pincer_user_id, "
            f"{_sel('telegram_user_id')} AS telegram_user_id, "
            f"{_sel('whatsapp_phone')} AS whatsapp_phone, "
            f"{_sel('discord_user_id')} AS discord_user_id, "
            f"{_sel('phone_number')} AS phone_number, "
            f"{_sel('signal_phone')} AS signal_phone, "
            f"{_sel('slack_user_id')} AS slack_user_id, "
            f"{_sel('display_name')} AS display_name, "
            f"{_sel('preferred_channel', default_preferred)} AS preferred_channel "
            "FROM identity_map"
        )
    ).fetchall()

    migrated = 0
    for row in rows:
        bind.execute(
            text(
                "INSERT OR IGNORE INTO identity_meta (pincer_user_id, preferred_channel, display_name) "
                "VALUES (:uid, :preferred, :display_name)"
            ),
            {"uid": row.pincer_user_id, "preferred": row.preferred_channel, "display_name": row.display_name},
        )
        for channel_name, attr in _CHANNEL_COLUMNS:
            value = getattr(row, attr)
            if value:
                bind.execute(
                    text(
                        "INSERT OR IGNORE INTO channel_identities (channel, channel_user_id, pincer_user_id) "
                        "VALUES (:ch, :cid, :uid)"
                    ),
                    {"ch": channel_name, "cid": str(value), "uid": row.pincer_user_id},
                )
        migrated += 1

    if migrated:
        bind.execute(text("DROP TABLE identity_map"))
        logger.info("Imported %d legacy identity_map rows; dropped identity_map", migrated)


def downgrade() -> None:
    # One-time import from a legacy table that no longer exists after
    # upgrade(); not meaningfully reversible.
    pass
