"""Tests for MS365 OneDrive tools — one test per tool."""

from __future__ import annotations

import pytest

from pincer.integrations.ms365.tools_onedrive import (
    onedrive__copy_file,
    onedrive__create_folder,
    onedrive__delete_file,
    onedrive__download_file,
    onedrive__get_file_metadata,
    onedrive__get_file_preview,
    onedrive__list_drive_items,
    onedrive__list_recent_files,
    onedrive__list_shared_with_me,
    onedrive__move_file,
    onedrive__rename_file,
    onedrive__search_files,
    onedrive__share_file,
    onedrive__upload_file,
)


@pytest.mark.asyncio
async def test_list_drive_items(mock_client):
    mock_client.get.return_value = {
        "value": [
            {"id": "file-1", "name": "report.docx", "size": 51200, "lastModifiedDateTime": "2026-03-26T10:00:00Z",
             "file": {"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}},
            {"id": "folder-1", "name": "Documents", "size": 0, "lastModifiedDateTime": "2026-03-25T10:00:00Z",
             "folder": {"childCount": 5}},
        ]
    }
    result = await onedrive__list_drive_items(mock_client)
    assert "report.docx" in result
    assert "Documents" in result
    assert "2 item(s)" in result


@pytest.mark.asyncio
async def test_search_files(mock_client):
    mock_client.get.return_value = {
        "value": [
            {"id": "file-2", "name": "Q3 Report.xlsx", "size": 102400, "lastModifiedDateTime": "2026-03-26T12:00:00Z",
             "file": {}, "parentReference": {"path": "/drive/root:/Documents"}},
        ]
    }
    result = await onedrive__search_files(mock_client, query="Q3 Report")
    assert "Q3 Report.xlsx" in result


@pytest.mark.asyncio
async def test_get_file_metadata(mock_client):
    mock_client.get.return_value = {
        "id": "file-1",
        "name": "presentation.pptx",
        "size": 204800,
        "createdDateTime": "2026-01-01T00:00:00Z",
        "lastModifiedDateTime": "2026-03-26T10:00:00Z",
        "webUrl": "https://onedrive.live.com/...",
        "file": {"mimeType": "application/pptx"},
        "parentReference": {"path": "/drive/root:/Work"},
    }
    result = await onedrive__get_file_metadata(mock_client, item_id="file-1")
    assert "presentation.pptx" in result
    assert "204800" in result


@pytest.mark.asyncio
async def test_download_file_text(mock_client):
    mock_client.get.return_value = {
        "name": "notes.txt", "size": 100, "file": {"mimeType": "text/plain"},
    }
    mock_client.get_binary.return_value = b"Hello, world!"
    result = await onedrive__download_file(mock_client, item_id="file-1")
    assert "notes.txt" in result
    assert "Hello, world!" in result


@pytest.mark.asyncio
async def test_download_file_binary(mock_client):
    mock_client.get.return_value = {
        "name": "image.png", "size": 5000, "file": {"mimeType": "image/png"},
    }
    mock_client.get_binary.return_value = b"\x89PNG"
    result = await onedrive__download_file(mock_client, item_id="file-2")
    assert "image.png" in result
    assert "base64" in result


@pytest.mark.asyncio
async def test_get_file_preview(mock_client):
    mock_client.get.return_value = {
        "value": [
            {"large": {"url": "https://preview.example.com/large"}, "medium": {"url": "https://preview.example.com/medium"}},
        ]
    }
    result = await onedrive__get_file_preview(mock_client, item_id="file-1")
    assert "preview.example.com" in result


@pytest.mark.asyncio
async def test_list_shared_with_me(mock_client):
    mock_client.get.return_value = {
        "value": [
            {"id": "shared-1", "name": "shared-doc.docx", "size": 30000, "lastModifiedDateTime": "2026-03-20T10:00:00Z",
             "file": {}},
        ]
    }
    result = await onedrive__list_shared_with_me(mock_client)
    assert "shared-doc.docx" in result


@pytest.mark.asyncio
async def test_list_recent_files(mock_client):
    mock_client.get.return_value = {
        "value": [
            {"id": "recent-1", "name": "memo.txt", "size": 500, "lastModifiedDateTime": "2026-03-26T15:00:00Z",
             "file": {}},
        ]
    }
    result = await onedrive__list_recent_files(mock_client)
    assert "memo.txt" in result


@pytest.mark.asyncio
async def test_upload_file(mock_client):
    mock_client.put.return_value = {"id": "uploaded-1", "size": 11}
    result = await onedrive__upload_file(mock_client, path="/Documents/test.txt", content="hello world")
    assert "uploaded" in result
    assert "/Documents/test.txt" in result


@pytest.mark.asyncio
async def test_create_folder(mock_client):
    mock_client.post.return_value = {"id": "new-folder-1"}
    result = await onedrive__create_folder(mock_client, name="New Folder")
    assert "Folder created" in result
    assert "New Folder" in result


@pytest.mark.asyncio
async def test_move_file(mock_client):
    mock_client.patch.return_value = {"id": "file-1"}
    result = await onedrive__move_file(mock_client, item_id="file-1", destination_path="/Archive")
    assert "moved" in result


@pytest.mark.asyncio
async def test_rename_file(mock_client):
    mock_client.patch.return_value = {"id": "file-1"}
    result = await onedrive__rename_file(mock_client, item_id="file-1", new_name="renamed.txt")
    assert "renamed" in result
    assert "renamed.txt" in result


@pytest.mark.asyncio
async def test_copy_file(mock_client):
    mock_client.post.return_value = {}
    result = await onedrive__copy_file(mock_client, item_id="file-1", destination_path="/Backup")
    assert "Copy initiated" in result


@pytest.mark.asyncio
async def test_delete_file(mock_client):
    mock_client.delete.return_value = {}
    result = await onedrive__delete_file(mock_client, item_id="file-1")
    assert "deleted" in result
    assert "recycle bin" in result


@pytest.mark.asyncio
async def test_share_file(mock_client):
    mock_client.post.return_value = {"link": {"webUrl": "https://1drv.ms/shared-link"}}
    result = await onedrive__share_file(mock_client, item_id="file-1")
    assert "Sharing link" in result
    assert "1drv.ms" in result
