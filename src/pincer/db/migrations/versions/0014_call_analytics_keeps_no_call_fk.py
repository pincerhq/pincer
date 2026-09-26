"""Drop `call_analytics`'s foreign key to `voice_calls`.

The retention policy deletes an expired call and deliberately keeps its
analytics row: talk ratio, silence and sentiment are derived numbers about a
call, not a recording of it (`pincer.services.retention`). The foreign key
0009 declared says the opposite, so the two cannot both hold.

SQLite never enforced it — `PRAGMA foreign_keys` is off — so on SQLite this
only removes a claim that was already untrue. On Postgres it is what lets the
purge run at all: there, deleting a call whose analytics row survives fails
with a foreign-key violation.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from pincer.db.migration_helpers import drop_stale_table

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

#: 0009's shape, minus the REFERENCES clause. SQLite cannot drop a constraint
#: in place, so the table is rebuilt.
_CALL_ANALYTICS_NO_FK = """
CREATE TABLE call_analytics_no_fk (
    call_sid TEXT PRIMARY KEY,
    agent_speech_ms INTEGER,
    caller_speech_ms INTEGER,
    silence_ms INTEGER,
    overlap_ms INTEGER,
    interruptions INTEGER DEFAULT 0,
    talk_ratio REAL,
    method TEXT NOT NULL,
    sentiment TEXT,
    sentiment_trajectory TEXT,
    sentiment_rationale TEXT,
    sentiment_reason TEXT DEFAULT '',
    created_at TEXT NOT NULL
)
"""

_COLUMNS = (
    "call_sid, agent_speech_ms, caller_speech_ms, silence_ms, overlap_ms, interruptions, talk_ratio, "
    "method, sentiment, sentiment_trajectory, sentiment_rationale, sentiment_reason, created_at"
)


def upgrade() -> None:
    bind = op.get_bind()
    if "call_analytics" not in set(sa.inspect(bind).get_table_names()):
        return
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE call_analytics DROP CONSTRAINT IF EXISTS call_analytics_call_sid_fkey")
        return
    drop_stale_table("call_analytics_no_fk")
    op.execute(_CALL_ANALYTICS_NO_FK)
    op.execute(f"INSERT INTO call_analytics_no_fk ({_COLUMNS}) SELECT {_COLUMNS} FROM call_analytics")
    op.execute("DROP TABLE call_analytics")
    op.execute("ALTER TABLE call_analytics_no_fk RENAME TO call_analytics")
    op.execute("CREATE INDEX IF NOT EXISTS idx_call_analytics_sentiment ON call_analytics(sentiment)")


def downgrade() -> None:
    """Put the foreign key back. Rows whose call is already gone would block
    it, so they are dropped — which is the state the constraint describes."""
    bind = op.get_bind()
    if "call_analytics" not in set(sa.inspect(bind).get_table_names()):
        return
    op.execute("DELETE FROM call_analytics WHERE call_sid NOT IN (SELECT call_sid FROM voice_calls)")
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE call_analytics ADD CONSTRAINT call_analytics_call_sid_fkey "
            "FOREIGN KEY (call_sid) REFERENCES voice_calls(call_sid)"
        )
        return
    drop_stale_table("call_analytics_no_fk")
    op.execute(
        _CALL_ANALYTICS_NO_FK.replace(
            "call_sid TEXT PRIMARY KEY", "call_sid TEXT PRIMARY KEY REFERENCES voice_calls(call_sid)"
        )
    )
    op.execute(f"INSERT INTO call_analytics_no_fk ({_COLUMNS}) SELECT {_COLUMNS} FROM call_analytics")
    op.execute("DROP TABLE call_analytics")
    op.execute("ALTER TABLE call_analytics_no_fk RENAME TO call_analytics")
    op.execute("CREATE INDEX IF NOT EXISTS idx_call_analytics_sentiment ON call_analytics(sentiment)")
