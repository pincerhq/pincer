"""The async engine: URL resolution, per-loop caching and SQLite connection settings."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.pool import NullPool

from pincer.config.database import DatabaseSettings
from pincer.db.engine import (
    SQLITE_BUSY_TIMEOUT_MS,
    _asyncpg_connect_args,
    build_config,
    dispose_engines,
    get_database_url,
    get_engine,
    get_sync_url,
    init_database,
    to_async_url,
)


def test_urls_are_mapped_onto_their_async_driver():
    assert to_async_url("sqlite:////tmp/p.db") == "sqlite+aiosqlite:////tmp/p.db"
    assert to_async_url("postgresql://u:secret@h:5432/db") == "postgresql+asyncpg://u:secret@h:5432/db"
    # An explicit sync driver is replaced, not stacked.
    assert to_async_url("postgresql+psycopg://u@h/db") == "postgresql+asyncpg://u@h/db"


def test_an_unsupported_backend_is_refused():
    with pytest.raises(ValueError, match="mysql"):
        to_async_url("mysql://u@h/db")


def test_the_runtime_url_follows_the_migration_url(monkeypatch, tmp_path):
    monkeypatch.delenv("PINCER_DATABASE_URL", raising=False)
    assert get_database_url(tmp_path / "p.db") == f"sqlite+aiosqlite:///{tmp_path / 'p.db'}"

    monkeypatch.setenv("PINCER_DATABASE_URL", "sqlite:////elsewhere/x.db")
    assert get_database_url(tmp_path / "p.db") == "sqlite+aiosqlite:////elsewhere/x.db"


def test_postgres_is_addressable_now_that_every_domain_uses_the_engine(monkeypatch, tmp_path):
    monkeypatch.setenv("PINCER_DATABASE_URL", "postgresql://u:secret@h/db")
    assert get_database_url(tmp_path / "p.db") == "postgresql+asyncpg://u:secret@h/db"
    # Alembic runs synchronously, so migrations use the sync driver. The
    # password has to survive being re-rendered, or Alembic authenticates as
    # `***` and the process dies at startup on a password error.
    assert get_sync_url(tmp_path / "p.db") == "postgresql+psycopg://u:secret@h/db"


def _use_dotenv(monkeypatch, tmp_path: Path, content: str) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(content)
    monkeypatch.setitem(DatabaseSettings.model_config, "env_file", dotenv)


def test_a_url_set_only_in_dotenv_is_honoured(monkeypatch, tmp_path):
    """`.env` used to reach the URL only through os.environ, which it joins when
    the MCP config loads — after `init_database` had already migrated SQLite, so
    the process then split between SQLite and an unmigrated Postgres."""
    _use_dotenv(monkeypatch, tmp_path, "PINCER_DATABASE_URL=postgresql://u:secret@h/db\n")

    assert get_sync_url(tmp_path / "p.db") == "postgresql+psycopg://u:secret@h/db"
    assert "PINCER_DATABASE_URL" not in os.environ


def test_the_environment_wins_over_dotenv(monkeypatch, tmp_path):
    _use_dotenv(monkeypatch, tmp_path, "PINCER_DATABASE_URL=postgresql://u@from-dotenv/db\n")
    monkeypatch.setenv("PINCER_DATABASE_URL", "postgresql://u@from-env/db")

    assert get_sync_url(tmp_path / "p.db") == "postgresql+psycopg://u@from-env/db"


def test_an_empty_url_in_the_environment_still_means_the_sqlite_file(monkeypatch, tmp_path):
    """`PINCER_DATABASE_URL= pincer run` (or compose's `${PINCER_DATABASE_URL:-}`)
    forces local SQLite; a URL in `.env` must not fill the blank in."""
    _use_dotenv(monkeypatch, tmp_path, "PINCER_DATABASE_URL=postgresql://u@from-dotenv/db\n")
    monkeypatch.setenv("PINCER_DATABASE_URL", "")

    assert get_sync_url(tmp_path / "p.db") == f"sqlite:///{tmp_path / 'p.db'}"


def test_an_invalid_unrelated_setting_does_not_break_url_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("PINCER_LOG_LEVEL", "bogus")
    monkeypatch.setenv("PINCER_DATABASE_URL", "postgresql://u@h/db")

    assert get_sync_url(tmp_path / "p.db") == "postgresql+psycopg://u@h/db"


def test_an_unsupported_backend_in_the_configured_url_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("PINCER_DATABASE_URL", "mysql://u@h/db")
    with pytest.raises(RuntimeError, match="mysql"):
        get_sync_url(tmp_path / "p.db")


def test_an_async_driver_in_the_configured_url_still_gives_alembic_a_sync_one(monkeypatch, tmp_path):
    """Alembic's `engine_from_config` is synchronous: an async driver reaches it
    as `MissingGreenlet` at startup, naming nothing that points back here."""
    monkeypatch.setenv("PINCER_DATABASE_URL", "sqlite+aiosqlite:////tmp/x.db")
    assert get_sync_url(tmp_path / "p.db") == "sqlite+pysqlite:////tmp/x.db"


def test_libpq_keys_reach_asyncpg_through_a_dsn_not_as_keywords():
    """Every managed Postgres hands out `?sslmode=require`; asyncpg.connect()
    has no `sslmode` keyword, so passing it through failed every query."""
    target, connect_args = _asyncpg_connect_args(
        "postgresql+asyncpg://u:p@db.example:6543/d?sslmode=require&application_name=pincer&target_session_attrs=read-write"
    )
    assert dict(target.query) == {"target_session_attrs": "read-write"}  # asyncpg takes this one itself
    assert (target.host, target.port, target.database) == ("db.example", 6543, "d")
    assert connect_args == {"dsn": "postgresql://?sslmode=require&application_name=pincer"}


def test_channel_binding_is_dropped_unless_it_is_required():
    target, connect_args = _asyncpg_connect_args("postgresql+asyncpg://u@h/d?channel_binding=prefer")
    assert dict(target.query) == {}
    assert connect_args == {"dsn": "postgresql://?"}
    with pytest.raises(ValueError, match="channel_binding"):
        _asyncpg_connect_args("postgresql+asyncpg://u@h/d?channel_binding=require")


@pytest.mark.parametrize("key", ["connect_timeout", "keepalives", "keepalives_idle", "hostaddr", "gssencmode"])
def test_libpq_keys_asyncpg_cannot_honour_are_rejected_not_silently_dropped(key):
    """These are neither an asyncpg.connect() keyword nor one the DSN parser
    special-cases, so moving them into the DSN would silently turn them into
    a Postgres server setting instead of the client-side option they name."""
    with pytest.raises(ValueError, match=key):
        _asyncpg_connect_args(f"postgresql+asyncpg://u@h/d?{key}=10")


def test_a_search_path_in_options_reaches_asyncpg_through_the_dsn():
    """libpq's `options` is a startup parameter Postgres parses itself, so the
    server setting asyncpg makes of it is what picks a non-`public` schema."""
    target, connect_args = _asyncpg_connect_args("postgresql+asyncpg://u@h/d?options=-csearch_path%3Dpincer")
    assert dict(target.query) == {}
    assert connect_args == {"dsn": "postgresql://?options=-csearch_path%3Dpincer"}


@pytest.mark.parametrize(
    "encoded", ["-c%20search_path%3Da%2Cb", "-c+search_path%3Da,b", "-csearch_path%3Da%20-cstatement_timeout%3D5s"]
)
def test_options_with_spaces_and_commas_reach_asyncpg_as_psycopg_reads_them(encoded):
    """Both drivers must get the same value: psycopg reads SQLAlchemy's parsed
    query, asyncpg re-parses the DSN rebuilt from it."""
    from urllib.parse import parse_qs, urlsplit

    target, connect_args = _asyncpg_connect_args(f"postgresql+asyncpg://u@h/d?options={encoded}")
    parsed_by_psycopg = make_url(f"postgresql+psycopg://u@h/d?options={encoded}").query["options"]
    assert parse_qs(urlsplit(connect_args["dsn"]).query) == {"options": [parsed_by_psycopg]}
    assert " " in parsed_by_psycopg


def test_a_url_without_options_adds_nothing_to_the_connection():
    """No schema named, nothing sent: Postgres's own `search_path` applies,
    including any `ALTER ROLE ... SET search_path` an operator has set."""
    assert _asyncpg_connect_args("postgresql+asyncpg://u@h/d")[1] == {}


def test_a_key_asyncpg_connect_actually_accepts_reaches_it_unmoved():
    target, connect_args = _asyncpg_connect_args("postgresql+asyncpg://u@h/d?command_timeout=5")
    assert dict(target.query) == {"command_timeout": "5"}
    assert connect_args == {}


def test_a_percent_encoded_url_survives_the_alembic_config(monkeypatch, tmp_path):
    """Alembic's config interpolates `%`; an encoded URL must come back intact,
    whether it comes from the setting or is passed in."""
    url = "postgresql+psycopg://u:p%40ss@h/d?options=-csearch_path%3Dpincer"
    monkeypatch.setenv("PINCER_DATABASE_URL", url)
    assert build_config(tmp_path / "p.db").get_main_option("sqlalchemy.url") == url
    monkeypatch.delenv("PINCER_DATABASE_URL")
    assert build_config(tmp_path / "p.db", url).get_main_option("sqlalchemy.url") == url


async def test_a_sslmode_url_opens_an_engine_that_reaches_the_socket():
    """The reviewer's reproduction: before, `connect()` got an unexpected `sslmode` keyword."""
    pytest.importorskip("asyncpg")
    engine = get_engine(to_async_url("postgresql://u:p@127.0.0.1:1/db?sslmode=disable"))
    with pytest.raises(OSError):
        async with engine.connect():
            pass
    await dispose_engines()


@contextlib.contextmanager
def _server_default_timezone(url: str, zone: str) -> Iterator[None]:
    """Give the scratch database a non-UTC default zone for the duration."""
    import sqlalchemy as sa

    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    database = sa.engine.make_url(url).database
    with admin.connect() as conn:
        conn.execute(sa.text(f"ALTER DATABASE \"{database}\" SET timezone TO '{zone}'"))
    try:
        yield
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'ALTER DATABASE "{database}" RESET timezone'))
        admin.dispose()


@pytest.mark.parametrize("query", [{}, {"application_name": "pincer-test"}])
async def test_postgres_sessions_run_in_utc_whatever_the_server_default(postgres_scratch_url, query):
    """The timestamp columns have no zone, so a `CURRENT_TIMESTAMP` default is
    stored in the session's zone; `IsoText` reads every value back as UTC."""
    import sqlalchemy as sa

    # Through the URL API: the scratch URL keeps `PINCER_TEST_PG_URL`'s own
    # query string, which a second `?` would fold into its last value.
    url = sa.engine.make_url(postgres_scratch_url).update_query_dict(query)
    with _server_default_timezone(postgres_scratch_url, "Europe/Berlin"):
        engine = get_engine(to_async_url(url.render_as_string(hide_password=False)))
        async with engine.connect() as conn:
            assert (await conn.execute(text("SHOW timezone"))).scalar() == "UTC"
            if query:
                assert (await conn.execute(text("SHOW application_name"))).scalar() == "pincer-test"
        await dispose_engines()


def test_postgres_migrations_run_in_utc_whatever_the_server_default(migration_url, monkeypatch, tmp_path):
    """A revision's `CURRENT_TIMESTAMP` default must land in UTC as the
    runtime's does, so the zone is checked inside the migration transaction."""
    if not migration_url.startswith("postgresql"):
        pytest.skip("SQLite has no session time zone")
    from alembic import command
    from alembic.runtime.environment import EnvironmentContext

    zones = []
    run_migrations = EnvironmentContext.run_migrations

    def spy(self, **kwargs):
        zones.append(self.get_bind().exec_driver_sql("SHOW timezone").scalar())
        return run_migrations(self, **kwargs)

    monkeypatch.setattr(EnvironmentContext, "run_migrations", spy)
    cfg = build_config(tmp_path / "unused.db", migration_url)
    with _server_default_timezone(migration_url, "Europe/Berlin"):
        command.upgrade(cfg, "head")

    assert zones == ["UTC"]


def test_the_migration_lock_sits_next_to_the_database_the_url_names(monkeypatch, tmp_path: Path):
    """Two processes whose settings differ but whose override names one file must share a lock."""
    shared = tmp_path / "shared" / "pincer.db"
    monkeypatch.setenv("PINCER_DATABASE_URL", f"sqlite:///{shared}")

    init_database_path = tmp_path / "settings" / "pincer.db"
    asyncio.run(init_database(init_database_path))

    assert (shared.parent / "pincer.db.migrate.lock").exists()
    assert not init_database_path.parent.exists()


async def test_sqlite_connections_are_never_held_between_units_of_work(tmp_path: Path):
    """A pooled SQLite connection keeps its write lock alive between units of
    work, and the other processes sharing the file get "database is locked".

    Telemetry ran on one pooled connection for a while: it failed a driven
    call in four, and measuring showed it bought no audio-loop time at all.
    """
    engine = get_engine(to_async_url(f"sqlite:///{tmp_path / 'p.db'}"))
    try:
        assert isinstance(engine.pool, NullPool)
    finally:
        await dispose_engines()


async def test_sqlite_connections_get_the_shared_pragmas(tmp_path: Path):
    engine = get_engine(to_async_url(f"sqlite:///{tmp_path / 'p.db'}"))
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
            assert (await conn.execute(text("PRAGMA busy_timeout"))).scalar() == SQLITE_BUSY_TIMEOUT_MS
            assert (await conn.execute(text("PRAGMA synchronous"))).scalar() == 1  # NORMAL
            assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 0
    finally:
        await dispose_engines()


async def test_one_engine_per_url_on_a_loop(tmp_path: Path):
    a = to_async_url(f"sqlite:///{tmp_path / 'a.db'}")
    b = to_async_url(f"sqlite:///{tmp_path / 'b.db'}")
    try:
        assert get_engine(a) is get_engine(a)
        assert get_engine(a) is not get_engine(b)
    finally:
        await dispose_engines()
    # Disposal forgets them: the next call builds a fresh engine.
    fresh = get_engine(a)
    try:
        async with fresh.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar() == 1
    finally:
        await dispose_engines()


def test_each_event_loop_gets_its_own_engine(tmp_path: Path):
    """Pooled connections are bound to the loop that opened them."""
    url = to_async_url(f"sqlite:///{tmp_path / 'p.db'}")

    async def engine_used_on_this_loop():
        engine = get_engine(url)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await dispose_engines()
        return engine

    def on_a_fresh_loop():
        # Not `asyncio.run`: it clears the thread's current event loop on exit,
        # which breaks later sync tests that call `asyncio.get_event_loop()`.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(engine_used_on_this_loop())
        finally:
            loop.close()

    assert on_a_fresh_loop() is not on_a_fresh_loop()


def test_an_engine_needs_a_running_loop(tmp_path: Path):
    with pytest.raises(RuntimeError):
        get_engine(to_async_url(f"sqlite:///{tmp_path / 'p.db'}"))


def test_several_processes_can_migrate_one_fresh_database_at_once(tmp_path: Path):
    """The prod compose file starts two containers on one volume.

    SQLite has no transactional DDL, so without a lock the loser of this race
    is not rolled back: it dies on a `CREATE TABLE` the winner already ran,
    having left `pincer_alembic_version` stamped at a revision only half applied.
    """
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import asyncio, sys\n"
        "from pathlib import Path\n"
        "from pincer.db.engine import init_database\n"
        "asyncio.run(init_database(Path(sys.argv[1])))\n"
    )
    db_path = tmp_path / "fresh.db"
    # The children get none of conftest's isolation, and would otherwise read
    # the URL from the developer's real `.env` and migrate that database. Pin
    # the URL, and run them away from the repo's `.env` / `../.env`.
    env = {**os.environ, "PINCER_DATABASE_URL": f"sqlite:///{db_path}"}

    started = [
        subprocess.Popen(  # noqa: S603 - fixed argv, test-local script
            [sys.executable, str(worker), str(db_path)],
            env=env,
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    # `communicate()` before looking at the exit code: `wait()` on a child
    # whose output fills the pipe buffer never returns.
    finished = [(process, process.communicate()[1]) for process in started]
    assert [stderr for process, stderr in finished if process.returncode != 0] == []

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pincer_alembic_version").fetchone()[0] == 1


async def test_init_database_rejects_a_bad_postgres_url_at_boot_not_first_query(monkeypatch, tmp_path: Path):
    """`init_database` runs once at startup; a URL asyncpg refuses should fail
    there, not surface as the first request's error. Alembic migrates through
    psycopg, which understands `channel_binding=require` even though asyncpg
    does not, so migration alone would report a clean start."""
    monkeypatch.setattr("pincer.db.engine.command.upgrade", lambda *a, **k: None)
    monkeypatch.setenv("PINCER_DATABASE_URL", "postgresql://u@h/d?channel_binding=require")

    with pytest.raises(ValueError, match="channel_binding"):
        await init_database(tmp_path / "p.db")


async def test_init_database_migrates_the_configured_database_and_names_it(monkeypatch, tmp_path: Path):
    """The entry points call this and discard the URL; `db_path=None` is the
    branch `pincer run` actually takes, through the settings."""
    monkeypatch.delenv("PINCER_DATABASE_URL", raising=False)
    db_path = tmp_path / "configured.db"

    class _Settings:
        pass

    settings = _Settings()
    settings.db_path = db_path
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)

    try:
        url = await init_database()
        assert url == to_async_url(f"sqlite:///{db_path}")
        with sqlite3.connect(db_path) as conn:
            # Not just "a file exists": the schema is actually at head.
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"pincer_alembic_version", "pincer_memories", "pincer_telephony_calls"} <= tables
    finally:
        await dispose_engines()


@pytest.mark.parametrize("schema", [None, "pincer_alt"])
async def test_the_schema_named_in_the_url_holds_every_table(migration_url, monkeypatch, tmp_path, schema):
    """`?options=-csearch_path%3D<schema>` puts the migrations and the runtime
    in that schema; without it, tables land in `public` as they always have.
    `migration_url` is a throwaway database, dropped with the schema in it."""
    if not migration_url.startswith("postgresql"):
        pytest.skip("SQLite has no schemas")
    import sqlalchemy as sa

    from pincer.db.session import session_scope
    from pincer.models.costs import CostLog
    from pincer.repositories.costs import CostLogRepository

    url = sa.engine.make_url(migration_url)
    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        if schema is not None:
            with admin.connect() as conn:
                conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            url = url.update_query_dict({"options": f"-csearch_path={schema}"})
        monkeypatch.setenv("PINCER_DATABASE_URL", url.render_as_string(hide_password=False))
        try:
            runtime_url = await init_database(tmp_path / "unused.db")
            async with session_scope(runtime_url) as session:
                await CostLogRepository(session).add(
                    CostLog(timestamp=1.0, provider="p", model="m", input_tokens=1, output_tokens=2, cost_usd=0.5),
                    refresh=False,
                )
            async with session_scope(runtime_url) as session:
                assert await CostLogRepository(session).totals() == (0.5, 1, 1, 2)
                assert (await session.exec(sa.text("SELECT current_schema()"))).scalar() == (schema or "public")
        finally:
            await dispose_engines()
        by_schema = _pincer_tables_by_schema(admin)
    finally:
        admin.dispose()
    assert set(by_schema) == {schema or "public"}
    assert by_schema[schema or "public"] > 10


@pytest.mark.parametrize("search_path", ["pincer_missing", "pincer_missing,public"])
async def test_a_search_path_naming_a_missing_schema_is_refused_before_any_table_is_made(
    migration_url, monkeypatch, tmp_path, search_path
):
    """A typo'd or never-created schema would otherwise fail on Postgres's
    "no schema has been selected", or silently fall through to `public`."""
    if not migration_url.startswith("postgresql"):
        pytest.skip("SQLite has no schemas")
    import sqlalchemy as sa

    url = sa.engine.make_url(migration_url).update_query_dict({"options": f"-csearch_path={search_path}"})
    monkeypatch.setenv("PINCER_DATABASE_URL", url.render_as_string(hide_password=False))
    with pytest.raises(RuntimeError, match="pincer_missing.*does not exist"):
        await init_database(tmp_path / "unused.db")
    admin = sa.create_engine(migration_url)
    try:
        assert _pincer_tables_by_schema(admin) == {}
    finally:
        admin.dispose()


@pytest.mark.parametrize("search_path", [None, '"$user",pincer_missing'])
async def test_a_schema_named_after_the_role_is_used_as_postgres_would(
    migration_url, monkeypatch, tmp_path, search_path
):
    """`"$user"` resolves to the role's own schema when it exists: with `public`
    dropped, the default path still has somewhere to put the tables, and any
    entry after it is never consulted."""
    if not migration_url.startswith("postgresql"):
        pytest.skip("SQLite has no schemas")
    import sqlalchemy as sa

    url = sa.engine.make_url(migration_url)
    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            role = conn.execute(sa.text("SELECT current_user")).scalar()
            conn.execute(sa.text(f'DROP SCHEMA public CASCADE; CREATE SCHEMA "{role}" AUTHORIZATION "{role}"'))
        if search_path is not None:
            url = url.update_query_dict({"options": f"-csearch_path={search_path}"})
        monkeypatch.setenv("PINCER_DATABASE_URL", url.render_as_string(hide_password=False))
        try:
            await init_database(tmp_path / "unused.db")
        finally:
            await dispose_engines()
        by_schema = _pincer_tables_by_schema(admin)
    finally:
        admin.dispose()
    assert set(by_schema) == {role}


def _pincer_tables_by_schema(engine) -> dict[str, int]:
    query = text(
        "SELECT table_schema, count(*) FROM information_schema.tables "
        "WHERE table_name LIKE 'pincer\\_%' GROUP BY table_schema"
    )
    with engine.connect() as conn:
        return dict(conn.execute(query).all())
