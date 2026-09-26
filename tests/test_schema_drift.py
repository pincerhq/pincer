"""The SQLModel tables and the migrations describe the same schema.

The models are what `alembic revision --autogenerate` diffs against, so any
difference here would show up as a spurious change in the next revision — or,
worse, a model the database does not actually match. Runs on SQLite, and on
Postgres when `PINCER_TEST_PG_URL` is set.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata, produce_migrations
from alembic.migration import MigrationContext

from pincer.db import build_config
from pincer.db.metadata import (
    HAND_WRITTEN_TABLES,
    VERSION_TABLE,
    compare_type,
    drop_sqlite_noise,
    include_object,
    is_sqlite_primary_key_nullability,
    metadata,
)

_OPTS = {
    "include_object": include_object,
    "compare_type": compare_type,
    "compare_server_default": False,
    "version_table": VERSION_TABLE,
}


def _migrate_to_head(url: str, tmp_path: Path) -> sa.Engine:
    cfg = build_config(tmp_path / "unused.db")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    return sa.create_engine(url)


def _is_known_noise(dialect: str, diff: object) -> bool:
    # Column changes come grouped in a list; everything else is a tuple.
    if not isinstance(diff, list):
        return False
    return all(
        change[0] == "modify_nullable"
        and is_sqlite_primary_key_nullability(dialect, change[2], change[3], change[5], change[6])
        for change in diff
    )


def test_the_models_match_the_migrated_schema(migration_url, tmp_path):
    engine = _migrate_to_head(migration_url, tmp_path)
    try:
        with engine.connect() as conn:
            diffs = compare_metadata(MigrationContext.configure(conn, opts=_OPTS), metadata)
            dialect = conn.dialect.name
    finally:
        engine.dispose()
    assert [d for d in diffs if not _is_known_noise(dialect, d)] == []


def test_autogenerate_at_head_produces_an_empty_revision(migration_url, tmp_path):
    """What `alembic revision --autogenerate` would write right now: nothing."""
    engine = _migrate_to_head(migration_url, tmp_path)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts=_OPTS)
            script = produce_migrations(context, metadata)
            drop_sqlite_noise(context, None, [script])
    finally:
        engine.dispose()
    assert script.upgrade_ops.is_empty(), script.upgrade_ops.as_diffs()
    # Both directions: under render_as_batch a leftover nullability change in
    # downgrade() would rebuild every table it names.
    assert script.downgrade_ops.is_empty(), script.downgrade_ops.as_diffs()


def test_every_live_table_has_a_model(migration_url, tmp_path):
    engine = _migrate_to_head(migration_url, tmp_path)
    try:
        tables = set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()
    unmodelled = {t for t in tables if t not in metadata.tables and include_object(None, t, "table", True, None)}
    assert unmodelled == {VERSION_TABLE}
    # And the hand-written list names nothing that has since gained a model.
    assert not HAND_WRITTEN_TABLES & set(metadata.tables)
