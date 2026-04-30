"""
Outlook email tools — 17 tools for reading, searching, sending, and managing email.

All tool functions are async and accept a GraphClient.
Registration helpers wire them into Pincer's ToolRegistry with the
``outlook__`` prefix and correct approval flags.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from pincer.integrations.ms365.graph_client import GraphClient
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ── Tool implementations ──────────────────────────────────────────────────────


async def outlook__list_mail_folders(client: GraphClient) -> str:
    """List all mail folders (Inbox, Sent Items, Drafts, etc.)."""
    data = await client.get("/me/mailFolders", params={"$top": "50"})
    folders = data.get("value", [])
    if not folders:
        return "No mail folders found."
    lines = []
    for f in folders:
        count = f.get("totalItemCount", 0)
        unread = f.get("unreadItemCount", 0)
        lines.append(f"  {f.get('displayName', '?')} — {count} messages ({unread} unread) [id={f.get('id', '')}]")
    return f"{len(lines)} mail folder(s):\n" + "\n".join(lines)


async def outlook__list_messages(
    client: GraphClient,
    folder: str = "Inbox",
    max_results: int = 25,
    unread_only: bool = False,
    has_attachments: bool = False,
    from_address: str = "",
) -> str:
    """List messages in a mail folder with optional filters."""
    # Resolve well-known folder names
    folder_path = f"/me/mailFolders/{folder}/messages"

    params: dict[str, Any] = {
        "$top": str(max_results),
        "$select": "id,subject,from,receivedDateTime,isRead,hasAttachments,bodyPreview",
        "$orderby": "receivedDateTime desc",
    }

    filters: list[str] = []
    if unread_only:
        filters.append("isRead eq false")
    if has_attachments:
        filters.append("hasAttachments eq true")
    if from_address:
        filters.append(f"from/emailAddress/address eq '{from_address}'")
    if filters:
        params["$filter"] = " and ".join(filters)

    data = await client.get(folder_path, params=params)
    messages = data.get("value", [])
    if not messages:
        return f"No messages found in {folder}."

    results = []
    for msg in messages:
        sender = msg.get("from", {}).get("emailAddress", {})
        sender_str = f"{sender.get('name', '')} <{sender.get('address', '')}>"
        preview = (msg.get("bodyPreview") or "")[:150]
        read_str = "Read" if msg.get("isRead") else "Unread"
        attach_str = " 📎" if msg.get("hasAttachments") else ""
        results.append(
            f"**{msg.get('subject', '(no subject)')}**{attach_str}\n"
            f"  From: {sender_str}\n"
            f"  Date: {msg.get('receivedDateTime', '')}\n"
            f"  Status: {read_str}\n"
            f"  Preview: {preview}\n"
            f"  ID: {msg.get('id', '')}"
        )

    header = f"Found {len(results)} messages in {folder}"
    if data.get("@odata.nextLink"):
        header += f" (showing first {max_results}, more available)"
    return header + "\n\n" + "\n\n---\n\n".join(results)


async def outlook__search_messages(
    client: GraphClient,
    query: str,
    max_results: int = 10,
) -> str:
    """Search emails across all folders. Supports natural language queries."""
    params: dict[str, Any] = {
        "$top": str(max_results),
        "$select": "id,subject,from,receivedDateTime,isRead,bodyPreview",
        "$search": f'"{query}"',
    }

    data = await client.get("/me/messages", params=params)
    messages = data.get("value", [])
    if not messages:
        return f"No messages found matching '{query}'."

    results = []
    for msg in messages:
        sender = msg.get("from", {}).get("emailAddress", {})
        sender_str = f"{sender.get('name', '')} <{sender.get('address', '')}>"
        results.append(
            f"**{msg.get('subject', '(no subject)')}**\n"
            f"  From: {sender_str}\n"
            f"  Date: {msg.get('receivedDateTime', '')}\n"
            f"  Preview: {(msg.get('bodyPreview') or '')[:150]}\n"
            f"  ID: {msg.get('id', '')}"
        )

    return f"Search results for '{query}' ({len(results)} found):\n\n" + "\n\n---\n\n".join(results)


async def outlook__get_message(
    client: GraphClient,
    message_id: str,
) -> str:
    """Get full message content by ID."""
    msg = await client.get(
        f"/me/messages/{message_id}",
        params={
            "$select": "id,subject,from,toRecipients,ccRecipients,bccRecipients,"
            "receivedDateTime,body,isRead,hasAttachments,importance,flag"
        },
    )
    sender = msg.get("from", {}).get("emailAddress", {})
    to_list = [r.get("emailAddress", {}).get("address", "") for r in msg.get("toRecipients", [])]
    cc_list = [r.get("emailAddress", {}).get("address", "") for r in msg.get("ccRecipients", [])]

    body = msg.get("body", {})
    body_content = body.get("content", "")
    # Strip HTML tags for cleaner display if HTML
    if body.get("contentType") == "html":
        import re

        body_content = re.sub(r"<[^>]+>", "", body_content)
        body_content = re.sub(r"\s+", " ", body_content).strip()

    lines = [
        f"Subject: {msg.get('subject', '(no subject)')}",
        f"From: {sender.get('name', '')} <{sender.get('address', '')}>",
        f"To: {', '.join(to_list)}",
    ]
    if cc_list:
        lines.append(f"CC: {', '.join(cc_list)}")
    lines.extend(
        [
            f"Date: {msg.get('receivedDateTime', '')}",
            f"Importance: {msg.get('importance', 'normal')}",
            f"Read: {'Yes' if msg.get('isRead') else 'No'}",
            f"Attachments: {'Yes' if msg.get('hasAttachments') else 'No'}",
            f"ID: {msg.get('id', '')}",
            "",
            body_content[:3000],
        ]
    )
    return "\n".join(lines)


async def outlook__get_message_attachments(
    client: GraphClient,
    message_id: str,
) -> str:
    """List attachments on a message."""
    data = await client.get(f"/me/messages/{message_id}/attachments")
    attachments = data.get("value", [])
    if not attachments:
        return "No attachments on this message."

    lines = []
    for att in attachments:
        size = att.get("size", 0)
        size_str = f"{size / 1024:.1f} KB" if size < 1048576 else f"{size / 1048576:.1f} MB"
        lines.append(
            f"  {att.get('name', '?')} ({att.get('contentType', '?')}, {size_str})\n    ID: {att.get('id', '')}"
        )
    return f"{len(lines)} attachment(s):\n" + "\n".join(lines)


async def outlook__download_attachment(
    client: GraphClient,
    message_id: str,
    attachment_id: str,
) -> str:
    """Download attachment content (returns base64 for binary, text for text)."""
    att = await client.get(f"/me/messages/{message_id}/attachments/{attachment_id}")
    content = att.get("contentBytes", "")
    name = att.get("name", "attachment")
    size = att.get("size", 0)
    content_type = att.get("contentType", "application/octet-stream")

    if content_type.startswith("text/"):
        import base64

        try:
            text = base64.b64decode(content).decode("utf-8", errors="replace")
            return f"Attachment: {name}\nContent:\n{text[:5000]}"
        except Exception:
            pass

    return (
        f"Attachment: {name}\nType: {content_type}\nSize: {size} bytes\nContent: [base64 encoded, {len(content)} chars]"
    )


async def outlook__send_message(
    client: GraphClient,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    importance: str = "normal",
    body_type: str = "text",
) -> str:
    """Send a new email."""
    message: dict[str, Any] = {
        "subject": subject,
        "body": {
            "contentType": "HTML" if body_type == "html" else "Text",
            "content": body,
        },
        "toRecipients": [{"emailAddress": {"address": addr.strip()}} for addr in to.split(",") if addr.strip()],
        "importance": importance,
    }
    if cc:
        message["ccRecipients"] = [
            {"emailAddress": {"address": addr.strip()}} for addr in cc.split(",") if addr.strip()
        ]
    if bcc:
        message["bccRecipients"] = [
            {"emailAddress": {"address": addr.strip()}} for addr in bcc.split(",") if addr.strip()
        ]

    await client.post("/me/sendMail", json={"message": message})
    recipients = [a.strip() for a in to.split(",") if a.strip()]
    return f'Email sent to {", ".join(recipients)}: "{subject}"'


async def outlook__reply_to_message(
    client: GraphClient,
    message_id: str,
    body: str,
    body_type: str = "text",
) -> str:
    """Reply to an email."""
    reply_body = {
        "message": {},
        "comment": body if body_type == "text" else "",
    }
    if body_type == "html":
        reply_body["message"] = {"body": {"contentType": "HTML", "content": body}}

    await client.post(f"/me/messages/{message_id}/reply", json=reply_body)
    return f"Reply sent to message {message_id}"


async def outlook__reply_all_to_message(
    client: GraphClient,
    message_id: str,
    body: str,
    body_type: str = "text",
) -> str:
    """Reply-all to an email."""
    reply_body = {
        "message": {},
        "comment": body if body_type == "text" else "",
    }
    if body_type == "html":
        reply_body["message"] = {"body": {"contentType": "HTML", "content": body}}

    await client.post(f"/me/messages/{message_id}/replyAll", json=reply_body)
    return f"Reply-all sent to message {message_id}"


async def outlook__forward_message(
    client: GraphClient,
    message_id: str,
    to: str,
    comment: str = "",
) -> str:
    """Forward an email to new recipients."""
    forward_body: dict[str, Any] = {
        "toRecipients": [{"emailAddress": {"address": addr.strip()}} for addr in to.split(",") if addr.strip()],
    }
    if comment:
        forward_body["comment"] = comment

    await client.post(f"/me/messages/{message_id}/forward", json=forward_body)
    return f"Message {message_id} forwarded to {to}"


async def outlook__create_draft(
    client: GraphClient,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    body_type: str = "text",
) -> str:
    """Create a draft email."""
    draft: dict[str, Any] = {
        "subject": subject,
        "body": {
            "contentType": "HTML" if body_type == "html" else "Text",
            "content": body,
        },
        "toRecipients": [{"emailAddress": {"address": addr.strip()}} for addr in to.split(",") if addr.strip()],
    }
    if cc:
        draft["ccRecipients"] = [{"emailAddress": {"address": addr.strip()}} for addr in cc.split(",") if addr.strip()]

    result = await client.post("/me/messages", json=draft)
    return f'Draft created: "{subject}" (id={result.get("id", "")})'


async def outlook__update_draft(
    client: GraphClient,
    message_id: str,
    subject: str = "",
    body: str = "",
    to: str = "",
    body_type: str = "text",
) -> str:
    """Update an existing draft email."""
    updates: dict[str, Any] = {}
    if subject:
        updates["subject"] = subject
    if body:
        updates["body"] = {
            "contentType": "HTML" if body_type == "html" else "Text",
            "content": body,
        }
    if to:
        updates["toRecipients"] = [
            {"emailAddress": {"address": addr.strip()}} for addr in to.split(",") if addr.strip()
        ]

    await client.patch(f"/me/messages/{message_id}", json=updates)
    return f"Draft {message_id} updated."


async def outlook__send_draft(
    client: GraphClient,
    message_id: str,
) -> str:
    """Send a previously created draft."""
    await client.post(f"/me/messages/{message_id}/send")
    return f"Draft {message_id} sent."


async def outlook__move_message(
    client: GraphClient,
    message_id: str,
    destination_folder: str,
) -> str:
    """Move message to another folder."""
    result = await client.post(
        f"/me/messages/{message_id}/move",
        json={"destinationId": destination_folder},
    )
    return f"Message moved to {destination_folder} (new id={result.get('id', message_id)})"


async def outlook__delete_message(
    client: GraphClient,
    message_id: str,
) -> str:
    """Delete a message (moves to Deleted Items)."""
    await client.delete(f"/me/messages/{message_id}")
    return f"Message {message_id} deleted."


async def outlook__mark_as_read(
    client: GraphClient,
    message_id: str,
    is_read: bool = True,
) -> str:
    """Mark message as read or unread."""
    await client.patch(f"/me/messages/{message_id}", json={"isRead": is_read})
    status = "read" if is_read else "unread"
    return f"Message {message_id} marked as {status}."


async def outlook__flag_message(
    client: GraphClient,
    message_id: str,
    flagged: bool = True,
) -> str:
    """Flag or unflag a message for follow-up."""
    flag_status = "flagged" if flagged else "notFlagged"
    await client.patch(
        f"/me/messages/{message_id}",
        json={"flag": {"flagStatus": flag_status}},
    )
    return f"Message {message_id} {'flagged' if flagged else 'unflagged'}."


# ── Registry ──────────────────────────────────────────────────────────────────


def register_email_tools(registry: ToolRegistry, client: GraphClient) -> int:
    """Register all 17 email tools. Returns count."""

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(**kwargs: Any) -> str:
            return await fn(client, **kwargs)

        return wrapper

    registry.register(
        name="outlook__list_mail_folders",
        description="List all Outlook mail folders (Inbox, Sent Items, Drafts, etc.) with message counts.",
        handler=_h(outlook__list_mail_folders),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    registry.register(
        name="outlook__list_messages",
        description="List messages in an Outlook mail folder. Supports filtering by read status, attachments, sender.",
        handler=_h(outlook__list_messages),
        parameters={
            "type": "object",
            "properties": {
                "folder": {"type": "string", "default": "Inbox", "description": "Folder name or ID"},
                "max_results": {"type": "integer", "default": 25},
                "unread_only": {"type": "boolean", "default": False},
                "has_attachments": {"type": "boolean", "default": False},
                "from_address": {"type": "string", "default": "", "description": "Filter by sender email"},
            },
            "required": [],
        },
    )
    registry.register(
        name="outlook__search_messages",
        description="Search Outlook emails across all folders. Supports natural language queries.",
        handler=_h(outlook__search_messages),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    )
    registry.register(
        name="outlook__get_message",
        description="Get full Outlook email content by message ID, including body, recipients, and metadata.",
        handler=_h(outlook__get_message),
        parameters={
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
    )
    registry.register(
        name="outlook__get_message_attachments",
        description="List all attachments on an Outlook email with names, types, and sizes.",
        handler=_h(outlook__get_message_attachments),
        parameters={
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
    )
    registry.register(
        name="outlook__download_attachment",
        description="Download an email attachment's content.",
        handler=_h(outlook__download_attachment),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "attachment_id": {"type": "string"},
            },
            "required": ["message_id", "attachment_id"],
        },
    )
    registry.register(
        name="outlook__send_message",
        description="Send a new Outlook email. Supports HTML body, CC, BCC, and importance levels.",
        handler=_h(outlook__send_message),
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Comma-separated email addresses"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "cc": {"type": "string", "default": ""},
                "bcc": {"type": "string", "default": ""},
                "importance": {"type": "string", "default": "normal", "description": "low, normal, or high"},
                "body_type": {"type": "string", "default": "text", "description": "text or html"},
            },
            "required": ["to", "subject", "body"],
        },
        require_approval=True,
    )
    registry.register(
        name="outlook__reply_to_message",
        description="Reply to an Outlook email.",
        handler=_h(outlook__reply_to_message),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "body": {"type": "string"},
                "body_type": {"type": "string", "default": "text"},
            },
            "required": ["message_id", "body"],
        },
        require_approval=True,
    )
    registry.register(
        name="outlook__reply_all_to_message",
        description="Reply-all to an Outlook email.",
        handler=_h(outlook__reply_all_to_message),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "body": {"type": "string"},
                "body_type": {"type": "string", "default": "text"},
            },
            "required": ["message_id", "body"],
        },
        require_approval=True,
    )
    registry.register(
        name="outlook__forward_message",
        description="Forward an Outlook email to new recipients.",
        handler=_h(outlook__forward_message),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "to": {"type": "string", "description": "Comma-separated email addresses"},
                "comment": {"type": "string", "default": ""},
            },
            "required": ["message_id", "to"],
        },
        require_approval=True,
    )
    registry.register(
        name="outlook__create_draft",
        description="Create a draft Outlook email.",
        handler=_h(outlook__create_draft),
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Comma-separated email addresses"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "cc": {"type": "string", "default": ""},
                "body_type": {"type": "string", "default": "text"},
            },
            "required": ["to", "subject", "body"],
        },
        require_approval=True,
    )
    registry.register(
        name="outlook__update_draft",
        description="Update an existing Outlook draft email.",
        handler=_h(outlook__update_draft),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "subject": {"type": "string", "default": ""},
                "body": {"type": "string", "default": ""},
                "to": {"type": "string", "default": ""},
                "body_type": {"type": "string", "default": "text"},
            },
            "required": ["message_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="outlook__send_draft",
        description="Send a previously created Outlook draft.",
        handler=_h(outlook__send_draft),
        parameters={
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="outlook__move_message",
        description="Move an Outlook message to another folder.",
        handler=_h(outlook__move_message),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "destination_folder": {"type": "string", "description": "Target folder name or ID"},
            },
            "required": ["message_id", "destination_folder"],
        },
        require_approval=True,
    )
    registry.register(
        name="outlook__delete_message",
        description="Delete an Outlook message (moves to Deleted Items).",
        handler=_h(outlook__delete_message),
        parameters={
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="outlook__mark_as_read",
        description="Mark an Outlook message as read or unread.",
        handler=_h(outlook__mark_as_read),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "is_read": {"type": "boolean", "default": True},
            },
            "required": ["message_id"],
        },
    )
    registry.register(
        name="outlook__flag_message",
        description="Flag or unflag an Outlook message for follow-up.",
        handler=_h(outlook__flag_message),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "flagged": {"type": "boolean", "default": True},
            },
            "required": ["message_id"],
        },
    )
    return 17
