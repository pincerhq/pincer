"""Expense tracker skill - track personal expenses using SQLite."""

import os
import sqlite3
from datetime import datetime, timedelta


def _db_path() -> str:
    return os.environ.get("PINCER_DB_PATH") or os.path.expanduser("~/.pincer/pincer.db")


def _ensure_expenses_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pincer_user_id TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()


def log_expense(
    amount: float,
    user_id: str,
    currency: str = "USD",
    category: str = "general",
    description: str = "",
) -> dict:
    """Log a new expense. Returns status, expense_id, amount, currency, category."""
    try:
        if amount <= 0:
            return {"error": "amount must be greater than 0"}
        amount = float(amount)
    except (TypeError, ValueError):
        return {"error": "amount must be a valid positive number"}

    try:
        db_path = _db_path()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(db_path)
        _ensure_expenses_table(conn)
        cursor = conn.execute(
            """
            INSERT INTO expenses (pincer_user_id, amount, currency, category, description, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, amount, currency, category, description, datetime.utcnow().isoformat()),
        )
        expense_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return {
            "status": "ok",
            "expense_id": expense_id,
            "amount": amount,
            "currency": currency,
            "category": category,
        }
    except Exception as e:
        return {"error": str(e)}


def expense_report(user_id: str, period: str = "month") -> dict:
    """Get expense report for user. Period: month (30 days), week (7 days), today."""
    try:
        conn = sqlite3.connect(_db_path())
        _ensure_expenses_table(conn)
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
            SELECT amount, currency, category FROM expenses
            WHERE pincer_user_id = ? AND created_at >= ?
            """,
            (user_id, start_str),
        ).fetchall()
        conn.close()

        total = sum(r[0] for r in rows)
        by_category: dict[str, float] = {}
        currency = "USD"
        for amount, curr, cat in rows:
            currency = curr
            by_category[cat] = by_category.get(cat, 0) + amount

        return {
            "period": period,
            "total": round(total, 2),
            "currency": currency,
            "count": len(rows),
            "by_category": {k: round(v, 2) for k, v in by_category.items()},
        }
    except Exception as e:
        return {"error": str(e)}
