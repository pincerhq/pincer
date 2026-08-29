-- Per-call conversation analytics: talk time, interruptions, sentiment.
-- (The spec calls this 010; 010 would sort before the 011-014 migrations that
--  already shipped, so it lands as 015 in file order.)
-- Applied automatically by pincer.voice.retention.ensure_voice_tables; kept
-- here as the human-readable record of the schema change.
CREATE TABLE IF NOT EXISTS call_analytics (
  call_sid TEXT PRIMARY KEY REFERENCES voice_calls(call_sid),
  agent_speech_ms INTEGER,          -- NULL when no conversation happened
  caller_speech_ms INTEGER,
  silence_ms INTEGER,
  overlap_ms INTEGER,               -- both parties speaking at once
  interruptions INTEGER DEFAULT 0,
  talk_ratio REAL,                  -- agent / (agent + caller), [0,1]
  method TEXT NOT NULL,             -- exact (media_streams) | estimated (conversation_relay)
  sentiment TEXT,                   -- positive | neutral | negative | mixed | NULL
  sentiment_trajectory TEXT,        -- improving | stable | declining | NULL
  sentiment_rationale TEXT,         -- one grounded sentence; NULLed by the retention purge
  sentiment_reason TEXT DEFAULT '', -- why sentiment is absent: too_short | not_conversed | extraction_failed
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_call_analytics_sentiment ON call_analytics(sentiment);
