"""Skill: phone_contacts — manage phone contacts for voice calling."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

_DB_PATH: str | None = None


def _get_db_path() -> str:
    global _DB_PATH  # noqa: PLW0603
    if _DB_PATH:
        return _DB_PATH
    data_dir = os.environ.get("PINCER_DATA_DIR", str(Path.home() / ".pincer"))
    db_path = str(Path(data_dir) / "pincer.db")
    _DB_PATH = db_path
    return db_path


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS phone_contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            phone_number TEXT NOT NULL,
            category TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_phone_contacts_name
        ON phone_contacts(name COLLATE NOCASE)
    """)
    conn.commit()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_get_db_path())
    conn.row_factory = sqlite3.Row
    _ensure_table(conn)
    return conn


def add_contact(
    name: str,
    phone_number: str,
    category: str = "",
    notes: str = "",
) -> dict:
    """Add a new phone contact."""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO phone_contacts (name, phone_number, category, notes) VALUES (?, ?, ?, ?)",
            (name, phone_number, category, notes),
        )
        conn.commit()
        return {"status": "ok", "message": f"Contact '{name}' added with number {phone_number}"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()


def search_contacts(query: str) -> dict:
    """Search contacts by name, number, category, or notes."""
    conn = _get_conn()
    try:
        pattern = f"%{query}%"
        rows = conn.execute(
            "SELECT name, phone_number, category, notes FROM phone_contacts "
            "WHERE name LIKE ? OR phone_number LIKE ? OR category LIKE ? OR notes LIKE ? "
            "ORDER BY name LIMIT 20",
            (pattern, pattern, pattern, pattern),
        ).fetchall()
        contacts = [
            {
                "name": r["name"],
                "phone_number": r["phone_number"],
                "category": r["category"],
                "notes": r["notes"],
            }
            for r in rows
        ]
        return {"contacts": contacts, "count": len(contacts)}
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()


def list_contacts(category: str = "", limit: int = 20) -> dict:
    """List all contacts, optionally filtered by category."""
    conn = _get_conn()
    try:
        if category:
            rows = conn.execute(
                "SELECT name, phone_number, category, notes FROM phone_contacts "
                "WHERE category LIKE ? ORDER BY name LIMIT ?",
                (f"%{category}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name, phone_number, category, notes FROM phone_contacts "
                "ORDER BY name LIMIT ?",
                (limit,),
            ).fetchall()
        contacts = [
            {
                "name": r["name"],
                "phone_number": r["phone_number"],
                "category": r["category"],
                "notes": r["notes"],
            }
            for r in rows
        ]
        return {"contacts": contacts, "count": len(contacts)}
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()


def update_contact(
    name: str,
    new_name: str = "",
    new_phone_number: str = "",
    new_category: str = "",
    new_notes: str = "",
) -> dict:
    """Update an existing contact's details."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM phone_contacts WHERE name LIKE ? LIMIT 1",
            (f"%{name}%",),
        ).fetchone()
        if not row:
            return {"error": f"Contact '{name}' not found"}

        updates = []
        params: list = []
        if new_name:
            updates.append("name = ?")
            params.append(new_name)
        if new_phone_number:
            updates.append("phone_number = ?")
            params.append(new_phone_number)
        if new_category:
            updates.append("category = ?")
            params.append(new_category)
        if new_notes:
            updates.append("notes = ?")
            params.append(new_notes)

        if not updates:
            return {"error": "No fields to update"}

        params.append(row["id"])
        conn.execute(
            f"UPDATE phone_contacts SET {', '.join(updates)} WHERE id = ?",  # noqa: S608
            params,
        )
        conn.commit()
        return {"status": "ok", "message": f"Contact '{name}' updated"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()


def delete_contact(name: str) -> dict:
    """Delete a contact by name."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, name FROM phone_contacts WHERE name LIKE ? LIMIT 1",
            (f"%{name}%",),
        ).fetchone()
        if not row:
            return {"error": f"Contact '{name}' not found"}

        conn.execute("DELETE FROM phone_contacts WHERE id = ?", (row["id"],))
        conn.commit()
        return {"status": "ok", "message": f"Contact '{row['name']}' deleted"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()
