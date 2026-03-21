"""Habit tracker skill - track daily habits using SQLite."""

import os
import sqlite3
from datetime import datetime, timedelta


def _db_path() -> str:
    return os.environ.get("PINCER_DB_PATH") or os.path.expanduser("~/.pincer/pincer.db")


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pincer_user_id TEXT NOT NULL,
            habit_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(pincer_user_id, habit_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS habit_checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_id INTEGER NOT NULL REFERENCES habits(id),
            checked_at TEXT NOT NULL,
            checkin_date TEXT NOT NULL,
            note TEXT,
            UNIQUE(habit_id, checkin_date)
        )
    """)
    conn.commit()


def add_habit(user_id: str, habit_name: str) -> dict:
    """Add a new habit. Handles duplicate gracefully."""
    if not habit_name or not habit_name.strip():
        return {"error": "habit_name cannot be empty"}

    try:
        db_path = _db_path()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(db_path)
        _ensure_tables(conn)
        try:
            conn.execute(
                """
                INSERT INTO habits (pincer_user_id, habit_name, created_at)
                VALUES (?, ?, ?)
                """,
                (user_id, habit_name.strip(), datetime.utcnow().isoformat()),
            )
            conn.commit()
            conn.close()
            return {"status": "ok", "habit_name": habit_name.strip()}
        except sqlite3.IntegrityError:
            conn.close()
            return {"status": "already_exists", "habit_name": habit_name.strip()}
    except Exception as e:
        return {"error": str(e)}


def _get_streak(conn: sqlite3.Connection, habit_id: int) -> int:
    """Count consecutive days of check-ins ending today."""
    rows = conn.execute(
        """
        SELECT checkin_date FROM habit_checkins
        WHERE habit_id = ? ORDER BY checkin_date DESC
        """,
        (habit_id,),
    ).fetchall()
    if not rows:
        return 0
    dates = [r[0] for r in rows]
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    if dates[0] != today_str:
        return 0
    streak = 0
    expected = datetime.utcnow()
    for d in dates:
        if d == expected.strftime("%Y-%m-%d"):
            streak += 1
            expected -= timedelta(days=1)
        else:
            break
    return streak


def checkin(user_id: str, habit_name: str, note: str = "") -> dict:
    """Record today's check-in. Returns status, habit_name, date, streak."""
    try:
        conn = sqlite3.connect(_db_path())
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT id FROM habits WHERE pincer_user_id = ? AND habit_name = ?",
            (user_id, habit_name.strip()),
        ).fetchone()
        if not row:
            conn.close()
            return {"error": f"habit '{habit_name}' not found"}

        habit_id = row[0]
        today = datetime.utcnow().strftime("%Y-%m-%d")
        now_iso = datetime.utcnow().isoformat()

        try:
            conn.execute(
                "INSERT INTO habit_checkins (habit_id, checked_at, checkin_date, note) VALUES (?, ?, ?, ?)",
                (habit_id, now_iso, today, note),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return {"status": "already_checked_in", "habit_name": habit_name, "date": today}

        streak = _get_streak(conn, habit_id)
        conn.close()
        return {"status": "ok", "habit_name": habit_name, "date": today, "streak": streak}
    except Exception as e:
        return {"error": str(e)}


def habit_status(user_id: str) -> dict:
    """List all habits with today's status and current streak."""
    try:
        conn = sqlite3.connect(_db_path())
        _ensure_tables(conn)
        habits = conn.execute(
            "SELECT id, habit_name FROM habits WHERE pincer_user_id = ?",
            (user_id,),
        ).fetchall()
        today = datetime.utcnow().strftime("%Y-%m-%d")
        result_habits = []
        for habit_id, habit_name in habits:
            checked = conn.execute(
                """
                SELECT 1 FROM habit_checkins
                WHERE habit_id = ? AND checkin_date = ?
                """,
                (habit_id, today),
            ).fetchone()
            streak = _get_streak(conn, habit_id)
            total = conn.execute(
                "SELECT COUNT(*) FROM habit_checkins WHERE habit_id = ?",
                (habit_id,),
            ).fetchone()[0]
            result_habits.append(
                {
                    "name": habit_name,
                    "checked_today": bool(checked),
                    "streak": streak,
                    "total_checkins": total,
                }
            )
        conn.close()
        return {"habits": result_habits}
    except Exception as e:
        return {"error": str(e)}
