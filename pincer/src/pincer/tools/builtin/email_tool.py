"""
Async email tools — IMAP (read/search/triage/cleanup) and SMTP (send).

Supports Gmail (app password), Outlook, generic IMAP/SMTP.
Uses aiosmtplib (async SMTP) and aioimaplib (async IMAP).

Tools are registered in cli.py via tools.register().
"""

from __future__ import annotations

import email
import email.policy
import logging
import re
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any

from pincer.config import get_settings

logger = logging.getLogger(__name__)

# Regex for parsing IMAP LIST response lines:  (\Flags) "delimiter" "FolderName"
_LIST_RE = re.compile(rb'\(([^)]*)\)\s+"(.)"\s+"?([^"]*)"?')

_TRASH_ATTRS = {"\\trash"}
_SPAM_ATTRS = {"\\junk"}

_TRASH_FALLBACKS = ["[Gmail]/Trash", "Trash", "Deleted Items", "Deleted Messages"]
_SPAM_FALLBACKS = ["[Gmail]/Spam", "Spam", "Junk", "Junk Email", "Bulk Mail"]


# ── IMAP Helpers ─────────────────────────────────

async def _get_imap_client():  # type: ignore[no-untyped-def]
    """Create and authenticate an async IMAP client."""
    from aioimaplib import IMAP4_SSL

    settings = get_settings()
    if not settings.email_username or not settings.email_password.get_secret_value():
        raise RuntimeError("Email credentials not configured")

    client = IMAP4_SSL(
        host=settings.email_imap_host,
        port=settings.email_imap_port,
        timeout=30,
    )
    await client.wait_hello_from_server()
    resp = await client.login(
        settings.email_username,
        settings.email_password.get_secret_value(),
    )
    if resp.result != "OK":
        raise RuntimeError(f"IMAP login failed: {resp.result}")
    return client


def _parse_email_headers(raw_data: bytes) -> dict[str, str]:
    msg = email.message_from_bytes(raw_data, policy=email.policy.default)
    return {
        "from": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "subject": str(msg.get("Subject", "(No subject)")),
        "date": str(msg.get("Date", "")),
        "message_id": str(msg.get("Message-ID", "")),
    }


def _extract_text_body(raw_data: bytes, max_chars: int = 2000) -> str:
    msg = email.message_from_bytes(raw_data, policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    if body:
        content = body.get_content()
        if isinstance(content, str):
            return content[:max_chars] + ("..." if len(content) > max_chars else "")
    return "(No readable content)"


def _parse_list_response(data: list[Any]) -> list[tuple[list[str], str]]:
    """Parse IMAP LIST response lines into (attributes, folder_name) pairs."""
    results: list[tuple[list[str], str]] = []
    for item in data:
        if not isinstance(item, bytes) or not item.strip():
            continue
        m = _LIST_RE.match(item)
        if m:
            attrs = m.group(1).decode(errors="replace").split()
            raw_name = m.group(3).decode(errors="replace").strip('"')
            results.append((attrs, raw_name))
    return results


async def _find_folder_by_attr(
    client: Any, target_attrs: set[str], fallbacks: list[str],
) -> str | None:
    """Find a special-use folder by IMAP attributes, falling back to common names."""
    try:
        status, data = await client.list('""', '*')
        if status != "OK":
            return fallbacks[0] if fallbacks else None

        parsed = _parse_list_response(data)

        for attrs, name in parsed:
            lower_attrs = {a.lower() for a in attrs}
            if lower_attrs & target_attrs:
                return name

        known_names = {name for _, name in parsed}
        for fb in fallbacks:
            if fb in known_names:
                return fb
    except Exception:
        logger.debug("Failed to discover special folder", exc_info=True)

    return fallbacks[0] if fallbacks else None


def _extract_fetch_literal(lines: list[Any]) -> bytes | None:
    """Extract the first literal-data bytearray from an aioimaplib FETCH response."""
    for item in lines:
        if isinstance(item, bytearray):
            return bytes(item)
    return None


# ── Tool: email_check ────────────────────────────

async def email_check(limit: int = 10, folder: str = "INBOX", status: str = "UNSEEN") -> str:
    """Check emails in a folder. Returns a formatted summary string with UIDs."""
    client = None
    try:
        client = await _get_imap_client()
        await client.select(folder)

        criteria = status.upper()
        search_status, data = await client.uid_search(criteria)
        if search_status != "OK":
            return f"IMAP search failed: {search_status}"

        uids = data[0].decode().split()
        if not uids:
            if criteria == "UNSEEN":
                return f"No unread emails in {folder}."
            return f"No emails in {folder} (filter: {criteria})."

        recent_uids = uids[-limit:]
        if criteria == "UNSEEN":
            header = f"Unread: {len(uids)} email(s) in {folder}. Showing {len(recent_uids)}:"
        else:
            header = f"Total: {len(uids)} email(s) in {folder}. Showing {len(recent_uids)}:"
        lines = [header + "\n"]

        for uid in reversed(recent_uids):
            fetch_status, fetch_data = await client.uid(
                "fetch", uid,
                "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])",
            )
            if fetch_status == "OK" and fetch_data:
                raw = _extract_fetch_literal(fetch_data)
                if raw:
                    h = _parse_email_headers(raw)
                    lines.append(
                        f"- UID: {uid}\n"
                        f"  From: {h['from']}\n"
                        f"  Subject: {h['subject']}\n"
                        f"  Date: {h['date']}"
                    )

        return "\n".join(lines)

    except Exception as e:
        logger.error("email_check error: %s", e, exc_info=True)
        return f"Error checking email: {e}"
    finally:
        if client:
            try:
                await client.logout()
            except Exception:
                pass


# ── Tool: email_send ─────────────────────────────

async def email_send(to: str, subject: str, body: str, cc: str = "") -> str:
    """Send an email via SMTP. Returns a status message."""
    try:
        import aiosmtplib

        settings = get_settings()
        message = EmailMessage()
        message["From"] = settings.email_from or settings.email_username
        message["To"] = to
        message["Subject"] = subject
        if cc:
            message["Cc"] = cc
        message.set_content(body)

        await aiosmtplib.send(
            message,
            hostname=settings.email_smtp_host,
            port=settings.email_smtp_port,
            username=settings.email_username,
            password=settings.email_password.get_secret_value(),
            use_tls=settings.email_smtp_port == 465,
            start_tls=settings.email_smtp_port == 587,
        )

        logger.info("Email sent to %s: %s", to, subject)
        return f"Email sent to {to} — Subject: {subject}"

    except Exception as e:
        logger.error("email_send error: %s", e, exc_info=True)
        return f"Error sending email: {e}"


# ── Tool: email_search ───────────────────────────

async def email_search(
    query: str, sender: str = "", days_back: int = 30, limit: int = 10,
    folder: str = "INBOX",
) -> str:
    """Search emails by keyword/sender. Returns formatted results with UIDs."""
    client = None
    try:
        client = await _get_imap_client()
        await client.select(folder)

        since_date = (
            datetime.now(timezone.utc) - timedelta(days=days_back)
        ).strftime("%d-%b-%Y")
        search_parts = [f"SINCE {since_date}"]
        if sender:
            search_parts.append(f'FROM "{sender}"')
        search_parts.append(f'TEXT "{query}"')

        status, data = await client.uid_search(" ".join(search_parts))
        if status != "OK":
            return f"IMAP search failed: {status}"

        uids = data[0].decode().split()
        if not uids:
            return f"No emails matching '{query}'."

        recent_uids = uids[-limit:]
        lines = [f"Found {len(uids)} email(s) matching '{query}'. Showing {len(recent_uids)}:\n"]

        for uid in reversed(recent_uids):
            fetch_status, fetch_data = await client.uid(
                "fetch", uid,
                "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])",
            )
            if fetch_status == "OK" and fetch_data:
                raw = _extract_fetch_literal(fetch_data)
                if raw:
                    h = _parse_email_headers(raw)
                    lines.append(
                        f"- UID: {uid}\n"
                        f"  From: {h['from']}\n"
                        f"  Subject: {h['subject']}\n"
                        f"  Date: {h['date']}"
                    )

        return "\n".join(lines)

    except Exception as e:
        logger.error("email_search error: %s", e, exc_info=True)
        return f"Error searching email: {e}"
    finally:
        if client:
            try:
                await client.logout()
            except Exception:
                pass


# ── Tool: email_read ─────────────────────────────

async def email_read(uid: str, folder: str = "INBOX", max_chars: int = 4000) -> str:
    """Read the full content of an email by UID."""
    client = None
    try:
        client = await _get_imap_client()
        await client.select(folder)

        fetch_status, fetch_data = await client.uid("fetch", uid, "(BODY.PEEK[])")
        if fetch_status != "OK":
            return f"Failed to fetch email UID {uid}: {fetch_status}"

        raw = _extract_fetch_literal(fetch_data)
        if raw:
            h = _parse_email_headers(raw)
            body = _extract_text_body(raw, max_chars=max_chars)
            return (
                f"UID: {uid}\n"
                f"From: {h['from']}\n"
                f"To: {h['to']}\n"
                f"Subject: {h['subject']}\n"
                f"Date: {h['date']}\n"
                f"Message-ID: {h['message_id']}\n"
                f"\n--- Body ---\n{body}"
            )

        return f"Email UID {uid} not found or empty."

    except Exception as e:
        logger.error("email_read error: %s", e, exc_info=True)
        return f"Error reading email: {e}"
    finally:
        if client:
            try:
                await client.logout()
            except Exception:
                pass


# ── Tool: email_list_folders ─────────────────────

async def email_list_folders() -> str:
    """List all available IMAP folders."""
    client = None
    try:
        client = await _get_imap_client()
        status, data = await client.list('""', '*')
        if status != "OK":
            return f"Failed to list folders: {status}"

        parsed = _parse_list_response(data)
        if not parsed:
            return "No folders found."

        lines = [f"Available folders ({len(parsed)}):\n"]
        for attrs, name in parsed:
            attr_str = ", ".join(attrs) if attrs else ""
            lines.append(f"- {name}" + (f"  [{attr_str}]" if attr_str else ""))

        return "\n".join(lines)

    except Exception as e:
        logger.error("email_list_folders error: %s", e, exc_info=True)
        return f"Error listing folders: {e}"
    finally:
        if client:
            try:
                await client.logout()
            except Exception:
                pass


# ── Tool: email_mark ─────────────────────────────

_MARK_ACTIONS: dict[str, tuple[str, str]] = {
    "read": ("+FLAGS", "(\\Seen)"),
    "unread": ("-FLAGS", "(\\Seen)"),
    "flag": ("+FLAGS", "(\\Flagged)"),
    "unflag": ("-FLAGS", "(\\Flagged)"),
}


async def email_mark(uids: str, action: str, folder: str = "INBOX") -> str:
    """Mark one or more emails (comma-separated UIDs). Actions: read, unread, flag, unflag."""
    if action not in _MARK_ACTIONS:
        return f"Invalid action '{action}'. Choose from: {', '.join(_MARK_ACTIONS)}"

    client = None
    try:
        client = await _get_imap_client()
        await client.select(folder)

        cmd, flags = _MARK_ACTIONS[action]
        uid_list = [u.strip() for u in uids.split(",")]

        marked = 0
        for uid_val in uid_list:
            status, _ = await client.uid("store", uid_val, cmd, flags)
            if status == "OK":
                marked += 1

        return f"Marked {marked}/{len(uid_list)} email(s) as '{action}'."

    except Exception as e:
        logger.error("email_mark error: %s", e, exc_info=True)
        return f"Error marking emails: {e}"
    finally:
        if client:
            try:
                await client.logout()
            except Exception:
                pass


# ── Tool: email_move ─────────────────────────────

async def email_move(uids: str, destination: str, folder: str = "INBOX") -> str:
    """Move one or more emails (comma-separated UIDs) to a destination folder."""
    client = None
    try:
        client = await _get_imap_client()
        await client.select(folder)

        uid_list = [u.strip() for u in uids.split(",")]
        moved = 0
        for uid_val in uid_list:
            status, _ = await client.uid("copy", uid_val, f'"{destination}"')
            if status == "OK":
                await client.uid("store", uid_val, "+FLAGS", "(\\Deleted)")
                moved += 1

        if moved:
            await client.expunge()

        return f"Moved {moved}/{len(uid_list)} email(s) to '{destination}'."

    except Exception as e:
        logger.error("email_move error: %s", e, exc_info=True)
        return f"Error moving emails: {e}"
    finally:
        if client:
            try:
                await client.logout()
            except Exception:
                pass


# ── Tool: email_trash ────────────────────────────

async def email_trash(uids: str, folder: str = "INBOX") -> str:
    """Move one or more emails (comma-separated UIDs) to the Trash folder."""
    client = None
    try:
        client = await _get_imap_client()

        trash_folder = await _find_folder_by_attr(client, _TRASH_ATTRS, _TRASH_FALLBACKS)
        if not trash_folder:
            return "Could not find Trash folder. Use email_list_folders to check available folders."

        await client.select(folder)

        uid_list = [u.strip() for u in uids.split(",")]
        trashed = 0
        for uid_val in uid_list:
            status, _ = await client.uid("copy", uid_val, f'"{trash_folder}"')
            if status == "OK":
                await client.uid("store", uid_val, "+FLAGS", "(\\Deleted)")
                trashed += 1

        if trashed:
            await client.expunge()

        return f"Trashed {trashed}/{len(uid_list)} email(s) (moved to '{trash_folder}')."

    except Exception as e:
        logger.error("email_trash error: %s", e, exc_info=True)
        return f"Error trashing emails: {e}"
    finally:
        if client:
            try:
                await client.logout()
            except Exception:
                pass


# ── Tool: email_empty_folder ─────────────────────

async def email_empty_folder(folder: str) -> str:
    """Empty all messages from a folder (e.g., Spam, Trash). Cannot empty INBOX."""
    if folder.upper() == "INBOX":
        return "Refusing to empty INBOX for safety. Specify a different folder."

    client = None
    try:
        client = await _get_imap_client()

        status, _ = await client.select(folder)
        if status != "OK":
            return f"Could not select folder '{folder}'. Check folder name with email_list_folders."

        status, data = await client.uid_search("ALL")
        if status != "OK":
            return f"Failed to search folder '{folder}': {status}"

        uids = data[0].decode().split()
        if not uids:
            return f"Folder '{folder}' is already empty."

        count = len(uids)
        uid_set = ",".join(uids)
        await client.uid("store", uid_set, "+FLAGS", "(\\Deleted)")
        await client.expunge()

        return f"Emptied folder '{folder}': {count} message(s) permanently deleted."

    except Exception as e:
        logger.error("email_empty_folder error: %s", e, exc_info=True)
        return f"Error emptying folder: {e}"
    finally:
        if client:
            try:
                await client.logout()
            except Exception:
                pass
