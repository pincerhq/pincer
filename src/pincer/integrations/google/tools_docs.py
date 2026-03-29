"""
Google Docs tools — 8 tools for reading and editing documents.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any, Callable

from pincer.integrations.google.quota import with_backoff

if TYPE_CHECKING:
    from pincer.integrations.google.service_factory import GoogleServiceFactory
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_doc_text(content: list[dict[str, Any]]) -> str:
    """Extract plain text from a Google Docs content array."""
    parts: list[str] = []
    for elem in content:
        para = elem.get("paragraph")
        if para:
            for pe in para.get("elements", []):
                tr = pe.get("textRun")
                if tr:
                    parts.append(tr.get("content", ""))
        tbl = elem.get("table")
        if tbl:
            for row in tbl.get("tableRows", []):
                for cell in row.get("tableCells", []):
                    parts.extend(
                        pe.get("textRun", {}).get("content", "")
                        for elem2 in cell.get("content", [])
                        for pe in elem2.get("paragraph", {}).get("elements", [])
                    )
    return "".join(parts)


# ── Tool implementations ──────────────────────────────────────────────────────

async def google__get_doc_content(
    factory: "GoogleServiceFactory",
    document_id: str,
    max_chars: int = 8000,
) -> str:
    """Get the full text content of a Google Document."""
    svc = await factory.get("docs")
    doc = await with_backoff(lambda: svc.documents().get(documentId=document_id).execute())
    content = doc.get("body", {}).get("content", [])
    text = _extract_doc_text(content)
    title = doc.get("title", "(untitled)")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... (truncated at {max_chars} chars)"
    return f"Document: {title}\n\n{text}"


async def google__get_doc_structure(
    factory: "GoogleServiceFactory",
    document_id: str,
) -> str:
    """Get the structural outline of a Google Document (headings, paragraph counts, tables)."""
    svc = await factory.get("docs")
    doc = await with_backoff(lambda: svc.documents().get(documentId=document_id).execute())
    content = doc.get("body", {}).get("content", [])
    title = doc.get("title", "(untitled)")
    sections: list[str] = []
    para_count = 0
    table_count = 0
    for elem in content:
        para = elem.get("paragraph")
        if para:
            style = para.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
            if style.startswith("HEADING_"):
                text = _extract_doc_text([elem]).strip()
                sections.append(f"  {style}: {text}")
            else:
                para_count += 1
        if elem.get("table"):
            table_count += 1
    lines = [
        f"Document: {title}",
        f"Paragraphs: {para_count} | Tables: {table_count}",
        "",
        "Structure:",
    ] + sections
    return "\n".join(lines)


async def google__create_doc(
    factory: "GoogleServiceFactory",
    title: str,
    content: str = "",
) -> str:
    """Create a new Google Document with optional initial content."""
    from googleapiclient.errors import HttpError

    svc = await factory.get("docs")
    try:
        doc = await with_backoff(lambda: svc.documents().create(body={"title": title}).execute())
    except HttpError as e:
        if e.resp.status == 403 and "SCOPE_INSUFFICIENT" in str(e):
            return (
                "❌ Bot lacks Google Docs write permission. "
                "Delete ~/.pincer/google_token.json (or google_workspace_token.json) "
                "and re-run 'pincer setup google' to re-authorise with the full scope set."
            )
        raise
    doc_id = doc.get("documentId", "")
    if content:
        requests: list[dict[str, Any]] = [
            {"insertText": {"location": {"index": 1}, "text": content}}
        ]
        await with_backoff(
            lambda: svc.documents().batchUpdate(
                documentId=doc_id, body={"requests": requests}
            ).execute()
        )
    link = f"https://docs.google.com/document/d/{doc_id}/edit"
    return f"Document created: '{title}'\nID: {doc_id}\nLink: {link}"


async def google__insert_text(
    factory: "GoogleServiceFactory",
    document_id: str,
    text: str,
    index: int = 1,
) -> str:
    """Insert text at a specific position in a document."""
    svc = await factory.get("docs")
    requests: list[dict[str, Any]] = [
        {"insertText": {"location": {"index": index}, "text": text}}
    ]
    await with_backoff(
        lambda: svc.documents().batchUpdate(
            documentId=document_id, body={"requests": requests}
        ).execute()
    )
    return f"Inserted {len(text)} characters at index {index} in document {document_id}."


async def google__replace_text(
    factory: "GoogleServiceFactory",
    document_id: str,
    find_text: str,
    replace_text: str,
    match_case: bool = True,
) -> str:
    """Find and replace text in a document."""
    svc = await factory.get("docs")
    requests: list[dict[str, Any]] = [
        {
            "replaceAllText": {
                "containsText": {"text": find_text, "matchCase": match_case},
                "replaceText": replace_text,
            }
        }
    ]
    result = await with_backoff(
        lambda: svc.documents().batchUpdate(
            documentId=document_id, body={"requests": requests}
        ).execute()
    )
    replies = result.get("replies", [{}])
    count = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0) if replies else 0
    return f"Replaced {count} occurrence(s) of '{find_text}' with '{replace_text}'."


async def google__insert_table(
    factory: "GoogleServiceFactory",
    document_id: str,
    rows: int,
    columns: int,
    index: int = 1,
) -> str:
    """Insert an empty table at a position in the document."""
    svc = await factory.get("docs")
    requests: list[dict[str, Any]] = [
        {
            "insertTable": {
                "location": {"index": index},
                "rows": rows,
                "columns": columns,
            }
        }
    ]
    await with_backoff(
        lambda: svc.documents().batchUpdate(
            documentId=document_id, body={"requests": requests}
        ).execute()
    )
    return f"Inserted {rows}×{columns} table at index {index}."


async def google__update_paragraph_style(
    factory: "GoogleServiceFactory",
    document_id: str,
    start_index: int,
    end_index: int,
    named_style: str = "HEADING_1",
) -> str:
    """Change a paragraph's named style (HEADING_1..6, NORMAL_TEXT, etc.)."""
    svc = await factory.get("docs")
    requests: list[dict[str, Any]] = [
        {
            "updateParagraphStyle": {
                "range": {"startIndex": start_index, "endIndex": end_index},
                "paragraphStyle": {"namedStyleType": named_style},
                "fields": "namedStyleType",
            }
        }
    ]
    await with_backoff(
        lambda: svc.documents().batchUpdate(
            documentId=document_id, body={"requests": requests}
        ).execute()
    )
    return f"Paragraph style set to {named_style} ({start_index}–{end_index})."


async def google__add_comment(
    factory: "GoogleServiceFactory",
    file_id: str,
    content: str,
    anchor: str = "",
) -> str:
    """Add a comment to a Google Document (uses Drive API comments endpoint)."""
    drive_svc = await factory.get("drive")
    body: dict[str, Any] = {"content": content}
    if anchor:
        body["anchor"] = anchor
    result = await with_backoff(
        lambda: drive_svc.comments().create(fileId=file_id, body=body, fields="id, content").execute()
    )
    return f"Comment added (id={result.get('id', '')}): \"{result.get('content', '')}\""


# ── Registry ──────────────────────────────────────────────────────────────────

def register_docs_tools(registry: "ToolRegistry", factory: "GoogleServiceFactory") -> int:
    """Register all 8 Docs tools. Returns count."""

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(**kwargs):  # type: ignore[no-untyped-def]
            return await fn(factory, **kwargs)
        return wrapper

    registry.register(
        name="google__get_doc_content",
        description="Get the full text content of a Google Document.",
        handler=_h(google__get_doc_content),
        parameters={
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "max_chars": {"type": "integer", "default": 8000},
            },
            "required": ["document_id"],
        },
    )
    registry.register(
        name="google__get_doc_structure",
        description="Get the structural outline of a Google Document (headings, paragraph count, tables).",
        handler=_h(google__get_doc_structure),
        parameters={
            "type": "object",
            "properties": {"document_id": {"type": "string"}},
            "required": ["document_id"],
        },
    )
    registry.register(
        name="google__create_doc",
        description="Create a new Google Document with optional initial content.",
        handler=_h(google__create_doc),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string", "description": "Initial text content", "default": ""},
            },
            "required": ["title"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__insert_text",
        description="Insert text at a specific character index in a Google Document.",
        handler=_h(google__insert_text),
        parameters={
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "text": {"type": "string"},
                "index": {"type": "integer", "description": "Character index (default: 1 = start of doc)", "default": 1},
            },
            "required": ["document_id", "text"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__replace_text",
        description="Find and replace all occurrences of text in a Google Document.",
        handler=_h(google__replace_text),
        parameters={
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "find_text": {"type": "string"},
                "replace_text": {"type": "string"},
                "match_case": {"type": "boolean", "default": True},
            },
            "required": ["document_id", "find_text", "replace_text"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__insert_table",
        description="Insert a table into a Google Document.",
        handler=_h(google__insert_table),
        parameters={
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "rows": {"type": "integer"},
                "columns": {"type": "integer"},
                "index": {"type": "integer", "default": 1},
            },
            "required": ["document_id", "rows", "columns"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__update_paragraph_style",
        description="Change the style of a paragraph in a Google Document (HEADING_1..6, NORMAL_TEXT).",
        handler=_h(google__update_paragraph_style),
        parameters={
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "start_index": {"type": "integer"},
                "end_index": {"type": "integer"},
                "named_style": {"type": "string", "enum": ["HEADING_1", "HEADING_2", "HEADING_3", "HEADING_4", "HEADING_5", "HEADING_6", "NORMAL_TEXT", "TITLE", "SUBTITLE"], "default": "HEADING_1"},
            },
            "required": ["document_id", "start_index", "end_index"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__add_comment",
        description="Add a comment to a Google Document.",
        handler=_h(google__add_comment),
        parameters={
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Document/Drive file ID"},
                "content": {"type": "string", "description": "Comment text"},
                "anchor": {"type": "string", "description": "Optional anchor JSON for inline comments", "default": ""},
            },
            "required": ["file_id", "content"],
        },
        require_approval=True,
    )
    return 8
