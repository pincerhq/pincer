"""
Async email tools — IMAP (read/search/triage/cleanup) and SMTP (send).

Supports Gmail (app password), Outlook, generic IMAP/SMTP.
Uses aiosmtplib (async SMTP) and aioimaplib (async IMAP).

Tools are registered in cli.py via tools.register().
"""

from __future__ import annotations

import contextlib
import email
import email.policy
import logging
import re
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

from pincer.config import get_settings

logger = logging.getLogger(__name__)

# Regex for parsing IMAP LIST response lines:  (\Flags) "delimiter" "FolderName"
_LIST_RE = re.compile(rb'\(([^)]*)\)\s+"(.)"\s+"?([^"]*)"?')
# Literal size in LIST response, e.g. {12} (may have leading/trailing space)
_LIST_LITERAL_RE = re.compile(rb"^\s*\{(\d+)\}\s*$")

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
    """Parse IMAP LIST response lines into (attributes, folder_name) pairs.
    Handles quoted folder names and LITERAL+ (line ending with {n}, next element bytearray).
    """
    results: list[tuple[list[str], str]] = []
    pending_attrs: list[str] | None = None
    i = 0
    while i < len(data):
        item = data[i]
        raw = bytes(item) if isinstance(item, bytearray) else item if isinstance(item, bytes) else None
        if raw is not None and raw.strip():
            m = _LIST_RE.match(raw)
            if m:
                attrs = m.group(1).decode(errors="replace").split()
                raw_name = m.group(3).decode(errors="replace").strip('"')
                name_is_literal_size = bool(raw_name and _LIST_LITERAL_RE.match(raw_name.strip().encode()))
                if raw_name and not name_is_literal_size:
                    results.append((attrs, raw_name))
                    pending_attrs = None
                else:
                    pending_attrs = attrs
                i += 1
                continue
            if pending_attrs is not None:
                name = raw.decode(errors="replace").strip('"')
                results.append((pending_attrs, name))
                pending_attrs = None
                i += 1
                continue
        if isinstance(item, bytearray) and pending_attrs is None:
            name = bytes(item).decode(errors="replace").strip('"')
            if name:
                results.append(([], name))
        i += 1
    return results


async def _find_folder_by_attr(
    client: Any,
    target_attrs: set[str],
    fallbacks: list[str],
) -> str | None:
    """Find a special-use folder by IMAP attributes, falling back to common names."""
    try:
        status, data = await client.list('""', "*")
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


def _quote_mailbox(folder: str) -> str:
    """Return mailbox string suitable for IMAP SELECT/COPY (quoted when needed per RFC 3501)."""
    if not folder:
        return '""'
    need_quote = any(c in folder for c in " /[]\\")
    if not need_quote:
        return folder
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _parse_search_uids(data: list[Any]) -> list[str]:
    """Extract UID list from SEARCH response lines; only numeric tokens (robust to * SEARCH prefix)."""
    if not data:
        return []
    first = data[0]
    raw = first if isinstance(first, bytes) else bytes(first) if isinstance(first, bytearray) else b""
    tokens = raw.decode(errors="replace").split()
    return [t for t in tokens if t.isdigit()]


# ── Tool: email_check ────────────────────────────


async def email_check(limit: int = 10, folder: str = "INBOX", status: str = "UNSEEN") -> str:
    """Check emails in a folder. Returns a formatted summary string with UIDs."""
    client = None
    try:
        client = await _get_imap_client()
        await client.select(_quote_mailbox(folder))

        criteria = status.upper()
        search_status, data = await client.uid_search(criteria)
        if search_status != "OK":
            return f"IMAP search failed: {search_status}"

        uids = _parse_search_uids(data)
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
                "fetch",
                uid,
                "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])",
            )
            if fetch_status == "OK" and fetch_data:
                raw = _extract_fetch_literal(fetch_data)
                if raw:
                    h = _parse_email_headers(raw)
                    lines.append(f"- UID: {uid}\n  From: {h['from']}\n  Subject: {h['subject']}\n  Date: {h['date']}")

        return "\n".join(lines)

    except Exception as e:
        logger.error("email_check error: %s", e, exc_info=True)
        return f"Error checking email: {e}"
    finally:
        if client:
            with contextlib.suppress(Exception):
                await client.logout()


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
    query: str,
    sender: str = "",
    days_back: int = 30,
    limit: int = 10,
    folder: str = "INBOX",
) -> str:
    """Search emails by keyword/sender. Returns formatted results with UIDs."""
    client = None
    try:
        client = await _get_imap_client()
        await client.select(_quote_mailbox(folder))

        since_date = (datetime.now(UTC) - timedelta(days=days_back)).strftime("%d-%b-%Y")
        search_parts = [f"SINCE {since_date}"]
        if sender:
            search_parts.append(f'FROM "{sender}"')
        search_parts.append(f'TEXT "{query}"')

        status, data = await client.uid_search(" ".join(search_parts))
        if status != "OK":
            return f"IMAP search failed: {status}"

        uids = _parse_search_uids(data)
        if not uids:
            return f"No emails matching '{query}'."

        recent_uids = uids[-limit:]
        lines = [f"Found {len(uids)} email(s) matching '{query}'. Showing {len(recent_uids)}:\n"]

        for uid in reversed(recent_uids):
            fetch_status, fetch_data = await client.uid(
                "fetch",
                uid,
                "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])",
            )
            if fetch_status == "OK" and fetch_data:
                raw = _extract_fetch_literal(fetch_data)
                if raw:
                    h = _parse_email_headers(raw)
                    lines.append(f"- UID: {uid}\n  From: {h['from']}\n  Subject: {h['subject']}\n  Date: {h['date']}")

        return "\n".join(lines)

    except Exception as e:
        logger.error("email_search error: %s", e, exc_info=True)
        return f"Error searching email: {e}"
    finally:
        if client:
            with contextlib.suppress(Exception):
                await client.logout()


# ── Tool: email_read ─────────────────────────────


async def email_read(uid: str, folder: str = "INBOX", max_chars: int = 4000) -> str:
    """Read the full content of an email by UID."""
    client = None
    try:
        client = await _get_imap_client()
        await client.select(_quote_mailbox(folder))

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
            with contextlib.suppress(Exception):
                await client.logout()


# ── Tool: email_list_folders ─────────────────────


async def email_list_folders() -> str:
    """List all available IMAP folders."""
    client = None
    try:
        client = await _get_imap_client()
        status, data = await client.list('""', "*")
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
            with contextlib.suppress(Exception):
                await client.logout()


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
        await client.select(_quote_mailbox(folder))

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
            with contextlib.suppress(Exception):
                await client.logout()


# ── Tool: email_move ─────────────────────────────


async def email_move(uids: str, destination: str, folder: str = "INBOX") -> str:
    """Move one or more emails (comma-separated UIDs) to a destination folder."""
    client = None
    try:
        client = await _get_imap_client()
        await client.select(_quote_mailbox(folder))

        uid_list = [u.strip() for u in uids.split(",")]
        moved = 0
        for uid_val in uid_list:
            status, _ = await client.uid("copy", uid_val, _quote_mailbox(destination))
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
            with contextlib.suppress(Exception):
                await client.logout()


# ── Tool: email_trash ────────────────────────────


async def email_trash(uids: str, folder: str = "INBOX") -> str:
    """Move one or more emails (comma-separated UIDs) to the Trash folder."""
    client = None
    try:
        client = await _get_imap_client()

        trash_folder = await _find_folder_by_attr(client, _TRASH_ATTRS, _TRASH_FALLBACKS)
        if not trash_folder:
            return "Could not find Trash folder. Use email_list_folders to check available folders."

        await client.select(_quote_mailbox(folder))

        uid_list = [u.strip() for u in uids.split(",")]
        trashed = 0
        for uid_val in uid_list:
            status, _ = await client.uid("copy", uid_val, _quote_mailbox(trash_folder))
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
            with contextlib.suppress(Exception):
                await client.logout()


# ── Tool: email_empty_folder ─────────────────────


async def email_empty_folder(folder: str) -> str:
    """Empty all messages from a folder (e.g., Spam, Trash). Cannot empty INBOX."""
    if folder.upper() == "INBOX":
        return "Refusing to empty INBOX for safety. Specify a different folder."

    client = None
    try:
        client = await _get_imap_client()

        status, _ = await client.select(_quote_mailbox(folder))
        if status != "OK":
            logger.warning("email_empty_folder: SELECT '%s' failed: %s", folder, status)
            return f"Could not select folder '{folder}'. Check folder name with email_list_folders."

        status, data = await client.uid_search("ALL")
        if status != "OK":
            logger.warning("email_empty_folder: SEARCH in '%s' failed: %s", folder, status)
            return f"Failed to search folder '{folder}': {status}"

        uids = _parse_search_uids(data)
        if not uids:
            return f"Folder '{folder}' is already empty."

        count = len(uids)
        logger.info("email_empty_folder: marking %d message(s) in '%s' as deleted", count, folder)

        deleted = 0
        for uid_val in uids:
            st, _ = await client.uid("store", uid_val, "+FLAGS", "(\\Deleted)")
            if st == "OK":
                deleted += 1

        if deleted:
            await client.expunge()
            logger.info("email_empty_folder: expunged %d/%d in '%s'", deleted, count, folder)
        else:
            logger.warning("email_empty_folder: STORE failed for all %d UIDs in '%s'", count, folder)

        return f"Emptied folder '{folder}': deleted {deleted}/{count} message(s)."

    except Exception as e:
        logger.error("email_empty_folder error: %s", e, exc_info=True)
        return f"Error emptying folder: {e}"
    finally:
        if client:
            with contextlib.suppress(Exception):
                await client.logout()
