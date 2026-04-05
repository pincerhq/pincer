"""
Google Contacts / People API tools — 7 tools for reading and managing contacts.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import TYPE_CHECKING, Any

from pincer.integrations.google.quota import with_backoff

if TYPE_CHECKING:
    from collections.abc import Callable

    from pincer.integrations.google.service_factory import GoogleServiceFactory
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Valid personFields for People API — NOTE: "resourceName" is NOT a valid personField
# and must NOT be included here (causes 400). It is always returned automatically.
_PERSON_FIELDS = "names,emailAddresses,phoneNumbers,addresses,organizations,biographies,metadata,birthdays,urls"

# Subset used for searchContacts (uses readMask, not personFields)
_SEARCH_FIELDS = "names,emailAddresses,phoneNumbers,organizations"


def _fmt_contact(person: dict[str, Any]) -> str:
    """Format a People API person resource (compact)."""
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


def _fmt_contact_full(person: dict[str, Any]) -> str:
    """Format a People API person resource with all available fields."""
    resource = person.get("resourceName", "?")
    output = ""

    names = person.get("names", [])
    if names:
        n = names[0]
        output += f"Name: {n.get('displayName', '?')}\n"
        if n.get("givenName"):
            output += f"  Given: {n['givenName']}\n"
        if n.get("familyName"):
            output += f"  Family: {n['familyName']}\n"

    for email in person.get("emailAddresses", []):
        output += f"Email ({email.get('type', 'other')}): {email.get('value', '?')}\n"

    for phone in person.get("phoneNumbers", []):
        output += f"Phone ({phone.get('type', 'other')}): {phone.get('value', '?')}\n"

    for org in person.get("organizations", []):
        parts: list[str] = []
        if org.get("title"):
            parts.append(org["title"])
        if org.get("name"):
            parts.append(f"at {org['name']}")
        if parts:
            output += f"Organization: {' '.join(parts)}\n"

    for addr in person.get("addresses", []):
        formatted = addr.get("formattedValue", "")
        if formatted:
            output += f"Address: {formatted}\n"

    for bio in person.get("biographies", []):
        output += f"Notes: {bio.get('value', '')[:200]}\n"

    bdays = person.get("birthdays", [])
    if bdays:
        d = bdays[0].get("date", {})
        if d:
            output += f"Birthday: {d.get('year', '??')}-{d.get('month', '??')}-{d.get('day', '??')}\n"

    output += f"Resource: {resource}\n"

    if person.get("etag") or any(
        s.get("type") == "CONTACT" and s.get("etag")
        for s in person.get("metadata", {}).get("sources", [])
    ):
        output += "(etag available — contact can be updated with google__update_contact)\n"

    return output


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

    # Google requires a warmup request with empty query to hydrate its lazy cache.
    # Failure is non-fatal — the real search below may still return results.
    try:
        await with_backoff(
            lambda: svc.people().searchContacts(query="", readMask=_SEARCH_FIELDS).execute()
        )
    except Exception:
        pass
    await asyncio.sleep(0.5)

    result = await with_backoff(
        lambda: svc.people().searchContacts(
            query=query, pageSize=max_results, readMask=_SEARCH_FIELDS
        ).execute()
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

    # Auto-prefix "people/" so bare IDs like "c123" also work
    if not resource_name.startswith("people/"):
        resource_name = f"people/{resource_name}"

    try:
        person = await with_backoff(
            lambda: svc.people().get(resourceName=resource_name, personFields=_PERSON_FIELDS).execute()
        )
    except Exception as e:
        err = str(e)
        if "404" in err or "not found" in err.lower():
            return (
                f"Contact '{resource_name}' not found. "
                "Use google__search_contacts to find the correct resource name."
            )
        return f"Failed to get contact: {err}"

    return "Contact details:\n\n" + _fmt_contact_full(person)


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
    organization: str = "",
    job_title: str = "",
    notes: str = "",
    remove_email: str = "",
    remove_phone: str = "",
) -> str:
    """Update a contact's fields."""
    svc = await factory.get("contacts")

    if not (given_name or family_name or email or phone or organization or job_title or notes or remove_email or remove_phone):
        return "No fields to update. Specify at least one: given_name, family_name, email, phone, organization, job_title, notes, remove_email, remove_phone."

    # Auto-prefix "people/" so bare IDs also work
    if not resource_name.startswith("people/"):
        resource_name = f"people/{resource_name}"

    # Fetch current contact to get the etag required by the update API
    try:
        current = await with_backoff(
            lambda: svc.people().get(resourceName=resource_name, personFields=_PERSON_FIELDS).execute()
        )
    except Exception as e:
        err = str(e)
        if "404" in err or "not found" in err.lower():
            return f"Contact '{resource_name}' not found. Use google__search_contacts."
        return f"Could not fetch contact for update: {e}"

    # Use TOP-LEVEL person.etag — this is what updateContact validates.
    # The metadata.sources[].etag is a different value and causes 400 if used.
    etag = current.get("etag", "")
    if not etag:
        # Fallback to source etag (older API responses may omit top-level etag)
        for src in current.get("metadata", {}).get("sources", []):
            if src.get("type") == "CONTACT":
                etag = src.get("etag", "")
                break

    if not etag:
        return (
            f"Cannot update '{resource_name}' — no etag found. "
            "This may be a read-only profile entry rather than a contact you own."
        )

    # Build body with etag at TOP LEVEL (not nested in metadata)
    body: dict[str, Any] = {"etag": etag}
    update_fields: list[str] = []

    if given_name or family_name:
        existing_names = current.get("names", [{}])
        existing = existing_names[0] if existing_names else {}
        body["names"] = [
            {
                "givenName": given_name or existing.get("givenName", ""),
                "familyName": family_name or existing.get("familyName", ""),
            }
        ]
        update_fields.append("names")

    if email or remove_email:
        existing_emails = current.get("emailAddresses", [])
        if remove_email:
            new_emails = [e for e in existing_emails if e.get("value", "").lower() != remove_email.lower()]
            if len(new_emails) == len(existing_emails):
                current_list = [e.get("value") for e in existing_emails]
                return f"Email '{remove_email}' not found. Current emails: {current_list}"
        else:
            # Append to existing list rather than replacing
            new_emails = existing_emails + [{"value": email}]
        body["emailAddresses"] = new_emails
        update_fields.append("emailAddresses")

    if phone or remove_phone:
        existing_phones = current.get("phoneNumbers", [])
        if remove_phone:
            new_phones = [p for p in existing_phones if p.get("value", "") != remove_phone]
            if len(new_phones) == len(existing_phones):
                current_list = [p.get("value") for p in existing_phones]
                return f"Phone '{remove_phone}' not found. Current phones: {current_list}"
        else:
            new_phones = existing_phones + [{"value": phone}]
        body["phoneNumbers"] = new_phones
        update_fields.append("phoneNumbers")

    if organization or job_title:
        existing_org = current.get("organizations", [{}])[0] if current.get("organizations") else {}
        body["organizations"] = [{
            "name": organization or existing_org.get("name", ""),
            "title": job_title or existing_org.get("title", ""),
        }]
        update_fields.append("organizations")

    if notes:
        body["biographies"] = [{"value": notes, "contentType": "TEXT_PLAIN"}]
        update_fields.append("biographies")

    def _do_update() -> Any:
        return (
            svc.people()
            .updateContact(
                resourceName=resource_name,
                updatePersonFields=",".join(update_fields),
                personFields=_PERSON_FIELDS,
                body=body,
            )
            .execute()
        )

    try:
        result = await with_backoff(_do_update)
    except Exception as e:
        err = str(e)
        if "failedPrecondition" in err or "etag" in err.lower():
            # Auto-retry once with a fresh etag
            try:
                fresh = await with_backoff(
                    lambda: svc.people().get(resourceName=resource_name, personFields=_PERSON_FIELDS).execute()
                )
                fresh_etag = fresh.get("etag", "")
                if fresh_etag and fresh_etag != etag:
                    body["etag"] = fresh_etag
                    result = await with_backoff(_do_update)
                    display = (
                        result.get("names", [{}])[0].get("displayName", resource_name)
                        if result.get("names")
                        else resource_name
                    )
                    return f"Contact updated (retried with fresh etag): '{display}'"
            except Exception as retry_err:
                return f"Update failed on retry: {retry_err}"
            return (
                f"etag conflict — contact may be read-only or synced from an external source.\n"
                f"Error: {err}"
            )
        return (
            f"Failed to update contact: {err}\n"
            f"Debug: resource={resource_name}, fields={','.join(update_fields)}, "
            f"etag={'present' if etag else 'MISSING'}, body_keys={list(body.keys())}"
        )

    display = (
        result.get("names", [{}])[0].get("displayName", resource_name)
        if result.get("names")
        else resource_name
    )
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
        description="Update a Google Contact's name, email, phone, organization, or notes. Use remove_email/remove_phone to delete a specific value.",
        handler=_h(google__update_contact),
        parameters={
            "type": "object",
            "properties": {
                "resource_name": {"type": "string"},
                "given_name": {"type": "string", "default": ""},
                "family_name": {"type": "string", "default": ""},
                "email": {"type": "string", "default": ""},
                "phone": {"type": "string", "default": ""},
                "organization": {"type": "string", "default": ""},
                "job_title": {"type": "string", "default": ""},
                "notes": {"type": "string", "default": ""},
                "remove_email": {"type": "string", "default": "", "description": "Exact email address to remove"},
                "remove_phone": {"type": "string", "default": "", "description": "Exact phone number to remove"},
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
