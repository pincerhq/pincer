"""Retire session messages instead of deleting them; web chat conversations get an owner (issue #243).

- `pincer_session_messages.retired_at`: a message that leaves the model's history (compaction, the
  emergency cut, a clear, a dropped tool pair) keeps its row, dated here. The model is sent the live
  rows only; the history people read keeps them all.
- `pincer_sessions.conversation_id`: the web chat conversation a `web:<user>:<conversation>` session
  is, filled here from the existing keys. The session created first that names an id owns it; another
  user's session naming the same id (allowed before this) keeps working as model context, unlisted, and
  that user is refused the id from now on. Indexed with the creation time, not unique: one person's two
  sessions naming one id are joined by an identity rename, which must not fail first.
- `idx_sessions_user_channel_updated`: a user's conversations, latest activity first.

The downgrade loses nothing. Code before this revision cannot tell a retired row from a live one, and
would send the whole history to the model again, so the retired rows are *parked*: moved to
`pincer_session_messages_retired` (no foreign key, so a session deleted meanwhile does not take them
with it), and the photos they show copied to `pincer_session_images_retired` (older code deletes the
photos no live message shows). `retired_at` stays (empty once its rows are parked; older code never
names it). `conversation_id` and the two indexes go: they are derived from the session keys, so nothing
is lost, and an older revision's table rebuild would trip over an index on a column it removes.

The upgrade, whenever it runs again (forward, rollback, forward), brings parked rows and photos back
retired, for the sessions that still exist, without duplicating any. Rows whose session is gone stay
parked, logged, and their table is kept: nothing is deleted. Whatever older code itself deleted while
it ran is gone: that was its behaviour.

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-24
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pincer.db.migration_helpers import hold_transaction
from pincer.db.types import JsonbDocument, JsonDocument, Real, UtcDateTime, Uuid7

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

SESSIONS = "pincer_sessions"
MESSAGES = "pincer_session_messages"
IMAGES = "pincer_session_images"
PARKED_MESSAGES = "pincer_session_messages_retired"
PARKED_IMAGES = "pincer_session_images_retired"
CONVERSATION_INDEX = "idx_sessions_conversation_id"
LIST_INDEX = "idx_sessions_user_channel_updated"
#: Rows per UPDATE of the backfill.
BATCH = 1000
#: `UtcDateTime`'s DDL, spelled out as in 0035: `op.add_column` renders the type decorator with
#: precision 6, not the `TIMESTAMP(3)` it declares.
_MILLISECONDS = sa.Text().with_variant(postgresql.TIMESTAMP(timezone=True, precision=3), "postgresql")

#: Every column of a message row, as 0035 created it plus `retired_at`: what a parked row holds.
MESSAGE_COLUMNS = (
    "id",
    "session_id",
    "role",
    "content",
    "tool_call_id",
    "is_error",
    "tool_calls",
    "images",
    "context",
    "raw",
    "wire",
    "channel_message_id",
    "created_at",
    "updated_at",
    "retired_at",
)
IMAGE_COLUMNS = ("id", "session_ref", "sha256", "media_type", "data", "created_at")


def upgrade() -> None:
    bind = op.get_bind()
    hold_transaction(bind)
    inspector = sa.inspect(bind)
    # Each piece only if missing: a downgrade keeps them, so an upgrade after one finds them in place
    if "retired_at" not in _columns(inspector, MESSAGES):
        op.add_column(MESSAGES, sa.Column("retired_at", _MILLISECONDS))
    if "conversation_id" not in _columns(inspector, SESSIONS):
        op.add_column(SESSIONS, sa.Column("conversation_id", Uuid7()))
    indexes = {index["name"] for index in inspector.get_indexes(SESSIONS)}
    if CONVERSATION_INDEX not in indexes:
        op.create_index(
            CONVERSATION_INDEX,
            SESSIONS,
            ["conversation_id", "created_at", "id"],
            postgresql_where=sa.text("conversation_id IS NOT NULL"),
            sqlite_where=sa.text("conversation_id IS NOT NULL"),
        )
    if LIST_INDEX not in indexes:
        op.create_index(LIST_INDEX, SESSIONS, ["user_id", "channel", "updated_at", "id"])
    tables = set(inspector.get_table_names())
    if PARKED_MESSAGES in tables:
        _restore_messages(bind)
    if PARKED_IMAGES in tables:
        _restore_images(bind)
    _fill_conversation_ids(bind)


def downgrade() -> None:
    bind = op.get_bind()
    hold_transaction(bind)
    tables = set(sa.inspect(bind).get_table_names())
    if PARKED_MESSAGES not in tables:
        _create_parked_messages()
    if PARKED_IMAGES not in tables:
        _create_parked_images()
    _park_images(bind)
    columns = ", ".join(MESSAGE_COLUMNS)
    parked = bind.execute(
        sa.text(
            f"INSERT INTO {PARKED_MESSAGES} ({columns}) SELECT {columns} FROM {MESSAGES} m "  # noqa: S608
            f"WHERE m.retired_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {PARKED_MESSAGES} p WHERE p.id = m.id)"
        )
    ).rowcount
    bind.execute(sa.text(f"DELETE FROM {MESSAGES} WHERE retired_at IS NOT NULL"))  # noqa: S608
    if parked:
        logger.info("Parked %d retired message(s) in %s until the next upgrade", parked, PARKED_MESSAGES)
    op.drop_index(LIST_INDEX, table_name=SESSIONS)
    op.drop_index(CONVERSATION_INDEX, table_name=SESSIONS)
    with op.batch_alter_table(SESSIONS) as batch:
        batch.drop_column("conversation_id")


# ── parking ──────────────────────────────────────────────────────────


def _create_parked_messages() -> None:
    """The message table's columns, without its foreign key, CHECKs or unique index: a parked row only
    waits to go back, whatever happens to its session meanwhile."""
    op.create_table(
        PARKED_MESSAGES,
        sa.Column("id", Uuid7(), primary_key=True),
        sa.Column("session_id", Uuid7(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_call_id", sa.Text()),
        sa.Column("is_error", sa.Boolean(), nullable=False),
        sa.Column("tool_calls", JsonDocument),
        sa.Column("images", JsonbDocument),
        sa.Column("context", sa.Text()),
        sa.Column("raw", JsonDocument),
        sa.Column("wire", sa.Text()),
        sa.Column("channel_message_id", sa.Text()),
        sa.Column("created_at", _MILLISECONDS, nullable=False),
        sa.Column("updated_at", _MILLISECONDS),
        sa.Column("retired_at", _MILLISECONDS),
    )


def _create_parked_images() -> None:
    op.create_table(
        PARKED_IMAGES,
        sa.Column("id", Uuid7(), primary_key=True),
        sa.Column("session_ref", Uuid7(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", Real(), nullable=False),
        sa.UniqueConstraint("session_ref", "sha256", name="uq_session_images_retired_session_ref_sha256"),
    )


def _park_images(bind: sa.Connection) -> None:
    """Copy the photos the retired rows show to the parking table. Copied, not moved: a live message may
    show the same photo, and older code keeps that one."""
    refs = bind.execute(
        sa.text(f"SELECT session_id, images FROM {MESSAGES} WHERE retired_at IS NOT NULL AND images IS NOT NULL")  # noqa: S608
    ).all()
    wanted = sorted(
        {
            (str(session_ref), ref["sha256"])
            for session_ref, images in refs
            for ref in _image_refs(images)
            if isinstance(ref, dict) and ref.get("sha256")
        }
    )
    if not wanted:
        return
    columns = ", ".join(IMAGE_COLUMNS)
    copy = sa.text(
        f"INSERT INTO {PARKED_IMAGES} ({columns}) SELECT {columns} FROM {IMAGES} i "  # noqa: S608
        "WHERE i.session_ref = :ref AND i.sha256 = :sha AND NOT EXISTS "
        f"(SELECT 1 FROM {PARKED_IMAGES} p WHERE p.session_ref = i.session_ref AND p.sha256 = i.sha256)"
    ).bindparams(sa.bindparam("ref", type_=Uuid7()))
    bind.execute(copy, [{"ref": ref, "sha": sha} for ref, sha in wanted])


def _restore_messages(bind: sa.Connection) -> None:
    """Bring the parked rows of the sessions that still exist back, retired (dated now if they somehow
    have no date). A row already back is skipped; a messenger id a live row of the session took meanwhile
    stays with that row."""
    now = datetime.now(UTC)
    target = ", ".join(MESSAGE_COLUMNS)
    selected = ", ".join(
        {
            "retired_at": "COALESCE(p.retired_at, :now)",
            "channel_message_id": (
                f"CASE WHEN EXISTS (SELECT 1 FROM {MESSAGES} m WHERE m.session_id = p.session_id "
                "AND m.channel_message_id = p.channel_message_id) THEN NULL ELSE p.channel_message_id END"
            ),
        }.get(name, f"p.{name}")
        for name in MESSAGE_COLUMNS
    )
    restore = sa.text(
        f"INSERT INTO {MESSAGES} ({target}) SELECT {selected} FROM {PARKED_MESSAGES} p "  # noqa: S608
        f"WHERE EXISTS (SELECT 1 FROM {SESSIONS} s WHERE s.id = p.session_id) "
        f"AND NOT EXISTS (SELECT 1 FROM {MESSAGES} m WHERE m.id = p.id)"
    ).bindparams(sa.bindparam("now", now, type_=UtcDateTime()))
    restored = bind.execute(restore).rowcount
    bind.execute(
        sa.text(f"DELETE FROM {PARKED_MESSAGES} WHERE id IN (SELECT id FROM {MESSAGES})")  # noqa: S608
    )
    if restored:
        logger.info("Restored %d parked message(s) as retired", restored)
    _drop_if_empty(bind, PARKED_MESSAGES, "message(s) whose session no longer exists")


def _restore_images(bind: sa.Connection) -> None:
    """Bring the parked photos of the sessions that still exist back where the session lacks them."""
    columns = ", ".join(IMAGE_COLUMNS)
    selected = ", ".join(f"p.{name}" for name in IMAGE_COLUMNS)
    bind.execute(
        sa.text(
            f"INSERT INTO {IMAGES} ({columns}) SELECT {selected} FROM {PARKED_IMAGES} p "  # noqa: S608
            f"WHERE EXISTS (SELECT 1 FROM {SESSIONS} s WHERE s.id = p.session_ref) "
            f"AND NOT EXISTS (SELECT 1 FROM {IMAGES} i WHERE i.session_ref = p.session_ref AND i.sha256 = p.sha256) "
            f"AND NOT EXISTS (SELECT 1 FROM {IMAGES} i WHERE i.id = p.id)"
        )
    )
    # Back, or the session holds that photo already (its live messages kept it)
    bind.execute(
        sa.text(
            f"DELETE FROM {PARKED_IMAGES} WHERE EXISTS (SELECT 1 FROM {IMAGES} i "  # noqa: S608
            f"WHERE i.session_ref = {PARKED_IMAGES}.session_ref AND i.sha256 = {PARKED_IMAGES}.sha256)"
        )
    )
    _drop_if_empty(bind, PARKED_IMAGES, "photo(s) whose session no longer exists")


def _drop_if_empty(bind: sa.Connection, table: str, what: str) -> None:
    left = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()  # noqa: S608
    if left:
        logger.warning("Keeping %d parked %s in %s; nothing was deleted", left, what, table)
        return
    op.drop_table(table)


# ── conversation ids ─────────────────────────────────────────────────


def _fill_conversation_ids(bind: sa.Connection) -> None:
    """Give each `web:<user>:<conversation>` session without one its conversation id."""
    candidates = bind.execute(
        sa.text(
            f"SELECT id, session_id FROM {SESSIONS} "  # noqa: S608
            "WHERE conversation_id IS NULL AND session_id LIKE 'web:%' ORDER BY created_at, id"
        )
    ).all()
    params: list[dict[str, str]] = []
    seen: set[str] = set()
    shared = 0
    for ref, key in candidates:
        conversation = _conversation_of(str(key))
        if conversation is None:
            continue
        shared += conversation in seen
        seen.add(conversation)
        params.append({"ref": str(ref), "conversation": conversation})
    update = sa.text(f"UPDATE {SESSIONS} SET conversation_id = :conversation WHERE id = :ref").bindparams(  # noqa: S608
        sa.bindparam("conversation", type_=Uuid7()), sa.bindparam("ref", type_=Uuid7())
    )
    for start in range(0, len(params), BATCH):
        bind.execute(update, params[start : start + BATCH])
    if shared:
        logger.warning(
            "%d web session(s) name a conversation id an earlier session owns; they keep working, unlisted",
            shared,
        )


def _conversation_of(key: str) -> str | None:
    """The conversation id of a `web:<user>:<conversation>` key: a canonical lowercase UUID after a user."""
    head, _, last = key.rpartition(":")
    if not head.startswith("web:") or len(head) <= len("web:"):
        return None
    try:
        return last if str(uuid.UUID(last)) == last else None
    except ValueError:
        return None


def _columns(inspector: sa.Inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def _image_refs(images: Any) -> list[Any]:
    """A row's `images` as a list of refs: JSON text on SQLite, already a list on Postgres."""
    if isinstance(images, str):
        try:
            images = json.loads(images)
        except ValueError:
            return []
    return images if isinstance(images, list) else []
