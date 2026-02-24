-- Sprint 4: Discord + Skills System

-- Add Discord user ID to identity_map (idempotent via column check)
-- Note: SQLite ALTER TABLE ADD COLUMN is idempotent if column doesn't exist,
-- but will error if it does. Apply via try/except in Python.
ALTER TABLE identity_map ADD COLUMN discord_user_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_discord
    ON identity_map(discord_user_id) WHERE discord_user_id IS NOT NULL;

-- Skill registry
CREATE TABLE IF NOT EXISTS skill_registry (
    skill_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    description TEXT,
    author TEXT DEFAULT 'unknown',
    safety_score INTEGER DEFAULT 100,
    install_path TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    installed_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Expense tracker (skill data)
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pincer_user_id TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    category TEXT NOT NULL DEFAULT 'general',
    description TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (pincer_user_id) REFERENCES identity_map(pincer_user_id)
);

CREATE INDEX IF NOT EXISTS idx_expenses_user
    ON expenses(pincer_user_id, created_at);

-- Habit tracker (skill data)
CREATE TABLE IF NOT EXISTS habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pincer_user_id TEXT NOT NULL,
    habit_name TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(pincer_user_id, habit_name)
);

CREATE TABLE IF NOT EXISTS habit_checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    habit_id INTEGER NOT NULL,
    checked_at TEXT DEFAULT (datetime('now')),
    note TEXT DEFAULT '',
    FOREIGN KEY (habit_id) REFERENCES habits(id) ON DELETE CASCADE,
    UNIQUE(habit_id, date(checked_at))
);

-- Pomodoro sessions (skill data)
CREATE TABLE IF NOT EXISTS pomodoro_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pincer_user_id TEXT NOT NULL,
    task TEXT NOT NULL DEFAULT 'Focus session',
    duration_min INTEGER NOT NULL DEFAULT 25,
    started_at TEXT DEFAULT (datetime('now')),
    completed INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_pomodoro_user
    ON pomodoro_sessions(pincer_user_id, started_at);

-- Discord thread -> session mapping
CREATE TABLE IF NOT EXISTS discord_threads (
    thread_id TEXT PRIMARY KEY,
    guild_id TEXT,
    pincer_user_id TEXT,
    session_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
