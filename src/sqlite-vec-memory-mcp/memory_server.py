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
    SERVER_UI           Enable the web UI in HTTP mode (default: 0 — disabled)
"""

from __future__ import annotations

import functools
import html as _html
import inspect
import json
import logging
import os
import sqlite3
import struct
import time as _time
import uuid
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
_UI_ENABLED: bool = os.environ.get("SERVER_UI", "0").lower() in ("1", "true", "yes")

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


def _migrate_id_to_uuid(db: sqlite3.Connection) -> None:
    """Migrate memories.id from INTEGER AUTOINCREMENT to UUID TEXT if needed."""
    col_info = db.execute("PRAGMA table_info(memories)").fetchall()
    id_col = next((c for c in col_info if c["name"] == "id"), None)
    if id_col is None or id_col["type"].upper() != "INTEGER":
        return

    logger.info("Migrating memories.id from INTEGER to UUID TEXT …")
    db.execute("ALTER TABLE memories RENAME TO memories_v1")
    db.execute("""
        CREATE TABLE memories (
            id         TEXT      PRIMARY KEY,
            content    TEXT      NOT NULL,
            tags       TEXT      NOT NULL DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    old_rows = db.execute("SELECT content, tags, created_at FROM memories_v1").fetchall()
    for row in old_rows:
        db.execute(
            "INSERT INTO memories (id, content, tags, created_at) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), row["content"], row["tags"], row["created_at"]),
        )
    db.execute("DROP TABLE memories_v1")
    # Embeddings cannot be preserved without re-embedding; recreate the vector table clean.
    db.execute("DROP TABLE IF EXISTS memory_vecs")
    db.execute(f"CREATE VIRTUAL TABLE memory_vecs USING vec0(embedding float[{_EMBED_DIM}])")
    db.commit()
    logger.info("Migration complete: %d memories assigned new UUID ids", len(old_rows))


def _init_db() -> None:
    db = _connect()
    _migrate_id_to_uuid(db)
    db.executescript(f"""
        CREATE TABLE IF NOT EXISTS memories (
            id         TEXT      PRIMARY KEY,
            content    TEXT      NOT NULL,
            tags       TEXT      NOT NULL DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_vecs USING vec0(
            embedding float[{_EMBED_DIM}]
        );
        CREATE TABLE IF NOT EXISTS tool_call_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name   TEXT    NOT NULL,
            arguments   TEXT,
            result      TEXT,
            duration_ms REAL,
            error       TEXT,
            called_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_logs_tool ON tool_call_logs(tool_name);
        CREATE INDEX IF NOT EXISTS idx_logs_called_at ON tool_call_logs(called_at DESC);
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
            "ui": "/ui" if _UI_ENABLED else None,
        }
    )


@mcp.custom_route("/", methods=["GET"])
async def root_redirect(request: Request) -> RedirectResponse | JSONResponse:
    if not _UI_ENABLED:
        return JSONResponse(
            {"error": "UI is disabled. Start with --ui or SERVER_UI=1 (HTTP mode only)."}, status_code=404
        )
    return RedirectResponse("/ui")


# ---------------------------------------------------------------------------
# Tool-call logging
# ---------------------------------------------------------------------------


def _logged(fn: Any) -> Any:
    """Decorator: records every tool invocation to tool_call_logs."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        log_args: dict[str, Any] = {}
        for k, v in bound.arguments.items():
            if isinstance(v, str):
                log_args[k] = v[:300] + ("…" if len(v) > 300 else "")
            elif isinstance(v, list):
                log_args[k] = [(i[:100] + "…" if isinstance(i, str) and len(i) > 100 else i) for i in v[:10]]
            else:
                log_args[k] = v

        start = _time.monotonic()
        error: str | None = None
        result: Any = None
        try:
            result = fn(*args, **kwargs)
            return result
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            duration_ms = round((_time.monotonic() - start) * 1000, 2)
            if result is not None:
                raw = json.dumps(result) if isinstance(result, (dict, list)) else str(result)
                result_str: str | None = raw[:500] + ("…" if len(raw) > 500 else "")
            else:
                result_str = None
            try:
                db = _connect()
                db.execute(
                    "INSERT INTO tool_call_logs (tool_name, arguments, result, duration_ms, error)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (fn.__name__, json.dumps(log_args)[:2000], result_str, duration_ms, error),
                )
                db.commit()
                db.close()
            except Exception:
                pass  # never fail the tool call because of a logging error

    return wrapper


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
@_logged
def memory_store(content: str, tags: list[str] | None = None) -> str:
    """Store a memory and index it for semantic search. Returns the memory ID."""
    logger.info("tool: memory_store")
    vec = _embed(content)
    mem_id = str(uuid.uuid4())
    db = _connect()
    cur = db.execute(
        "INSERT INTO memories (id, content, tags) VALUES (?, ?, ?)",
        (mem_id, content, json.dumps(tags or [])),
    )
    # memory_vecs is a rowid table; use the internal integer rowid for the vec link.
    db.execute(
        "INSERT INTO memory_vecs (rowid, embedding) VALUES (?, ?)",
        (cur.lastrowid, vec),
    )
    db.commit()
    db.close()
    return f"stored:{mem_id}"


def _normalize_tags(tags: str | list[str] | None) -> list[str]:
    """Accept a single tag string or a list of tags; always return a list."""
    if tags is None:
        return []
    if isinstance(tags, str):
        return [tags]
    return list(tags)


@mcp.tool()
@_logged
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
        JOIN memories m ON m.rowid = v.rowid
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
        results.append(
            {
                "id": r["id"],
                "content": r["content"],
                "tags": record_tags,
                "created_at": r["created_at"],
                "distance": round(r["distance"], 6),
            }
        )
        if len(results) >= limit:
            break
    return results


@mcp.tool()
@_logged
def memory_list(
    limit: int = 20,
    offset: int = 0,
    tags: str | list[str] | None = None,
    match_all_tags: bool = False,
) -> list[dict[str, Any]]:
    """List recent memories, optionally filtered by tags.

    tags: a single tag string or a list of tags.
    match_all:
        when False (default) returns records matching ANY tag (OR logic);
        when True returns only records matching ALL provided tags (AND logic).
    offset: number of records to skip for pagination.
    """
    tag_list = _normalize_tags(tags)
    db = _connect()
    if tag_list:
        if match_all_tags:
            # AND logic: one json_each join per required tag via GROUP BY + COUNT
            placeholders = ",".join("?" * len(tag_list))
            rows = db.execute(
                f"""
                SELECT m.id, m.content, m.tags, m.created_at
                FROM memories m, json_each(m.tags) t
                WHERE t.value IN ({placeholders})
                GROUP BY m.id
                HAVING COUNT(DISTINCT t.value) = ?
                ORDER BY m.created_at DESC LIMIT ? OFFSET ?
                """,  # noqa: S608
                (*tag_list, len(tag_list), limit, offset),
            ).fetchall()
        else:
            placeholders = ",".join("?" * len(tag_list))
            rows = db.execute(
                f"""
                SELECT DISTINCT m.id, m.content, m.tags, m.created_at
                FROM memories m, json_each(m.tags) t
                WHERE t.value IN ({placeholders})
                ORDER BY m.created_at DESC LIMIT ? OFFSET ?
                """,  # noqa: S608
                (*tag_list, limit, offset),
            ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, content, tags, created_at FROM memories ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
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
@_logged
def memory_get(memory_id: str) -> dict[str, Any] | None:
    """Fetch a single memory record by UUID. Returns null if not found."""
    logger.info("tool: memory_get")
    db = _connect()
    row = db.execute(
        "SELECT id, content, tags, created_at FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()
    db.close()
    if row is None:
        return None
    return {
        "id": row["id"],
        "content": row["content"],
        "tags": json.loads(row["tags"]),
        "created_at": row["created_at"],
    }


@mcp.tool()
@_logged
def memory_delete(memory_id: str) -> str:
    """Delete a memory by UUID."""
    logger.info("tool: memory_delete")
    db = _connect()
    row = db.execute("SELECT rowid FROM memories WHERE id = ?", (memory_id,)).fetchone()
    db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    if row:
        db.execute("DELETE FROM memory_vecs WHERE rowid = ?", (row[0],))
    db.commit()
    db.close()
    return f"deleted:{memory_id}"


@mcp.tool()
@_logged
def memory_update(memory_id: str, content: str, tags: list[str] | None = None) -> str:
    """Replace a memory's content and re-index it with a fresh embedding."""
    logger.info("tool: memory_update")
    vec = _embed(content)
    db = _connect()
    row = db.execute("SELECT rowid FROM memories WHERE id = ?", (memory_id,)).fetchone()
    db.execute(
        "UPDATE memories SET content = ?, tags = ? WHERE id = ?",
        (content, json.dumps(tags or []), memory_id),
    )
    if row:
        db.execute(
            "UPDATE memory_vecs SET embedding = ? WHERE rowid = ?",
            (vec, row[0]),
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
.nav { display: flex; gap: 4px; margin-bottom: 24px; border-bottom: 1px solid #e5e7eb; padding-bottom: 0; }
.nav a { padding: 8px 16px; border-radius: 6px 6px 0 0; font-size: 13px; font-weight: 500;
          color: #6b7280; border: 1px solid transparent; border-bottom: none; margin-bottom: -1px; }
.nav a:hover { color: #111; text-decoration: none; background: #f9fafb; }
.nav a.active { background: #fff; color: #111; border-color: #e5e7eb; }
.ok { color: #16a34a; font-weight: 600; }
.err { color: #dc2626; font-weight: 600; }
.mono { font-family: ui-monospace, monospace; font-size: 12px; }
.pager-info { color: #6b7280; font-size: 12px; margin-right: 4px; }
details summary { cursor: pointer; list-style: none; }
details summary::-webkit-details-marker { display: none; }
details summary::marker { display: none; }
details summary::before { content: "▶ "; font-size: 10px; color: #9ca3af; }
details[open] summary::before { content: "▼ "; }
.args-full { margin-top: 6px; background: #f9fafb; border: 1px solid #e5e7eb;
             border-radius: 4px; padding: 8px 10px; font-size: 11px;
             overflow-x: auto; white-space: pre; max-height: 260px;
             overflow-y: auto; }
/* modal */
.modal-backdrop { display:none; position:fixed; inset:0; background:rgba(0,0,0,.45);
                  z-index:100; align-items:center; justify-content:center; }
.modal-backdrop.open { display:flex; }
.modal { background:#fff; border-radius:10px; padding:28px 28px 20px;
         max-width:420px; width:100%; box-shadow:0 8px 32px rgba(0,0,0,.18); }
.modal h3 { font-size:16px; font-weight:600; margin-bottom:10px; }
.modal p  { color:#6b7280; font-size:13px; line-height:1.6; margin-bottom:20px; }
.modal-actions { display:flex; gap:8px; justify-content:flex-end; }
"""

_MODAL_JS = """
<div class="modal-backdrop" id="deleteAllModal">
  <div class="modal">
    <h3>Delete all memories?</h3>
    <p>This will permanently remove every memory record from the database.
       This action cannot be undone.</p>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <form method="post" action="/ui/delete-all" style="display:inline">
        <button class="btn btn-danger" type="submit">Yes, delete all</button>
      </form>
    </div>
  </div>
</div>
<script>
function openDeleteAll() { document.getElementById('deleteAllModal').classList.add('open'); }
function closeModal()    { document.getElementById('deleteAllModal').classList.remove('open'); }
document.getElementById('deleteAllModal').addEventListener('click', function(e) {
  if (e.target === this) closeModal();
});
</script>
"""

_NAV = """<nav class="nav">
  <a href="/ui"{mem_active}>Memories</a>
  <a href="/ui/logs"{log_active}>Logs</a>
</nav>"""


def _page(title: str, body: str, active: str = "memories") -> HTMLResponse:
    nav = _NAV.format(
        mem_active=' class="active"' if active == "memories" else "",
        log_active=' class="active"' if active == "logs" else "",
    )
    extra = _MODAL_JS if active == "memories" else ""
    html = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Memory</title>
<style>{_CSS}</style>
</head>
<body><div class="wrap">{nav}{body}</div>{extra}</body>
</html>"""
    return HTMLResponse(html)


def _fmt_ts(ts: str) -> str:
    return ts[:19] if ts else "—"


def _build_pager(
    page: int,
    total_pages: int,
    total: int,
    per_page: int,
    base_url: str,
    extra_params: str = "",
) -> str:
    """Return a pager div with 'Showing X–Y of Z' info and prev/next/page links."""
    if total == 0:
        return ""
    start = (page - 1) * per_page + 1
    end = min(page * per_page, total)
    info = f'<span class="pager-info">Showing {start}–{end} of {total}</span>'

    if total_pages <= 1:
        return f'<div class="pager">{info}</div>'

    def _link(p: int, label: str | None = None) -> str:
        lbl = label or str(p)
        params = f"page={p}" + (f"&{extra_params}" if extra_params else "")
        if p == page:
            return f'<span class="cur">{lbl}</span>'
        return f'<a href="{base_url}?{params}">{lbl}</a>'

    links: list[str] = [info]
    if page > 1:
        links.append(_link(page - 1, "← Prev"))
    for p in range(1, total_pages + 1):
        if p == 1 or p == total_pages or abs(p - page) <= 2:
            links.append(_link(p))
        elif links[-1] != '<span class="dots">…</span>':
            links.append('<span class="dots">…</span>')
    if page < total_pages:
        links.append(_link(page + 1, "Next →"))
    return f'<div class="pager">{"".join(links)}</div>'


def _tags_html(tags_json: str) -> str:
    try:
        tags = json.loads(tags_json) if tags_json else []
    except Exception:
        tags = []
    return "".join(f'<span class="badge">{t}</span>' for t in tags) or '<span class="ts">—</span>'


_UI_404 = JSONResponse({"error": "UI is disabled. Start with --ui or SERVER_UI=1 (HTTP mode only)."}, status_code=404)


@mcp.custom_route("/ui", methods=["GET"])
async def ui_list(request: Request) -> HTMLResponse | JSONResponse:
    if not _UI_ENABLED:
        return _UI_404
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
        total = db.execute("SELECT COUNT(*) FROM memories WHERE content LIKE ?", (f"%{q}%",)).fetchone()[0]
    else:
        rows = db.execute(
            "SELECT id, content, tags, created_at FROM memories ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ).fetchall()
        total = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    db.close()

    q_enc = q.replace('"', "&quot;")
    delete_all_btn = (
        f'<button class="btn btn-danger" type="button" onclick="openDeleteAll()"'
        f' title="Delete all {total} records">Delete all</button>'
        if total > 0
        else ""
    )
    search_bar = f"""
<div class="bar">
  <form method="get" action="/ui">
    <input type="text" name="q" value="{q_enc}" placeholder="Search content…">
    <button class="btn btn-primary" type="submit">Search</button>
    {"<a class='btn btn-ghost' href='/ui'>Clear</a>" if q else ""}
  </form>
  <span class="count">{total} record{"s" if total != 1 else ""}</span>
  {delete_all_btn}
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

    total_pages = max(1, (total + per_page - 1) // per_page)
    extra = f"q={q_enc}" if q else ""
    pager = _build_pager(page, total_pages, total, per_page, "/ui", extra)

    body = f"<h1>Memory Records</h1>{search_bar}{table}{pager}"
    return _page("List", body, active="memories")


@mcp.custom_route("/ui/detail", methods=["GET"])
async def ui_detail(request: Request) -> HTMLResponse | JSONResponse:
    if not _UI_ENABLED:
        return _UI_404
    mem_id = request.query_params.get("id", "").strip()
    if not mem_id:
        return RedirectResponse("/ui")  # type: ignore[return-value]

    db = _connect()
    row = db.execute("SELECT id, content, tags, created_at FROM memories WHERE id = ?", (mem_id,)).fetchone()
    db.close()

    if not row:
        body = (
            '<a class="btn btn-ghost" href="/ui">← Back</a><div class="empty" style="margin-top:24px">'
            f"Memory #{mem_id} not found.</div>"
        )
        return _page("Not found", body, active="memories")

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
    return _page(f"Memory #{row['id']}", body, active="memories")


@mcp.custom_route("/ui/delete", methods=["POST"])
async def ui_delete(request: Request) -> RedirectResponse | JSONResponse:
    if not _UI_ENABLED:
        return _UI_404
    form = await request.form()
    mem_id = form.get("id", "")
    q = form.get("q", "")
    page = form.get("page", "1")
    if mem_id:
        db = _connect()
        row = db.execute("SELECT rowid FROM memories WHERE id = ?", (str(mem_id),)).fetchone()
        db.execute("DELETE FROM memories WHERE id = ?", (str(mem_id),))
        if row:
            db.execute("DELETE FROM memory_vecs WHERE rowid = ?", (row[0],))
        db.commit()
        db.close()
    params = f"page={page}" + (f"&q={q}" if q else "")
    return RedirectResponse(f"/ui?{params}", status_code=303)


@mcp.custom_route("/ui/delete-all", methods=["POST"])
async def ui_delete_all(request: Request) -> RedirectResponse | JSONResponse:
    if not _UI_ENABLED:
        return _UI_404
    db = _connect()
    db.execute("DELETE FROM memories")
    db.execute("DELETE FROM memory_vecs")
    db.commit()
    db.close()
    return RedirectResponse("/ui", status_code=303)


@mcp.custom_route("/ui/logs", methods=["GET"])
async def ui_logs(request: Request) -> HTMLResponse | JSONResponse:
    if not _UI_ENABLED:
        return _UI_404
    tool_filter = request.query_params.get("tool", "").strip()
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    per_page = 50
    offset = (page - 1) * per_page

    db = _connect()

    # Distinct tool names for filter dropdown
    tool_names = [
        r[0] for r in db.execute("SELECT DISTINCT tool_name FROM tool_call_logs ORDER BY tool_name").fetchall()
    ]

    if tool_filter:
        rows = db.execute(
            "SELECT id, tool_name, arguments, result, duration_ms, error, called_at"
            " FROM tool_call_logs WHERE tool_name = ?"
            " ORDER BY called_at DESC LIMIT ? OFFSET ?",
            (tool_filter, per_page, offset),
        ).fetchall()
        total = db.execute("SELECT COUNT(*) FROM tool_call_logs WHERE tool_name = ?", (tool_filter,)).fetchone()[0]
    else:
        rows = db.execute(
            "SELECT id, tool_name, arguments, result, duration_ms, error, called_at"
            " FROM tool_call_logs ORDER BY called_at DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ).fetchall()
        total = db.execute("SELECT COUNT(*) FROM tool_call_logs").fetchone()[0]
    db.close()

    # Filter bar
    tool_opts = "".join(f'<option value="{t}"{"selected" if t == tool_filter else ""}>{t}</option>' for t in tool_names)
    filter_bar = f"""
<div class="bar">
  <form method="get" action="/ui/logs" style="display:flex;gap:6px;flex:1">
    <select name="tool" style="border:1px solid #d1d5db;border-radius:6px;padding:6px 10px;font-size:13px">
      <option value="">All tools</option>{tool_opts}
    </select>
    <button class="btn btn-primary" type="submit">Filter</button>
    {"<a class='btn btn-ghost' href='/ui/logs'>Clear</a>" if tool_filter else ""}
  </form>
  <span class="count">{total} call{"s" if total != 1 else ""}</span>
</div>"""

    if rows:
        rows_html = ""
        for r in rows:
            status = '<span class="err">✗ error</span>' if r["error"] else '<span class="ok">✓</span>'
            dur = f"{r['duration_ms']:.1f} ms" if r["duration_ms"] is not None else "—"
            try:
                args_obj = json.loads(r["arguments"] or "{}")
                # Summary: first two params, values truncated to 60 chars each
                summary_parts = []
                for k, v in list(args_obj.items())[:2]:
                    v_str = json.dumps(v, ensure_ascii=False)
                    if len(v_str) > 60:
                        v_str = v_str[:60] + "…"
                    summary_parts.append(f"{k}={v_str}")
                if len(args_obj) > 2:
                    summary_parts.append(f"… +{len(args_obj) - 2} more")
                summary = _html.escape(", ".join(summary_parts))
                full_json = _html.escape(json.dumps(args_obj, indent=2, ensure_ascii=False))
                args_cell = (
                    f'<details><summary class="mono">{summary}</summary>'
                    f'<pre class="args-full">{full_json}</pre></details>'
                )
            except Exception:
                args_cell = f'<span class="mono">{_html.escape((r["arguments"] or "")[:300])}</span>'
            rows_html += f"""<tr>
  <td class="ts">{r["id"]}</td>
  <td><code class="mono">{r["tool_name"]}</code></td>
  <td class="content-cell">{args_cell}</td>
  <td class="ts">{dur}</td>
  <td>{status}</td>
  <td class="ts">{_fmt_ts(r["called_at"])}</td>
</tr>"""
        table = f"""<table>
<thead><tr>
  <th style="width:60px">ID</th>
  <th style="width:140px">Tool</th>
  <th>Arguments</th>
  <th style="width:90px">Duration</th>
  <th style="width:80px">Status</th>
  <th style="width:150px">Called at</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>"""
    else:
        table = '<div class="empty">No tool calls logged yet.</div>'

    total_pages = max(1, (total + per_page - 1) // per_page)
    extra = f"tool={tool_filter}" if tool_filter else ""
    pager = _build_pager(page, total_pages, total, per_page, "/ui/logs", extra)

    body = f"<h1>Tool Call Logs</h1>{filter_bar}{table}{pager}"
    return _page("Logs", body, active="logs")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    global _UI_ENABLED

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
    parser.add_argument(
        "--ui",
        action="store_true",
        default=_UI_ENABLED,
        help="Enable the web UI at /ui — HTTP mode only (default: off; also set via SERVER_UI=1)",
    )
    parser.add_argument(
        "--no-ui",
        dest="ui",
        action="store_false",
        help="Disable the web UI (overrides --ui and SERVER_UI)",
    )
    args = parser.parse_args()

    # UI is only meaningful in HTTP mode; force it off for stdio regardless of flags/env.
    _UI_ENABLED = args.ui and args.transport == "http"

    if args.transport == "http":
        logger.info("Starting sqlite_vec_memory_mcp · HTTP transport · %s:%s/mcp", args.host, args.port)
        app = mcp.http_app()
        cors_app = CORSMiddleware(app=app, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
        uvicorn.run(cors_app, host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
