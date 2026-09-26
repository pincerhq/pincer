from __future__ import annotations

from logging.config import fileConfig
from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import context
from sqlalchemy import engine_from_config, pool

from pincer.db.metadata import VERSION_TABLE, compare_type, drop_sqlite_noise, include_object, metadata

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The SQLModel tables are the schema's source of truth: `alembic revision
# --autogenerate` diffs them against the database. Revisions 0001-0013 predate
# the models and stay raw SQL; `tests/test_schema_drift.py` keeps the models
# and those revisions in step.
target_metadata = metadata


def _bootstrap_version_table(engine: Engine) -> None:
    """Rename `alembic_version` to `VERSION_TABLE`, once, before Alembic reads it.

    Alembic decides which table to read the current revision from here, in
    code, not from the database — so this has to run before `context.configure`
    below. A database migrated before this change has its history in
    `alembic_version`; renaming it in place (same row, same revision) is what
    lets Alembic find that history under the new name instead of concluding
    the database is unmigrated and replaying every revision from scratch. A
    fresh database has neither table yet, so this is a no-op and Alembic
    creates `VERSION_TABLE` directly.

    Runs on its own connection, committed and closed before the migration
    connection below is opened: reflecting `get_table_names()` on that
    connection first — even read-only — starts its transaction, and every
    later reflection call a revision makes (`0011`'s rename among them) then
    sees that transaction's original, empty snapshot instead of the tables
    revisions before it just created, silently skipping every one of them.
    """
    with engine.connect() as bootstrap:
        existing = set(sa.inspect(bootstrap).get_table_names())
        if "alembic_version" in existing and VERSION_TABLE not in existing:
            bootstrap.execute(sa.text(f"ALTER TABLE alembic_version RENAME TO {VERSION_TABLE}"))
            bootstrap.commit()


def _get_url() -> str:
    """Resolve the target DB URL.

    Set programmatically by `pincer.db.engine.ensure_schema_current()` for
    the app's normal auto-upgrade-on-startup path. Falls back to the app's
    configured `settings.db_path` when running the `alembic` CLI directly
    (e.g. `alembic -c src/pincer/db/alembic.ini current`).
    """
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url

    from pincer.config import get_settings_relaxed
    from pincer.db.engine import get_sync_url

    return get_sync_url(get_settings_relaxed().db_path)


def run_migrations_offline() -> None:
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=VERSION_TABLE,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _get_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    try:
        _bootstrap_version_table(connectable)
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_object=include_object,
                compare_type=compare_type,
                process_revision_directives=drop_sqlite_noise,
                # SQLite reflects defaults as raw text ('0' vs 0, quoting),
                # so comparing them would only report noise.
                compare_server_default=False,
                # SQLite cannot ALTER most things in place; batch mode
                # rebuilds the table instead.
                render_as_batch=connection.dialect.name == "sqlite",
                version_table=VERSION_TABLE,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
