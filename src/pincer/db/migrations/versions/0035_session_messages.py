"""Store each session message as a row, and session times as millisecond datetimes (issue #406).

`pincer_sessions.messages` held a conversation's whole history as one JSON list, rewritten on every
save. Each message is now a row of `pincer_session_messages`, ordered by (`created_at`, `id`):

- `role` and `content` are always there; the fields of some roles only are NULL elsewhere. `raw` and
  `tool_calls` are `json` on Postgres (their text, key order included, comes back as written: they are
  sent to the LLM again), `images` is `jsonb`.
- `channel_message_id` and `updated_at` are new, for a user message edited in its messenger; NULL here.

`pincer_sessions.created_at` / `updated_at` turn from float epoch seconds into `TIMESTAMP(3) WITH TIME
ZONE` on Postgres and `YYYY-MM-DDTHH:MM:SS.mmm+00:00` text on SQLite (`db.types.UtcDateTime`), cut to
the millisecond. The floats are 8-byte, so the milliseconds they held are kept.

Messages had no time of their own. A session's N messages are spread evenly over its life: message i
gets `created_at + i * (updated_at - created_at) / (N - 1)`, cut to the millisecond, so the first is
dated when the session was created and the last when it was last saved. Each id is minted at its
message's millisecond by a per-session `Uuid7Sequence`, strictly increasing, so (`created_at`, `id`) is
the list's order even where messages share a millisecond.

The legacy `messages` column is left as it was, and no longer written; a later revision drops it. The
downgrade writes it again from the rows, then drops the table and turns the times back into floats.

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-21
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from alembic import op

from pincer.db.ids import Uuid7Sequence
from pincer.db.migration_helpers import drop_stale_batch_table, hold_transaction, in_range
from pincer.db.types import JsonbDocument, JsonDocument, UtcDateTime, Uuid7, from_epoch_ms, utc_text

if TYPE_CHECKING:
    from collections.abc import Iterator

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

SESSIONS = "pincer_sessions"
MESSAGES = "pincer_session_messages"
ROLES = ("system", "user", "assistant", "tool_result")
TIMES = ("created_at", "updated_at")
#: Rows per INSERT of the backfill.
BATCH = 1000
#: The latest millisecond a `datetime` can hold (9999-12-31T23:59:59.999).
_MAX_MS = 253_402_300_799_999

_messages = sa.table(
    MESSAGES,
    sa.column("id", Uuid7()),
    sa.column("session_id", Uuid7()),
    sa.column("role", sa.Text()),
    sa.column("content", sa.Text()),
    sa.column("tool_call_id", sa.Text()),
    sa.column("is_error", sa.Boolean()),
    sa.column("tool_calls", JsonDocument),
    sa.column("images", JsonbDocument),
    sa.column("context", sa.Text()),
    sa.column("raw", JsonDocument),
    sa.column("wire", sa.Text()),
    sa.column("created_at", UtcDateTime()),
)


def upgrade() -> None:
    bind = op.get_bind()
    hold_transaction(bind)
    # Read before the times change type: as floats, on both dialects.
    sessions = bind.execute(sa.text(f"SELECT id, messages, created_at, updated_at FROM {SESSIONS}")).all()  # noqa: S608
    _times_to_datetimes(bind)
    _create_messages_table()
    rows: list[dict[str, Any]] = []
    for session_ref, stored, created, updated in sessions:
        rows.extend(_rows_of(str(session_ref), stored, _ms(created), _ms(updated)))
        if len(rows) >= BATCH:
            bind.execute(_messages.insert(), rows)
            rows = []
    if rows:
        bind.execute(_messages.insert(), rows)


def downgrade() -> None:
    bind = op.get_bind()
    hold_transaction(bind)
    histories: dict[str, list[dict[str, Any]]] = {
        str(session_ref): []
        for (session_ref,) in bind.execute(sa.text(f"SELECT id FROM {SESSIONS}")).all()  # noqa: S608
    }
    ordered = sa.select(_messages).order_by(_messages.c.session_id, _messages.c.created_at, _messages.c.id)
    for row in bind.execute(ordered).mappings():
        if (history := histories.get(str(row["session_id"]))) is not None:
            history.append(_as_dict(row))
    update = sa.text(f"UPDATE {SESSIONS} SET messages = :messages WHERE id = :id")  # noqa: S608
    params = [{"id": ref, "messages": json.dumps(history)} for ref, history in histories.items() if history]
    if params:
        bind.execute(update.bindparams(sa.bindparam("id", type_=Uuid7())), params)
    op.drop_table(MESSAGES)
    _times_to_floats(bind)


# ── the messages table ───────────────────────────────────────────────


def _create_messages_table() -> None:
    op.create_table(
        MESSAGES,
        sa.Column("id", Uuid7(), primary_key=True),
        sa.Column(
            "session_id",
            Uuid7(),
            sa.ForeignKey(f"{SESSIONS}.id", ondelete="CASCADE", name="fk_session_messages_session_id"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("tool_call_id", sa.Text()),
        sa.Column("is_error", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tool_calls", JsonDocument),
        sa.Column("images", JsonbDocument),
        sa.Column("context", sa.Text()),
        sa.Column("raw", JsonDocument),
        sa.Column("wire", sa.Text()),
        sa.Column("channel_message_id", sa.Text()),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime()),
        sa.CheckConstraint(f"role IN ({', '.join(repr(r) for r in ROLES)})", name="ck_session_messages_role"),
        sa.CheckConstraint("role = 'tool_result' OR tool_call_id IS NULL", name="ck_session_messages_tool_call_id"),
        sa.CheckConstraint("role = 'assistant' OR tool_calls IS NULL", name="ck_session_messages_tool_calls"),
        sa.CheckConstraint(
            "role IN ('assistant', 'system') OR (raw IS NULL AND wire IS NULL)", name="ck_session_messages_raw"
        ),
        sa.CheckConstraint(
            "role = 'user' OR (channel_message_id IS NULL AND updated_at IS NULL)", name="ck_session_messages_user_edit"
        ),
    )
    op.create_index("idx_session_messages_session_created", MESSAGES, ["session_id", "created_at", "id"])
    op.create_index(
        "uq_session_messages_channel_message",
        MESSAGES,
        ["session_id", "channel_message_id"],
        unique=True,
        postgresql_where=sa.text("channel_message_id IS NOT NULL"),
        sqlite_where=sa.text("channel_message_id IS NOT NULL"),
    )


def _rows_of(session_ref: str, stored: str | None, created: int, updated: int) -> Iterator[dict[str, Any]]:
    """The rows of one session's stored list, dated evenly from `created` to `updated` (epoch ms)."""
    try:
        history = json.loads(stored) if stored else []
    except ValueError:
        history = None
    if not isinstance(history, list):
        logger.warning(
            "Session %s: its stored messages are not a JSON list; none are moved: %.80r", session_ref, stored
        )
        return
    messages = [m for m in history if _is_message(session_ref, m)]
    last = len(messages) - 1
    span = max(updated - created, 0)
    ids = Uuid7Sequence()
    for index, message in enumerate(messages):
        ms = created + (index * span // last if last else 0)
        yield _as_row(session_ref, message, ms, ids.next(in_range(ms)))


def _is_message(session_ref: str, message: Any) -> bool:
    if isinstance(message, dict) and message.get("role") in ROLES:
        return True
    logger.warning("Session %s: dropping a stored message with no known role: %.80r", session_ref, message)
    return False


def _as_row(session_ref: str, d: dict[str, Any], ms: int, row_id: str) -> dict[str, Any]:
    """A stored message (`LLMMessage.to_dict`'s format) as a row. A field its role cannot have, which no
    code writes, is dropped with a warning rather than failing the upgrade on a CHECK."""
    role = d["role"]
    allowed = {
        "tool_call_id": role == "tool_result",
        "tool_calls": role == "assistant",
        "raw": role in ("assistant", "system"),
        "wire": role in ("assistant", "system"),
    }
    for name, ok in allowed.items():
        if d.get(name) and not ok:
            logger.warning("Session %s: dropping %s from a %s message", session_ref, name, role)
    return {
        "id": row_id,
        "session_id": session_ref,
        "role": role,
        "content": _text(d.get("content")) or "",
        "tool_call_id": _text(d.get("tool_call_id")) if allowed["tool_call_id"] else None,
        "is_error": bool(d.get("is_error", False)),
        "tool_calls": (d.get("tool_calls") or None) if allowed["tool_calls"] else None,
        "images": d.get("images") or None,
        "context": _text(d.get("context")),
        "raw": (d.get("raw") or None) if allowed["raw"] else None,
        "wire": _text(d.get("wire")) if allowed["wire"] else None,
        "created_at": from_epoch_ms(min(max(ms, 0), _MAX_MS)),
    }


def _text(value: Any) -> str | None:
    """A text field as a TEXT column takes it: NULL when empty, and no NUL, which Postgres refuses
    (the JSON list escaped it as `\\u0000`)."""
    if not value:
        return None
    return str(value).replace("\x00", "�")


def _as_dict(row: Any) -> dict[str, Any]:
    """A row as `LLMMessage.to_dict` writes a message: only the fields that are set, in its order."""
    d: dict[str, Any] = {"role": row["role"], "content": row["content"]}
    if row["tool_call_id"]:
        d["tool_call_id"] = row["tool_call_id"]
    if row["is_error"]:
        d["is_error"] = True
    for name in ("tool_calls", "images", "context", "raw", "wire"):
        if row[name]:
            d[name] = row[name]
    return d


# ── session times ────────────────────────────────────────────────────


def _ms(value: Any) -> int:
    """Float epoch seconds as epoch milliseconds, cut (rounded to the microsecond first, as
    `db.types.from_epoch_seconds` does)."""
    try:
        return round(float(value) * 1_000_000) // 1000
    except (TypeError, ValueError, OverflowError):
        logger.warning("A session time that is not a number (%r) is taken as the epoch", value)
        return 0


def _times_to_datetimes(bind: sa.Connection) -> None:
    if bind.dialect.name == "postgresql":
        for column in TIMES:
            op.execute(
                f"ALTER TABLE {SESSIONS} ALTER COLUMN {column} TYPE TIMESTAMP(3) WITH TIME ZONE "
                f"USING to_timestamp(floor(round({column} * 1000000) / 1000) / 1000)"
            )
        return
    stamps = bind.execute(sa.text(f"SELECT id, created_at, updated_at FROM {SESSIONS}")).all()  # noqa: S608
    _retype_sqlite(sa.Text())
    update = sa.text(f"UPDATE {SESSIONS} SET created_at = :created_at, updated_at = :updated_at WHERE id = :id")  # noqa: S608
    params = [
        {
            "id": row_id,
            "created_at": utc_text(from_epoch_ms(min(max(_ms(created), 0), _MAX_MS))),
            "updated_at": utc_text(from_epoch_ms(min(max(_ms(updated), 0), _MAX_MS))),
        }
        for row_id, created, updated in stamps
    ]
    if params:
        bind.execute(update, params)


def _times_to_floats(bind: sa.Connection) -> None:
    if bind.dialect.name == "postgresql":
        for column in TIMES:
            op.execute(
                f"ALTER TABLE {SESSIONS} ALTER COLUMN {column} TYPE DOUBLE PRECISION USING EXTRACT(EPOCH FROM {column})"
            )
        return
    stamps = bind.execute(sa.text(f"SELECT id, created_at, updated_at FROM {SESSIONS}")).all()  # noqa: S608
    _retype_sqlite(sa.REAL())
    update = sa.text(f"UPDATE {SESSIONS} SET created_at = :created_at, updated_at = :updated_at WHERE id = :id")  # noqa: S608
    params = [
        {"id": row_id, "created_at": _seconds(created), "updated_at": _seconds(updated)}
        for row_id, created, updated in stamps
    ]
    if params:
        bind.execute(update, params)


def _seconds(value: Any) -> float:
    parsed = datetime.fromisoformat(str(value))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()


def _retype_sqlite(type_: sa.types.TypeEngine[Any]) -> None:
    """Rebuild the SQLite sessions table with its times declared `type_` (the values are rewritten after)."""
    drop_stale_batch_table(SESSIONS)
    with op.batch_alter_table(SESSIONS, recreate="always") as batch:
        for column in TIMES:
            batch.alter_column(column, type_=type_, existing_nullable=False)
