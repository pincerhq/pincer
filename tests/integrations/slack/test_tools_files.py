"""Tests for Slack file tools (10 tools)."""

from __future__ import annotations

import pytest


def fake_resp(**kwargs):
    d = {'ok': True}
    d.update(kwargs)
    return d


@pytest.mark.asyncio
async def test_list_files_empty(mock_client):
    from pincer.integrations.slack.tools_files import slack__list_files

    result = await slack__list_files(mock_client)
    assert "No files" in result


@pytest.mark.asyncio
async def test_list_files_with_data(mock_client):
    from pincer.integrations.slack.tools_files import slack__list_files

    mock_client.bot.files_list.return_value = fake_resp(
        files=[
            {"id": "F001", "name": "report.pdf", "size": 1024, "filetype": "pdf",
             "pretty_type": "PDF", "user": "U001"},
        ]
    )
    result = await slack__list_files(mock_client)
    assert "report.pdf" in result
    assert "F001" in result


@pytest.mark.asyncio
async def test_get_file_info(mock_client):
    from pincer.integrations.slack.tools_files import slack__get_file_info

    mock_client.bot.files_info.return_value = fake_resp(
        file={
            "id": "F001", "name": "report.pdf", "size": 2048,
            "pretty_type": "PDF", "filetype": "pdf",
            "title": "Q4 Report", "user": "U001",
            "created": 1700000000, "url_private": "https://files.slack.com/F001",
            "channels": ["C001"],
        }
    )
    result = await slack__get_file_info(mock_client, file="F001")
    assert "report.pdf" in result
    assert "Q4 Report" in result
    assert "C001" in result


@pytest.mark.asyncio
async def test_upload_file_no_content(mock_client):
    from pincer.integrations.slack.tools_files import slack__upload_file

    result = await slack__upload_file(mock_client, channel="C001")
    assert "Error" in result


@pytest.mark.asyncio
async def test_upload_file_with_content(mock_client):
    from pincer.integrations.slack.tools_files import slack__upload_file

    result = await slack__upload_file(
        mock_client, channel="C001", content="print('hello')", filename="hello.py"
    )
    assert "F123" in result or "uploaded" in result.lower()


@pytest.mark.asyncio
async def test_share_file(mock_client):
    from pincer.integrations.slack.tools_files import slack__share_file

    result = await slack__share_file(mock_client, file="F001", channels="C001,C002")
    assert "F001" in result or "shared" in result.lower()


@pytest.mark.asyncio
async def test_delete_file(mock_client):
    from pincer.integrations.slack.tools_files import slack__delete_file

    result = await slack__delete_file(mock_client, file="F001")
    assert "deleted" in result.lower()


@pytest.mark.asyncio
async def test_list_remote_files_empty(mock_client):
    from pincer.integrations.slack.tools_files import slack__list_remote_files

    result = await slack__list_remote_files(mock_client)
    assert "No remote files" in result


@pytest.mark.asyncio
async def test_add_remote_file(mock_client):
    from pincer.integrations.slack.tools_files import slack__add_remote_file

    result = await slack__add_remote_file(
        mock_client, title="Google Doc", external_url="https://docs.google.com/123"
    )
    assert "added" in result.lower() or "F123" in result


@pytest.mark.asyncio
async def test_update_remote_file(mock_client):
    from pincer.integrations.slack.tools_files import slack__update_remote_file

    result = await slack__update_remote_file(mock_client, file="F001", title="New Title")
    assert "updated" in result.lower() or "F123" in result


@pytest.mark.asyncio
async def test_remove_remote_file(mock_client):
    from pincer.integrations.slack.tools_files import slack__remove_remote_file

    result = await slack__remove_remote_file(mock_client, file="F001")
    assert "removed" in result.lower()


@pytest.mark.asyncio
async def test_register_file_tools_count():
    from unittest.mock import MagicMock

    from pincer.integrations.slack.tools_files import register_file_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    count = register_file_tools(registry, client)
    assert count == 10
    assert len(registry.list_tools()) == 10


@pytest.mark.asyncio
async def test_file_write_tools_require_approval():
    from unittest.mock import MagicMock

    from pincer.integrations.slack.tools_files import register_file_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    register_file_tools(registry, client)

    write_tools = [
        "slack__upload_file",
        "slack__share_file",
        "slack__delete_file",
        "slack__add_remote_file",
        "slack__update_remote_file",
        "slack__remove_remote_file",
    ]
    for name in write_tools:
        assert registry.requires_approval(name), f"{name} should require approval"


@pytest.mark.asyncio
async def test_file_read_tools_no_approval():
    from unittest.mock import MagicMock

    from pincer.integrations.slack.tools_files import register_file_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    register_file_tools(registry, client)

    for name in ["slack__list_files", "slack__get_file_info", "slack__download_file", "slack__list_remote_files"]:
        assert not registry.requires_approval(name), f"{name} should NOT require approval"
