-- Sprint 12 — inbound receptionist (§11).
-- Applied automatically by pincer.voice.retention.ensure_voice_tables (CREATE
-- TABLE IF NOT EXISTS + try/except "duplicate column"); kept as the readable record.
CREATE TABLE IF NOT EXISTS inbound_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_sid TEXT NOT NULL,
  caller_name TEXT DEFAULT '',
  caller_name_unverified INTEGER DEFAULT 0,
  callback_number TEXT DEFAULT '',
  callback_unverified INTEGER DEFAULT 0,
  matter TEXT DEFAULT '',
  urgent INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  delivered_to_owner_at TEXT
);
ALTER TABLE voice_calls ADD COLUMN inbound_intent TEXT DEFAULT '';  -- question|message|appointment|human|unknown|after_hours
