"""Give every table a plural name.

Eight tables were named in the singular. They are renamed in place, so rows,
indexes and triggers carry over. Index names never contained the old table
names, so they stay as they are. Migrations 0001–0010 keep the old names: they
record how the schema got here.

`call_analytics` keeps its name ("analytics" already reads as a plural), as do
`alembic_version` (Alembic's own) and `memories_fts` (the FTS5 shadow of
`memories`, which its triggers reference by name).

Foreign keys follow the rename: `channel_identities` and the dormant
`expenses` reference `identity_meta`. Postgres always rewrites the reference.
SQLite does it only from 3.26 on, with `legacy_alter_table` off (its default);
otherwise the rename would leave the reference pointing at a table that no
longer exists, so the migration refuses to run instead.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

#: (old, new). The last one is dormant: no code reads or writes it.
RENAMES: tuple[tuple[str, str], ...] = (
    ("audit_log", "audit_logs"),
    ("cost_log", "cost_logs"),
    ("image_cost_log", "image_cost_logs"),
    ("outbound_call_log", "outbound_call_logs"),
    ("briefing_config", "briefing_configs"),
    ("identity_meta", "identity_profiles"),
    ("do_not_call", "do_not_call_numbers"),
    ("skill_registry", "registry_skills"),
)

_MIN_SQLITE = (3, 26, 0)


def _require_foreign_key_aware_rename(bind: sa.engine.Connection) -> None:
    version = bind.execute(sa.text("SELECT sqlite_version()")).scalar_one()
    if tuple(int(part) for part in str(version).split(".")[:3]) < _MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {version} cannot rename a table referenced by a foreign key; "
            f"{'.'.join(map(str, _MIN_SQLITE))} or newer is required"
        )
    if bind.execute(sa.text("PRAGMA legacy_alter_table")).scalar_one():
        raise RuntimeError("PRAGMA legacy_alter_table is ON; renaming would leave foreign keys dangling")


def _rename(pairs: tuple[tuple[str, str], ...]) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _require_foreign_key_aware_rename(bind)
    existing = set(sa.inspect(bind).get_table_names())
    for old, new in pairs:
        # Guarded both ways, so a database where a rename already happened, or
        # that never had the table, upgrades cleanly.
        if old in existing and new not in existing:
            op.execute(f"ALTER TABLE {old} RENAME TO {new}")


def upgrade() -> None:
    _rename(RENAMES)


def downgrade() -> None:
    _rename(tuple((new, old) for old, new in RENAMES))
