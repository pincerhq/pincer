from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from pincer.db.metadata import compare_type, drop_sqlite_noise, include_object, metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The SQLModel tables are the schema's source of truth: `alembic revision
# --autogenerate` diffs them against the database. Revisions 0001-0013 predate
# the models and stay raw SQL; `tests/test_schema_drift.py` keeps the models
# and those revisions in step.
target_metadata = metadata


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
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _get_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    try:
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
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
