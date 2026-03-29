"""
Google Tasks tools — 8 tools for reading and managing task lists and tasks.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any

from pincer.integrations.google.models import fmt_task
from pincer.integrations.google.quota import with_backoff

if TYPE_CHECKING:
    from collections.abc import Callable

    from pincer.integrations.google.service_factory import GoogleServiceFactory
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ── Tool implementations ──────────────────────────────────────────────────────

async def google__list_task_lists(factory: GoogleServiceFactory) -> str:
    """List all task lists."""
    svc = await factory.get("tasks")
    result = await with_backoff(lambda: svc.tasklists().list(maxResults=100).execute())
    lists = result.get("items", [])
    if not lists:
        return "No task lists found."
    lines = [f"  {t.get('title', '?')} (id={t['id']})" for t in lists]
    return f"{len(lines)} task list(s):\n" + "\n".join(lines)


async def google__list_tasks(
    factory: GoogleServiceFactory,
    tasklist_id: str = "@default",
    show_completed: bool = False,
    max_results: int = 100,
) -> str:
    """List tasks in a task list."""
    svc = await factory.get("tasks")
    result = await with_backoff(
        lambda: svc.tasks().list(
            tasklist=tasklist_id,
            showCompleted=show_completed,
            maxResults=max_results,
        ).execute()
    )
    tasks = result.get("items", [])
    if not tasks:
        return f"No tasks found in list {tasklist_id}."
    lines = [fmt_task(t) for t in tasks]
    return f"{len(lines)} task(s):\n" + "\n".join(lines)


async def google__get_task(
    factory: GoogleServiceFactory,
    task_id: str,
    tasklist_id: str = "@default",
) -> str:
    """Get details of a specific task."""
    svc = await factory.get("tasks")
    task = await with_backoff(
        lambda: svc.tasks().get(tasklist=tasklist_id, task=task_id).execute()
    )
    return fmt_task(task)


async def google__create_task(
    factory: GoogleServiceFactory,
    title: str,
    tasklist_id: str = "@default",
    notes: str = "",
    due: str = "",
) -> str:
    """Create a new task."""
    svc = await factory.get("tasks")
    body: dict[str, str] = {"title": title}
    if notes:
        body["notes"] = notes
    if due:
        body["due"] = due  # RFC 3339 format
    result = await with_backoff(
        lambda: svc.tasks().insert(tasklist=tasklist_id, body=body).execute()
    )
    return f"Task created: '{result.get('title', title)}' (id={result.get('id', '')})"


async def google__update_task(
    factory: GoogleServiceFactory,
    task_id: str,
    tasklist_id: str = "@default",
    title: str = "",
    notes: str = "",
    due: str = "",
    status: str = "",
) -> str:
    """Update task fields."""
    svc = await factory.get("tasks")
    task = await with_backoff(
        lambda: svc.tasks().get(tasklist=tasklist_id, task=task_id).execute()
    )
    if title:
        task["title"] = title
    if notes:
        task["notes"] = notes
    if due:
        task["due"] = due
    if status:
        task["status"] = status
    result = await with_backoff(
        lambda: svc.tasks().update(tasklist=tasklist_id, task=task_id, body=task).execute()
    )
    return f"Task updated: '{result.get('title', task_id)}'"


async def google__complete_task(
    factory: GoogleServiceFactory,
    task_id: str,
    tasklist_id: str = "@default",
) -> str:
    """Mark a task as completed."""
    svc = await factory.get("tasks")
    task = await with_backoff(
        lambda: svc.tasks().get(tasklist=tasklist_id, task=task_id).execute()
    )
    task["status"] = "completed"
    result = await with_backoff(
        lambda: svc.tasks().update(tasklist=tasklist_id, task=task_id, body=task).execute()
    )
    return f"Task completed: '{result.get('title', task_id)}'"


async def google__delete_task(
    factory: GoogleServiceFactory,
    task_id: str,
    tasklist_id: str = "@default",
) -> str:
    """Delete a task."""
    svc = await factory.get("tasks")
    await with_backoff(lambda: svc.tasks().delete(tasklist=tasklist_id, task=task_id).execute())
    return f"Task {task_id} deleted."


async def google__create_task_list(
    factory: GoogleServiceFactory,
    title: str,
) -> str:
    """Create a new task list."""
    svc = await factory.get("tasks")
    result = await with_backoff(
        lambda: svc.tasklists().insert(body={"title": title}).execute()
    )
    return f"Task list created: '{result.get('title', title)}' (id={result.get('id', '')})"


# ── Registry ──────────────────────────────────────────────────────────────────

def register_tasks_tools(registry: ToolRegistry, factory: GoogleServiceFactory) -> int:
    """Register all 8 Tasks tools. Returns count."""

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(**kwargs):  # type: ignore[no-untyped-def]
            return await fn(factory, **kwargs)
        return wrapper

    registry.register(
        name="google__list_task_lists",
        description="List all Google Task lists.",
        handler=_h(google__list_task_lists),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    registry.register(
        name="google__list_tasks",
        description="List tasks in a Google Tasks list.",
        handler=_h(google__list_tasks),
        parameters={
            "type": "object",
            "properties": {
                "tasklist_id": {
                    "type": "string",
                    "description": "Task list ID (default: @default)",
                    "default": "@default",
                },
                "show_completed": {"type": "boolean", "description": "Include completed tasks", "default": False},
                "max_results": {"type": "integer", "default": 100},
            },
            "required": [],
        },
    )
    registry.register(
        name="google__get_task",
        description="Get details of a specific task by ID.",
        handler=_h(google__get_task),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "tasklist_id": {"type": "string", "default": "@default"},
            },
            "required": ["task_id"],
        },
    )
    registry.register(
        name="google__create_task",
        description="Create a new task in Google Tasks.",
        handler=_h(google__create_task),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "tasklist_id": {"type": "string", "default": "@default"},
                "notes": {"type": "string", "description": "Task notes", "default": ""},
                "due": {
                    "type": "string",
                    "description": "Due date in RFC 3339 format (e.g. 2026-03-28T00:00:00Z)",
                    "default": "",
                },
            },
            "required": ["title"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__update_task",
        description="Update a task's title, notes, due date, or status.",
        handler=_h(google__update_task),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "tasklist_id": {"type": "string", "default": "@default"},
                "title": {"type": "string", "default": ""},
                "notes": {"type": "string", "default": ""},
                "due": {"type": "string", "default": ""},
                "status": {"type": "string", "enum": ["needsAction", "completed"], "default": ""},
            },
            "required": ["task_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__complete_task",
        description="Mark a Google Task as completed.",
        handler=_h(google__complete_task),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "tasklist_id": {"type": "string", "default": "@default"},
            },
            "required": ["task_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__delete_task",
        description="Delete a task from Google Tasks.",
        handler=_h(google__delete_task),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "tasklist_id": {"type": "string", "default": "@default"},
            },
            "required": ["task_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__create_task_list",
        description="Create a new Google Tasks list.",
        handler=_h(google__create_task_list),
        parameters={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
        require_approval=True,
    )
    return 8
