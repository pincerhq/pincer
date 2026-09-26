"""Widen the monotonic-clock columns to BIGINT on Postgres.

`mono_ns`, `start_mono_ns` and `end_mono_ns` hold `time.monotonic_ns()` —
nanoseconds since boot. Postgres' `INTEGER` is 4 bytes, so it overflows 2.15
seconds after a host starts: every event and span write was refused with
"value out of int32 range", while call and turn rows kept landing, leaving a
dashboard that looks populated with an empty timeline. SQLite's `INTEGER` is
already 64-bit, so there this revision does nothing.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

COLUMNS: tuple[tuple[str, str], ...] = (
    ("telephony_events", "mono_ns"),
    ("telephony_spans", "start_mono_ns"),
    ("telephony_spans", "end_mono_ns"),
)


def _retype(to: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    existing = set(sa.inspect(bind).get_table_names())
    for table, column in COLUMNS:
        if table in existing:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {to}")


def upgrade() -> None:
    _retype("BIGINT")


def downgrade() -> None:
    # Lossy by nature: any real value written since the upgrade is far beyond
    # int32. Kept for a clean round trip on a database that has none.
    _retype("INTEGER")
