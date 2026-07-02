"""
Organization directory tools — 1 read-only tool for looking up other users.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastmcp import Context

    from ms365._registry import ClientResolver, ToolRegistry
    from ms365.graph_client import GraphClient

logger = logging.getLogger(__name__)

_MAX_SHOWN = 5


def _escape_odata_literal(value: str) -> str:
    """Escape a single-quoted OData string literal."""
    return value.replace("'", "''")


def _build_search_filter(email: str, first_name: str, last_name: str) -> str:
    """Build an OData $filter clause for /users from the given search terms."""
    if email:
        e = _escape_odata_literal(email)
        return f"mail eq '{e}' or userPrincipalName eq '{e}'"

    clauses = []
    if first_name:
        clauses.append(f"startswith(givenName,'{_escape_odata_literal(first_name)}')")
    if last_name:
        clauses.append(f"startswith(surname,'{_escape_odata_literal(last_name)}')")
    return " and ".join(clauses)


async def ms365__search_users(
    client: GraphClient,
    email: str = "",
    first_name: str = "",
    last_name: str = "",
) -> str:
    """Search the organization's directory for a user by email or first/last name."""
    if not email and not first_name and not last_name:
        return "Provide an email address, or a first name and/or last name, to search for."

    filter_clause = _build_search_filter(email, first_name, last_name)

    data = await client.get(
        "/users",
        params={
            "$filter": filter_clause,
            "$select": "displayName,mail,userPrincipalName,givenName,surname",
            "$top": str(_MAX_SHOWN + 1),
            "$count": "true",
        },
        headers={"ConsistencyLevel": "eventual"},
    )
    users = data.get("value", [])

    if not users:
        return "No users found matching that search."

    if len(users) == 1:
        u = users[0]
        lines = [
            f"Name: {u.get('displayName', '?')}",
            f"Email: {u.get('mail') or u.get('userPrincipalName', '?')}",
        ]
        return "\n".join(lines)

    shown = users[:_MAX_SHOWN]
    lines = [f"{len(shown)} user(s) found (showing up to {_MAX_SHOWN}):"]
    for u in shown:
        email_str = u.get("mail") or u.get("userPrincipalName", "?")
        lines.append(f"  {u.get('displayName', '?')} <{email_str}>")
    if len(users) > _MAX_SHOWN:
        lines.append("  ... more matches exist — refine your search for a smaller result set.")
    return "\n".join(lines)


# ── Registry ──────────────────────────────────────────────────────────────────


def register_directory_tools(registry: ToolRegistry, resolve_client: ClientResolver) -> int:
    """Register the 1 directory search tool. Returns count."""

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(ctx: Context, **kwargs: Any) -> str:
            client = await resolve_client(ctx)
            return str(await fn(client, **kwargs))

        return wrapper

    registry.register(
        name="ms365__search_users",
        description=(
            "Search the Microsoft 365 organization's directory for a user by email, "
            "or by first name and/or last name. Returns a single user's basic info, "
            "or a list of up to 5 matches if more than one user matches."
        ),
        handler=_h(ms365__search_users),
        parameters={
            "type": "object",
            "properties": {
                "email": {"type": "string", "default": "", "description": "Exact email address to look up"},
                "first_name": {"type": "string", "default": "", "description": "First name (matches prefix)"},
                "last_name": {"type": "string", "default": "", "description": "Last name (matches prefix)"},
            },
            "required": [],
        },
    )
    return 1
