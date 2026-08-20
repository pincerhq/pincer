-- Sprint 11 — in-call tool execution (§9).
-- Applied automatically by pincer.voice.retention.ensure_voice_tables via the
-- project-wide try/except "duplicate column" pattern; kept here as the
-- human-readable record of the schema change.
ALTER TABLE call_actions ADD COLUMN tier TEXT DEFAULT '';
ALTER TABLE call_actions ADD COLUMN approval_mode TEXT DEFAULT '';
ALTER TABLE call_actions ADD COLUMN deny_reason TEXT DEFAULT '';
