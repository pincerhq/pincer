-- Sprint 13 — call threads (§2).
-- Applied automatically by pincer.voice.retention.ensure_voice_tables
-- (CREATE TABLE IF NOT EXISTS + the project-wide try/except "duplicate
-- column" pattern); kept here as the human-readable record.
CREATE TABLE IF NOT EXISTS call_threads (
  thread_id TEXT PRIMARY KEY,              -- "thr_" + 12 hex
  subject TEXT NOT NULL,                   -- human title, <= 120 chars
  status TEXT NOT NULL DEFAULT 'open',     -- open | resolved | closed
  origin TEXT NOT NULL,                    -- user_task | inbound
  primary_number TEXT DEFAULT '',          -- E.164, informational
  contact_name TEXT DEFAULT '',
  language TEXT DEFAULT '',                -- thread default language (Sprint 2 semantics)
  rolling_summary TEXT DEFAULT '',
  open_commitments TEXT DEFAULT '[]',      -- JSON array (§6.2 schema)
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT,
  closed_at TEXT
);

-- Durable membership record. voice_calls rows age out with the Sprint 0
-- retention purge; a thread must still be able to list the call that
-- happened (§5: "purged calls remain listed in the thread as stubs"), so
-- membership + the minimum stub facts live in their own table that the
-- purge deliberately does NOT touch. call_sid is the PRIMARY KEY, which is
-- the single-thread rule (§2) enforced by the database itself.
CREATE TABLE IF NOT EXISTS call_thread_members (
  call_sid TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES call_threads(thread_id),
  attach_kind TEXT NOT NULL DEFAULT '',    -- origin | retry | followup | inbound_matched | manual
  attached_at TEXT NOT NULL,
  call_started_at TEXT DEFAULT '',
  direction TEXT DEFAULT '',
  outcome_code TEXT DEFAULT '',            -- Sprint 3 outcome (stub survives the purge)
  task_result TEXT DEFAULT ''
);

ALTER TABLE voice_calls ADD COLUMN thread_id TEXT DEFAULT '' REFERENCES call_threads(thread_id);
ALTER TABLE voice_calls ADD COLUMN thread_attach_kind TEXT DEFAULT '';
  -- origin | retry | followup | inbound_matched | manual
CREATE INDEX IF NOT EXISTS idx_calls_thread ON voice_calls(thread_id);
CREATE INDEX IF NOT EXISTS idx_threads_number_status ON call_threads(primary_number, status);
CREATE INDEX IF NOT EXISTS idx_thread_members_thread ON call_thread_members(thread_id);
