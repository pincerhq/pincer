"""Abstract memory backend interface.

All memory backends must implement this interface. The SQLite backend is the
default; the MCP backend delegates to an external sqlite-vec-memory MCP server.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Memory:
    id: str
    user_id: str
    content: str
    category: str
    created_at: float
    score: float = 0.0


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
        user:{user_id} and category:{category} tags.  Backends that do not
        use a tag model (e.g. SQLite column-based) may ignore this parameter.
        """

    async def get_memory(self, memory_id: str) -> Memory | None:
        """Fetch a single memory by ID. Returns None if not supported or not found."""
        return None

    @abstractmethod
    async def list_memories(
        self,
        user_id: str | None = None,
        limit: int = 10,
        category: str | None = None,
    ) -> list[Memory]:
        """List memories, newest first. If user_id is None, list across all users."""

    @abstractmethod
    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        category: str | None = None,
    ) -> None:
        """Update an existing memory in place.

        Note: some backends (e.g. MCP) may not preserve the original ID after
        an update — callers must not assume ID stability.
        """

    @abstractmethod
    async def delete_memory(self, memory_id: str) -> None:
        """Delete a single memory by ID."""

    @abstractmethod
    async def delete_user_memories(self, user_id: str) -> None:
        """Delete all memories belonging to a user."""

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
    ) -> list[Memory]:
        """Keyword or full-text search over memory content."""

    async def search_similar(
        self,
        embedding: list[float],
        user_id: str | None = None,
        limit: int = 5,
    ) -> list[Memory]:
        """Vector similarity search. Raises NotImplementedError if not supported."""
        raise NotImplementedError
