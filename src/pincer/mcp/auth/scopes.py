"""Scope definitions and enforcement for Pincer MCP OAuth."""

from __future__ import annotations

# Scope name → human description
PINCER_SCOPES: dict[str, str] = {
    "tools:read": "Read and list available tools",
    "tools:write": "Execute write/create operations via tools",
    "tools:execute": "Execute any tool (read + write + run)",
    "tools:all": "Full access to all tools",
    "resources:read": "Read MCP resources",
    "prompts:read": "Access MCP prompts",
    "sampling:request": "Submit LLM sampling requests",
    "admin:ask_user": "Send messages to the agent owner",
    "offline_access": "Obtain refresh tokens for long-lived access",
}

# Which tool name patterns each scope grants
SCOPE_TOOL_MAP: dict[str, list[str]] = {
    "tools:read": [
        "web_search",
        "memory_search",
        "email_check",
        "calendar_today",
        "file_read",
        "list_tools",
        "memory_read",
    ],
    "tools:write": [
        "file_write",
        "memory_store",
        "email_send",
        "calendar_create",
        "memory_update",
    ],
    "tools:execute": [
        "web_search",
        "memory_search",
        "email_check",
        "calendar_today",
        "file_read",
        "list_tools",
        "file_write",
        "memory_store",
        "email_send",
        "calendar_create",
        "memory_update",
        "execute_command",
        "browser_action",
        "run_script",
    ],
    "tools:all": [],  # Matches everything — checked separately
    "admin:ask_user": ["ask_user", "pincer_ask_user"],
}


def validate_scopes(requested: str, allowed: str) -> bool:
    """Return True if all requested scopes are a subset of allowed scopes.

    tools:all in allowed expands to cover all tools:* scopes.
    """
    req_set = set(requested.split())
    all_set = set(allowed.split())

    # tools:all in allowed covers all tools:* in requested
    if "tools:all" in all_set:
        tools_scopes = {s for s in req_set if s.startswith("tools:")}
        non_tools = req_set - tools_scopes
        return non_tools.issubset(all_set)

    return req_set.issubset(all_set)


def check_tool_scope(tool_name: str, granted_scope: str) -> bool:
    """Return True if granted_scope permits calling tool_name."""
    granted = set(granted_scope.split())

    if "tools:all" in granted:
        return True

    for scope in granted:
        tools = SCOPE_TOOL_MAP.get(scope, [])
        if tool_name in tools:
            return True

    return False


def get_scope_descriptions(scopes: str) -> list[str]:
    """Return human-readable descriptions for a space-separated scope string."""
    descriptions = []
    for s in scopes.split():
        desc = PINCER_SCOPES.get(s)
        if desc:
            descriptions.append(f"{s}: {desc}")
        else:
            descriptions.append(s)
    return descriptions
