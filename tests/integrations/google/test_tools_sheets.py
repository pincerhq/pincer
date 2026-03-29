"""
Tests for Sheets tools — one test per tool (10 tools).
"""

from __future__ import annotations

import pytest

from pincer.integrations.google.tools_sheets import (
    _col_letter,
    google__add_sheet,
    google__append_sheet_values,
    google__clear_sheet_values,
    google__create_spreadsheet,
    google__format_cells,
    google__get_sheet_metadata,
    google__get_sheet_values,
    google__list_sheets,
    google__search_sheet_values,
    google__update_sheet_values,
)


async def test_list_sheets(mock_factory, mock_sheets_service):
    mock_sheets_service.spreadsheets().get().execute.return_value = {
        "properties": {"title": "Q1 Budget"},
        "sheets": [
            {"properties": {"title": "January", "index": 0, "sheetId": 0}},
            {"properties": {"title": "February", "index": 1, "sheetId": 1}},
        ],
    }
    result = await google__list_sheets(mock_factory, spreadsheet_id="ss1")
    assert "Q1 Budget" in result
    assert "January" in result
    assert "February" in result


async def test_get_sheet_values(mock_factory, mock_sheets_service):
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "range": "Sheet1!A1:C3",
        "values": [["Name", "Amount", "Date"], ["Marketing", "12400", "2026-03-01"]],
    }
    result = await google__get_sheet_values(mock_factory, spreadsheet_id="ss1", range_="Sheet1!A1:C3")
    assert "Name" in result
    assert "12400" in result


async def test_get_sheet_values_empty(mock_factory, mock_sheets_service):
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "range": "Sheet1", "values": []
    }
    result = await google__get_sheet_values(mock_factory, spreadsheet_id="ss1")
    assert "No data" in result


async def test_get_sheet_metadata(mock_factory, mock_sheets_service):
    mock_sheets_service.spreadsheets().get().execute.return_value = {
        "properties": {"title": "My Sheet", "locale": "en_US", "timeZone": "UTC"},
        "namedRanges": [{"name": "TaxRate", "namedRangeId": "nr1"}],
    }
    result = await google__get_sheet_metadata(mock_factory, spreadsheet_id="ss1")
    assert "My Sheet" in result
    assert "TaxRate" in result


async def test_search_sheet_values(mock_factory, mock_sheets_service):
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "range": "Sheet1",
        "values": [["Name", "Value"], ["Marketing", "12400"], ["Sales", "8000"]],
    }
    result = await google__search_sheet_values(
        mock_factory, spreadsheet_id="ss1", search_value="marketing"
    )
    assert "Marketing" in result


async def test_search_sheet_values_not_found(mock_factory, mock_sheets_service):
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "range": "Sheet1",
        "values": [["Name", "Value"]],
    }
    result = await google__search_sheet_values(
        mock_factory, spreadsheet_id="ss1", search_value="XYZ_NOTFOUND"
    )
    assert "No cells" in result


async def test_create_spreadsheet(mock_factory, mock_sheets_service):
    mock_sheets_service.spreadsheets().create().execute.return_value = {
        "spreadsheetId": "new_ss",
        "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/new_ss/edit",
    }
    result = await google__create_spreadsheet(mock_factory, title="Budget 2026")
    assert "Budget 2026" in result
    assert "new_ss" in result


async def test_update_sheet_values(mock_factory, mock_sheets_service):
    mock_sheets_service.spreadsheets().values().update().execute.return_value = {
        "updatedCells": 3,
        "updatedRange": "Sheet1!A1:C1",
    }
    result = await google__update_sheet_values(
        mock_factory,
        spreadsheet_id="ss1",
        range_="Sheet1!A1:C1",
        values=[["Header1", "Header2", "Header3"]],
    )
    assert "3" in result
    assert "cell" in result.lower()


async def test_append_sheet_values(mock_factory, mock_sheets_service):
    mock_sheets_service.spreadsheets().values().append().execute.return_value = {
        "updates": {"updatedCells": 3, "updatedRange": "Sheet1!A5:C5"}
    }
    result = await google__append_sheet_values(
        mock_factory,
        spreadsheet_id="ss1",
        range_="Sheet1",
        values=[["March 2026", "Marketing", "12400"]],
    )
    assert "1 row(s)" in result
    assert "Sheet1" in result


async def test_clear_sheet_values(mock_factory, mock_sheets_service):
    mock_sheets_service.spreadsheets().values().clear().execute.return_value = {}
    result = await google__clear_sheet_values(mock_factory, spreadsheet_id="ss1", range_="Sheet1!A1:Z100")
    assert "Cleared" in result
    assert "Sheet1!A1:Z100" in result


async def test_add_sheet(mock_factory, mock_sheets_service):
    mock_sheets_service.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [{"addSheet": {"properties": {"sheetId": 123}}}]
    }
    result = await google__add_sheet(mock_factory, spreadsheet_id="ss1", title="Q4")
    assert "Q4" in result
    assert "123" in result


async def test_format_cells(mock_factory, mock_sheets_service):
    mock_sheets_service.spreadsheets().batchUpdate().execute.return_value = {"replies": []}
    result = await google__format_cells(
        mock_factory, spreadsheet_id="ss1", sheet_id=0,
        start_row=0, end_row=1, start_col=0, end_col=3,
        bold=True, background_color="#FF0000",
    )
    assert "Formatting applied" in result


async def test_format_cells_no_changes(mock_factory, mock_sheets_service):
    result = await google__format_cells(
        mock_factory, spreadsheet_id="ss1", sheet_id=0,
        start_row=0, end_row=1, start_col=0, end_col=3,
    )
    assert "No formatting" in result


def test_col_letter():
    assert _col_letter(0) == "A"
    assert _col_letter(25) == "Z"
    assert _col_letter(26) == "AA"
    assert _col_letter(27) == "AB"
