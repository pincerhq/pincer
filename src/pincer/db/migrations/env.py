from __future__ import annotations

import logging
from contextlib import contextmanager
from logging.config import fileConfig
from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import context
from sqlalchemy import engine_from_config, pool

from pincer.db.metadata import (
    LEGACY_VERSION_TABLE,
    MIGRATION_ADVISORY_KEY,
    VERSION_TABLE,
    compare_type,
    drop_sqlite_noise,
    include_object,
    metadata,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

logger = logging.getLogger("pincer.db.migrations")

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The SQLModel tables are the schema's source of truth: `alembic revision
# --autogenerate` diffs them against the database. Revisions 0001-0013 predate
# the models and stay raw SQL; `tests/test_schema_drift.py` keeps the models
# and those revisions in step.
target_metadata = metadata

#: Tables that only Pincer creates, under their name before `0019` and after:
#: `channel_identities` exists from `0001` on and is never renamed until
#: `0019` prefixes it. Both names are specific to Pincer.
_PINCER_MARKER_TABLES = frozenset({"channel_identities", "pincer_channel_identities"})


def _bootstrap_version_table(engine: Engine, revisions: set[str]) -> None:
    """Rename Pincer's own `alembic_version` to `VERSION_TABLE`, once, before Alembic reads it.

    Alembic decides which table to read the current revision from here, in
    code, not from the database — so this has to run before `context.configure`
    below. A database migrated before this change has its history in
    `alembic_version`; renaming it in place (same row, same revision) is what
    lets Alembic find that history under the new name instead of concluding
    the database is unmigrated and replaying every revision from scratch. A
    fresh database has neither table yet, so this is a no-op and Alembic
    creates `VERSION_TABLE` directly.

    Only Pincer's own `alembic_version` is renamed. In a database shared with
    another Alembic application, that table is the other application's
    history: taking it would strip that application of its record and feed
    Pincer a revision that is not its own. It is Pincer's only when every
    revision it records is one of `revisions` *and* a Pincer table is there —
    neither alone is enough, since other apps also number their revisions
    0001, 0002, … as Pincer does. With no Pincer table it is left exactly as
    it is, and Alembic builds `VERSION_TABLE` beside it. Once `VERSION_TABLE`
    exists, `alembic_version` is never looked at again.

    A Pincer table beside a revision this checkout doesn't know is refused,
    not guessed at. It is most likely Pincer's own history from a fork, a
    feature branch or a revision since renamed; leaving it would have Alembic
    replay every revision from `0001` over a populated schema, which SQLite
    cannot roll back. It may also be another application's table that happens
    to sit beside Pincer's, so it is not renamed either: the operator decides.

    With no Pincer table, `alembic_version` is never read at all. Beside
    Pincer's tables, one without a `version_num` column, or one Postgres
    denies Pincer's role, is another application's and is left alone the same
    way. Any other failure to read it propagates: a timeout or a dropped
    connection says nothing about whose table it is, and taking it as "not
    ours" would replay every revision over Pincer's schema.

    One case remains that this cannot tell apart: Pincer tables with no
    version table of Pincer's at all (an install from before Alembic, or one
    restored without it) beside another application's `alembic_version` at a
    revision id Pincer also has. That table is taken as Pincer's. Both
    conditions together are narrow enough to accept.

    Runs on its own connection, committed and closed before the migration
    connection below is opened: reflecting `get_table_names()` on that
    connection first — even read-only — starts its transaction, and every
    later reflection call a revision makes (`0011`'s rename among them) then
    sees that transaction's original, empty snapshot instead of the tables
    revisions before it just created, silently skipping every one of them.

    Runs under `_upgrade_lock`, so two Pincer processes starting at once
    cannot both see `alembic_version`: the second sees `VERSION_TABLE` and
    does nothing.
    """
    with engine.connect() as bootstrap:
        inspector = sa.inspect(bootstrap)
        existing = set(inspector.get_table_names())
        if VERSION_TABLE in existing or LEGACY_VERSION_TABLE not in existing:
            return
        # No Pincer table: not ours whatever it records, so it is never read.
        if not existing & _PINCER_MARKER_TABLES:
            return
        if "version_num" not in {column["name"] for column in inspector.get_columns(LEGACY_VERSION_TABLE)}:
            return
        try:
            recorded = set(bootstrap.execute(sa.text(f"SELECT version_num FROM {LEGACY_VERSION_TABLE}")).scalars())
        except sa.exc.ProgrammingError:
            # Postgres: permission denied, so another role's table. Anything
            # else (a lock or statement timeout, a dropped connection) says
            # nothing about whose table it is, and propagates.
            bootstrap.rollback()
            return
        if not recorded:
            return
        if not recorded <= revisions:
            raise RuntimeError(
                f"{LEGACY_VERSION_TABLE} records revision(s) {sorted(recorded - revisions)}, which this "
                f"version of Pincer does not have, beside Pincer's own tables. Pincer changes nothing rather "
                f"than guess. If it is Pincer's history (a fork or feature branch), run the Pincer version "
                f"that has that revision; if {LEGACY_VERSION_TABLE} belongs to another application, give "
                f"Pincer its own database or schema."
            )
        bootstrap.execute(sa.text(f"ALTER TABLE {LEGACY_VERSION_TABLE} RENAME TO {VERSION_TABLE}"))
        bootstrap.commit()


@contextmanager
def _upgrade_lock(engine: Engine) -> Iterator[None]:
    """Hold Pincer's Postgres advisory lock for the whole upgrade, bootstrap included.

    `pincer` and `pincer-tasks` start together against one database. Postgres
    DDL is transactional, so without this the second upgrader is not left
    half-applied, but it does fail: both read the same revision, both run the
    same revision, and the second errors once the first commits. Held across
    the whole upgrade, the second waits, then finds the database at head and
    starts cleanly. SQLite gets the same from `ensure_schema_current`'s file
    lock.

    Transaction-level, on a connection of its own whose transaction stays
    open for the whole upgrade and is then rolled back, which releases it.
    Behind a transaction-mode pooler (PgBouncer, managed "pooled" endpoints)
    that is what keeps the lock on one server connection: a session-level
    lock would go back to the pool with the server connection at the first
    commit, still held, and its unlock could land elsewhere. Nothing reflects
    on this connection, so its open transaction cannot hide new tables from
    the revisions (see `_bootstrap_version_table`).

    A starter that has to wait says so once, so a start held up by another
    process's long migration is not silent. Ending the transaction cannot fail
    the upgrade: if the connection died while it sat idle (a firewall, an
    `idle_session_timeout`), the server released the lock with it, and the
    migration it guarded has already committed.
    """
    if engine.dialect.name != "postgresql":
        yield
        return
    with engine.connect() as lock:
        if not lock.execute(
            sa.text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": MIGRATION_ADVISORY_KEY}
        ).scalar():
            logger.info("Waiting for another Pincer process to finish migrating the database")
            lock.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_ADVISORY_KEY})
        try:
            yield
        finally:
            try:
                lock.rollback()
            except sa.exc.DBAPIError:
                logger.warning("Could not end the migration lock's transaction; its connection is gone", exc_info=True)


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
    raise SystemExit("Offline --sql migrations are not supported; run upgrades online.")


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _get_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    try:
        with _upgrade_lock(connectable):
            _bootstrap_version_table(connectable, {script.revision for script in context.script.walk_revisions()})
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
