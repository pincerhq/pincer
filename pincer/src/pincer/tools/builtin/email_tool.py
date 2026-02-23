"""
Async email tools — IMAP (read/search) and SMTP (send).

Supports Gmail (app password), Outlook, generic IMAP/SMTP.
Uses aiosmtplib (async SMTP) and aioimaplib (async IMAP).

Tools are registered in cli.py via tools.register().
"""

from __future__ import annotations

import email
import email.policy
import logging
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any

from pincer.config import get_settings

logger = logging.getLogger(__name__)


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


# ── Tool: email_check ────────────────────────────

async def email_check(limit: int = 10, folder: str = "INBOX") -> str:
    """Check unread emails. Returns a formatted summary string."""
    client = None
    try:
        client = await _get_imap_client()
        await client.select(folder)

        status, data = await client.search("UNSEEN")
        if status != "OK":
            return f"IMAP search failed: {status}"

        message_ids = data[0].decode().split()
        if not message_ids:
            return "No unread emails."

        recent_ids = message_ids[-limit:]
        lines = [f"Unread: {len(message_ids)} email(s). Showing {len(recent_ids)}:\n"]

        for msg_id in reversed(recent_ids):
            status, fetch_data = await client.fetch(
                msg_id,
                "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])",
            )
            if status == "OK" and fetch_data:
                for item in fetch_data:
                    if isinstance(item, tuple) and len(item) >= 2:
                        raw = item[1] if isinstance(item[1], bytes) else b""
                        if raw:
                            h = _parse_email_headers(raw)
                            lines.append(
                                f"- From: {h['from']}\n"
                                f"  Subject: {h['subject']}\n"
                                f"  Date: {h['date']}"
                            )
                            break

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
) -> str:
    """Search emails by keyword/sender. Returns formatted results."""
    client = None
    try:
        client = await _get_imap_client()
        await client.select("INBOX")

        since_date = (
            datetime.now(timezone.utc) - timedelta(days=days_back)
        ).strftime("%d-%b-%Y")
        search_parts = [f"SINCE {since_date}"]
        if sender:
            search_parts.append(f'FROM "{sender}"')
        search_parts.append(f'TEXT "{query}"')

        status, data = await client.search(" ".join(search_parts))
        if status != "OK":
            return f"IMAP search failed: {status}"

        message_ids = data[0].decode().split()
        if not message_ids:
            return f"No emails matching '{query}'."

        recent_ids = message_ids[-limit:]
        lines = [f"Found {len(message_ids)} email(s) matching '{query}'. Showing {len(recent_ids)}:\n"]

        for msg_id in reversed(recent_ids):
            status, fetch_data = await client.fetch(
                msg_id,
                "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])",
            )
            if status == "OK" and fetch_data:
                for item in fetch_data:
                    if isinstance(item, tuple) and len(item) >= 2:
                        raw = item[1] if isinstance(item[1], bytes) else b""
                        if raw:
                            h = _parse_email_headers(raw)
                            lines.append(
                                f"- From: {h['from']}\n"
                                f"  Subject: {h['subject']}\n"
                                f"  Date: {h['date']}"
                            )
                            break

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
