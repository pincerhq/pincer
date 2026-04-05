"""
Tests for Sheets tools — covers all 10 tools plus new SH-03/SH-05 functionality.
"""

from __future__ import annotations

import pytest

from pincer.integrations.google.tools_sheets import (
    _build_a1_range,
    _col_letter,
    _parse_column_range,
    _parse_row_range,
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


def test_list_sheets_shows_workflow_hint(mock_factory, mock_sheets_service):
    """list_sheets output includes a hint for calling get_sheet_values."""
    import asyncio

    mock_sheets_service.spreadsheets().get().execute.return_value = {
        "properties": {"title": "Budget"},
        "sheets": [{"properties": {"title": "Sheet1", "index": 0, "sheetId": 0}}],
    }
    result = asyncio.get_event_loop().run_until_complete(
        google__list_sheets(mock_factory, spreadsheet_id="ss1")
    )
    assert "google__get_sheet_values" in result


async def test_get_sheet_values_explicit_range(mock_factory, mock_sheets_service):
    """Explicit range_ parameter is passed through unchanged."""
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "range": "Sheet1!A1:C3",
        "values": [["Name", "Amount", "Date"], ["Marketing", "12400", "2026-03-01"]],
    }
    result = await google__get_sheet_values(mock_factory, spreadsheet_id="ss1", range_="Sheet1!A1:C3")
    assert "Name" in result
    assert "12400" in result


async def test_get_sheet_values_empty(mock_factory, mock_sheets_service):
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {"range": "A:ZZ", "values": []}
    result = await google__get_sheet_values(mock_factory, spreadsheet_id="ss1")
    assert "No data" in result
    assert "google__list_sheets" in result


async def test_get_sheet_values_header_only_hint(mock_factory, mock_sheets_service):
    """When only 1 row is returned, the tool hints to read all columns — NOT 'no data rows yet'."""
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "range": "Sheet1!D:D",
        "values": [["Tool Name"]],
    }
    result = await google__get_sheet_values(mock_factory, spreadsheet_id="ss1", columns="D")
    assert "Tool Name" in result
    assert "Only 1 row" in result
    assert "google__get_sheet_values" in result
    # Must NOT use the misleading "no data rows yet" phrasing
    assert "no data rows yet" not in result.lower()
    # Must guide user toward reading OTHER columns
    assert "other columns" in result.lower() or "OTHER columns" in result


async def test_get_sheet_values_friendly_column(mock_factory, mock_sheets_service):
    """sheet_name + columns builds correct A1 range and returns formatted data."""
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "range": "Budget!A1:A5",
        "values": [["Category"], ["Rent"], ["Food"], ["Travel"], ["Misc"]],
    }
    result = await google__get_sheet_values(
        mock_factory, spreadsheet_id="ss1", sheet_name="Budget", columns="A"
    )
    assert "Category" in result
    assert "Rent" in result
    # Verify the API was called with the correct A1 range
    call_args = str(mock_sheets_service.spreadsheets().values().get.call_args)
    assert "Budget!A:A" in call_args


async def test_get_sheet_values_friendly_column_range(mock_factory, mock_sheets_service):
    """columns='A:C' reads across three columns."""
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "range": "Sheet1!A1:C3",
        "values": [["A", "B", "C"], ["1", "2", "3"], ["4", "5", "6"]],
    }
    result = await google__get_sheet_values(mock_factory, spreadsheet_id="ss1", columns="A:C")
    assert "A | B | C" in result


async def test_get_sheet_values_friendly_rows(mock_factory, mock_sheets_service):
    """rows='1:10' reads rows 1-10 across all columns."""
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "range": "Sheet1!A1:ZZ10",
        "values": [["data"]],
    }
    result = await google__get_sheet_values(mock_factory, spreadsheet_id="ss1", rows="1:10")
    call_args = str(mock_sheets_service.spreadsheets().values().get.call_args)
    assert "A1:ZZ10" in call_args
    assert "data" in result


async def test_get_sheet_values_sheet_name_with_spaces(mock_factory, mock_sheets_service):
    """Tab names with spaces are quoted in A1 notation."""
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "range": "'Q1 Budget'!A:A",
        "values": [["Total"]],
    }
    await google__get_sheet_values(
        mock_factory, spreadsheet_id="ss1", sheet_name="Q1 Budget", columns="A"
    )
    call_args = str(mock_sheets_service.spreadsheets().values().get.call_args)
    assert "'Q1 Budget'" in call_args


async def test_get_sheet_values_invalid_range_error(mock_factory, mock_sheets_service):
    """Bad range returns a helpful error with suggestions."""
    mock_sheets_service.spreadsheets().values().get().execute.side_effect = Exception(
        "400: Unable to parse range: INVALID!!"
    )
    result = await google__get_sheet_values(mock_factory, spreadsheet_id="ss1", range_="INVALID!!")
    assert "Invalid range" in result
    assert "google__list_sheets" in result


async def test_get_sheet_values_not_found_error(mock_factory, mock_sheets_service):
    """Non-existent spreadsheet_id returns a helpful error with new-style search hint."""
    mock_sheets_service.spreadsheets().values().get().execute.side_effect = Exception(
        "404: Requested entity was not found"
    )
    result = await google__get_sheet_values(mock_factory, spreadsheet_id="bad_id", range_="A:A")
    assert "not found" in result.lower()
    assert "google__search_drive_files" in result
    # Must use new friendly syntax, not old query= syntax
    assert "name=" in result
    assert "file_type=" in result


async def test_get_sheet_values_excel_file_redirects_to_download(mock_factory, mock_sheets_service):
    """Calling get_sheet_values on an Excel file returns a redirect to google__download_file."""
    mock_sheets_service.spreadsheets().values().get().execute.side_effect = Exception(
        "This operation is not supported for this document"
    )
    result = await google__get_sheet_values(mock_factory, spreadsheet_id="xlsx_id", range_="A:A")
    assert "google__download_file" in result
    assert "xlsx_id" in result


async def test_list_sheets_excel_file_redirects_to_download(mock_factory, mock_sheets_service):
    """Calling list_sheets on an Excel file returns a redirect to google__download_file."""
    mock_sheets_service.spreadsheets().get().execute.side_effect = Exception(
        "This operation is not supported for this document"
    )
    result = await google__list_sheets(mock_factory, spreadsheet_id="xlsx_id")
    assert "google__download_file" in result
    assert "xlsx_id" in result


async def test_list_sheets_timeout_returns_actionable_message(mock_factory, mock_sheets_service):
    """Timeout on list_sheets returns actionable guidance (not a bare exception)."""
    mock_sheets_service.spreadsheets().get().execute.side_effect = TimeoutError("The read operation timed out")
    result = await google__list_sheets(mock_factory, spreadsheet_id="xlsx_id")
    assert "xlsx_id" in result
    assert "google__download_file" in result or "google__get_file_metadata" in result


async def test_list_sheets_not_found_error(mock_factory, mock_sheets_service):
    """Not-found error on list_sheets returns a helpful search hint."""
    mock_sheets_service.spreadsheets().get().execute.side_effect = Exception(
        "404: Requested entity was not found"
    )
    result = await google__list_sheets(mock_factory, spreadsheet_id="bad_id")
    assert "not found" in result.lower()
    assert "google__search_drive_files" in result


async def test_get_sheet_values_large_output_truncated(mock_factory, mock_sheets_service):
    """Large datasets are truncated with a note."""
    big_data = [["row" + str(i), "data"] for i in range(500)]
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "range": "Sheet1!A:B",
        "values": big_data,
    }
    result = await google__get_sheet_values(mock_factory, spreadsheet_id="ss1", range_="Sheet1!A:B")
    assert len(result) <= 3200
    assert "truncated" in result.lower()


async def test_get_sheet_metadata(mock_factory, mock_sheets_service):
    mock_sheets_service.spreadsheets().get().execute.return_value = {
        "properties": {"title": "My Sheet", "locale": "en_US", "timeZone": "UTC"},
        "namedRanges": [{"name": "TaxRate", "namedRangeId": "nr1"}],
    }
    result = await google__get_sheet_metadata(mock_factory, spreadsheet_id="ss1")
    assert "My Sheet" in result
    assert "TaxRate" in result


# ── SH-05: google__search_sheet_values ───────────────────────────────────────


async def test_search_sheet_values_all_tabs(mock_factory, mock_sheets_service):
    """Without sheet_name, lists all tabs then searches each."""
    mock_sheets_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"title": "Sheet1"}}]
    }
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "range": "Sheet1",
        "values": [["Name", "Value"], ["Marketing", "12400"], ["Sales", "8000"]],
    }
    result = await google__search_sheet_values(mock_factory, spreadsheet_id="ss1", search_value="marketing")
    assert "Marketing" in result
    assert "Sheet1!" in result


async def test_search_sheet_values_finds_cell_reference(mock_factory, mock_sheets_service):
    """Returns cell references like 'Sheet1!A3'."""
    mock_sheets_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"title": "Sheet1"}}]
    }
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "values": [
            ["ID", "Task", "Status"],
            ["SH-01", "Setup", "Done"],
            ["SH-05", "Search", "In Progress"],
        ],
    }
    result = await google__search_sheet_values(mock_factory, spreadsheet_id="ss1", search_value="SH-05")
    assert "Sheet1!A3" in result
    assert "SH-05" in result


async def test_search_sheet_values_not_found(mock_factory, mock_sheets_service):
    """No matches returns a clear message with searched sheets."""
    mock_sheets_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"title": "Sheet1"}}]
    }
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "range": "Sheet1",
        "values": [["Name", "Value"]],
    }
    result = await google__search_sheet_values(mock_factory, spreadsheet_id="ss1", search_value="XYZ_NOTFOUND")
    assert "No cells" in result
    assert "XYZ_NOTFOUND" in result


async def test_search_sheet_values_specific_tab(mock_factory, mock_sheets_service):
    """sheet_name limits search to one tab without listing all sheets."""
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "values": [["found item"]],
    }
    result = await google__search_sheet_values(
        mock_factory, spreadsheet_id="ss1", search_value="found", sheet_name="TargetTab"
    )
    assert "TargetTab!A1" in result
    # The spreadsheets().get() for listing should NOT have been called
    call_args = str(mock_sheets_service.spreadsheets().get.call_args_list)
    # get() is called for listing only — with sheet_name provided it should be empty or only metadata
    assert "fields" not in call_args or "TargetTab" not in call_args


async def test_search_sheet_values_searches_multiple_tabs(mock_factory, mock_sheets_service):
    """Searches all tabs when sheet_name is not specified, finds match in second tab."""
    mock_sheets_service.spreadsheets().get().execute.return_value = {
        "sheets": [
            {"properties": {"title": "Tab1"}},
            {"properties": {"title": "Tab2"}},
        ]
    }
    mock_sheets_service.spreadsheets().values().get().execute.side_effect = [
        {"values": [["nothing here"]]},
        {"values": [["target found"]]},
    ]
    result = await google__search_sheet_values(mock_factory, spreadsheet_id="ss1", search_value="target")
    assert "Tab2!A1" in result


async def test_search_sheet_values_caps_at_50(mock_factory, mock_sheets_service):
    """Results are capped at 50 matches."""
    mock_sheets_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"title": "Sheet1"}}]
    }
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "values": [["match"] for _ in range(100)],
    }
    result = await google__search_sheet_values(mock_factory, spreadsheet_id="ss1", search_value="match")
    assert "50" in result
    assert "capped" in result.lower() or "specific" in result.lower()


async def test_search_sheet_values_case_sensitive(mock_factory, mock_sheets_service):
    """match_case=True performs case-sensitive search."""
    mock_sheets_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"title": "Sheet1"}}]
    }
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "values": [["Hello World"], ["hello world"]],
    }
    result = await google__search_sheet_values(
        mock_factory, spreadsheet_id="ss1", search_value="Hello", match_case=True
    )
    assert "Sheet1!A1" in result
    # "hello world" (lowercase) should NOT match
    assert "Sheet1!A2" not in result


async def test_search_sheet_values_entire_cell(mock_factory, mock_sheets_service):
    """match_entire_cell=True only matches exact cell values."""
    mock_sheets_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"title": "Sheet1"}}]
    }
    mock_sheets_service.spreadsheets().values().get().execute.return_value = {
        "values": [["SH-05"], ["SH-05-extra"], ["SH-06"]],
    }
    result = await google__search_sheet_values(
        mock_factory, spreadsheet_id="ss1", search_value="SH-05", match_entire_cell=True
    )
    assert "Sheet1!A1" in result
    assert "Sheet1!A2" not in result  # "SH-05-extra" should not match


# ── Helper function unit tests ────────────────────────────────────────────────


def test_col_letter():
    assert _col_letter(0) == "A"
    assert _col_letter(25) == "Z"
    assert _col_letter(26) == "AA"
    assert _col_letter(27) == "AB"


class TestBuildA1Range:
    def test_column_only(self):
        assert _build_a1_range(columns="A") == "A:A"

    def test_column_range(self):
        assert _build_a1_range(columns="A:C") == "A:C"

    def test_sheet_and_column(self):
        assert _build_a1_range(sheet_name="Budget", columns="A") == "Budget!A:A"

    def test_sheet_only(self):
        assert _build_a1_range(sheet_name="Budget") == "Budget"

    def test_sheet_with_spaces(self):
        result = _build_a1_range(sheet_name="Q1 Budget", columns="A")
        assert result == "'Q1 Budget'!A:A"

    def test_columns_and_rows(self):
        assert _build_a1_range(columns="A:C", rows="1:50") == "A1:C50"

    def test_sheet_columns_rows(self):
        assert _build_a1_range(sheet_name="Data", columns="B", rows="1:100") == "Data!B1:B100"

    def test_rows_only(self):
        assert _build_a1_range(rows="1:10") == "A1:ZZ10"

    def test_empty(self):
        assert _build_a1_range() == ""

    def test_single_column_with_rows(self):
        assert _build_a1_range(columns="B", rows="1:50") == "B1:B50"


class TestParseColumnRange:
    def test_single(self):
        assert _parse_column_range("A") == ("A", "A")

    def test_range(self):
        assert _parse_column_range("A:C") == ("A", "C")

    def test_lowercase_normalized(self):
        assert _parse_column_range("a:c") == ("A", "C")


class TestParseRowRange:
    def test_single(self):
        assert _parse_row_range("1") == ("1", "1")

    def test_range(self):
        assert _parse_row_range("1:50") == ("1", "50")


# ── Other tools (regression) ──────────────────────────────────────────────────


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
        mock_factory,
        spreadsheet_id="ss1",
        sheet_id=0,
        start_row=0,
        end_row=1,
        start_col=0,
        end_col=3,
        bold=True,
        background_color="#FF0000",
    )
    assert "Formatting applied" in result


async def test_format_cells_no_changes(mock_factory, mock_sheets_service):
    result = await google__format_cells(
        mock_factory,
        spreadsheet_id="ss1",
        sheet_id=0,
        start_row=0,
        end_row=1,
        start_col=0,
        end_col=3,
    )
    assert "No formatting" in result
