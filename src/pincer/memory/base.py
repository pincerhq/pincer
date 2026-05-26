"""Abstract memory backend interface.

All memory backends must implement this interface. The SQLite backend is the
default; the MCP backend delegates to an external sqlite-vec-memory MCP server.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

PINCER_MEMORY_USER_TAG_PREFIX = "user"
PINCER_MEMORY_CATEGORY_TAG_PREFIX = "category"
PINCER_MEMORY_COUNT_FETCH_LIMIT = 50
PINCER_MEMORY_USER_REQUEST_PREFIX = "User asked"
PINCER_MEMORY_ASSISTANT_RESPONSE_PREFIX = "Assistant replied"


@dataclass(frozen=True, slots=True)
class Memory:
    id: str
    user_id: str
    content: str
    category: str
    created_at: float
    score: float = 0.0
    tags: list[str] = field(default_factory=list)


class BaseMemoryBackend(ABC):
    """Pluggable memory storage backend."""

    # ── Lifecycle ─────────────────────────────────────────

    @abstractmethod
    async def initialize(self) -> None:
        """Set up storage (open connections, create schema, etc.)."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""

    # ── Core CRUD ─────────────────────────────────────────

    @abstractmethod
    async def store_memory(
        self,
        user_id: str,
        content: str,
        category: str = "general",
        embedding: list[float] | None = None,
        extra_tags: list[str] | None = None,
    ) -> str:
        """Persist a memory entry. Returns the new memory ID.

        extra_tags are merged into the tag list alongside the default
        user:{user_id} and category:{category} tags.
        """

    @abstractmethod
    async def get_memory(self, memory_id: str) -> Memory | None:
        """Fetch a single memory by ID. Returns None if not found."""

    @abstractmethod
    async def list_memories(
        self,
        user_id: str | None = None,
        limit: int = 10,
        offset: int = 0,
        category: str | None = None,
        tags: list[str] | None = None,
        match_all_tags: bool = False,
    ) -> list[Memory]:
        """List memories, newest first, with optional filtering and offset pagination.

        If user_id is None, list across all users.
        tags with match_all_tags=False (default) returns records matching ANY tag (OR logic).
        tags with match_all_tags=True returns only records matching ALL tags (AND logic).
        Backends that do not support offset (e.g. MCP) may silently ignore it.
        """

    @abstractmethod
    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Update an existing memory in place.

        Only provided (non-None) fields are changed.
        Note: some backends (e.g. MCP) may not preserve the original ID after
        an update — callers must not assume ID stability.
        """

    @abstractmethod
    async def delete_memory(self, memory_id: str) -> None:
        """Delete a single memory by ID."""

    @abstractmethod
    async def delete_user_memories(self, user_id: str) -> int:
        """Delete all memories belonging to a user. Returns the number of deleted records."""

    @abstractmethod
    async def count(self, user_id: str | None = None) -> int:
        """Return the total number of stored memories, optionally scoped to one user."""

    # ── Search ────────────────────────────────────────────

    @abstractmethod
    async def search_text(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = 5,
        tags: list[str] | None = None,
    ) -> list[Memory]:
        """Keyword or full-text search over memory content.

        tags filters use OR logic. Backends that do not support tag filtering
        on search may silently ignore this parameter.
        """

    async def search_similar(
        self,
        embedding: list[float],
        user_id: str | None = None,
        limit: int = 5,
        tags: list[str] | None = None,
    ) -> list[Memory]:
        """Vector similarity search. Raises NotImplementedError if not supported."""
        raise NotImplementedError
