"""`pincer memory` — manage local sqlite based conversation memory."""

from __future__ import annotations

import typer
from async_typer import AsyncTyper

from pincer.cli._shared import console

memory_app = AsyncTyper(
    name="memory", help="Manage local sqlite based conversation memory. MCP servers are not supported yet!"
)


@memory_app.command(name="search")
async def memory_search(query: str = typer.Argument(help="Search query")) -> None:
    """Search conversation memory."""
    await _memory_search(query)


async def _memory_search(query: str) -> None:
    from pincer.config import get_settings_relaxed
    from pincer.memory.sqlite import SQLiteMemoryBackend

    settings = get_settings_relaxed()
    store = SQLiteMemoryBackend(settings.db_path)
    await store.initialize()
    results = await store.search_text(query, limit=10)
    if not results:
        console.print("[dim]No memories found.[/dim]")
    else:
        for i, mem in enumerate(results, 1):
            console.print(f"  {i}. [{mem.category}] {mem.content[:200]}")
    await store.close()


@memory_app.command(name="stats")
async def memory_stats() -> None:
    """Show memory usage statistics."""
    await _memory_stats()


async def _memory_stats() -> None:
    from pincer.config import get_settings_relaxed
    from pincer.memory.sqlite import SQLiteMemoryBackend

    settings = get_settings_relaxed()
    store = SQLiteMemoryBackend(settings.db_path)
    await store.initialize()

    total = await store.count()
    assert store._db is not None
    async with store._db.execute("SELECT COUNT(DISTINCT user_id) FROM memories") as cur:
        row = await cur.fetchone()
        users = row[0] if row else 0
    async with store._db.execute("SELECT category, COUNT(*) FROM memories GROUP BY category") as cur:
        categories = {r[0]: r[1] async for r in cur}

    console.print("[bold]Memory Stats[/bold]")
    console.print(f"  Total memories: {total}")
    console.print(f"  Users:          {users}")
    for cat, count in categories.items():
        console.print(f"  {cat}: {count}")
    await store.close()


@memory_app.command(name="clear")
async def memory_clear(
    user_id: str = typer.Option(..., "--user", help="User ID to clear"),
) -> None:
    """Clear memory for a user."""
    await _memory_clear(user_id)


async def _memory_clear(user_id: str) -> None:
    from pincer.config import get_settings_relaxed
    from pincer.memory.sqlite import SQLiteMemoryBackend

    settings = get_settings_relaxed()
    store = SQLiteMemoryBackend(settings.db_path)
    await store.initialize()
    await store.delete_user_memories(user_id)
    console.print(f"[green]Cleared memories for {user_id}[/green]")
    await store.close()


@memory_app.command(name="list")
async def memory_list(
    user_id: str = typer.Option(None, "--user", help="Filter by user ID"),
    tags: str = typer.Option(None, "--tags", help="Comma-separated tag filter (OR logic)"),
    limit: int = typer.Option(20, "--limit", help="Records per page"),
    offset: int = typer.Option(0, "--offset", help="Pagination offset"),
) -> None:
    """List memories, newest first, with optional user, tag, and pagination filters."""
    await _memory_list(user_id, tags, limit, offset)


async def _memory_list(user_id: str | None, tags_str: str | None, limit: int, offset: int) -> None:
    from pincer.config import get_settings_relaxed
    from pincer.memory.sqlite import SQLiteMemoryBackend

    settings = get_settings_relaxed()
    store = SQLiteMemoryBackend(settings.db_path)
    await store.initialize()

    tag_list = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else None
    memories = await store.list_memories(user_id=user_id, limit=limit, offset=offset, tags=tag_list)

    if not memories:
        console.print("[dim]No memories found.[/dim]")
    else:
        for mem in memories:
            tag_str = f" {mem.tags}" if mem.tags else ""
            console.print(f"  [{mem.category}]{tag_str} {mem.content[:200]}")
        console.print(f"\n[dim]Showing {len(memories)} records (offset={offset})[/dim]")

    await store.close()


@memory_app.command(name="export")
async def memory_export(
    user_id: str = typer.Option(..., "--user", help="User ID to export"),
    output: str = typer.Option("memories.json", "--output", help="Output file"),
) -> None:
    """Export user memories to JSON."""
    await _memory_export(user_id, output)


async def _memory_export(user_id: str, output: str) -> None:
    import json as _json
    from pathlib import Path as _P

    from pincer.config import get_settings_relaxed
    from pincer.memory.sqlite import SQLiteMemoryBackend

    settings = get_settings_relaxed()
    store = SQLiteMemoryBackend(settings.db_path)
    await store.initialize()

    memories = await store.list_memories(user_id=user_id, limit=100_000)
    records = [{"content": m.content, "category": m.category, "created_at": m.created_at} for m in memories]

    _P(output).write_text(_json.dumps(records, indent=2))
    console.print(f"[green]Exported {len(records)} memories to {output}[/green]")
    await store.close()
