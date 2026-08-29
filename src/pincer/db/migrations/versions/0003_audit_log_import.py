"""One-time import of the legacy standalone audit.db into the unified database.

`security/audit.py` previously defaulted to a separate `<data_dir>/audit.db`
SQLite file rather than the shared db every other module already used.
Now that AuditLogger points at the same unified `settings.db_path`, any
pre-existing `audit.db` sitting next to it is copied in here via
``ATTACH DATABASE`` + ``INSERT ... SELECT`` and left on disk afterward,
untouched. SQLite-only: Postgres installs never had a separate audit.db
file to import from.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-22
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import op
from sqlalchemy import text

logger = logging.getLogger(__name__)

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_AUDIT_LOG_COLUMNS = (
    "id",
    "timestamp",
    "user_id",
    "session_id",
    "action",
    "tool",
    "input_summary",
    "output_summary",
    "approved",
    "cost_usd",
    "duration_ms",
    "ip_address",
    "channel",
    "metadata_json",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return

    target_db = bind.engine.url.database
    if not target_db or target_db == ":memory:":
        return

    legacy_audit_db = Path(target_db).parent / "audit.db"
    if not legacy_audit_db.is_file() or legacy_audit_db.resolve() == Path(target_db).resolve():
        return

    # Guard against duplicating history if this migration ever runs again
    # against a db that already has audit_log rows (e.g. a downgrade/upgrade
    # round-trip in tests).
    if bind.execute(text("SELECT COUNT(*) FROM audit_log")).scalar():
        return

    # Left attached rather than explicitly DETACHed: this migration runs
    # inside Alembic's single wrapping transaction, and SQLite refuses to
    # DETACH a database with uncommitted changes in the current transaction.
    # The attachment is released automatically when this connection closes
    # at the end of the upgrade run (see db/engine.py).
    bind.execute(text("ATTACH DATABASE :path AS legacy_audit"), {"path": str(legacy_audit_db)})
    has_table = bind.execute(
        text("SELECT name FROM legacy_audit.sqlite_master WHERE type='table' AND name='audit_log'")
    ).fetchone()
    if has_table:
        legacy_cols = {r[1] for r in bind.execute(text("PRAGMA legacy_audit.table_info(audit_log)")).fetchall()}
        cols = ", ".join(c for c in _AUDIT_LOG_COLUMNS if c in legacy_cols)
        result = bind.execute(text(f"INSERT INTO audit_log ({cols}) SELECT {cols} FROM legacy_audit.audit_log"))
        logger.info("Imported %d audit rows from %s", result.rowcount, legacy_audit_db)


def downgrade() -> None:
    # One-time data import from an external file; not meaningfully reversible.
    pass
