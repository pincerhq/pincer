"""Record why an agent transcript line was not played.

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pincer_call_transcripts",
        sa.Column("delivery_reason", sa.Text(), nullable=True, server_default=sa.text("''")),
    )


def downgrade() -> None:
    op.drop_column("pincer_call_transcripts", "delivery_reason")
