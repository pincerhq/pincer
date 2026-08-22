from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Migrations are raw SQL (op.execute()), not ORM-model-driven, so there is no
# SQLAlchemy metadata for Alembic to diff against.
target_metadata = None


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
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
