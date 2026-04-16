"""User / user-group tools — 10 tools.

All functions are async, accept a SlackClient as first arg, and return str.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.integrations.slack.client import SlackClient
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────


def _fmt_user(u: dict[str, Any]) -> str:
    name = u.get("real_name") or u.get("name", "?")
    uid = u.get("id", "?")
    email = u.get("profile", {}).get("email", "")
    title = u.get("profile", {}).get("title", "")
    deleted = " [deleted]" if u.get("deleted") else ""
    bot = " [bot]" if u.get("is_bot") else ""
    extras = f" | {title}" if title else ""
    extras += f" | {email}" if email else ""
    return f"{name} (ID: {uid}){extras}{deleted}{bot}"


# ── tool functions ─────────────────────────────────────────────────────────────


async def slack__list_users(
    client: SlackClient, include_bots: bool = False, limit: int = 200
) -> str:
    """List all workspace members."""
    try:
        from pincer.integrations.slack.pagination import paginate

        users = await paginate(
            client.bot.users_list, "members", limit=min(limit, 200)
        )
        if not include_bots:
            users = [u for u in users if not u.get("is_bot") and not u.get("deleted")]
        if not users:
            return "No users found."
        lines = [_fmt_user(u) for u in users]
        return f"Found {len(lines)} user(s):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error listing users: {exc}"


async def slack__get_user_info(client: SlackClient, user: str) -> str:
    """Get detailed profile info for a user: name, email, title, avatar, status."""
    try:
        resp = await client.bot.users_info(user=user, include_locale=True)
        u = resp.get("user", {})
        profile = u.get("profile", {})
        lines = [
            f"Name: {u.get('real_name', u.get('name', '?'))}",
            f"ID: {u.get('id', '?')}",
            f"Username: @{u.get('name', '?')}",
            f"Email: {profile.get('email', '(hidden)')}",
            f"Title: {profile.get('title', '')}",
            f"Phone: {profile.get('phone', '')}",
            f"Status: {profile.get('status_emoji', '')} {profile.get('status_text', '')}",
            f"Time zone: {u.get('tz', '')} ({u.get('tz_label', '')})",
            f"Bot: {u.get('is_bot', False)}",
            f"Admin: {u.get('is_admin', False)}",
        ]
        return "\n".join(lines)
    except Exception as exc:
        return f"Error getting user info: {exc}"


async def slack__get_user_by_email(client: SlackClient, email: str) -> str:
    """Look up a workspace user by email address."""
    try:
        resp = await client.bot.users_lookupByEmail(email=email)
        u = resp.get("user", {})
        return _fmt_user(u)
    except Exception as exc:
        return f"Error looking up user by email: {exc}"


async def slack__set_user_status(
    client: SlackClient,
    status_text: str,
    status_emoji: str = "",
    expiration: int = 0,
) -> str:
    """Set the calling user's status text and emoji.

    expiration: Unix timestamp when status expires (0 = no expiration).
    Requires user token.
    """
    try:
        profile: dict[str, Any] = {
            "status_text": status_text,
            "status_emoji": status_emoji,
            "status_expiration": expiration,
        }
        await client.user.users_profile_set(profile=profile)
        return f"Status set: {status_emoji} {status_text}"
    except Exception as exc:
        return f"Error setting user status: {exc}"


async def slack__get_user_presence(client: SlackClient, user: str) -> str:
    """Check if a user is active or away."""
    try:
        resp = await client.bot.users_getPresence(user=user)
        presence = resp.get("presence", "unknown")
        online = resp.get("online", False)
        auto_away = resp.get("auto_away", False)
        manual_away = resp.get("manual_away", False)
        return (
            f"User {user}: presence={presence}, online={online}, "
            f"auto_away={auto_away}, manual_away={manual_away}"
        )
    except Exception as exc:
        return f"Error getting user presence: {exc}"


async def slack__list_user_groups(
    client: SlackClient, include_disabled: bool = False
) -> str:
    """List all user groups (teams/departments) in the workspace."""
    try:
        resp = await client.bot.usergroups_list(
            include_disabled=include_disabled, include_users=False
        )
        groups = resp.get("usergroups", [])
        if not groups:
            return "No user groups found."
        lines = []
        for g in groups:
            status = " [disabled]" if g.get("date_delete") else ""
            lines.append(
                f"{g.get('name', '?')} (@{g.get('handle', '?')}) "
                f"(ID: {g.get('id', '?')}, {g.get('user_count', 0)} members){status}"
            )
        return f"Found {len(lines)} user group(s):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Error listing user groups: {exc}"


async def slack__get_user_group_members(
    client: SlackClient, usergroup: str
) -> str:
    """List all members of a user group."""
    try:
        resp = await client.bot.usergroups_users_list(usergroup=usergroup)
        users = resp.get("users", [])
        if not users:
            return f"No members in user group {usergroup}."
        return f"Members of {usergroup} ({len(users)}):\n" + "\n".join(users)
    except Exception as exc:
        return f"Error getting user group members: {exc}"


async def slack__create_user_group(
    client: SlackClient,
    name: str,
    handle: str,
    description: str = "",
) -> str:
    """Create a new user group."""
    try:
        kwargs: dict[str, Any] = {"name": name, "handle": handle}
        if description:
            kwargs["description"] = description
        resp = await client.bot.usergroups_create(**kwargs)
        g = resp.get("usergroup", {})
        return f"User group created: {g.get('name', '?')} (@{g.get('handle', '?')}) ID: {g.get('id', '?')}"
    except Exception as exc:
        return f"Error creating user group: {exc}"


async def slack__update_user_group(
    client: SlackClient,
    usergroup: str,
    name: str = "",
    handle: str = "",
    description: str = "",
    users: str = "",
) -> str:
    """Update a user group's name, handle, description, or members.

    users: comma-separated user IDs to set as members.
    """
    try:
        kwargs: dict[str, Any] = {"usergroup": usergroup}
        if name:
            kwargs["name"] = name
        if handle:
            kwargs["handle"] = handle
        if description:
            kwargs["description"] = description
        resp = await client.bot.usergroups_update(**kwargs)
        g = resp.get("usergroup", {})

        # Update members if provided
        if users:
            user_list = [u.strip() for u in users.split(",") if u.strip()]
            await client.bot.usergroups_users_update(
                usergroup=usergroup, users=user_list
            )
            return (
                f"User group {g.get('name', usergroup)} updated, members set to: "
                + ", ".join(user_list)
            )
        return f"User group updated: {g.get('name', usergroup)}"
    except Exception as exc:
        return f"Error updating user group: {exc}"


async def slack__disable_user_group(client: SlackClient, usergroup: str) -> str:
    """Disable (archive) a user group."""
    try:
        resp = await client.bot.usergroups_disable(usergroup=usergroup)
        g = resp.get("usergroup", {})
        return f"User group {g.get('name', usergroup)} disabled."
    except Exception as exc:
        return f"Error disabling user group: {exc}"


# ── registration ───────────────────────────────────────────────────────────────


def register_user_tools(registry: ToolRegistry, client: SlackClient) -> int:
    """Register all 10 user tools. Returns 10."""

    def _h(fn: Any):  # type: ignore[return]
        async def handler(**kwargs: Any) -> str:
            return await fn(client, **kwargs)

        return handler

    registry.register(
        name="slack__list_users",
        description="List all workspace members.",
        handler=_h(slack__list_users),
        parameters={
            "type": "object",
            "properties": {
                "include_bots": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 200},
            },
            "required": [],
        },
    )
    registry.register(
        name="slack__get_user_info",
        description="Get user profile: name, email, title, status, timezone.",
        handler=_h(slack__get_user_info),
        parameters={
            "type": "object",
            "properties": {"user": {"type": "string", "description": "User ID (U...)"}},
            "required": ["user"],
        },
    )
    registry.register(
        name="slack__get_user_by_email",
        description="Look up a workspace user by email address.",
        handler=_h(slack__get_user_by_email),
        parameters={
            "type": "object",
            "properties": {"email": {"type": "string", "description": "Email address"}},
            "required": ["email"],
        },
    )
    registry.register(
        name="slack__set_user_status",
        description="Set your own Slack status text and emoji.",
        handler=_h(slack__set_user_status),
        parameters={
            "type": "object",
            "properties": {
                "status_text": {"type": "string", "description": "Status message text"},
                "status_emoji": {"type": "string", "description": "Status emoji (e.g. :coffee:)", "default": ""},
                "expiration": {
                    "type": "integer",
                    "description": "Unix timestamp when status expires (0 = never)",
                    "default": 0,
                },
            },
            "required": ["status_text"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__get_user_presence",
        description="Check if a user is currently active or away.",
        handler=_h(slack__get_user_presence),
        parameters={
            "type": "object",
            "properties": {"user": {"type": "string", "description": "User ID (U...)"}},
            "required": ["user"],
        },
    )
    registry.register(
        name="slack__list_user_groups",
        description="List all user groups (teams/departments) in the workspace.",
        handler=_h(slack__list_user_groups),
        parameters={
            "type": "object",
            "properties": {"include_disabled": {"type": "boolean", "default": False}},
            "required": [],
        },
    )
    registry.register(
        name="slack__get_user_group_members",
        description="List all members of a specific user group.",
        handler=_h(slack__get_user_group_members),
        parameters={
            "type": "object",
            "properties": {"usergroup": {"type": "string", "description": "User group ID (S...)"}},
            "required": ["usergroup"],
        },
    )
    registry.register(
        name="slack__create_user_group",
        description="Create a new user group.",
        handler=_h(slack__create_user_group),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Display name"},
                "handle": {"type": "string", "description": "Short handle (@mention)"},
                "description": {"type": "string", "description": "Group description", "default": ""},
            },
            "required": ["name", "handle"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__update_user_group",
        description="Update a user group name, handle, description, or member list.",
        handler=_h(slack__update_user_group),
        parameters={
            "type": "object",
            "properties": {
                "usergroup": {"type": "string", "description": "User group ID"},
                "name": {"type": "string", "description": "New name", "default": ""},
                "handle": {"type": "string", "description": "New handle", "default": ""},
                "description": {"type": "string", "description": "New description", "default": ""},
                "users": {"type": "string", "description": "Comma-separated user IDs for membership", "default": ""},
            },
            "required": ["usergroup"],
        },
        require_approval=True,
    )
    registry.register(
        name="slack__disable_user_group",
        description="Disable (archive) a user group.",
        handler=_h(slack__disable_user_group),
        parameters={
            "type": "object",
            "properties": {"usergroup": {"type": "string", "description": "User group ID"}},
            "required": ["usergroup"],
        },
        require_approval=True,
    )

    return 10
