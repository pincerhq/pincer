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

Date/time columns follow the project dialect convention — `TEXT` on SQLite,
`TIMESTAMP` on PostgreSQL — so templates substitute the type per dialect
rather than hard-coding one. See `_NOW_COL` in any voice revision.
