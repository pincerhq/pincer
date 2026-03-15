-- Sprint 7: Voice Calling System (v0.7.2)
-- Adds voice call sessions, transcripts, actions, and phone contacts tables.

-- Voice call sessions
CREATE TABLE IF NOT EXISTS voice_calls (
    id TEXT PRIMARY KEY,               -- Twilio Call SID
    user_id TEXT NOT NULL,
    direction TEXT NOT NULL,            -- 'inbound' | 'outbound'
    caller_number TEXT NOT NULL,
    target_number TEXT,                 -- for outbound calls
    target_name TEXT,                   -- human-friendly name
    purpose TEXT,                       -- user's stated purpose
    status TEXT NOT NULL,               -- state machine phase
    engine TEXT NOT NULL,               -- 'conversation_relay' | 'media_streams'
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    duration_seconds INTEGER,
    recording_url TEXT,
    recording_consent BOOLEAN DEFAULT FALSE,
    session_id TEXT,
    metadata_json TEXT
);

-- Call transcripts (real-time, append-only)
CREATE TABLE IF NOT EXISTS call_transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL REFERENCES voice_calls(id),
    speaker TEXT NOT NULL,              -- 'agent' | 'caller' | 'provider' | 'system'
    text TEXT NOT NULL,
    confidence REAL,
    is_final BOOLEAN DEFAULT TRUE,
    state TEXT,                         -- state machine phase at time of utterance
    timestamp TIMESTAMP NOT NULL
);

-- Call actions (tool calls during voice sessions)
CREATE TABLE IF NOT EXISTS call_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL REFERENCES voice_calls(id),
    action_type TEXT NOT NULL,          -- 'tool_call' | 'dtmf' | 'transfer' | 'confirm'
    tool_name TEXT,
    input_summary TEXT,
    output_summary TEXT,
    user_confirmed BOOLEAN,
    timestamp TIMESTAMP NOT NULL
);

-- Phone contacts (for outbound calling)
CREATE TABLE IF NOT EXISTS phone_contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    category TEXT DEFAULT '',           -- 'doctor' | 'business' | 'personal'
    ivr_tree_json TEXT,                 -- cached IVR navigation for this number
    notes TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Add phone_number to identity_map if not present
-- (SQLite ALTER TABLE ADD COLUMN is idempotent if wrapped in try/catch at app level)

-- Indexes
CREATE INDEX IF NOT EXISTS idx_voice_calls_user ON voice_calls(user_id);
CREATE INDEX IF NOT EXISTS idx_voice_calls_status ON voice_calls(status);
CREATE INDEX IF NOT EXISTS idx_call_transcripts_call ON call_transcripts(call_id);
CREATE INDEX IF NOT EXISTS idx_call_actions_call ON call_actions(call_id);
CREATE INDEX IF NOT EXISTS idx_phone_contacts_user ON phone_contacts(user_id);
CREATE INDEX IF NOT EXISTS idx_phone_contacts_name ON phone_contacts(name COLLATE NOCASE);
