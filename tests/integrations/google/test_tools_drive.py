"""
Tests for Drive tools — one test per tool (15 tools).
"""

from __future__ import annotations

import os

from pincer.integrations.google.tools_drive import (
    google__copy_file,
    google__create_folder,
    google__download_file,
    google__export_google_doc,
    google__get_file_metadata,
    google__get_file_permissions,
    google__list_drive_files,
    google__list_recent_files,
    google__list_shared_drives,
    google__move_file,
    google__rename_file,
    google__search_drive_files,
    google__share_file,
    google__trash_file,
    google__upload_file,
)


def _file(file_id="f1", name="budget.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
    return {
        "id": file_id,
        "name": name,
        "mimeType": mime,
        "modifiedTime": "2026-03-20T10:00:00Z",
        "size": "4096",
        "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
        "owners": [{"emailAddress": "alice@example.com"}],
    }


async def test_list_drive_files(mock_factory, mock_drive_service):
    mock_drive_service.files().list().execute.return_value = {
        "files": [_file("f1", "Report.pdf"), _file("f2", "Notes.txt")]
    }
    result = await google__list_drive_files(mock_factory)
    assert "Report.pdf" in result
    assert "Notes.txt" in result


async def test_list_drive_files_empty(mock_factory, mock_drive_service):
    mock_drive_service.files().list().execute.return_value = {"files": []}
    result = await google__list_drive_files(mock_factory)
    assert "No files" in result


async def test_search_drive_files(mock_factory, mock_drive_service):
    mock_drive_service.files().list().execute.return_value = {"files": [_file("f1", "Q1 Budget.xlsx")]}
    result = await google__search_drive_files(
        mock_factory, query='name contains "budget" type:spreadsheet sharedWithMe'
    )
    assert "Q1 Budget.xlsx" in result
    assert "f1" in result


async def test_search_drive_files_none(mock_factory, mock_drive_service):
    mock_drive_service.files().list().execute.return_value = {"files": []}
    result = await google__search_drive_files(mock_factory, query="xyz123notfound")
    assert "No files" in result


async def test_get_file_metadata(mock_factory, mock_drive_service):
    mock_drive_service.files().get().execute.return_value = {
        "id": "f1",
        "name": "Report.pdf",
        "mimeType": "application/pdf",
        "size": "10240",
        "createdTime": "2026-01-01T00:00:00Z",
        "modifiedTime": "2026-03-20T00:00:00Z",
        "owners": [{"emailAddress": "alice@example.com"}],
        "shared": True,
        "webViewLink": "https://drive.google.com/file/d/f1/view",
    }
    result = await google__get_file_metadata(mock_factory, file_id="f1")
    assert "Report.pdf" in result
    assert "alice@example.com" in result
    assert "10240" in result


async def test_download_file(mock_factory, mock_drive_service, tmp_path, monkeypatch):
    """google__download_file saves bytes to disk and returns short metadata string."""
    test_content = b"%PDF-1.4 fake content"
    mock_drive_service.files().get().execute.return_value = {
        "id": "f1",
        "name": "report.pdf",
        "mimeType": "application/pdf",
        "size": str(len(test_content)),
    }
    mock_drive_service.files().get_media().execute.return_value = test_content

    download_dir = str(tmp_path / "downloads")
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~/.pincer/downloads", download_dir))

    result = await google__download_file(mock_factory, file_id="f1")

    assert "report.pdf" in result
    assert "Saved to:" in result
    assert len(result) < 500
    # Raw bytes must not appear in response
    assert b"%PDF" not in result.encode()
    # File must exist on disk
    save_path = result.split("Saved to:")[1].strip().split("\n")[0]
    assert os.path.exists(save_path)
    with open(save_path, "rb") as fh:
        assert fh.read() == test_content


async def test_download_file_response_never_contains_binary(mock_factory, mock_drive_service, tmp_path, monkeypatch):
    """Response is short metadata text even for large files."""
    big_content = b"x" * 500_000
    mock_drive_service.files().get().execute.return_value = {
        "id": "f1",
        "name": "big.zip",
        "mimeType": "application/zip",
        "size": str(len(big_content)),
    }
    mock_drive_service.files().get_media().execute.return_value = big_content

    download_dir = str(tmp_path / "downloads")
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~/.pincer/downloads", download_dir))

    result = await google__download_file(mock_factory, file_id="f1")

    assert len(result) < 500
    assert "big.zip" in result
    assert "base64" not in result.lower()


async def test_download_file_redirects_google_native(mock_factory, mock_drive_service):
    """google__download_file on a Google Doc returns redirect to google__export_google_doc."""
    mock_drive_service.files().get().execute.return_value = {
        "id": "doc1",
        "name": "My Proposal",
        "mimeType": "application/vnd.google-apps.document",
    }

    result = await google__download_file(mock_factory, file_id="doc1")

    assert "google__export_google_doc" in result
    assert "Cannot download" in result


async def test_download_file_duplicate_filename(mock_factory, mock_drive_service, tmp_path, monkeypatch):
    """Second download of same filename gets _1 suffix."""
    test_content = b"data"
    mock_drive_service.files().get().execute.return_value = {
        "id": "f1",
        "name": "data.csv",
        "mimeType": "text/csv",
        "size": "4",
    }
    mock_drive_service.files().get_media().execute.return_value = test_content

    download_dir = str(tmp_path / "downloads")
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~/.pincer/downloads", download_dir))

    result1 = await google__download_file(mock_factory, file_id="f1")
    result2 = await google__download_file(mock_factory, file_id="f1")

    path1 = result1.split("Saved to:")[1].strip().split("\n")[0]
    path2 = result2.split("Saved to:")[1].strip().split("\n")[0]
    assert path1 != path2
    assert "data_1.csv" in path2


async def test_export_google_doc(mock_factory, mock_drive_service, tmp_path, monkeypatch):
    """google__export_google_doc saves exported bytes to disk and returns metadata."""
    pdf_bytes = b"%PDF-1.4 fake exported content"
    mock_drive_service.files().get().execute.return_value = {
        "id": "doc1",
        "name": "My Proposal",
        "mimeType": "application/vnd.google-apps.document",
    }
    mock_drive_service.files().export_media().execute.return_value = pdf_bytes

    download_dir = str(tmp_path / "downloads")
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~/.pincer/downloads", download_dir))

    result = await google__export_google_doc(mock_factory, file_id="doc1", export_format="pdf")

    assert "My Proposal.pdf" in result
    assert "Saved to:" in result
    assert len(result) < 500
    save_path = result.split("Saved to:")[1].strip().split("\n")[0]
    assert os.path.exists(save_path)
    with open(save_path, "rb") as fh:
        assert fh.read() == pdf_bytes


async def test_export_google_sheet_as_xlsx(mock_factory, mock_drive_service, tmp_path, monkeypatch):
    """Google Sheet exported as XLSX."""
    xlsx_bytes = b"PK fake xlsx"
    mock_drive_service.files().get().execute.return_value = {
        "id": "sheet1",
        "name": "Q1 Budget",
        "mimeType": "application/vnd.google-apps.spreadsheet",
    }
    mock_drive_service.files().export_media().execute.return_value = xlsx_bytes

    download_dir = str(tmp_path / "downloads")
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~/.pincer/downloads", download_dir))

    result = await google__export_google_doc(mock_factory, file_id="sheet1", export_format="xlsx")

    assert "Q1 Budget.xlsx" in result
    assert "XLSX" in result


async def test_export_invalid_format(mock_factory, mock_drive_service):
    """Invalid export format returns error listing supported formats."""
    mock_drive_service.files().get().execute.return_value = {
        "id": "doc1",
        "name": "Doc",
        "mimeType": "application/vnd.google-apps.document",
    }

    result = await google__export_google_doc(mock_factory, file_id="doc1", export_format="mp3")

    assert "Unsupported" in result
    assert "pdf" in result


async def test_export_wrong_format_for_type(mock_factory, mock_drive_service):
    """CSV export from a Google Doc returns error with allowed formats."""
    mock_drive_service.files().get().execute.return_value = {
        "id": "doc1",
        "name": "My Doc",
        "mimeType": "application/vnd.google-apps.document",
    }

    result = await google__export_google_doc(mock_factory, file_id="doc1", export_format="csv")

    assert "cannot be exported as 'csv'" in result
    assert "pdf" in result


async def test_export_non_google_file_redirects(mock_factory, mock_drive_service):
    """Calling export on a regular PDF redirects to google__download_file."""
    mock_drive_service.files().get().execute.return_value = {
        "id": "f1",
        "name": "manual.pdf",
        "mimeType": "application/pdf",
    }

    result = await google__export_google_doc(mock_factory, file_id="f1", export_format="pdf")

    assert "google__download_file" in result
    assert "not a Google Doc" in result


async def test_export_response_never_contains_binary(mock_factory, mock_drive_service, tmp_path, monkeypatch):
    """Export response is short metadata text even for large exports."""
    big_bytes = b"x" * 2_000_000
    mock_drive_service.files().get().execute.return_value = {
        "id": "doc1",
        "name": "Huge Report",
        "mimeType": "application/vnd.google-apps.document",
    }
    mock_drive_service.files().export_media().execute.return_value = big_bytes

    download_dir = str(tmp_path / "downloads")
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~/.pincer/downloads", download_dir))

    result = await google__export_google_doc(mock_factory, file_id="doc1", export_format="pdf")

    assert len(result) < 500


async def test_export_duplicate_filename(mock_factory, mock_drive_service, tmp_path, monkeypatch):
    """Second export of same filename gets _1 suffix."""
    pdf_bytes = b"%PDF fake"
    mock_drive_service.files().get().execute.return_value = {
        "id": "doc1",
        "name": "Report",
        "mimeType": "application/vnd.google-apps.document",
    }
    mock_drive_service.files().export_media().execute.return_value = pdf_bytes

    download_dir = str(tmp_path / "downloads")
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~/.pincer/downloads", download_dir))

    result1 = await google__export_google_doc(mock_factory, file_id="doc1", export_format="pdf")
    result2 = await google__export_google_doc(mock_factory, file_id="doc1", export_format="pdf")

    path1 = result1.split("Saved to:")[1].strip().split("\n")[0]
    path2 = result2.split("Saved to:")[1].strip().split("\n")[0]
    assert path1 != path2
    assert "Report_1.pdf" in path2


async def test_list_shared_drives(mock_factory, mock_drive_service):
    mock_drive_service.drives().list().execute.return_value = {"drives": [{"id": "d1", "name": "Team Drive"}]}
    result = await google__list_shared_drives(mock_factory)
    assert "Team Drive" in result


async def test_list_shared_drives_empty(mock_factory, mock_drive_service):
    mock_drive_service.drives().list().execute.return_value = {"drives": []}
    result = await google__list_shared_drives(mock_factory)
    assert "No shared drives" in result


async def test_get_file_permissions(mock_factory, mock_drive_service):
    mock_drive_service.permissions().list().execute.return_value = {
        "permissions": [
            {"id": "p1", "type": "user", "role": "owner", "emailAddress": "alice@example.com"},
            {"id": "p2", "type": "user", "role": "reader", "emailAddress": "bob@example.com"},
        ]
    }
    result = await google__get_file_permissions(mock_factory, file_id="f1")
    assert "alice@example.com" in result
    assert "owner" in result
    assert "bob@example.com" in result


async def test_list_recent_files(mock_factory, mock_drive_service):
    mock_drive_service.files().list().execute.return_value = {"files": [_file("f1", "recent.docx")]}
    result = await google__list_recent_files(mock_factory)
    assert "recent.docx" in result


async def test_upload_file_not_found(mock_factory, mock_drive_service, tmp_path):
    result = await google__upload_file(mock_factory, local_path="/nonexistent/file.txt")
    assert "Error" in result


async def test_upload_file_success(mock_factory, mock_drive_service, tmp_path):
    from unittest.mock import MagicMock, patch

    test_file = tmp_path / "test.txt"
    test_file.write_text("hello")
    mock_drive_service.files().create().execute.return_value = {
        "id": "uploaded1",
        "name": "test.txt",
        "webViewLink": "https://drive.google.com/file/d/uploaded1/view",
    }
    with patch("googleapiclient.http.MediaFileUpload", MagicMock()):
        result = await google__upload_file(mock_factory, local_path=str(test_file))
    assert "uploaded1" in result


async def test_create_folder(mock_factory, mock_drive_service):
    mock_drive_service.files().create().execute.return_value = {
        "id": "folder1",
        "name": "Projects",
        "webViewLink": "https://drive.google.com/drive/folders/folder1",
    }
    result = await google__create_folder(mock_factory, name="Projects")
    assert "Projects" in result
    assert "folder1" in result


async def test_move_file(mock_factory, mock_drive_service):
    mock_drive_service.files().get().execute.return_value = {"parents": ["old_parent"]}
    mock_drive_service.files().update().execute.return_value = {"id": "f1", "name": "moved.pdf"}
    result = await google__move_file(mock_factory, file_id="f1", destination_folder_id="new_folder")
    assert "moved" in result.lower() or "new_folder" in result


async def test_rename_file(mock_factory, mock_drive_service):
    mock_drive_service.files().update().execute.return_value = {"id": "f1", "name": "renamed.pdf"}
    result = await google__rename_file(mock_factory, file_id="f1", new_name="renamed.pdf")
    assert "renamed.pdf" in result


async def test_copy_file(mock_factory, mock_drive_service):
    mock_drive_service.files().copy().execute.return_value = {
        "id": "copy1",
        "name": "Copy of budget.xlsx",
        "webViewLink": "https://drive.google.com/file/d/copy1/view",
    }
    result = await google__copy_file(mock_factory, file_id="f1")
    assert "copy1" in result


async def test_trash_file(mock_factory, mock_drive_service):
    mock_drive_service.files().update().execute.return_value = {"id": "f1", "name": "old.pdf"}
    result = await google__trash_file(mock_factory, file_id="f1")
    assert "Trash" in result


async def test_share_file_with_email(mock_factory, mock_drive_service):
    mock_drive_service.permissions().create().execute.return_value = {"id": "perm1"}
    result = await google__share_file(mock_factory, file_id="f1", email="carol@example.com", role="writer")
    assert "carol@example.com" in result


async def test_share_file_link_sharing(mock_factory, mock_drive_service):
    mock_drive_service.permissions().create().execute.return_value = {"id": "perm1"}
    result = await google__share_file(mock_factory, file_id="f1", link_sharing=True)
    assert "Link sharing" in result


async def test_share_file_no_email_no_link(mock_factory, mock_drive_service):
    result = await google__share_file(mock_factory, file_id="f1")
    assert "Error" in result
