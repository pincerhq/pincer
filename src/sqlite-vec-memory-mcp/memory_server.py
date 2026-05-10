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
import logging
import os
import sqlite3
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlite_vec
import uvicorn
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastmcp import FastMCP
from starlette.middleware.cors import CORSMiddleware

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)

mcp = FastMCP(name="sqlite_vec_memory", instructions="Long-term memory with semantic search.")

_DB_PATH = Path(os.environ.get("MEMORY_DB_PATH", Path.home() / "memory.db"))
_EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5")  # ex. all-MiniLM-L6-v2
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
    vec = next(model.embed([text]))  # numpy float32 array
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


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "healthy",
            "service": "sqlite-vec-memory-mcp",
            "version": "0.1.0",
            "transport": "http",
            "ui": "/ui",
        }
    )


@mcp.custom_route("/", methods=["GET"])
async def root_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse("/ui")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def memory_store(content: str, tags: list[str] | None = None) -> str:
    """Store a memory and index it for semantic search. Returns the memory ID."""
    logger.info("tool: memory_store")
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


def _normalize_tags(tags: str | list[str] | None) -> list[str]:
    """Accept a single tag string or a list of tags; always return a list."""
    if tags is None:
        return []
    if isinstance(tags, str):
        return [tags]
    return list(tags)


@mcp.tool()
def memory_search(
    query: str,
    limit: int = 5,
    tags: str | list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search memories by semantic similarity, with optional tag filtering (OR logic).

    tags: a single tag string or a list of tags — returns records matching ANY tag.
    Fetch extra candidates from KNN when filtering so the result set stays full.
    """
    tag_list = _normalize_tags(tags)
    fetch_k = limit * 4 if tag_list else limit
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
        (vec, fetch_k),
    ).fetchall()
    db.close()

    results = []
    tag_set = set(tag_list)
    for r in rows:
        record_tags = json.loads(r["tags"]) if r["tags"] else []
        if tag_set and not tag_set.intersection(record_tags):
            continue
        results.append({
            "id": r["id"],
            "content": r["content"],
            "tags": record_tags,
            "created_at": r["created_at"],
            "distance": round(r["distance"], 6),
        })
        if len(results) >= limit:
            break
    return results


@mcp.tool()
def memory_list(
    limit: int = 20,
    tags: str | list[str] | None = None,
) -> list[dict[str, Any]]:
    """List recent memories, optionally filtered by tags (OR logic).

    tags: a single tag string or a list of tags — returns records matching ANY tag.
    """
    tag_list = _normalize_tags(tags)
    db = _connect()
    if tag_list:
        placeholders = ",".join("?" * len(tag_list))
        rows = db.execute(
            f"""
            SELECT DISTINCT m.id, m.content, m.tags, m.created_at
            FROM memories m, json_each(m.tags) t
            WHERE t.value IN ({placeholders})
            ORDER BY m.created_at DESC LIMIT ?
            """,  # noqa: S608
            (*tag_list, limit),
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
    logger.info("tool: memory_delete")
    db = _connect()
    db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    db.execute("DELETE FROM memory_vecs WHERE rowid = ?", (memory_id,))
    db.commit()
    db.close()
    return f"deleted:{memory_id}"


@mcp.tool()
def memory_update(memory_id: int, content: str, tags: list[str] | None = None) -> str:
    """Replace a memory's content and re-index it with a fresh embedding."""
    logger.info("tool: memory_update")
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
# Web UI
# ---------------------------------------------------------------------------

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, -apple-system, sans-serif; font-size: 14px;
       background: #f5f5f5; color: #222; }
.wrap { max-width: 1100px; margin: 0 auto; padding: 24px 16px; }
h1 { font-size: 20px; font-weight: 600; margin-bottom: 20px; }
h2 { font-size: 16px; font-weight: 600; margin-bottom: 14px; }
a { color: #2563eb; text-decoration: none; }
a:hover { text-decoration: underline; }
.bar { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
.bar form { display: flex; gap: 6px; flex: 1; }
input[type=text] { border: 1px solid #d1d5db; border-radius: 6px;
                   padding: 6px 10px; font-size: 13px; flex: 1; }
input[type=text]:focus { outline: none; border-color: #2563eb; }
.btn { display: inline-block; padding: 6px 14px; border-radius: 6px;
       font-size: 13px; cursor: pointer; border: none; font-weight: 500; }
.btn-primary { background: #2563eb; color: #fff; }
.btn-primary:hover { background: #1d4ed8; }
.btn-danger { background: #dc2626; color: #fff; }
.btn-danger:hover { background: #b91c1c; }
.btn-ghost { background: #e5e7eb; color: #374151; }
.btn-ghost:hover { background: #d1d5db; }
.badge { display: inline-block; background: #dbeafe; color: #1e40af;
         border-radius: 4px; padding: 2px 7px; font-size: 11px; margin: 2px; }
table { width: 100%; border-collapse: collapse; background: #fff;
        border-radius: 8px; overflow: hidden;
        box-shadow: 0 1px 3px rgba(0,0,0,.07); }
th { background: #f9fafb; font-weight: 600; text-align: left;
     padding: 10px 14px; border-bottom: 1px solid #e5e7eb; }
td { padding: 10px 14px; border-bottom: 1px solid #f3f4f6; vertical-align: top; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: #f9fafb; }
.content-cell { max-width: 480px; word-break: break-word; }
.ts { color: #6b7280; font-size: 12px; white-space: nowrap; }
.actions { display: flex; gap: 6px; }
.pager { display: flex; gap: 6px; margin-top: 16px; align-items: center; }
.pager a, .pager span { padding: 5px 11px; border-radius: 6px; font-size: 13px; }
.pager a { background: #fff; border: 1px solid #d1d5db; }
.pager a:hover { background: #f3f4f6; text-decoration: none; }
.pager .cur { background: #2563eb; color: #fff; border: 1px solid #2563eb; }
.pager .dots { color: #9ca3af; border: none; }
.card { background: #fff; border-radius: 8px; padding: 20px;
        box-shadow: 0 1px 3px rgba(0,0,0,.07); }
.field { margin-bottom: 14px; }
.label { font-size: 11px; font-weight: 600; text-transform: uppercase;
         color: #6b7280; letter-spacing: .05em; margin-bottom: 4px; }
.value { word-break: break-word; line-height: 1.6; }
.empty { text-align: center; color: #9ca3af; padding: 40px; }
.count { color: #6b7280; font-size: 13px; }
"""


def _page(title: str, body: str) -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Memory</title>
<style>{_CSS}</style>
</head>
<body><div class="wrap">{body}</div></body>
</html>"""
    return HTMLResponse(html)


def _fmt_ts(ts: str) -> str:
    return ts[:19] if ts else "—"


def _tags_html(tags_json: str) -> str:
    try:
        tags = json.loads(tags_json) if tags_json else []
    except Exception:
        tags = []
    return "".join(f'<span class="badge">{t}</span>' for t in tags) or '<span class="ts">—</span>'


@mcp.custom_route("/ui", methods=["GET"])
async def ui_list(request: Request) -> HTMLResponse:
    q = request.query_params.get("q", "").strip()
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    per_page = 25
    offset = (page - 1) * per_page

    db = _connect()
    if q:
        rows = db.execute(
            "SELECT id, content, tags, created_at FROM memories"
            " WHERE content LIKE ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (f"%{q}%", per_page, offset),
        ).fetchall()
        total = db.execute(
            "SELECT COUNT(*) FROM memories WHERE content LIKE ?", (f"%{q}%",)
        ).fetchone()[0]
    else:
        rows = db.execute(
            "SELECT id, content, tags, created_at FROM memories ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ).fetchall()
        total = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    db.close()

    q_enc = q.replace('"', "&quot;")
    search_bar = f"""
<div class="bar">
  <form method="get" action="/ui">
    <input type="text" name="q" value="{q_enc}" placeholder="Search content…">
    <button class="btn btn-primary" type="submit">Search</button>
    {"<a class='btn btn-ghost' href='/ui'>Clear</a>" if q else ""}
  </form>
  <span class="count">{total} record{"s" if total != 1 else ""}</span>
</div>"""

    if rows:
        rows_html = ""
        for r in rows:
            snippet = r["content"][:200] + ("…" if len(r["content"]) > 200 else "")
            rows_html += f"""<tr>
  <td class="ts">{r["id"]}</td>
  <td class="content-cell">{snippet}</td>
  <td>{_tags_html(r["tags"])}</td>
  <td class="ts">{_fmt_ts(r["created_at"])}</td>
  <td><div class="actions">
    <a class="btn btn-ghost" href="/ui/detail?id={r["id"]}">View</a>
    <form method="post" action="/ui/delete" onsubmit="return confirm('Delete this memory?')">
      <input type="hidden" name="id" value="{r["id"]}">
      <input type="hidden" name="q" value="{q_enc}">
      <input type="hidden" name="page" value="{page}">
      <button class="btn btn-danger" type="submit">Delete</button>
    </form>
  </div></td>
</tr>"""
        table = f"""<table>
<thead><tr>
  <th style="width:60px">ID</th><th>Content</th><th>Tags</th>
  <th style="width:150px">Created</th><th style="width:140px">Actions</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>"""
    else:
        table = '<div class="empty">No records found.</div>'

    # Pagination
    total_pages = max(1, (total + per_page - 1) // per_page)
    pager = ""
    if total_pages > 1:
        def _page_link(p: int, label: str | None = None) -> str:
            lbl = label or str(p)
            params = f"page={p}" + (f"&q={q_enc}" if q else "")
            if p == page:
                return f'<span class="cur">{lbl}</span>'
            return f'<a href="/ui?{params}">{lbl}</a>'

        links = []
        if page > 1:
            links.append(_page_link(page - 1, "← Prev"))
        for p in range(1, total_pages + 1):
            if p == 1 or p == total_pages or abs(p - page) <= 2:
                links.append(_page_link(p))
            elif links and links[-1] != '<span class="dots">…</span>':
                links.append('<span class="dots">…</span>')
        if page < total_pages:
            links.append(_page_link(page + 1, "Next →"))
        pager = f'<div class="pager">{"".join(links)}</div>'

    body = f'<h1>Memory Records</h1>{search_bar}{table}{pager}'
    return _page("List", body)


@mcp.custom_route("/ui/detail", methods=["GET"])
async def ui_detail(request: Request) -> HTMLResponse:
    mem_id = request.query_params.get("id", "").strip()
    if not mem_id:
        return RedirectResponse("/ui")  # type: ignore[return-value]

    db = _connect()
    row = db.execute(
        "SELECT id, content, tags, created_at FROM memories WHERE id = ?", (int(mem_id),)
    ).fetchone()
    db.close()

    if not row:
        body = f'<a class="btn btn-ghost" href="/ui">← Back</a><div class="empty" style="margin-top:24px">Memory #{mem_id} not found.</div>'
        return _page("Not found", body)

    body = f"""
<div style="margin-bottom:16px"><a class="btn btn-ghost" href="/ui">← Back to list</a></div>
<h1>Memory #{row["id"]}</h1>
<div class="card">
  <div class="field">
    <div class="label">ID</div>
    <div class="value">{row["id"]}</div>
  </div>
  <div class="field">
    <div class="label">Created at</div>
    <div class="value ts">{_fmt_ts(row["created_at"])}</div>
  </div>
  <div class="field">
    <div class="label">Tags</div>
    <div class="value">{_tags_html(row["tags"])}</div>
  </div>
  <div class="field">
    <div class="label">Content</div>
    <div class="value" style="white-space:pre-wrap">{row["content"]}</div>
  </div>
  <form method="post" action="/ui/delete" onsubmit="return confirm('Delete this memory permanently?')">
    <input type="hidden" name="id" value="{row["id"]}">
    <button class="btn btn-danger" type="submit">Delete this memory</button>
  </form>
</div>"""
    return _page(f"Memory #{row['id']}", body)


@mcp.custom_route("/ui/delete", methods=["POST"])
async def ui_delete(request: Request) -> RedirectResponse:
    form = await request.form()
    mem_id = form.get("id", "")
    q = form.get("q", "")
    page = form.get("page", "1")
    if mem_id:
        db = _connect()
        db.execute("DELETE FROM memories WHERE id = ?", (int(mem_id),))
        db.execute("DELETE FROM memory_vecs WHERE rowid = ?", (int(mem_id),))
        db.commit()
        db.close()
    params = f"page={page}" + (f"&q={q}" if q else "")
    return RedirectResponse(f"/ui?{params}", status_code=303)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    _init_db()

    try:
        from pincer_telemetry import init as _init_telemetry

        _init_telemetry(project_name="sqlite_vec_memory_mcp", version="0.1.0")
        logger.info("Telemetry init success")
    except ImportError:
        logger.warning(
            "OTEL_DSN is set but pincer-telemetry is not installed — "
            'skipping. Install with: pip install "sqlite-vec-memory-mcp[telemetry]"'
        )
    except Exception as _tel_err:
        logger.error("Telemetry init failed (non-fatal): %s", _tel_err)

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
        logger.info("Starting sqlite_vec_memory_mcp · HTTP transport · %s:%s/mcp", args.host, args.port)
        app = mcp.http_app()
        cors_app = CORSMiddleware(app=app, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
        uvicorn.run(cors_app, host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
