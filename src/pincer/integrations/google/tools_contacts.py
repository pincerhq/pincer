"""
Google Contacts / People API tools — 7 tools for reading and managing contacts.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any

from pincer.integrations.google.quota import with_backoff

if TYPE_CHECKING:
    from collections.abc import Callable

    from pincer.integrations.google.service_factory import GoogleServiceFactory
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_PERSON_FIELDS = "names,emailAddresses,phoneNumbers,addresses,organizations,biographies,resourceName"


def _fmt_contact(person: dict[str, Any]) -> str:
    """Format a People API person resource."""
    names = person.get("names", [{}])
    name = names[0].get("displayName", "(no name)") if names else "(no name)"
    emails = [e.get("value", "") for e in person.get("emailAddresses", [])]
    phones = [p.get("value", "") for p in person.get("phoneNumbers", [])]
    orgs = [o.get("name", "") for o in person.get("organizations", [])]
    resource = person.get("resourceName", "")
    parts = [f"  {name}"]
    if emails:
        parts.append(f"    Email: {', '.join(emails)}")
    if phones:
        parts.append(f"    Phone: {', '.join(phones)}")
    if orgs:
        parts.append(f"    Org:   {', '.join(orgs)}")
    parts.append(f"    ID:    {resource}")
    return "\n".join(parts)


# ── Tool implementations ──────────────────────────────────────────────────────


async def google__list_contacts(
    factory: GoogleServiceFactory,
    max_results: int = 50,
    page_token: str = "",
) -> str:
    """List contacts."""
    svc = await factory.get("contacts")
    kwargs: dict[str, Any] = {
        "resourceName": "people/me",
        "pageSize": max_results,
        "personFields": _PERSON_FIELDS,
    }
    if page_token:
        kwargs["pageToken"] = page_token
    result = await with_backoff(lambda: svc.people().connections().list(**kwargs).execute())
    connections = result.get("connections", [])
    if not connections:
        return "No contacts found."
    lines = [_fmt_contact(c) for c in connections]
    more = bool(result.get("nextPageToken"))
    suffix = "\n(more results available)" if more else ""
    return f"{len(lines)} contact(s):\n" + "\n".join(lines) + suffix


async def google__search_contacts(
    factory: GoogleServiceFactory,
    query: str,
    max_results: int = 20,
) -> str:
    """Search contacts by name, email, or phone."""
    svc = await factory.get("contacts")
    result = await with_backoff(
        lambda: svc.people().searchContacts(query=query, pageSize=max_results, readMask=_PERSON_FIELDS).execute()
    )
    results = result.get("results", [])
    if not results:
        return f"No contacts found for: {query}"
    lines = [_fmt_contact(r.get("person", {})) for r in results]
    return f"Found {len(lines)} contact(s):\n" + "\n".join(lines)


async def google__get_contact(
    factory: GoogleServiceFactory,
    resource_name: str,
) -> str:
    """Get details of a specific contact by resource name (e.g. 'people/c123456')."""
    svc = await factory.get("contacts")
    person = await with_backoff(
        lambda: svc.people().get(resourceName=resource_name, personFields=_PERSON_FIELDS).execute()
    )
    return _fmt_contact(person)


async def google__create_contact(
    factory: GoogleServiceFactory,
    given_name: str,
    family_name: str = "",
    email: str = "",
    phone: str = "",
    organization: str = "",
) -> str:
    """Create a new contact."""
    svc = await factory.get("contacts")
    body: dict[str, Any] = {
        "names": [{"givenName": given_name, "familyName": family_name}],
    }
    if email:
        body["emailAddresses"] = [{"value": email}]
    if phone:
        body["phoneNumbers"] = [{"value": phone}]
    if organization:
        body["organizations"] = [{"name": organization}]
    result = await with_backoff(lambda: svc.people().createContact(body=body).execute())
    display = result.get("names", [{}])[0].get("displayName", given_name)
    return f"Contact created: '{display}' (id={result.get('resourceName', '')})"


async def google__update_contact(
    factory: GoogleServiceFactory,
    resource_name: str,
    given_name: str = "",
    family_name: str = "",
    email: str = "",
    phone: str = "",
) -> str:
    """Update a contact's fields."""
    svc = await factory.get("contacts")
    person = await with_backoff(
        lambda: svc.people().get(resourceName=resource_name, personFields=_PERSON_FIELDS).execute()
    )
    etag = person.get("etag", "")
    update_fields: list[str] = []

    if given_name or family_name:
        existing_name = person.get("names", [{}])[0] if person.get("names") else {}
        person["names"] = [
            {
                "givenName": given_name or existing_name.get("givenName", ""),
                "familyName": family_name or existing_name.get("familyName", ""),
            }
        ]
        update_fields.append("names")
    if email:
        person["emailAddresses"] = [{"value": email}]
        update_fields.append("emailAddresses")
    if phone:
        person["phoneNumbers"] = [{"value": phone}]
        update_fields.append("phoneNumbers")

    if not update_fields:
        return "No fields to update."

    person["etag"] = etag
    result = await with_backoff(
        lambda: (
            svc.people()
            .updateContact(
                resourceName=resource_name,
                updatePersonFields=",".join(update_fields),
                body=person,
            )
            .execute()
        )
    )
    display = result.get("names", [{}])[0].get("displayName", resource_name) if result.get("names") else resource_name
    return f"Contact updated: '{display}'"


async def google__delete_contact(
    factory: GoogleServiceFactory,
    resource_name: str,
) -> str:
    """Delete a contact."""
    svc = await factory.get("contacts")
    await with_backoff(lambda: svc.people().deleteContact(resourceName=resource_name).execute())
    return f"Contact {resource_name} deleted."


async def google__list_contact_groups(factory: GoogleServiceFactory) -> str:
    """List contact groups (labels)."""
    svc = await factory.get("contacts")
    result = await with_backoff(lambda: svc.contactGroups().list(pageSize=200).execute())
    groups = result.get("contactGroups", [])
    if not groups:
        return "No contact groups found."
    lines = [
        f"  {g.get('name', '?')} "
        f"(formattedName={g.get('formattedName', '')}, "
        f"memberCount={g.get('memberCount', 0)}, "
        f"id={g.get('resourceName', '')})"
        for g in groups
    ]
    return f"{len(lines)} group(s):\n" + "\n".join(lines)


# ── Registry ──────────────────────────────────────────────────────────────────


def register_contacts_tools(registry: ToolRegistry, factory: GoogleServiceFactory) -> int:
    """Register all 7 Contacts tools. Returns count."""

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(**kwargs):  # type: ignore[no-untyped-def]
            return await fn(factory, **kwargs)

        return wrapper

    registry.register(
        name="google__list_contacts",
        description="List Google Contacts.",
        handler=_h(google__list_contacts),
        parameters={
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "default": 50},
                "page_token": {"type": "string", "default": ""},
            },
            "required": [],
        },
    )
    registry.register(
        name="google__search_contacts",
        description="Search Google Contacts by name, email, or phone number.",
        handler=_h(google__search_contacts),
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
        name="google__get_contact",
        description="Get full details of a contact by resource name (e.g. 'people/c123456').",
        handler=_h(google__get_contact),
        parameters={
            "type": "object",
            "properties": {
                "resource_name": {"type": "string", "description": "Contact resource name from search results"},
            },
            "required": ["resource_name"],
        },
    )
    registry.register(
        name="google__create_contact",
        description="Create a new Google Contact.",
        handler=_h(google__create_contact),
        parameters={
            "type": "object",
            "properties": {
                "given_name": {"type": "string"},
                "family_name": {"type": "string", "default": ""},
                "email": {"type": "string", "default": ""},
                "phone": {"type": "string", "default": ""},
                "organization": {"type": "string", "default": ""},
            },
            "required": ["given_name"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__update_contact",
        description="Update a Google Contact's name, email, or phone.",
        handler=_h(google__update_contact),
        parameters={
            "type": "object",
            "properties": {
                "resource_name": {"type": "string"},
                "given_name": {"type": "string", "default": ""},
                "family_name": {"type": "string", "default": ""},
                "email": {"type": "string", "default": ""},
                "phone": {"type": "string", "default": ""},
            },
            "required": ["resource_name"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__delete_contact",
        description="Delete a Google Contact.",
        handler=_h(google__delete_contact),
        parameters={
            "type": "object",
            "properties": {"resource_name": {"type": "string"}},
            "required": ["resource_name"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__list_contact_groups",
        description="List Google Contact groups/labels.",
        handler=_h(google__list_contact_groups),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    return 7
