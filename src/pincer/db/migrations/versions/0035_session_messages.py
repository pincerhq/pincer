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

Messages had no time of their own. A session's N units are spread evenly over its life: unit i gets
`created_at + i * (updated_at - created_at) / (N - 1)`, cut to the millisecond, so the first is dated
when the session was created and the last when it was last saved. A unit is one message, or a tool
group (an assistant message with tool calls and its results), which an identity merge joining two
histories by time must not split. Each id is minted at its
message's millisecond by a per-session `Uuid7Sequence`, strictly increasing, so (`created_at`, `id`) is
the list's order even where messages share a millisecond.

The legacy `messages` column is left as it was, and no longer written; a later revision drops it. The
downgrade writes it again from the rows (except where it never held a JSON list: nothing moved, so it
keeps that text), then drops the table and turns the times back into floats.

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
from sqlalchemy.dialects import postgresql

from pincer.db.ids import Uuid7Sequence
from pincer.db.migration_helpers import drop_stale_batch_table, hold_transaction, in_range
from pincer.db.types import JsonbDocument, JsonDocument, Real, UtcDateTime, Uuid7, from_epoch_ms, utc_text

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
#: The fields only some roles have, and those roles: NULL on every other role. A frozen copy of
#: `models.sessions.ROLE_FIELDS` as this revision created the table (a test holds the two equal).
ROLE_FIELDS: dict[str, tuple[str, ...]] = {
    "tool_call_id": ("tool_result",),
    "tool_calls": ("assistant",),
    "raw": ("assistant", "system"),
    "wire": ("assistant", "system"),
    "context": ("user",),
    "channel_message_id": ("user",),
    "updated_at": ("user",),
}
#: What a save writes of `ROLE_FIELDS` (the last two belong to an edit in a messenger).
_SAVED_ROLE_FIELDS = ("tool_call_id", "tool_calls", "raw", "wire", "context")
TIMES = ("created_at", "updated_at")
#: Rows per INSERT of the backfill.
BATCH = 1000
#: Sessions whose histories are read at once, either way: a long one's list (provider output included)
#: runs to megabytes, so the whole table is never held in memory.
SESSIONS_PER_READ = 100
#: `UtcDateTime`'s DDL, spelled out: `op.create_table` renders the type decorator as a plain
#: `TIMESTAMP WITH TIME ZONE` (precision 6), not the `TIMESTAMP(3)` it declares.
_MILLISECONDS = sa.Text().with_variant(postgresql.TIMESTAMP(timezone=True, precision=3), "postgresql")
#: The latest millisecond a `datetime` can hold (9999-12-31T23:59:59.999).
_MAX_MS = 253_402_300_799_999

_sessions = sa.table(SESSIONS, sa.column("id", Uuid7()), sa.column("messages", sa.Text()))
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
    # Read before the times change type: as floats, on both dialects. The histories are read later, a few
    # sessions at a time
    times = {
        str(session_ref): (_ms(created), _ms(updated))
        for session_ref, created, updated in bind.execute(
            sa.text(f"SELECT id, created_at, updated_at FROM {SESSIONS}")  # noqa: S608
        ).all()
    }
    _times_to_datetimes(bind)
    _create_messages_table()
    rows: list[dict[str, Any]] = []
    for chunk in _chunks(list(times)):
        for session_ref, stored in _legacy_values(bind, chunk).items():
            rows.extend(_rows_of(session_ref, stored, *times[session_ref]))
            if len(rows) >= BATCH:
                bind.execute(_messages.insert(), rows)
                rows = []
    if rows:
        bind.execute(_messages.insert(), rows)


def downgrade() -> None:
    bind = op.get_bind()
    hold_transaction(bind)
    update = sa.update(_sessions).where(_sessions.c.id == sa.bindparam("ref")).values(messages=sa.bindparam("text"))
    refs = [str(ref) for (ref,) in bind.execute(sa.text(f"SELECT id FROM {SESSIONS}")).all()]  # noqa: S608
    for chunk in _chunks(refs):
        histories: dict[str, list[dict[str, Any]]] = {ref: [] for ref in chunk}
        ordered = (
            sa.select(_messages)
            .where(_messages.c.session_id.in_(chunk))
            .order_by(_messages.c.session_id, _messages.c.created_at, _messages.c.id)
        )
        for row in bind.execute(ordered).mappings():
            histories[str(row["session_id"])].append(_as_dict(row))
        # Sessions left with no rows too: the legacy column was not written since the upgrade, so a session
        # cleared since then would otherwise get back the history it had before it. Except one whose legacy
        # value the upgrade could not read (not a JSON list): it moved nothing, so that value is the only
        # copy. Only these sessions' legacy values are read
        empty = [ref for ref, history in histories.items() if not history]
        readable = {ref for ref, stored in _legacy_values(bind, empty).items() if _legacy_list(stored) is not None}
        params = [
            {"ref": ref, "text": json.dumps(history)}
            for ref, history in histories.items()
            if history or ref in readable
        ]
        if params:
            bind.execute(update, params)
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
        sa.Column("created_at", _MILLISECONDS, nullable=False),
        sa.Column("updated_at", _MILLISECONDS),
        *_role_checks(),
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


def _role_checks() -> list[sa.CheckConstraint]:
    def quoted(values: tuple[str, ...]) -> str:
        return ", ".join(f"'{value}'" for value in values)

    return [
        sa.CheckConstraint(f"role IN ({quoted(ROLES)})", name="ck_session_messages_role"),
        *(
            sa.CheckConstraint(f"role IN ({quoted(roles)}) OR {name} IS NULL", name=f"ck_session_messages_{name}")
            for name, roles in ROLE_FIELDS.items()
        ),
    ]


def _chunks(refs: list[str]) -> Iterator[list[str]]:
    for start in range(0, len(refs), SESSIONS_PER_READ):
        yield refs[start : start + SESSIONS_PER_READ]


def _legacy_values(bind: sa.Connection, refs: list[str]) -> dict[str, str | None]:
    """The legacy `messages` value of each of these sessions."""
    if not refs:
        return {}
    stmt = sa.select(_sessions.c.id, _sessions.c.messages).where(_sessions.c.id.in_(refs))
    return {str(ref): stored for ref, stored in bind.execute(stmt).all()}


def _rows_of(session_ref: str, stored: str | None, created: int, updated: int) -> Iterator[dict[str, Any]]:
    """The rows of one session's stored list, dated evenly from `created` to `updated` (epoch ms).

    A tool group (an assistant message with tool calls and the results after it) shares one millisecond,
    the ids keeping its order: these times are made up, and a group spread over the session's life would
    have another conversation's messages between its call and its results once an identity merge joins
    the two by time (`SessionMessageRepository.move`), and the agent would drop the pair."""
    history = _legacy_list(stored)
    if history is None:
        logger.warning(
            "Session %s: its stored messages are not a JSON list; none are moved: %.80r", session_ref, stored
        )
        return
    messages = [m for m in history if _is_message(session_ref, m)]
    units = _units(messages)
    last = units[-1] if units else 0
    span = max(updated - created, 0)
    ids = Uuid7Sequence()
    for message, unit in zip(messages, units, strict=True):
        ms = created + (unit * span // last if last else 0)
        yield _as_row(session_ref, message, ms, ids.next(in_range(ms)))


def _units(messages: list[dict[str, Any]]) -> list[int]:
    """Each message's place among the history's units: a tool group is one, any other message one."""
    units: list[int] = []
    unit = -1
    in_group = False
    for message in messages:
        role = message["role"]
        if not (in_group and role == "tool_result"):
            unit += 1
            in_group = role == "assistant" and bool(message.get("tool_calls"))
        units.append(unit)
    return units


def _legacy_list(stored: str | None) -> list[Any] | None:
    """A session's legacy `messages` value as the list it holds, None when it is not a JSON list."""
    try:
        history = json.loads(stored) if stored else []
    except ValueError:
        return None
    return history if isinstance(history, list) else None


def _is_message(session_ref: str, message: Any) -> bool:
    if isinstance(message, dict) and message.get("role") in ROLES:
        return True
    logger.warning("Session %s: dropping a stored message with no known role: %.80r", session_ref, message)
    return False


def _as_row(session_ref: str, message: dict[str, Any], ms: int, row_id: str) -> dict[str, Any]:
    """A stored message (`LLMMessage.to_dict`'s format) as a row. A field its role cannot have, which no
    code writes, is dropped with a warning rather than failing the upgrade on a CHECK."""
    role = message["role"]
    role_fields: dict[str, Any] = {}
    for name in _SAVED_ROLE_FIELDS:
        if not message.get(name):
            role_fields[name] = None
        elif role not in ROLE_FIELDS[name]:
            logger.warning("Session %s: dropping %s from a %s message", session_ref, name, role)
            role_fields[name] = None
        else:
            role_fields[name] = (
                _calls(message[name])
                if name == "tool_calls"
                else _json(message[name])
                if name == "raw"
                else _text(message[name])
            )
    return {
        "id": row_id,
        "session_id": session_ref,
        "role": role,
        "content": _text(message.get("content")) or "",
        "is_error": bool(message.get("is_error", False)),
        "images": _json(message.get("images")) or None,
        **role_fields,
        "created_at": _at(ms),
    }


def _at(ms: int) -> datetime:
    """Epoch milliseconds as a datetime, held to what a `datetime` can carry."""
    return from_epoch_ms(min(max(ms, 0), _MAX_MS))


def _text(value: Any) -> str | None:
    """A text field as a TEXT column takes it, NULL when empty: no NUL (stripped; Postgres text takes
    none) and no lone surrogate (`?`; UTF-8 cannot encode one). The JSON list escaped both. A frozen copy
    of `services.sessions.storable_text`."""
    if not value:
        return None
    text = _utf8(str(value).replace("\x00", ""))
    return text or None


def _utf8(text: str) -> str:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")
    return text


def _json(value: Any) -> Any:
    """A JSON value with no lone surrogate in any string (`?`): Postgres refuses one in `json` and
    `jsonb`, and would fail the whole upgrade on it. A frozen copy of `services.sessions.storable_json`."""
    if isinstance(value, str):
        return _utf8(value)
    if isinstance(value, list):
        return [_json(item) for item in value]
    if isinstance(value, dict):
        return {_utf8(key): _json(item) for key, item in value.items()}
    return value


def _calls(value: Any) -> Any:
    """Tool calls as `_json` holds them, each call's id and name with no NUL either: a result's
    `tool_call_id` is TEXT, which strips it (`_text`), and the two must still match to pair."""
    calls = _json(value)
    if not isinstance(calls, list):
        return calls
    return [
        {
            key: (item.replace("\x00", "") if key in ("id", "name") and isinstance(item, str) else item)
            for key, item in call.items()
        }
        if isinstance(call, dict)
        else call
        for call in calls
    ]


def _as_dict(row: Any) -> dict[str, Any]:
    """A row as `LLMMessage.to_dict` writes a message: only the fields that are set, in its order."""
    message: dict[str, Any] = {"role": row["role"], "content": row["content"]}
    if row["tool_call_id"]:
        message["tool_call_id"] = row["tool_call_id"]
    if row["is_error"]:
        message["is_error"] = True
    for name in ("tool_calls", "images", "context", "raw", "wire"):
        if row[name]:
            message[name] = row[name]
    return message


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
            "created_at": utc_text(_at(_ms(created))),
            "updated_at": utc_text(_at(_ms(updated))),
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
    _retype_sqlite(Real())
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
