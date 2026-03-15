"""Pomodoro skill - track pomodoro timer sessions using SQLite."""

import os
import sqlite3
from datetime import datetime, timedelta


def _db_path() -> str:
    return os.environ.get("PINCER_DB_PATH") or os.path.expanduser("~/.pincer/pincer.db")


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pomodoro_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pincer_user_id TEXT NOT NULL,
            task TEXT NOT NULL,
            duration_min INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            completed INTEGER DEFAULT 1
        )
    """)
    conn.commit()


def start_pomodoro(
    user_id: str,
    task: str = "Focus session",
    duration_min: int = 25,
) -> dict:
    """Start a new pomodoro session. Returns status, session_id, task, duration_min, started_at."""
    try:
        db_path = _db_path()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(db_path)
        _ensure_table(conn)
        started_at = datetime.utcnow().isoformat()
        cursor = conn.execute(
            """
            INSERT INTO pomodoro_sessions (pincer_user_id, task, duration_min, started_at, completed)
            VALUES (?, ?, ?, ?, 1)
            """,
            (user_id, task, duration_min, started_at),
        )
        session_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return {
            "status": "ok",
            "session_id": session_id,
            "task": task,
            "duration_min": duration_min,
            "started_at": started_at,
        }
    except Exception as e:
        return {"error": str(e)}


def pomodoro_stats(user_id: str, period: str = "today") -> dict:
    """Get pomodoro stats. Period: today, week, month."""
    try:
        conn = sqlite3.connect(_db_path())
        _ensure_table(conn)
        now = datetime.utcnow()

        if period == "today":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            start = now - timedelta(days=7)
        else:
            start = now - timedelta(days=30)

        start_str = start.isoformat()
        rows = conn.execute(
            """
            SELECT task, duration_min FROM pomodoro_sessions
            WHERE pincer_user_id = ? AND started_at >= ?
            """,
            (user_id, start_str),
        ).fetchall()
        conn.close()

        total_sessions = len(rows)
        total_minutes = sum(r[1] for r in rows)
        by_task: dict[str, tuple[int, int]] = {}
        for task, dur in rows:
            if task not in by_task:
                by_task[task] = (0, 0)
            cnt, mins = by_task[task]
            by_task[task] = (cnt + 1, mins + dur)

        tasks = [{"task": t, "count": c, "total_minutes": m} for t, (c, m) in by_task.items()]
        return {
            "period": period,
            "total_sessions": total_sessions,
            "total_minutes": total_minutes,
            "tasks": tasks,
        }
    except Exception as e:
        return {"error": str(e)}
