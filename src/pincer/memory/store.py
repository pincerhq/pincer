"""Backward-compatibility shim. Use SQLiteMemoryBackend directly for new code."""

from pincer.memory.sqlite import SQLiteMemoryBackend as MemoryStore

__all__ = ["MemoryStore"]
