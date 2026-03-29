"""
Tests for Docs tools — one test per tool (8 tools).
"""

from __future__ import annotations

import pytest

from pincer.integrations.google.tools_docs import (
    google__add_comment,
    google__create_doc,
    google__get_doc_content,
    google__get_doc_structure,
    google__insert_table,
    google__insert_text,
    google__replace_text,
    google__update_paragraph_style,
)


def _doc(doc_id="doc1", title="My Doc"):
    return {
        "documentId": doc_id,
        "title": title,
        "body": {
            "content": [
                {
                    "paragraph": {
                        "paragraphStyle": {"namedStyleType": "HEADING_1"},
                        "elements": [{"textRun": {"content": "Introduction\n"}}],
                    }
                },
                {
                    "paragraph": {
                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                        "elements": [{"textRun": {"content": "Some body text here.\n"}}],
                    }
                },
            ]
        },
    }


async def test_get_doc_content(mock_factory, mock_docs_service):
    mock_docs_service.documents().get().execute.return_value = _doc()
    result = await google__get_doc_content(mock_factory, document_id="doc1")
    assert "My Doc" in result
    assert "Introduction" in result
    assert "Some body text" in result


async def test_get_doc_content_truncated(mock_factory, mock_docs_service):
    mock_docs_service.documents().get().execute.return_value = _doc()
    result = await google__get_doc_content(mock_factory, document_id="doc1", max_chars=10)
    assert "truncated" in result


async def test_get_doc_structure(mock_factory, mock_docs_service):
    mock_docs_service.documents().get().execute.return_value = _doc()
    result = await google__get_doc_structure(mock_factory, document_id="doc1")
    assert "HEADING_1" in result
    assert "Introduction" in result
    assert "Paragraphs:" in result


async def test_create_doc_no_content(mock_factory, mock_docs_service):
    mock_docs_service.documents().create().execute.return_value = {
        "documentId": "new_doc", "title": "New Document"
    }
    result = await google__create_doc(mock_factory, title="New Document")
    assert "New Document" in result
    assert "new_doc" in result


async def test_create_doc_with_content(mock_factory, mock_docs_service):
    mock_docs_service.documents().create().execute.return_value = {
        "documentId": "new_doc2", "title": "Doc With Content"
    }
    mock_docs_service.documents().batchUpdate().execute.return_value = {}
    result = await google__create_doc(
        mock_factory, title="Doc With Content", content="Hello, World!"
    )
    assert "new_doc2" in result


async def test_insert_text(mock_factory, mock_docs_service):
    mock_docs_service.documents().batchUpdate().execute.return_value = {}
    result = await google__insert_text(
        mock_factory, document_id="doc1", text="New paragraph\n", index=1
    )
    assert "Inserted" in result
    assert "doc1" in result


async def test_replace_text(mock_factory, mock_docs_service):
    mock_docs_service.documents().batchUpdate().execute.return_value = {
        "replies": [{"replaceAllText": {"occurrencesChanged": 3}}]
    }
    result = await google__replace_text(
        mock_factory, document_id="doc1", find_text="foo", replace_text="bar"
    )
    assert "3" in result
    assert "foo" in result
    assert "bar" in result


async def test_insert_table(mock_factory, mock_docs_service):
    mock_docs_service.documents().batchUpdate().execute.return_value = {}
    result = await google__insert_table(
        mock_factory, document_id="doc1", rows=3, columns=4
    )
    assert "3" in result
    assert "4" in result


async def test_update_paragraph_style(mock_factory, mock_docs_service):
    mock_docs_service.documents().batchUpdate().execute.return_value = {}
    result = await google__update_paragraph_style(
        mock_factory, document_id="doc1", start_index=1, end_index=20, named_style="HEADING_2"
    )
    assert "HEADING_2" in result


async def test_add_comment(mock_factory, mock_docs_service):
    # add_comment uses drive service, not docs — patch via mock_factory
    from unittest.mock import MagicMock
    drive_svc = MagicMock()
    drive_svc.comments().create().execute.return_value = {
        "id": "comment1", "content": "Please review this section"
    }
    services = getattr(mock_factory, "_services_dict", {})
    services["drive"] = drive_svc

    async def _get(name):
        return services.get(name, MagicMock())

    mock_factory.get = _get

    result = await google__add_comment(
        mock_factory, file_id="doc1", content="Please review this section"
    )
    assert "comment1" in result
    assert "Please review" in result
