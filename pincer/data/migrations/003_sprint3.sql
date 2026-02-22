-- Sprint 3: Cross-channel identity

CREATE TABLE IF NOT EXISTS identity_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pincer_user_id TEXT NOT NULL UNIQUE,
    telegram_user_id INTEGER,
    whatsapp_phone TEXT,
    display_name TEXT,
    preferred_channel TEXT DEFAULT 'telegram',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_telegram
    ON identity_map(telegram_user_id) WHERE telegram_user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_whatsapp
    ON identity_map(whatsapp_phone) WHERE whatsapp_phone IS NOT NULL;

-- Sprint 3: Scheduled tasks

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pincer_user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    cron_expr TEXT NOT NULL,
    action TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'telegram',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    next_run_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (pincer_user_id) REFERENCES identity_map(pincer_user_id)
);

CREATE INDEX IF NOT EXISTS idx_schedules_next_run
    ON schedules(next_run_at) WHERE enabled = 1;

-- Sprint 3: Briefing customization per user

CREATE TABLE IF NOT EXISTS briefing_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pincer_user_id TEXT NOT NULL UNIQUE,
    sections TEXT NOT NULL DEFAULT '["weather","calendar","email","news"]',
    custom_sections TEXT DEFAULT '[]',
    weather_location TEXT DEFAULT 'Berlin,DE',
    news_topics TEXT DEFAULT '["technology","business"]',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (pincer_user_id) REFERENCES identity_map(pincer_user_id)
);

-- Sprint 3: Event trigger deduplication log

CREATE TABLE IF NOT EXISTS event_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_type TEXT NOT NULL,
    trigger_key TEXT NOT NULL,
    pincer_user_id TEXT NOT NULL,
    processed_at TEXT DEFAULT (datetime('now')),
    result TEXT,
    UNIQUE(trigger_type, trigger_key)
);
