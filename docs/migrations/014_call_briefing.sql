-- Call briefing — the task the agent was given, persisted verbatim.
-- (The spec calls this 011; 011 and 013 are already taken by the in-call tool
--  and call-thread migrations, so it ships as 014 in file order.)
-- Applied automatically by pincer.voice.retention.ensure_voice_tables via the
-- project-wide try/except "duplicate column" pattern; kept here as the
-- human-readable record of the schema change.
ALTER TABLE voice_calls ADD COLUMN briefing_json TEXT DEFAULT '';
  -- {"task": "...", "target_name": "...", "language": "...",
  --  "source": "dashboard|chat|api|scheduler", "instructions": "..."}
  -- '' for inbound calls and for outbound calls placed before this migration.
