"""
Microsoft To Do tools — 8 tools for task list management.
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


async def ms_todo__list_task_lists(client: GraphClient) -> str:
    """List all task lists."""
    data = await client.get("/me/todo/lists")
    lists = data.get("value", [])
    if not lists:
        return "No task lists found."
    lines = []
    for tl in lists:
        lines.append(f"  {tl.get('displayName', '?')} (id={tl.get('id', '')})")
    return f"{len(lines)} task list(s):\n" + "\n".join(lines)


async def ms_todo__list_tasks(
    client: GraphClient,
    list_id: str,
    max_results: int = 50,
    include_completed: bool = False,
) -> str:
    """List tasks in a task list."""
    params: dict[str, Any] = {
        "$top": str(max_results),
        "$select": "id,title,status,importance,dueDateTime,body",
        "$orderby": "importance desc,dueDateTime/dateTime",
    }
    if not include_completed:
        params["$filter"] = "status ne 'completed'"

    data = await client.get(f"/me/todo/lists/{list_id}/tasks", params=params)
    tasks = data.get("value", [])
    if not tasks:
        return "No tasks found."
    lines = []
    for t in tasks:
        status = t.get("status", "?")
        due = t.get("dueDateTime", {})
        due_str = f" — due {due.get('dateTime', '')[:10]}" if due else ""
        imp = f" [!{t.get('importance', '')}]" if t.get("importance") != "normal" else ""
        lines.append(f"  [{status}] {t.get('title', '?')}{imp}{due_str}\n    ID: {t.get('id', '')}")
    return f"{len(lines)} task(s):\n" + "\n\n".join(lines)


async def ms_todo__get_task(
    client: GraphClient,
    list_id: str,
    task_id: str,
) -> str:
    """Get task details."""
    t = await client.get(f"/me/todo/lists/{list_id}/tasks/{task_id}")
    due = t.get("dueDateTime", {})
    lines = [
        f"Title: {t.get('title', '?')}",
        f"Status: {t.get('status', '?')}",
        f"Importance: {t.get('importance', 'normal')}",
        f"Due: {due.get('dateTime', 'none') if due else 'none'}",
        f"Body: {t.get('body', {}).get('content', '')}",
        f"Created: {t.get('createdDateTime', '')}",
        f"Modified: {t.get('lastModifiedDateTime', '')}",
        f"ID: {t.get('id', '')}",
    ]
    return "\n".join(lines)


async def ms_todo__create_task(
    client: GraphClient,
    list_id: str,
    title: str,
    due_date: str = "",
    importance: str = "normal",
    body: str = "",
) -> str:
    """Create a new task."""
    task_body: dict[str, Any] = {
        "title": title,
        "importance": importance,
    }
    if due_date:
        task_body["dueDateTime"] = {"dateTime": due_date, "timeZone": "UTC"}
    if body:
        task_body["body"] = {"contentType": "text", "content": body}

    result = await client.post(f"/me/todo/lists/{list_id}/tasks", json=task_body)
    return f"Task created: '{title}' (id={result.get('id', '')})"


async def ms_todo__update_task(
    client: GraphClient,
    list_id: str,
    task_id: str,
    title: str = "",
    due_date: str = "",
    importance: str = "",
    body: str = "",
) -> str:
    """Update a task's title, due date, importance, or body."""
    updates: dict[str, Any] = {}
    if title:
        updates["title"] = title
    if due_date:
        updates["dueDateTime"] = {"dateTime": due_date, "timeZone": "UTC"}
    if importance:
        updates["importance"] = importance
    if body:
        updates["body"] = {"contentType": "text", "content": body}

    await client.patch(f"/me/todo/lists/{list_id}/tasks/{task_id}", json=updates)
    return f"Task {task_id} updated."


async def ms_todo__complete_task(
    client: GraphClient,
    list_id: str,
    task_id: str,
) -> str:
    """Mark a task as completed."""
    await client.patch(
        f"/me/todo/lists/{list_id}/tasks/{task_id}",
        json={"status": "completed"},
    )
    return f"Task {task_id} marked as completed."


async def ms_todo__delete_task(
    client: GraphClient,
    list_id: str,
    task_id: str,
) -> str:
    """Delete a task."""
    await client.delete(f"/me/todo/lists/{list_id}/tasks/{task_id}")
    return f"Task {task_id} deleted."


async def ms_todo__create_task_list(
    client: GraphClient,
    name: str,
) -> str:
    """Create a new task list."""
    result = await client.post("/me/todo/lists", json={"displayName": name})
    return f"Task list created: '{name}' (id={result.get('id', '')})"


# ── Registry ──────────────────────────────────────────────────────────────────


def register_todo_tools(registry: ToolRegistry, client: GraphClient) -> int:
    """Register all 8 To Do tools. Returns count."""

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(**kwargs: Any) -> str:
            return await fn(client, **kwargs)
        return wrapper

    registry.register(
        name="ms_todo__list_task_lists",
        description="List all Microsoft To Do task lists.",
        handler=_h(ms_todo__list_task_lists),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    registry.register(
        name="ms_todo__list_tasks",
        description="List tasks in a Microsoft To Do list.",
        handler=_h(ms_todo__list_tasks),
        parameters={
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
                "max_results": {"type": "integer", "default": 50},
                "include_completed": {"type": "boolean", "default": False},
            },
            "required": ["list_id"],
        },
    )
    registry.register(
        name="ms_todo__get_task",
        description="Get details of a Microsoft To Do task.",
        handler=_h(ms_todo__get_task),
        parameters={
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["list_id", "task_id"],
        },
    )
    registry.register(
        name="ms_todo__create_task",
        description="Create a new Microsoft To Do task.",
        handler=_h(ms_todo__create_task),
        parameters={
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
                "title": {"type": "string"},
                "due_date": {"type": "string", "default": "", "description": "Due date ISO 8601"},
                "importance": {"type": "string", "default": "normal", "description": "low, normal, or high"},
                "body": {"type": "string", "default": ""},
            },
            "required": ["list_id", "title"],
        },
        require_approval=True,
    )
    registry.register(
        name="ms_todo__update_task",
        description="Update a Microsoft To Do task (title, due date, importance, body).",
        handler=_h(ms_todo__update_task),
        parameters={
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
                "task_id": {"type": "string"},
                "title": {"type": "string", "default": ""},
                "due_date": {"type": "string", "default": ""},
                "importance": {"type": "string", "default": ""},
                "body": {"type": "string", "default": ""},
            },
            "required": ["list_id", "task_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="ms_todo__complete_task",
        description="Mark a Microsoft To Do task as completed.",
        handler=_h(ms_todo__complete_task),
        parameters={
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["list_id", "task_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="ms_todo__delete_task",
        description="Delete a Microsoft To Do task.",
        handler=_h(ms_todo__delete_task),
        parameters={
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["list_id", "task_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="ms_todo__create_task_list",
        description="Create a new Microsoft To Do task list.",
        handler=_h(ms_todo__create_task_list),
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        require_approval=True,
    )
    return 8
