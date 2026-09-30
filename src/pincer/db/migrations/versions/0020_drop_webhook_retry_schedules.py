"""Delete orphaned `webhook_retry` schedules (issue #217).

An earlier run of the Sprint E3 (signed webhooks + DLQ) code created a
`webhook_retry` schedule, but that code never reached `dev`: no handler for
the action type exists, so the row fired a no-op on every tick.
`run_scheduled_action` now disables such a schedule the first time it comes
due, but a disabled row still sits in the table and `pincer doctor` keeps
reporting it. This removes it outright.

`action` is JSON stored as text on both dialects, so rows are matched in
Python rather than with a dialect-specific JSON operator.

Forward-only: the deleted rows have no handler to run them, so there is
nothing for `downgrade` to put back.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-30
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

ORPHANED_ACTION_TYPES = frozenset({"webhook_retry"})


def _action_type(raw: str) -> str | None:
    try:
        action = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return action.get("type") if isinstance(action, dict) else None


def upgrade() -> None:
    bind = op.get_bind()
    if "pincer_schedules" not in set(sa.inspect(bind).get_table_names()):
        return
    rows = bind.execute(sa.text("SELECT id, action FROM pincer_schedules")).all()
    orphaned = [row.id for row in rows if _action_type(row.action) in ORPHANED_ACTION_TYPES]
    if orphaned:
        bind.execute(
            sa.text("DELETE FROM pincer_schedules WHERE id IN :ids").bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": orphaned},
        )


def downgrade() -> None:
    pass
