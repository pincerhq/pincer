# Database migrations

Schema DDL lives in Alembic revisions under
[`src/pincer/db/migrations/versions/`](../../src/pincer/db/migrations/versions/),
and nowhere else. No module issues `CREATE TABLE`/`ALTER TABLE` at runtime;
`pincer.db.ensure_schema_current(db_path)` brings a database to head before the
first query against it (it is synchronous — async callers wrap it in
`asyncio.to_thread`).

This directory previously held hand-written `.sql` files as the human-readable
record of voice schema changes that the runtime applied itself via a
try/except "duplicate column" pattern. Those are now real revisions, and the
raw SQL has been removed rather than left to drift out of sync:

| Former file              | Revision                      | Schema |
| ------------------------ | ----------------------------- | ------ |
| `011_in_call_tools.sql`  | `0005_voice_schema_reconcile` | `call_actions.tier`, `.approval_mode`, `.deny_reason` |
| `012_receptionist.sql`   | `0006_voice_receptionist`     | `inbound_messages`, `voice_calls.inbound_intent` |
| `013_call_threads.sql`   | `0007_voice_threads`          | `call_threads`, `call_thread_members`, `voice_calls.thread_id`, `.thread_attach_kind` |
| `014_call_briefing.sql`  | `0008_voice_briefing`         | `voice_calls.briefing_json` |
| `015_call_analytics.sql` | `0009_voice_analytics`        | `call_analytics` |

`0005` additionally reconciles the legacy `voice_calls` shape frozen in
`0001_baseline` (`id`/`user_id`/`caller_number`/`target_number`/`status`) with
the `call_sid`-based schema the voice subsystem actually uses, preserving rows
from both that shape and from pre-Alembic databases that already carry the
newer one.

## Adding a revision

The SQLModel tables in [`src/pincer/models/`](../../src/pincer/models/) are
the source of truth for the schema, and Alembic's `target_metadata` points at
them. `0001`–`0013` predate the models and stay hand-written SQL, as do the
Postgres-only type widenings (`0013`, `0016`) and the FTS revisions.
`tests/test_schema_drift.py` checks that the models and those revisions
describe the same schema, on SQLite and (in CI) on Postgres.

To change the schema:

1. Change the model. Keep storage types as they are (`IsoText` for timestamps,
   which is `TEXT` on SQLite and `TIMESTAMP` on Postgres; `Real` for floats,
   which is 8 bytes on both — never plain `REAL`, which is 4 bytes on Postgres;
   `BigInt` for anything that can outgrow 2³¹, such as a nanosecond clock,
   since Postgres' `INTEGER` is 4 bytes too; JSON stays in `TEXT`).
2. Autogenerate the revision against a database at head:
   `uv run alembic -c src/pincer/db/alembic.ini revision --autogenerate --rev-id 0014 -m "..."`
   (revisions are numbered, not hashed; `PINCER_DATABASE_URL` picks the database).
3. Review it. SQLite runs in batch mode (the table is rebuilt), and Alembic
   cannot see everything: data moves, renames (which it writes as a drop plus
   an add) and anything in the list below are yours to write.
4. Run `uv run pytest tests/test_schema_drift.py tests/test_db_migrations.py`,
   with `PINCER_TEST_PG_URL` set if you can, to cover Postgres as well.

Autogenerate leaves these alone (`pincer.db.metadata.include_object`), because
they have no model and are written by hand:

- the FTS5 table `memories_fts`, its shadow tables and its triggers (SQLite only)
- `idx_phone_contacts_name` (`COLLATE NOCASE` on SQLite, `lower(name)` on Postgres)
- the dormant tables from `0001` (`registry_skills`, `expenses`, `habits`,
  `habit_checkins`, `pomodoro_sessions`, `discord_threads`)

It also ignores two differences that SQLite reports on its own and that are
not worth rebuilding a table for: primary keys that reflect as nullable, and
`phone_contacts.created_at`, which is declared `TIMESTAMP`.
