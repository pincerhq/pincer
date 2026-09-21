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
Postgres-only type widenings (`0013`, `0016`), the FTS revisions, and the
UUIDv7 key conversions (`0017`, `0018`), which move data rather than DDL.
`tests/test_schema_drift.py` checks that the models and those revisions
describe the same schema, on SQLite and (in CI) on Postgres.

To change the schema:

1. Change the model. Keep storage types as they are (`IsoText` for timestamps,
   which is `TEXT` on SQLite and `TIMESTAMP` on Postgres; `Real` for floats,
   which is 8 bytes on both — never plain `REAL`, which is 4 bytes on Postgres;
   `BigInt` for anything that can outgrow 2³¹, such as a nanosecond clock,
   since Postgres' `INTEGER` is 4 bytes too; `Uuid7` for a row id Pincer
   mints, which is a native `uuid` on Postgres and the canonical 36-character
   string on SQLite; JSON stays in `TEXT`).
2. Autogenerate the revision against a database at head:
   `uv run alembic -c src/pincer/db/alembic.ini revision --autogenerate --rev-id NNNN -m "..."`
   (revisions are numbered, not hashed: `NNNN` is the next free number after the
   newest file in `versions/`; `PINCER_DATABASE_URL` picks the database).
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

## Row identifiers

Every id Pincer mints for a row of its own is a UUIDv7, from
[`pincer.db.ids`](../../src/pincer/db/ids.py) — `new_id()` and nothing else.
The column type is `Uuid7`: a native `uuid` on Postgres, the canonical
36-character string on SQLite. A new table's key should look like every other
one:

```python
id: str = Field(default=None, sa_column=Column(Uuid7(), primary_key=True, default=new_id))
```

The generator goes on the **`Column`**, not in `Field(default_factory=…)`:
with `sa_column` present, a field factory only fires when the model is
constructed, so `add_all`, `insert()` and the executemany paths would leave
the id NULL.

Two properties this buys, both relied on and both tested:

- **Ids sort by time, as strings.** Several reads order by `(timestamp, id)`
  and use the key to separate rows that share a timestamp — a call's
  utterances, the newest messages. The canonical form is fixed-width lowercase
  hex, so ordering it as text is ordering the uuid by its bytes.
- **A writer needs no round trip.** The id exists before the INSERT, so
  nothing reads back a sequence value.

Identifiers that come from somewhere else are **not** UUIDs and must not be
converted: Twilio CallSids (including `call_transcripts.call_id` and
`call_actions.call_id`, which hold one despite the name), `pincer_user_id` (a
deterministic hash whose `usr_` prefix is branched on), the derived
`{channel}:{user}` session key, phone numbers, W3C `trace_id` / `span_id` /
`parent_span_id`, and `telephony_events.event_id`, which is a SHA-1 that
collapses a provider's retried callbacks into one row.

`Uuid7` refuses to bind a value that is not a uuid, on SQLite as well as
Postgres — a lenient SQLite branch would let a bad id through locally and fail
only in CI. Lookups are the exception: `BaseRepository.get` answers "no such
row" for a key that could never name one, so a malformed id in a URL is a 404
rather than a 500.
