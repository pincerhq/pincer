"""Add the cache token counts to `pincer_cost_logs` (issue #357).

From here on `input_tokens` is uncached input for every provider, and the
prompt's cached part is stored beside it: cache reads, and cache writes by TTL
(5-minute and 1-hour; OpenAI's writes count as 5-minute). Rows written before
this revision are not rewritten: their cache columns read 0 (not recorded).

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

_COLUMNS = ("cache_read_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens")


def upgrade() -> None:
    with op.batch_alter_table("pincer_cost_logs") as batch:
        for name in _COLUMNS:
            batch.add_column(sa.Column(name, sa.Integer(), server_default=sa.text("0"), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("pincer_cost_logs") as batch:
        for name in reversed(_COLUMNS):
            batch.drop_column(name)
