"""Calendar names shared by tests in different directories."""

from __future__ import annotations

#: The builtin calendar tools removed from the model's registry (calendar reliability, slice D):
#: the Workspace ``google__*`` tools are the one calendar toolset, on chats and calls alike.
BUILTIN_CALENDAR_TOOLS = frozenset({"calendar_today", "calendar_week", "calendar_create"})
