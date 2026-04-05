"""
Gmail tools — 19 tools for reading, searching, sending, and managing email.

All tool functions are async and accept a GoogleServiceFactory.
Registration helpers wire them into Pincer's ToolRegistry with the
``google__`` prefix and correct approval flags.
"""

from __future__ import annotations

import base64
import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pincer.integrations.google.email_guard import EmailGuard, EmailVerdict
from pincer.integrations.google.models import fmt_list, fmt_message_full, fmt_message_summary
from pincer.integrations.google.quota import with_backoff

if TYPE_CHECKING:
    from collections.abc import Callable

    from pincer.integrations.google.service_factory import GoogleServiceFactory
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ── Guard helpers ─────────────────────────────────────────────────────────────


def _check_recipients(guard: EmailGuard, address_fields: list[str]) -> tuple[str | None, str]:
    """
    Run all addresses through the email guard.

    Returns ``(block_message, warning_text)``.

    * If any address is hard-denied, *block_message* is a non-empty string and
      the caller should return it immediately without sending.
    * *warning_text* is appended to success messages for WARN-level results.
    """
    all_addrs: list[str] = []
    for field in address_fields:
        if field:
            all_addrs.extend(a.strip() for a in field.split(",") if a.strip())

    results = guard.check_all(all_addrs)

    for r in results:
        if r.verdict in (
            EmailVerdict.DENY_BLOCKLIST,
            EmailVerdict.DENY_DANGEROUS,
            EmailVerdict.DENY_DISPOSABLE,
        ):
            return (
                f"⛔ Email BLOCKED — {r.reason}\n"
                f"  Recipient: {r.address}\n\n"
                f"This email was NOT sent. If this is a legitimate address, "
                f"add it to data/email_allowlist.txt",
                "",
            )

    warns = [r for r in results if r.verdict == EmailVerdict.WARN]
    warning_text = ""
    if warns:
        warning_text = "\n⚠️ Warnings:\n" + "".join(f"  - {w.address}: {w.reason}\n" for w in warns)
    return None, warning_text


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_body(payload: dict[str, Any]) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    elif mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            result = _extract_body(part)
            if result:
                return result
    return ""


def _build_raw(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    body_type: str = "plain",
    in_reply_to: str = "",
    references: str = "",
    from_: str = "",
) -> str:
    if body_type == "html":
        msg: MIMEMultipart | MIMEText = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "html"))  # type: ignore[union-attr]
    else:
        msg = MIMEText(body, "plain")
    msg["to"] = to
    msg["subject"] = subject
    if cc:
        msg["cc"] = cc
    if bcc:
        msg["bcc"] = bcc
    if from_:
        msg["from"] = from_
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


# ── Tool implementations ──────────────────────────────────────────────────────


async def google__list_labels(factory: GoogleServiceFactory) -> str:
    """List all Gmail labels."""
    svc = await factory.get("gmail")
    result = await with_backoff(lambda: svc.users().labels().list(userId="me").execute())
    labels = result.get("labels", [])
    if not labels:
        return "No labels found."
    lines = [f"  {lbl['name']} (id={lbl['id']})" for lbl in labels]
    return fmt_list(lines, header=f"{len(labels)} label(s):")


async def google__list_messages(
    factory: GoogleServiceFactory,
    label_ids: str = "INBOX",
    max_results: int = 20,
    page_token: str = "",
) -> str:
    """List Gmail messages filtered by label."""
    svc = await factory.get("gmail")
    kwargs: dict[str, Any] = {"userId": "me", "maxResults": max_results, "labelIds": [label_ids]}
    if page_token:
        kwargs["pageToken"] = page_token
    result = await with_backoff(lambda: svc.users().messages().list(**kwargs).execute())
    msgs = result.get("messages", [])
    if not msgs:
        return f"No messages in {label_ids}."
    summaries = []
    for ref in msgs:
        _mid = ref["id"]

        def _fetch_msg(mid: str = _mid) -> Any:
            return (
                svc.users()
                .messages()
                .get(  # type: ignore[misc]
                    userId="me", id=mid, format="metadata", metadataHeaders=["Subject", "From", "Date"]
                )
                .execute()
            )

        msg = await with_backoff(cast("Callable[[], Any]", _fetch_msg))
        summaries.append(fmt_message_summary(msg))
    more = bool(result.get("nextPageToken"))
    return fmt_list(summaries, header=f"{len(summaries)} message(s) in {label_ids}:", more=more)


async def google__search_messages(
    factory: GoogleServiceFactory,
    query: str,
    max_results: int = 20,
    page_token: str = "",
) -> str:
    """Search Gmail using query syntax (from:, subject:, is:unread, has:attachment, newer_than:7d)."""
    svc = await factory.get("gmail")
    kwargs: dict[str, Any] = {"userId": "me", "q": query, "maxResults": max_results}
    if page_token:
        kwargs["pageToken"] = page_token
    result = await with_backoff(lambda: svc.users().messages().list(**kwargs).execute())
    msgs = result.get("messages", [])
    if not msgs:
        return f"No messages found for: {query}"
    summaries = []
    for ref in msgs:
        _mid = ref["id"]

        def _fetch_msg(mid: str = _mid) -> Any:
            return (
                svc.users()
                .messages()
                .get(  # type: ignore[misc]
                    userId="me", id=mid, format="metadata", metadataHeaders=["Subject", "From", "Date"]
                )
                .execute()
            )

        msg = await with_backoff(cast("Callable[[], Any]", _fetch_msg))
        summaries.append(fmt_message_summary(msg))
    more = bool(result.get("nextPageToken"))
    return fmt_list(summaries, header=f"Found {len(summaries)} message(s):", more=more)


def _extract_attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract attachment metadata from a Gmail message payload."""
    attachments: list[dict[str, Any]] = []
    for part in payload.get("parts", []):
        fname = part.get("filename", "")
        att_id = part.get("body", {}).get("attachmentId")
        if fname and att_id:
            attachments.append(
                {
                    "filename": fname,
                    "attachment_id": att_id,
                    "mime_type": part.get("mimeType", "unknown"),
                    "size_bytes": part.get("body", {}).get("size", 0),
                }
            )
    return attachments


async def google__get_message(
    factory: GoogleServiceFactory,
    message_id: str,
    max_body_chars: int = 4000,
) -> str:
    """Get full message content (headers + body + attachment metadata)."""
    svc = await factory.get("gmail")
    msg = await with_backoff(lambda: svc.users().messages().get(userId="me", id=message_id, format="full").execute())
    body = _extract_body(msg.get("payload", {}))[:max_body_chars]
    result = fmt_message_full(msg, body)

    attachments = _extract_attachments(msg.get("payload", {}))
    if attachments:
        result += "\n\nAttachments:"
        for att in attachments:
            result += (
                f"\n  - {att['filename']} ({att['mime_type']}, {att['size_bytes']} bytes)"
                f"\n    attachment_id: {att['attachment_id']}"
            )
        result += (
            f"\n\nTo download an attachment, call google__get_attachment("
            f'message_id="{msg["id"]}", attachment_id="<id from above>")'
        )

    return result


async def google__get_thread(
    factory: GoogleServiceFactory,
    thread_id: str,
    max_body_chars: int = 2000,
) -> str:
    """Get a full conversation thread."""
    svc = await factory.get("gmail")
    thread = await with_backoff(lambda: svc.users().threads().get(userId="me", id=thread_id, format="full").execute())
    messages = thread.get("messages", [])
    if not messages:
        return f"Thread {thread_id} is empty."
    parts = []
    for i, msg in enumerate(messages, 1):
        body = _extract_body(msg.get("payload", {}))[:max_body_chars]
        parts.append(f"--- Message {i} ---\n{fmt_message_full(msg, body)}")
    return f"Thread ({len(messages)} message(s)):\n\n" + "\n\n".join(parts)


async def google__get_attachment(
    factory: GoogleServiceFactory,
    message_id: str,
    attachment_id: str,
    filename: str = "",
) -> str:
    """Download an attachment from Gmail, save to disk, return file path and metadata."""
    svc = await factory.get("gmail")

    # 1. Fetch attachment data from Gmail API
    att = await with_backoff(
        lambda: svc.users().messages().attachments().get(userId="me", messageId=message_id, id=attachment_id).execute()
    )

    raw_data = att.get("data", "")
    if not raw_data:
        return "Error: Attachment data is empty."

    # 2. Decode base64
    file_bytes = base64.urlsafe_b64decode(raw_data)

    # 3. Resolve filename — use provided name, or look it up from message metadata
    if not filename:
        msg = await with_backoff(
            lambda: svc.users().messages().get(userId="me", id=message_id, format="full").execute()
        )
        for part in msg.get("payload", {}).get("parts", []):
            if part.get("body", {}).get("attachmentId") == attachment_id:
                filename = part.get("filename", "")
                break
    if not filename:
        filename = "attachment"

    # 4. Save to disk
    download_dir = os.path.expanduser("~/.pincer/downloads")
    os.makedirs(download_dir, exist_ok=True)

    save_path = os.path.join(download_dir, filename)

    # Handle duplicate filenames
    if os.path.exists(save_path):
        name, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(save_path):
            save_path = os.path.join(download_dir, f"{name}_{counter}{ext}")
            counter += 1

    with open(save_path, "wb") as f:
        f.write(file_bytes)

    size_kb = len(file_bytes) / 1024
    size_str = f"{size_kb:.0f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"

    # 5. Return ONLY metadata — never raw content
    return (
        f"Attachment saved successfully.\n"
        f"  Filename: {os.path.basename(save_path)}\n"
        f"  Size: {len(file_bytes)} bytes ({size_str})\n"
        f"  Saved to: {save_path}\n\n"
        f"The file is already saved to disk. Do NOT use python_exec to save it again."
    )


async def google__send_message(
    factory: GoogleServiceFactory,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    body_type: str = "plain",
) -> str:
    """Send a new email."""
    svc = await factory.get("gmail")
    raw = _build_raw(to, subject, body, cc=cc, bcc=bcc, body_type=body_type)
    await with_backoff(lambda: svc.users().messages().send(userId="me", body={"raw": raw}).execute())
    return f'Email sent to {to}: "{subject}"'


async def google__reply_to_message(
    factory: GoogleServiceFactory,
    message_id: str,
    body: str,
    body_type: str = "plain",
    _guard: EmailGuard | None = None,
) -> str:
    """Reply to a message (in-thread)."""
    svc = await factory.get("gmail")
    orig = await with_backoff(
        lambda: (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["Subject", "From", "Message-ID", "References"],
            )
            .execute()
        )
    )
    hdrs = {h["name"]: h["value"] for h in orig.get("payload", {}).get("headers", [])}
    to = hdrs.get("From", "")
    subject = "Re: " + hdrs.get("Subject", "")
    in_reply_to = hdrs.get("Message-ID", "")
    references = hdrs.get("References", "")
    thread_id = orig.get("threadId", "")

    if _guard is not None:
        block, warning = _check_recipients(_guard, [to])
        if block:
            return block
    else:
        warning = ""

    raw = _build_raw(to, subject, body, body_type=body_type, in_reply_to=in_reply_to, references=references)
    await with_backoff(
        lambda: svc.users().messages().send(userId="me", body={"raw": raw, "threadId": thread_id}).execute()
    )
    return f"Reply sent to {to}{warning}"


async def google__reply_all(
    factory: GoogleServiceFactory,
    message_id: str,
    body: str,
    body_type: str = "plain",
    _guard: EmailGuard | None = None,
) -> str:
    """Reply-all to a message."""
    svc = await factory.get("gmail")
    orig = await with_backoff(
        lambda: (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["Subject", "From", "To", "Cc", "Message-ID", "References"],
            )
            .execute()
        )
    )
    hdrs = {h["name"]: h["value"] for h in orig.get("payload", {}).get("headers", [])}
    original_from = hdrs.get("From", "")
    original_to = hdrs.get("To", "")
    cc = hdrs.get("Cc", "")
    # Build combined to list excluding self
    all_recipients = ", ".join(filter(None, [original_from, original_to]))
    subject = "Re: " + hdrs.get("Subject", "")
    in_reply_to = hdrs.get("Message-ID", "")
    thread_id = orig.get("threadId", "")

    if _guard is not None:
        block, warning = _check_recipients(_guard, [all_recipients, cc])
        if block:
            return block
    else:
        warning = ""

    raw = _build_raw(all_recipients, subject, body, cc=cc, body_type=body_type, in_reply_to=in_reply_to)
    await with_backoff(
        lambda: svc.users().messages().send(userId="me", body={"raw": raw, "threadId": thread_id}).execute()
    )
    return f"Reply-all sent to {all_recipients}{warning}"


async def google__forward_message(
    factory: GoogleServiceFactory,
    message_id: str,
    to: str,
    note: str = "",
) -> str:
    """Forward a message to a new recipient."""
    svc = await factory.get("gmail")
    orig = await with_backoff(lambda: svc.users().messages().get(userId="me", id=message_id, format="full").execute())
    hdrs = {h["name"]: h["value"] for h in orig.get("payload", {}).get("headers", [])}
    subject = "Fwd: " + hdrs.get("Subject", "")
    body = _extract_body(orig.get("payload", {}))
    sep = "---------- Forwarded message ----------\n"
    prefix = note + "\n\n" + sep if note else sep
    fwd_body = prefix + body
    raw = _build_raw(to, subject, fwd_body)
    await with_backoff(lambda: svc.users().messages().send(userId="me", body={"raw": raw}).execute())
    return f'Forwarded to {to}: "{subject}"'


async def google__create_draft(
    factory: GoogleServiceFactory,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    body_type: str = "plain",
) -> str:
    """Create an email draft."""
    svc = await factory.get("gmail")
    raw = _build_raw(to, subject, body, cc=cc, body_type=body_type)
    result = await with_backoff(
        lambda: svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    )
    return f'Draft created (id={result.get("id", "")}) to {to}: "{subject}"'


async def google__send_draft(
    factory: GoogleServiceFactory,
    draft_id: str,
) -> str:
    """Send an existing draft."""
    svc = await factory.get("gmail")
    result = await with_backoff(lambda: svc.users().drafts().send(userId="me", body={"id": draft_id}).execute())
    return f"Draft {draft_id} sent (message id={result.get('id', '')})"


async def google__trash_message(
    factory: GoogleServiceFactory,
    message_id: str,
) -> str:
    """Move a message to Trash."""
    svc = await factory.get("gmail")
    await with_backoff(lambda: svc.users().messages().trash(userId="me", id=message_id).execute())
    return f"Message {message_id} moved to Trash."


async def google__untrash_message(
    factory: GoogleServiceFactory,
    message_id: str,
) -> str:
    """Restore a message from Trash."""
    svc = await factory.get("gmail")
    await with_backoff(lambda: svc.users().messages().untrash(userId="me", id=message_id).execute())
    return f"Message {message_id} restored from Trash."


async def google__mark_as_read(
    factory: GoogleServiceFactory,
    message_id: str,
) -> str:
    """Mark a message as read."""
    svc = await factory.get("gmail")
    await with_backoff(
        lambda: svc.users().messages().modify(userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}).execute()
    )
    return f"Message {message_id} marked as read."


async def google__mark_as_unread(
    factory: GoogleServiceFactory,
    message_id: str,
) -> str:
    """Mark a message as unread."""
    svc = await factory.get("gmail")
    await with_backoff(
        lambda: svc.users().messages().modify(userId="me", id=message_id, body={"addLabelIds": ["UNREAD"]}).execute()
    )
    return f"Message {message_id} marked as unread."


async def google__add_label(
    factory: GoogleServiceFactory,
    message_id: str,
    label_id: str,
) -> str:
    """Apply a label to a message."""
    svc = await factory.get("gmail")
    await with_backoff(
        lambda: svc.users().messages().modify(userId="me", id=message_id, body={"addLabelIds": [label_id]}).execute()
    )
    return f"Label {label_id} added to message {message_id}."


async def google__remove_label(
    factory: GoogleServiceFactory,
    message_id: str,
    label_id: str,
) -> str:
    """Remove a label from a message."""
    svc = await factory.get("gmail")
    await with_backoff(
        lambda: svc.users().messages().modify(userId="me", id=message_id, body={"removeLabelIds": [label_id]}).execute()
    )
    return f"Label {label_id} removed from message {message_id}."


async def google__create_label(
    factory: GoogleServiceFactory,
    name: str,
    label_list_visibility: str = "labelShow",
    message_list_visibility: str = "show",
) -> str:
    """Create a new Gmail label."""
    svc = await factory.get("gmail")
    result = await with_backoff(
        lambda: (
            svc.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": label_list_visibility,
                    "messageListVisibility": message_list_visibility,
                },
            )
            .execute()
        )
    )
    return f'Label created: "{name}" (id={result.get("id", "")})'


# ── Registry ──────────────────────────────────────────────────────────────────


def register_gmail_tools(
    registry: ToolRegistry,
    factory: GoogleServiceFactory,
    data_dir: Path | None = None,
    email_block_disposable: bool = True,
) -> int:
    """Register all 19 Gmail tools in *registry*. Returns count.

    Args:
        registry: Tool registry to register into.
        factory: Google service factory for API access.
        data_dir: Directory containing ``email_allowlist.txt``,
            ``email_blocklist.txt``, and ``disposable_domains.txt``.
            Defaults to ``./data`` (project root data directory).
        email_block_disposable: If ``True`` (default), block disposable/
            temporary email domain services.
    """
    import functools

    _data_dir = data_dir if data_dir is not None else Path("data")
    guard = EmailGuard(data_dir=_data_dir, block_disposable=email_block_disposable)

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a tool function to inject the factory (read-only tools)."""

        @functools.wraps(fn)
        async def wrapper(**kwargs):  # type: ignore[no-untyped-def]
            return await fn(factory, **kwargs)

        return wrapper

    def _h_send(fn: Callable[..., Any], *addr_param_names: str) -> Callable[..., Any]:
        """Wrap a send tool: pre-check named address params, inject factory."""

        @functools.wraps(fn)
        async def wrapper(**kwargs):  # type: ignore[no-untyped-def]
            address_fields = [kwargs.get(p, "") for p in addr_param_names]
            block, warning = _check_recipients(guard, address_fields)
            if block:
                return block
            result = await fn(factory, **kwargs)
            return result + warning

        return wrapper

    def _h_reply(fn: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a reply tool: inject factory + guard (recipient fetched inside fn)."""

        @functools.wraps(fn)
        async def wrapper(**kwargs):  # type: ignore[no-untyped-def]
            return await fn(factory, _guard=guard, **kwargs)

        return wrapper

    registry.register(
        name="google__list_labels",
        description="List all Gmail labels (Inbox, Sent, custom). Returns label names and IDs.",
        handler=_h(google__list_labels),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    registry.register(
        name="google__list_messages",
        description="List Gmail messages in a label (default: INBOX). Returns sender, subject, snippet.",
        handler=_h(google__list_messages),
        parameters={
            "type": "object",
            "properties": {
                "label_ids": {
                    "type": "string",
                    "description": "Label ID to filter by (default: INBOX)",
                    "default": "INBOX",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max messages to return (default: 20)",
                    "default": 20,
                },
                "page_token": {"type": "string", "description": "Pagination token from previous result", "default": ""},
            },
            "required": [],
        },
    )
    registry.register(
        name="google__search_messages",
        description=(
            "Search Gmail using query syntax. Supports: from:, to:, subject:, has:attachment, "
            "is:unread, is:starred, newer_than:7d, older_than:30d, in:inbox, label:. "
            "Examples: 'from:sarah subject:report newer_than:7d', 'has:attachment is:unread'"
        ),
        handler=_h(google__search_messages),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query"},
                "max_results": {"type": "integer", "description": "Max results (default: 20)", "default": 20},
                "page_token": {"type": "string", "description": "Pagination token", "default": ""},
            },
            "required": ["query"],
        },
    )
    registry.register(
        name="google__get_message",
        description=(
            "Get full email content including headers, body, and attachment metadata. "
            "Use message ID from search or list results. "
            "If the email has attachments, returns their filenames, sizes, and attachment_ids "
            "needed for google__get_attachment."
        ),
        handler=_h(google__get_message),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Gmail message ID"},
                "max_body_chars": {
                    "type": "integer",
                    "description": "Max body characters (default: 4000)",
                    "default": 4000,
                },
            },
            "required": ["message_id"],
        },
    )
    registry.register(
        name="google__get_thread",
        description="Get a full conversation thread with all messages.",
        handler=_h(google__get_thread),
        parameters={
            "type": "object",
            "properties": {
                "thread_id": {"type": "string", "description": "Gmail thread ID"},
                "max_body_chars": {
                    "type": "integer",
                    "description": "Max body chars per message (default: 2000)",
                    "default": 2000,
                },
            },
            "required": ["thread_id"],
        },
    )
    registry.register(
        name="google__get_attachment",
        description=(
            "Download a file attachment from a Gmail email and save it to disk. "
            "Returns the saved file path, filename, and size. "
            "PREREQUISITE: First call google__search_messages to find the email, "
            "then call google__get_message to get attachment metadata "
            "(attachment_id in the Attachments section). Then call this tool with those IDs. "
            "The file is saved automatically — do NOT use python_exec to save it again."
        ),
        handler=_h(google__get_attachment),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Gmail message ID from google__search_messages or google__get_message",
                },
                "attachment_id": {
                    "type": "string",
                    "description": (
                        "Attachment ID from google__get_message output "
                        "(shown as 'attachment_id' in the Attachments section)"
                    ),
                },
                "filename": {
                    "type": "string",
                    "description": "Optional filename (from google__get_message). If omitted, looked up automatically.",
                    "default": "",
                },
            },
            "required": ["message_id", "attachment_id"],
        },
    )
    registry.register(
        name="google__send_message",
        description="Send a new email. Supports plain text and HTML, CC, and BCC.",
        handler=_h_send(google__send_message, "to", "cc", "bcc"),
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body"},
                "cc": {"type": "string", "description": "CC addresses (comma-separated)", "default": ""},
                "bcc": {"type": "string", "description": "BCC addresses (comma-separated)", "default": ""},
                "body_type": {
                    "type": "string",
                    "enum": ["plain", "html"],
                    "description": "Body format (default: plain)",
                    "default": "plain",
                },
            },
            "required": ["to", "subject", "body"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__reply_to_message",
        description="Reply to a specific message, keeping it in-thread.",
        handler=_h_reply(google__reply_to_message),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Message ID to reply to"},
                "body": {"type": "string", "description": "Reply body"},
                "body_type": {"type": "string", "enum": ["plain", "html"], "default": "plain"},
            },
            "required": ["message_id", "body"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__reply_all",
        description="Reply-all to a message (responds to all recipients).",
        handler=_h_reply(google__reply_all),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Message ID to reply-all to"},
                "body": {"type": "string", "description": "Reply body"},
                "body_type": {"type": "string", "enum": ["plain", "html"], "default": "plain"},
            },
            "required": ["message_id", "body"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__forward_message",
        description="Forward an email to a new recipient.",
        handler=_h_send(google__forward_message, "to"),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Message ID to forward"},
                "to": {"type": "string", "description": "Recipient to forward to"},
                "note": {"type": "string", "description": "Optional note to prepend", "default": ""},
            },
            "required": ["message_id", "to"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__create_draft",
        description="Create an email draft (not sent yet).",
        handler=_h_send(google__create_draft, "to", "cc"),
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "cc": {"type": "string", "default": ""},
                "body_type": {"type": "string", "enum": ["plain", "html"], "default": "plain"},
            },
            "required": ["to", "subject", "body"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__send_draft",
        description="Send an existing email draft.",
        handler=_h(google__send_draft),
        parameters={
            "type": "object",
            "properties": {
                "draft_id": {"type": "string", "description": "Draft ID to send"},
            },
            "required": ["draft_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__trash_message",
        description="Move an email to Trash.",
        handler=_h(google__trash_message),
        parameters={
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__untrash_message",
        description="Restore an email from Trash.",
        handler=_h(google__untrash_message),
        parameters={
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__mark_as_read",
        description="Mark an email as read.",
        handler=_h(google__mark_as_read),
        parameters={
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
    )
    registry.register(
        name="google__mark_as_unread",
        description="Mark an email as unread.",
        handler=_h(google__mark_as_unread),
        parameters={
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
    )
    registry.register(
        name="google__add_label",
        description="Apply a Gmail label to a message. Use google__list_labels to find label IDs.",
        handler=_h(google__add_label),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "label_id": {"type": "string", "description": "Label ID (from google__list_labels)"},
            },
            "required": ["message_id", "label_id"],
        },
    )
    registry.register(
        name="google__remove_label",
        description="Remove a Gmail label from a message.",
        handler=_h(google__remove_label),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "label_id": {"type": "string"},
            },
            "required": ["message_id", "label_id"],
        },
    )
    registry.register(
        name="google__create_label",
        description="Create a new Gmail label.",
        handler=_h(google__create_label),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Label name"},
                "label_list_visibility": {"type": "string", "default": "labelShow"},
                "message_list_visibility": {"type": "string", "default": "show"},
            },
            "required": ["name"],
        },
        require_approval=True,
    )
    return 19
