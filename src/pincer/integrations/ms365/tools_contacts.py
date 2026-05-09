"""
Microsoft 365 Contacts tools — 6 tools for contact management.
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


def _fmt_contact(c: dict[str, Any]) -> str:
    """Format a contact for display."""
    name = c.get("displayName", "?")
    emails = c.get("emailAddresses", [])
    email_str = ", ".join(e.get("address", "") for e in emails) if emails else "no email"
    phones = c.get("businessPhones", [])
    if isinstance(c.get("mobilePhone"), list):
        phones = phones + c.get("mobilePhone", [])
    mobile = c.get("mobilePhone", "")
    phone_str = ", ".join(phones) if phones else (mobile or "no phone")
    company = c.get("companyName", "")
    title = c.get("jobTitle", "")
    role = f"{title} at {company}" if title and company else (title or company or "")
    return f"  {name} — {email_str} — {phone_str}" + (f" — {role}" if role else "") + f"\n    ID: {c.get('id', '')}"


async def outlook__list_contacts(
    client: GraphClient,
    max_results: int = 50,
) -> str:
    """List contacts."""
    data = await client.get(
        "/me/contacts",
        params={
            "$top": str(max_results),
            "$select": "id,displayName,emailAddresses,businessPhones,mobilePhone,companyName,jobTitle",
            "$orderby": "displayName",
        },
    )
    contacts = data.get("value", [])
    if not contacts:
        return "No contacts found."
    lines = [_fmt_contact(c) for c in contacts]
    return f"{len(lines)} contact(s):\n" + "\n\n".join(lines)


async def outlook__search_contacts(
    client: GraphClient,
    query: str,
    max_results: int = 20,
) -> str:
    """Search contacts by name or email."""
    data = await client.get(
        "/me/contacts",
        params={
            "$top": str(max_results),
            "$filter": f"contains(displayName, '{query}') or contains(emailAddresses/any(e:e/address), '{query}')",
            "$select": "id,displayName,emailAddresses,businessPhones,mobilePhone,companyName,jobTitle",
        },
    )
    contacts = data.get("value", [])
    if not contacts:
        # Fallback: try $search if $filter fails
        data = await client.get(
            "/me/contacts",
            params={
                "$top": str(max_results),
                "$search": f'"{query}"',
                "$select": "id,displayName,emailAddresses,businessPhones,mobilePhone,companyName,jobTitle",
            },
        )
        contacts = data.get("value", [])

    if not contacts:
        return f"No contacts matching '{query}'."
    lines = [_fmt_contact(c) for c in contacts]
    return f"Search results for '{query}' ({len(lines)} found):\n" + "\n\n".join(lines)


async def outlook__get_contact(
    client: GraphClient,
    contact_id: str,
) -> str:
    """Get contact details."""
    c = await client.get(f"/me/contacts/{contact_id}")
    emails = c.get("emailAddresses", [])
    phones = c.get("businessPhones", [])
    mobile = c.get("mobilePhone", "")

    lines = [
        f"Name: {c.get('displayName', '?')}",
        f"First: {c.get('givenName', '')} | Last: {c.get('surname', '')}",
        f"Email(s): {', '.join(e.get('address', '') for e in emails) if emails else 'none'}",
        f"Business Phone(s): {', '.join(phones) if phones else 'none'}",
        f"Mobile: {mobile or 'none'}",
        f"Company: {c.get('companyName', '')}",
        f"Job Title: {c.get('jobTitle', '')}",
        f"Department: {c.get('department', '')}",
        f"ID: {c.get('id', '')}",
    ]
    address = c.get("businessAddress", {})
    if any(address.values()):
        street = address.get("street", "")
        city = address.get("city", "")
        state = address.get("state", "")
        postal = address.get("postalCode", "")
        lines.append(f"Address: {street}, {city} {state} {postal}")
    return "\n".join(lines)


async def outlook__create_contact(
    client: GraphClient,
    given_name: str,
    surname: str = "",
    email: str = "",
    mobile_phone: str = "",
    business_phone: str = "",
    company_name: str = "",
    job_title: str = "",
) -> str:
    """Create a new contact."""
    body: dict[str, Any] = {
        "givenName": given_name,
    }
    if surname:
        body["surname"] = surname
    if email:
        body["emailAddresses"] = [{"address": email, "name": f"{given_name} {surname}".strip()}]
    if mobile_phone:
        body["mobilePhone"] = mobile_phone
    if business_phone:
        body["businessPhones"] = [business_phone]
    if company_name:
        body["companyName"] = company_name
    if job_title:
        body["jobTitle"] = job_title

    result = await client.post("/me/contacts", json=body)
    display = result.get("displayName", f"{given_name} {surname}".strip())
    return f"Contact created: {display} (id={result.get('id', '')})"


async def outlook__update_contact(
    client: GraphClient,
    contact_id: str,
    given_name: str = "",
    surname: str = "",
    email: str = "",
    mobile_phone: str = "",
    company_name: str = "",
    job_title: str = "",
) -> str:
    """Update a contact."""
    updates: dict[str, Any] = {}
    if given_name:
        updates["givenName"] = given_name
    if surname:
        updates["surname"] = surname
    if email:
        updates["emailAddresses"] = [{"address": email}]
    if mobile_phone:
        updates["mobilePhone"] = mobile_phone
    if company_name:
        updates["companyName"] = company_name
    if job_title:
        updates["jobTitle"] = job_title

    await client.patch(f"/me/contacts/{contact_id}", json=updates)
    return f"Contact {contact_id} updated."


async def outlook__delete_contact(
    client: GraphClient,
    contact_id: str,
) -> str:
    """Delete a contact."""
    await client.delete(f"/me/contacts/{contact_id}")
    return f"Contact {contact_id} deleted."


# ── Registry ──────────────────────────────────────���───────────────────────────


def register_contacts_tools(registry: ToolRegistry, client: GraphClient) -> int:
    """Register all 6 Contacts tools. Returns count."""

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(**kwargs: Any) -> str:
            return await fn(client, **kwargs)

        return wrapper

    registry.register(
        name="outlook__list_contacts",
        description="List Microsoft 365 contacts.",
        handler=_h(outlook__list_contacts),
        parameters={
            "type": "object",
            "properties": {"max_results": {"type": "integer", "default": 50}},
            "required": [],
        },
    )
    registry.register(
        name="outlook__search_contacts",
        description="Search contacts by name or email.",
        handler=_h(outlook__search_contacts),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    )
    registry.register(
        name="outlook__get_contact",
        description="Get full details of a contact.",
        handler=_h(outlook__get_contact),
        parameters={
            "type": "object",
            "properties": {"contact_id": {"type": "string"}},
            "required": ["contact_id"],
        },
    )
    registry.register(
        name="outlook__create_contact",
        description="Create a new Microsoft 365 contact.",
        handler=_h(outlook__create_contact),
        parameters={
            "type": "object",
            "properties": {
                "given_name": {"type": "string"},
                "surname": {"type": "string", "default": ""},
                "email": {"type": "string", "default": ""},
                "mobile_phone": {"type": "string", "default": ""},
                "business_phone": {"type": "string", "default": ""},
                "company_name": {"type": "string", "default": ""},
                "job_title": {"type": "string", "default": ""},
            },
            "required": ["given_name"],
        },
        require_approval=True,
    )
    registry.register(
        name="outlook__update_contact",
        description="Update a Microsoft 365 contact.",
        handler=_h(outlook__update_contact),
        parameters={
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "given_name": {"type": "string", "default": ""},
                "surname": {"type": "string", "default": ""},
                "email": {"type": "string", "default": ""},
                "mobile_phone": {"type": "string", "default": ""},
                "company_name": {"type": "string", "default": ""},
                "job_title": {"type": "string", "default": ""},
            },
            "required": ["contact_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="outlook__delete_contact",
        description="Delete a Microsoft 365 contact.",
        handler=_h(outlook__delete_contact),
        parameters={
            "type": "object",
            "properties": {"contact_id": {"type": "string"}},
            "required": ["contact_id"],
        },
        require_approval=True,
    )
    return 6
