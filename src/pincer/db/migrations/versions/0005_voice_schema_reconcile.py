"""Reconcile the legacy voice schema with the active voice subsystem.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-12
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
from sqlalchemy import inspect, text

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_AUTOPK = {
    "sqlite": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "postgresql": "SERIAL PRIMARY KEY",
}
_NOW_COL = {
    "sqlite": "TEXT",
    "postgresql": "TIMESTAMP",
}

_VOICE_CALLS = """
CREATE TABLE voice_calls (
    id {AUTOPK},
    call_sid TEXT NOT NULL UNIQUE,
    direction TEXT NOT NULL DEFAULT 'inbound',
    from_number TEXT DEFAULT '',
    to_number TEXT DEFAULT '',
    pincer_user_id TEXT DEFAULT '',
    recording_enabled BOOLEAN DEFAULT FALSE,
    consent_given BOOLEAN DEFAULT FALSE,
    started_at {NOW_COL} NOT NULL,
    ended_at {NOW_COL},
    failure_code TEXT DEFAULT '',
    engine TEXT DEFAULT '',
    language TEXT DEFAULT '',
    report_delivered_at {NOW_COL}
)
"""

_CALL_TRANSCRIPTS = """
CREATE TABLE call_transcripts (
    id {AUTOPK},
    call_id TEXT NOT NULL,
    speaker TEXT NOT NULL,
    text TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    is_final BOOLEAN DEFAULT TRUE,
    state TEXT DEFAULT '',
    timestamp {NOW_COL} NOT NULL
)
"""

_CALL_ACTIONS = """
CREATE TABLE call_actions (
    id {AUTOPK},
    call_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    tool_name TEXT DEFAULT '',
    input_summary TEXT DEFAULT '',
    output_summary TEXT DEFAULT '',
    user_confirmed BOOLEAN,
    timestamp {NOW_COL} NOT NULL,
    tier TEXT DEFAULT '',
    approval_mode TEXT DEFAULT '',
    deny_reason TEXT DEFAULT ''
)
"""

_DO_NOT_CALL = """
CREATE TABLE IF NOT EXISTS do_not_call (
    phone_number TEXT PRIMARY KEY,
    reason TEXT DEFAULT '',
    source TEXT DEFAULT '',
    call_sid TEXT DEFAULT '',
    added_at {NOW_COL} NOT NULL
)
"""

_OUTBOUND_CALL_LOG = """
CREATE TABLE IF NOT EXISTS outbound_call_log (
    id {AUTOPK},
    phone_number TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    channel TEXT DEFAULT '',
    call_sid TEXT DEFAULT '',
    placed_at {NOW_COL} NOT NULL,
    local_day TEXT NOT NULL
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_call_transcripts_call ON call_transcripts(call_id)",
    "CREATE INDEX IF NOT EXISTS idx_call_transcripts_ts ON call_transcripts(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_call_actions_call ON call_actions(call_id)",
    "CREATE INDEX IF NOT EXISTS idx_call_actions_ts ON call_actions(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_voice_calls_started ON voice_calls(started_at)",
    "CREATE INDEX IF NOT EXISTS idx_voice_calls_failure ON voice_calls(failure_code)",
    "CREATE INDEX IF NOT EXISTS idx_outbound_log_day ON outbound_call_log(local_day)",
    "CREATE INDEX IF NOT EXISTS idx_outbound_log_number ON outbound_call_log(phone_number, placed_at)",
)


def _sql(template: str, dialect: str) -> str:
    return (
        template.replace("{AUTOPK}", _AUTOPK[dialect])
        .replace("{NOW_COL}", _NOW_COL[dialect])
    )


def _columns(bind: Connection, table: str) -> set[str]:
    return {str(col["name"]) for col in inspect(bind).get_columns(table)}


def _add_column_if_missing(
    bind: Connection,
    dialect: str,
    table: str,
    column: str,
    coldef: str,
) -> None:
    if column in _columns(bind, table):
        return

    if dialect == "postgresql":
        bind.execute(
            text(
                f"ALTER TABLE {table} "
                f"ADD COLUMN IF NOT EXISTS {column} {coldef}"
            )
        )
    else:
        bind.execute(
            text(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
        )


def _create_modern_voice_tables(dialect: str) -> None:
    op.execute(_sql(_VOICE_CALLS, dialect))
    op.execute(_sql(_CALL_TRANSCRIPTS, dialect))
    op.execute(_sql(_CALL_ACTIONS, dialect))


def _rebuild_legacy_voice_schema(
    bind: Connection,
    dialect: str,
) -> None:
    """Replace 0001's dormant voice schema while preserving legacy rows."""
    voice_cols = _columns(bind, "voice_calls")
    action_cols = _columns(bind, "call_actions")

    failure_expr = (
        "COALESCE(failure_code, '')"
        if "failure_code" in voice_cols
        else "''"
    )
    language_expr = (
        "COALESCE(language, '')"
        if "language" in voice_cols
        else "''"
    )
    report_expr = (
        "report_delivered_at"
        if "report_delivered_at" in voice_cols
        else "NULL"
    )

    tier_expr = (
        "COALESCE(tier, '')"
        if "tier" in action_cols
        else "''"
    )
    approval_expr = (
        "COALESCE(approval_mode, '')"
        if "approval_mode" in action_cols
        else "''"
    )
    deny_expr = (
        "COALESCE(deny_reason, '')"
        if "deny_reason" in action_cols
        else "''"
    )

    op.execute(
        "ALTER TABLE voice_calls "
        "RENAME TO voice_calls_legacy_0005"
    )
    op.execute(
        "ALTER TABLE call_transcripts "
        "RENAME TO call_transcripts_legacy_0005"
    )
    op.execute(
        "ALTER TABLE call_actions "
        "RENAME TO call_actions_legacy_0005"
    )

    _create_modern_voice_tables(dialect)

    op.execute(
        f"""
        INSERT INTO voice_calls (
            call_sid,
            direction,
            from_number,
            to_number,
            pincer_user_id,
            recording_enabled,
            consent_given,
            started_at,
            ended_at,
            failure_code,
            engine,
            language,
            report_delivered_at
        )
        SELECT
            id,
            direction,
            caller_number,
            COALESCE(target_number, ''),
            user_id,
            CASE
                WHEN recording_url IS NOT NULL
                     AND recording_url <> ''
                THEN TRUE
                ELSE FALSE
            END,
            COALESCE(recording_consent, FALSE),
            started_at,
            ended_at,
            {failure_expr},
            COALESCE(engine, ''),
            {language_expr},
            {report_expr}
        FROM voice_calls_legacy_0005
        """
    )

    op.execute(
        """
        INSERT INTO call_transcripts (
            call_id,
            speaker,
            text,
            confidence,
            is_final,
            state,
            timestamp
        )
        SELECT
            call_id,
            speaker,
            text,
            COALESCE(confidence, 1.0),
            COALESCE(is_final, TRUE),
            COALESCE(state, ''),
            timestamp
        FROM call_transcripts_legacy_0005
        """
    )

    op.execute(
        f"""
        INSERT INTO call_actions (
            call_id,
            action_type,
            tool_name,
            input_summary,
            output_summary,
            user_confirmed,
            timestamp,
            tier,
            approval_mode,
            deny_reason
        )
        SELECT
            call_id,
            action_type,
            COALESCE(tool_name, ''),
            COALESCE(input_summary, ''),
            COALESCE(output_summary, ''),
            user_confirmed,
            timestamp,
            {tier_expr},
            {approval_expr},
            {deny_expr}
        FROM call_actions_legacy_0005
        """
    )

    # Child tables first: their legacy FKs may point at the renamed parent.
    op.execute("DROP TABLE call_actions_legacy_0005")
    op.execute("DROP TABLE call_transcripts_legacy_0005")
    op.execute("DROP TABLE voice_calls_legacy_0005")


def _ensure_already_modern_schema(
    bind: Connection,
    dialect: str,
) -> None:
    """Bring pre-Alembic installs that already have call_sid up to 0005."""
    now_col = _NOW_COL[dialect]

    for column, coldef in (
        ("failure_code", "TEXT DEFAULT ''"),
        ("engine", "TEXT DEFAULT ''"),
        ("language", "TEXT DEFAULT ''"),
        ("report_delivered_at", now_col),
    ):
        _add_column_if_missing(
            bind,
            dialect,
            "voice_calls",
            column,
            coldef,
        )

    for column, coldef in (
        ("tier", "TEXT DEFAULT ''"),
        ("approval_mode", "TEXT DEFAULT ''"),
        ("deny_reason", "TEXT DEFAULT ''"),
    ):
        _add_column_if_missing(
            bind,
            dialect,
            "call_actions",
            column,
            coldef,
        )


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect not in _AUTOPK:
        raise RuntimeError(
            f"Unsupported database dialect for voice migrations: {dialect}"
        )

    if "call_sid" not in _columns(bind, "voice_calls"):
        _rebuild_legacy_voice_schema(bind, dialect)
    else:
        _ensure_already_modern_schema(bind, dialect)

    op.execute(_sql(_DO_NOT_CALL, dialect))
    op.execute(_sql(_OUTBOUND_CALL_LOG, dialect))

    for stmt in _INDEXES:
        op.execute(stmt)


def downgrade() -> None:
    # This revision reconciles an incompatible pre-Alembic schema.
    # Mirroring 0004's SQLite policy, a one-step downgrade does not rebuild
    # that broken legacy shape. `downgrade base` remains complete because
    # 0001 drops the voice tables after these two 0005-owned tables are gone.
    op.execute("DROP TABLE IF EXISTS outbound_call_log")
    op.execute("DROP TABLE IF EXISTS do_not_call")
