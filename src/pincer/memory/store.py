"""Backward-compatibility shim. Use SqlMemoryBackend directly for new code."""

from pincer.memory.sqlite import SqlMemoryBackend as MemoryStore

__all__ = ["MemoryStore"]
