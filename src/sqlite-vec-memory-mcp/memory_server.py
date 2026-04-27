"""
SQLite-vec memory MCP server.

Stores and retrieves text memories using sqlite_vec for KNN semantic search.
Embeddings are computed locally via ONNX (fastembed) — no external API needed.

Environment variables:
    MEMORY_DB_PATH      Path to the SQLite database file (default: ~/memory.db)
    EMBED_MODEL         fastembed model name (default: BAAI/bge-small-en-v1.5)
    EMBED_DIM           Vector dimension — must match model (default: 384)
    FASTEMBED_CACHE_DIR Model cache directory (default: ~/.cache/fastembed)
    TRANSPORT           stdio | http (default: stdio)
    HOST                HTTP host (default: 127.0.0.1)
    PORT                HTTP port (default: 8000)
"""

from __future__ import annotations

import json
import os
import sqlite3
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlite_vec
import uvicorn
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from starlette.middleware.cors import CORSMiddleware

if TYPE_CHECKING:
    from starlette.requests import Request

mcp = FastMCP(
    name="sqlite_vec_memory",
    instructions="Long-term memory with semantic search."
)

_DB_PATH = Path(os.environ.get("MEMORY_DB_PATH", Path.home() / "memory.db"))
_EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5") # ex. all-MiniLM-L6-v2
_EMBED_DIM = int(os.environ.get("EMBED_DIM", "384"))

# Lazy singleton — loaded once on first embedding call.
_embed_model: Any = None


# ---------------------------------------------------------------------------
# Embeddings (ONNX via fastembed)
# ---------------------------------------------------------------------------

def _get_embed_model() -> Any:
    global _embed_model
    if _embed_model is None:
        from fastembed import TextEmbedding
        _embed_model = TextEmbedding(model_name=_EMBED_MODEL)
    return _embed_model


def _embed(text: str) -> bytes:
    """Return float32 bytes ready for sqlite_vec."""
    model = _get_embed_model()
    vec = next(model.embed([text]))          # numpy float32 array
    return struct.pack(f"{len(vec)}f", *vec)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(_DB_PATH)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.row_factory = sqlite3.Row
    return db


def _init_db() -> None:
    db = _connect()
    db.executescript(f"""
        CREATE TABLE IF NOT EXISTS memories (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            content    TEXT    NOT NULL,
            tags       TEXT    NOT NULL DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_vecs USING vec0(
            embedding float[{_EMBED_DIM}]
        );
    """)
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@mcp.custom_route("/", methods=["GET"])
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "healthy",
            "service": "pomodoro-mcp",
            "version": "0.1.0",
            "transport": "http",
        }
    )

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def memory_store(content: str, tags: list[str] | None = None) -> str:
    """Store a memory and index it for semantic search. Returns the memory ID."""
    vec = _embed(content)
    db = _connect()
    cur = db.execute(
        "INSERT INTO memories (content, tags) VALUES (?, ?)",
        (content, json.dumps(tags or [])),
    )
    mid = cur.lastrowid
    db.execute(
        "INSERT INTO memory_vecs (rowid, embedding) VALUES (?, ?)",
        (mid, vec),
    )
    db.commit()
    db.close()
    return f"stored:{mid}"


@mcp.tool()
def memory_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search memories by semantic similarity. Returns closest matches first."""
    vec = _embed(query)
    db = _connect()
    rows = db.execute(
        """
        SELECT m.id, m.content, m.tags, m.created_at, v.distance
        FROM memory_vecs v
        JOIN memories m ON m.id = v.rowid
        WHERE v.embedding MATCH ?
          AND k = ?
        ORDER BY v.distance
        """,
        (vec, limit),
    ).fetchall()
    db.close()
    return [
        {
            "id": r["id"],
            "content": r["content"],
            "tags": json.loads(r["tags"]),
            "created_at": r["created_at"],
            "distance": round(r["distance"], 6),
        }
        for r in rows
    ]


@mcp.tool()
def memory_list(limit: int = 20, tag: str | None = None) -> list[dict[str, Any]]:
    """List recent memories, optionally filtered by tag."""
    db = _connect()
    if tag:
        rows = db.execute(
            """
            SELECT DISTINCT m.id, m.content, m.tags, m.created_at
            FROM memories m, json_each(m.tags) t
            WHERE t.value = ?
            ORDER BY m.created_at DESC LIMIT ?
            """,
            (tag, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, content, tags, created_at FROM memories ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    db.close()
    return [
        {
            "id": r["id"],
            "content": r["content"],
            "tags": json.loads(r["tags"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@mcp.tool()
def memory_delete(memory_id: int) -> str:
    """Delete a memory by ID."""
    db = _connect()
    db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    db.execute("DELETE FROM memory_vecs WHERE rowid = ?", (memory_id,))
    db.commit()
    db.close()
    return f"deleted:{memory_id}"


@mcp.tool()
def memory_update(memory_id: int, content: str, tags: list[str] | None = None) -> str:
    """Replace a memory's content and re-index it with a fresh embedding."""
    vec = _embed(content)
    db = _connect()
    db.execute(
        "UPDATE memories SET content = ?, tags = ? WHERE id = ?",
        (content, json.dumps(tags or []), memory_id),
    )
    db.execute(
        "UPDATE memory_vecs SET embedding = ? WHERE rowid = ?",
        (vec, memory_id),
    )
    db.commit()
    db.close()
    return f"updated:{memory_id}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    _init_db()

    parser = argparse.ArgumentParser(description="Memory MCP server (fastmcp)")
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default=os.environ.get("TRANSPORT", "stdio"),
        help="Transport: 'http' (streamable HTTP) or 'stdio' (default)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Bind host for HTTP transport (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=os.environ.get("PORT", 8000),
        help="Bind port for HTTP transport (default: 8000)",
    )
    args = parser.parse_args()

    if args.transport == "http":
        print(f"Starting sqlite_vec_memory_mcp · HTTP transport · {args.host}:{args.port}/mcp")
        app = mcp.http_app()
        cors_app = CORSMiddleware(app=app, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
        uvicorn.run(cors_app, host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
