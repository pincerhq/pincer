"""
Google Sheets tools — 10 tools for reading and writing spreadsheets.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any

from pincer.integrations.google.quota import with_backoff

if TYPE_CHECKING:
    from collections.abc import Callable

    from pincer.integrations.google.service_factory import GoogleServiceFactory
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ── Tool implementations ──────────────────────────────────────────────────────


async def google__list_sheets(
    factory: GoogleServiceFactory,
    spreadsheet_id: str,
) -> str:
    """List all sheets/tabs in a spreadsheet."""
    svc = await factory.get("sheets")
    try:
        result = await with_backoff(
            lambda: (
                svc.spreadsheets()
                .get(
                    spreadsheetId=spreadsheet_id,
                    fields="properties,sheets.properties",
                )
                .execute()
            )
        )
    except Exception as e:
        error_msg = str(e)
        if "not supported" in error_msg.lower() or "unsupported" in error_msg.lower():
            return (
                f"This file (ID: {spreadsheet_id}) is not a Google Sheets spreadsheet — "
                f"it may be an Excel (.xlsx) file. "
                f"Download it with google__download_file(file_id='{spreadsheet_id}') instead.\n"
                f"To confirm the file type: google__get_file_metadata(file_id='{spreadsheet_id}')."
            )
        if "not found" in error_msg.lower() or "404" in error_msg:
            return (
                f"Spreadsheet not found (ID: {spreadsheet_id}). "
                f"Search with google__search_drive_files(name='<name>', file_type='spreadsheet')."
            )
        # Timeout or other transient error — give actionable guidance
        return (
            f"Error accessing spreadsheet (ID: {spreadsheet_id}): {type(e).__name__}: {error_msg}\n"
            f"If this is an Excel file, use google__download_file(file_id='{spreadsheet_id}') instead.\n"
            f"To check file type: google__get_file_metadata(file_id='{spreadsheet_id}')."
        )
    title = result.get("properties", {}).get("title", "?")
    sheets = result.get("sheets", [])
    lines = [
        f"  {s['properties'].get('title', '?')} "
        f"(index={s['properties'].get('index', '?')}, "
        f"id={s['properties'].get('sheetId', '?')})"
        for s in sheets
    ]
    output = f"Spreadsheet: {title}\n{len(lines)} sheet(s):\n" + "\n".join(lines)
    if sheets:
        first_tab = sheets[0]["properties"].get("title", "Sheet1")
        output += (
            f'\n\nTo read data: google__get_sheet_values(spreadsheet_id="{spreadsheet_id}",'
            f' sheet_name="{first_tab}", columns="A:C")'
        )
    return output


async def google__get_sheet_values(
    factory: GoogleServiceFactory,
    spreadsheet_id: str,
    range_: str = "",
    sheet_name: str = "",
    columns: str = "",
    rows: str = "",
) -> str:
    """Read cell values from a Google Sheet using A1 notation or friendly parameters."""
    svc = await factory.get("sheets")

    # Build A1 range from friendly parameters if no explicit range given
    if not range_:
        range_ = _build_a1_range(sheet_name, columns, rows)

    # Default: read all populated cells from first sheet
    if not range_:
        range_ = "A:ZZ"

    try:
        result = await with_backoff(
            lambda: (
                svc.spreadsheets()
                .values()
                .get(
                    spreadsheetId=spreadsheet_id,
                    range=range_,
                    valueRenderOption="FORMATTED_VALUE",
                )
                .execute()
            )
        )
    except Exception as e:
        error_msg = str(e)
        if "Unable to parse range" in error_msg:
            return (
                f"Invalid range: '{range_}'. "
                f"Check tab names with google__list_sheets(spreadsheet_id='{spreadsheet_id}'). "
                f"A1 examples: 'Sheet1!A1:D10', 'Budget!A:A', 'A1:C50'."
            )
        if "Requested entity was not found" in error_msg:
            return (
                f"Spreadsheet not found (ID: {spreadsheet_id}). "
                f"Search with google__search_drive_files(name='<name>', file_type='spreadsheet')."
            )
        if "not supported" in error_msg.lower() or "unsupported" in error_msg.lower():
            return (
                f"This file (ID: {spreadsheet_id}) is not a Google Sheets spreadsheet — "
                f"it may be an Excel (.xlsx) file. "
                f"Download it with google__download_file(file_id='{spreadsheet_id}') instead. "
                f"To confirm: google__get_file_metadata(file_id='{spreadsheet_id}')."
            )
        return f"Error reading spreadsheet: {error_msg}"

    values = result.get("values", [])
    if not values:
        return (
            f"No data found in range '{range_}'. "
            f"The sheet may be empty or the range may be wrong. "
            f"Try google__list_sheets(spreadsheet_id='{spreadsheet_id}') to check tab names, "
            f"then call google__get_sheet_values with no range to read all columns."
        )

    actual_range = result.get("range", range_)
    output = f"Data from {actual_range} ({len(values)} rows):\n\n"

    # Treat first row as headers when multiple rows present
    if len(values) > 1:
        headers = values[0]
        output += " | ".join(str(h) for h in headers) + "\n"
        output += "-|-".join("---" for _ in headers) + "\n"
        for row in values[1:]:
            padded = row + [""] * (len(headers) - len(row))
            output += " | ".join(str(c) for c in padded) + "\n"
    else:
        # Only a single row returned (possibly just a header)
        output += " | ".join(str(c) for c in values[0]) + "\n"
        output += (
            f"\n(Only 1 row returned — this column may be empty or the data is in OTHER columns. "
            f"Read all columns to see the full spreadsheet structure:\n"
            f"  google__get_sheet_values(spreadsheet_id='{spreadsheet_id}'"
        )
        if sheet_name:
            output += f", sheet_name='{sheet_name}'"
        output += f")\nOr list tabs first: google__list_sheets(spreadsheet_id='{spreadsheet_id}'))"

    # Truncate large output to avoid context window overload
    max_output = 3000
    if len(output) > max_output:
        output = output[:max_output] + f"\n\n... (truncated at {max_output} chars of {len(output)} total)"

    return output


async def google__get_sheet_metadata(
    factory: GoogleServiceFactory,
    spreadsheet_id: str,
) -> str:
    """Get spreadsheet properties: title, locale, timezone, named ranges."""
    svc = await factory.get("sheets")
    result = await with_backoff(
        lambda: (
            svc.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                fields="properties, namedRanges",
            )
            .execute()
        )
    )
    props = result.get("properties", {})
    named = result.get("namedRanges", [])
    lines = [
        f"Title:    {props.get('title', '')}",
        f"Locale:   {props.get('locale', '')}",
        f"Timezone: {props.get('timeZone', '')}",
        f"ID:       {spreadsheet_id}",
    ]
    if named:
        lines.append(f"Named ranges ({len(named)}):")
        for nr in named:
            lines.append(f"  {nr.get('name', '?')} → {nr.get('namedRangeId', '?')}")
    return "\n".join(lines)


async def google__search_sheet_values(
    factory: GoogleServiceFactory,
    spreadsheet_id: str,
    search_value: str,
    sheet_name: str = "",
    match_case: bool = False,
    match_entire_cell: bool = False,
) -> str:
    """Find cells in a spreadsheet matching a value (case-insensitive substring by default).

    Searches all tabs unless sheet_name is specified.
    """
    svc = await factory.get("sheets")

    # Determine which tabs to search
    if sheet_name:
        sheets_to_search = [sheet_name]
    else:
        try:
            spreadsheet = await with_backoff(
                lambda: (
                    svc.spreadsheets()
                    .get(
                        spreadsheetId=spreadsheet_id,
                        fields="sheets.properties.title",
                    )
                    .execute()
                )
            )
            sheets_to_search = [s["properties"]["title"] for s in spreadsheet.get("sheets", [])]
        except Exception as e:
            return f"Error accessing spreadsheet: {e}"

    if not sheets_to_search:
        return "No sheets/tabs found in this spreadsheet."

    search_lower = search_value.lower() if not match_case else search_value
    matches: list[str] = []

    for tab in sheets_to_search:
        safe_tab = _quote_tab_name(tab)

        try:
            result = await with_backoff(
                (
                    lambda t: (
                        lambda: (
                            svc.spreadsheets()
                            .values()
                            .get(
                                spreadsheetId=spreadsheet_id,
                                range=f"{t}!A:ZZ",
                                valueRenderOption="FORMATTED_VALUE",
                            )
                            .execute()
                        )
                    )
                )(safe_tab)
            )
        except Exception:
            continue  # skip tabs that can't be read (e.g. chart sheets)

        values = result.get("values", [])
        for row_idx, row in enumerate(values):
            for col_idx, cell in enumerate(row):
                cell_str = str(cell)
                if match_entire_cell:
                    is_match = (cell_str == search_value) if match_case else (cell_str.lower() == search_lower)
                else:
                    is_match = (search_value in cell_str) if match_case else (search_lower in cell_str.lower())

                if is_match:
                    col_letter = _col_letter(col_idx)
                    cell_ref = f"{tab}!{col_letter}{row_idx + 1}"
                    row_context = " | ".join(str(c) for c in row[:10])
                    matches.append(f"  {cell_ref}\n   Value: {cell_str[:200]}\n   Row: {row_context[:300]}")
                    if len(matches) >= 50:
                        break
            if len(matches) >= 50:
                break
        if len(matches) >= 50:
            break

    if not matches:
        searched = ", ".join(sheets_to_search)
        return f"No cells containing '{search_value}' found.\nSearched sheets: {searched}"

    output = f"Found {len(matches)} cell(s) containing '{search_value}':\n\n"
    output += "\n\n".join(matches)

    if len(matches) == 50:
        output += "\n\n(Results capped at 50 matches. Use a more specific search term.)"

    return output


def _col_letter(index: int) -> str:
    """Convert zero-based column index to letter(s) (0→A, 25→Z, 26→AA)."""
    result = ""
    while index >= 0:
        result = chr(index % 26 + ord("A")) + result
        index = index // 26 - 1
    return result


def _quote_tab_name(name: str) -> str:
    """Quote a sheet/tab name for A1 notation if it contains spaces or apostrophes."""
    if " " in name or "'" in name or "!" in name:
        return "'" + name.replace("'", "\\'") + "'"
    return name


def _build_a1_range(sheet_name: str = "", columns: str = "", rows: str = "") -> str:
    """Build A1 notation from friendly parameters.

    Examples:
        sheet_name="Budget", columns="A"         → "Budget!A:A"
        sheet_name="Budget", columns="A:C"        → "Budget!A:C"
        columns="A", rows="1:50"                  → "A1:A50"
        sheet_name="Q1 Budget", columns="A"       → "'Q1 Budget'!A:A"
        rows="1:10"                               → "A1:ZZ10"
        sheet_name="Sheet1"                       → "Sheet1"
        (nothing)                                 → ""
    """
    if not columns and not rows and not sheet_name:
        return ""

    range_part = ""
    if columns and rows:
        col_start, col_end = _parse_column_range(columns)
        row_start, row_end = _parse_row_range(rows)
        range_part = f"{col_start}{row_start}:{col_end}{row_end}"
    elif columns:
        col_start, col_end = _parse_column_range(columns)
        range_part = f"{col_start}:{col_end}"
    elif rows:
        row_start, row_end = _parse_row_range(rows)
        range_part = f"A{row_start}:ZZ{row_end}"

    if sheet_name:
        safe_name = _quote_tab_name(sheet_name)
        return f"{safe_name}!{range_part}" if range_part else safe_name
    return range_part


def _parse_column_range(columns: str) -> tuple[str, str]:
    """Parse 'A', 'A:C', 'B:D' into (start_col, end_col)."""
    columns = columns.upper().strip()
    if ":" in columns:
        parts = columns.split(":", 1)
        return parts[0].strip(), parts[1].strip()
    return columns, columns


def _parse_row_range(rows: str) -> tuple[str, str]:
    """Parse '1:50', '1', '10:100' into (start_row, end_row)."""
    rows = rows.strip()
    if ":" in rows:
        parts = rows.split(":", 1)
        return parts[0].strip(), parts[1].strip()
    return rows, rows


async def google__create_spreadsheet(
    factory: GoogleServiceFactory,
    title: str,
) -> str:
    """Create a new Google Spreadsheet."""
    svc = await factory.get("sheets")
    result = await with_backoff(lambda: svc.spreadsheets().create(body={"properties": {"title": title}}).execute())
    ss_id = result.get("spreadsheetId", "")
    link = result.get("spreadsheetUrl", f"https://docs.google.com/spreadsheets/d/{ss_id}/edit")
    return f"Spreadsheet created: '{title}'\nID: {ss_id}\nLink: {link}"


async def google__update_sheet_values(
    factory: GoogleServiceFactory,
    spreadsheet_id: str,
    range_: str,
    values: list[list[Any]],
    value_input_option: str = "USER_ENTERED",
) -> str:
    """Write values to a specific range in a spreadsheet."""
    svc = await factory.get("sheets")
    result = await with_backoff(
        lambda: (
            svc.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=range_,
                valueInputOption=value_input_option,
                body={"values": values},
            )
            .execute()
        )
    )
    updated = result.get("updatedCells", 0)
    return f"Updated {updated} cell(s) in {range_}."


async def google__append_sheet_values(
    factory: GoogleServiceFactory,
    spreadsheet_id: str,
    range_: str,
    values: list[list[Any]],
    value_input_option: str = "USER_ENTERED",
) -> str:
    """Append rows to a sheet (adds after last row with data)."""
    svc = await factory.get("sheets")
    result = await with_backoff(
        lambda: (
            svc.spreadsheets()
            .values()
            .append(
                spreadsheetId=spreadsheet_id,
                range=range_,
                valueInputOption=value_input_option,
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            )
            .execute()
        )
    )
    updates = result.get("updates", {})
    updated = updates.get("updatedCells", 0)
    updated_range = updates.get("updatedRange", range_)
    return f"Appended {len(values)} row(s) → {updated_range} ({updated} cells updated)."


async def google__clear_sheet_values(
    factory: GoogleServiceFactory,
    spreadsheet_id: str,
    range_: str,
) -> str:
    """Clear all values in a cell range."""
    svc = await factory.get("sheets")
    await with_backoff(
        lambda: svc.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=range_, body={}).execute()
    )
    return f"Cleared range {range_} in spreadsheet {spreadsheet_id}."


async def google__add_sheet(
    factory: GoogleServiceFactory,
    spreadsheet_id: str,
    title: str,
) -> str:
    """Add a new sheet/tab to an existing spreadsheet."""
    svc = await factory.get("sheets")
    requests: list[dict[str, Any]] = [{"addSheet": {"properties": {"title": title}}}]
    result = await with_backoff(
        lambda: svc.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()
    )
    replies = result.get("replies", [{}])
    sheet_id = replies[0].get("addSheet", {}).get("properties", {}).get("sheetId", "") if replies else ""
    return f"Sheet '{title}' added (sheetId={sheet_id})."


async def google__format_cells(
    factory: GoogleServiceFactory,
    spreadsheet_id: str,
    sheet_id: int,
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
    bold: bool = False,
    background_color: str = "",
) -> str:
    """Apply formatting to a cell range (bold, background color)."""
    svc = await factory.get("sheets")
    cell_format: dict[str, Any] = {}
    fields_list: list[str] = []

    if bold:
        cell_format["textFormat"] = {"bold": True}
        fields_list.append("userEnteredFormat.textFormat.bold")

    if background_color:
        # Parse hex color like #FF0000
        r, g, b = _parse_hex_color(background_color)
        cell_format["backgroundColor"] = {"red": r, "green": g, "blue": b}
        fields_list.append("userEnteredFormat.backgroundColor")

    if not cell_format:
        return "No formatting changes requested."

    range_spec = {
        "sheetId": sheet_id,
        "startRowIndex": start_row,
        "endRowIndex": end_row,
        "startColumnIndex": start_col,
        "endColumnIndex": end_col,
    }
    requests: list[dict[str, Any]] = [
        {
            "repeatCell": {
                "range": range_spec,
                "cell": {"userEnteredFormat": cell_format},
                "fields": ",".join(fields_list),
            }
        }
    ]
    await with_backoff(
        lambda: svc.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()
    )
    return f"Formatting applied to rows {start_row}–{end_row}, cols {start_col}–{end_col}."


def _parse_hex_color(hex_color: str) -> tuple[float, float, float]:
    """Parse #RRGGBB → (r, g, b) as 0..1 floats."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return 0.0, 0.0, 0.0
    r = int(hex_color[0:2], 16) / 255
    g = int(hex_color[2:4], 16) / 255
    b = int(hex_color[4:6], 16) / 255
    return r, g, b


# ── Registry ──────────────────────────────────────────────────────────────────


def register_sheets_tools(registry: ToolRegistry, factory: GoogleServiceFactory) -> int:
    """Register all 10 Sheets tools. Returns count."""

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(**kwargs):  # type: ignore[no-untyped-def]
            return await fn(factory, **kwargs)

        return wrapper

    registry.register(
        name="google__list_sheets",
        description=(
            "List all sheets/tabs in a Google Spreadsheet. "
            "Returns tab names, indices, and sheet IDs. "
            "Use this when you need tab names before calling google__get_sheet_values. "
            "If you don't have the spreadsheet_id, call "
            "google__search_drive_files(name='<name>', file_type='spreadsheet') first."
        ),
        handler=_h(google__list_sheets),
        parameters={
            "type": "object",
            "properties": {"spreadsheet_id": {"type": "string"}},
            "required": ["spreadsheet_id"],
        },
    )
    registry.register(
        name="google__get_sheet_values",
        description=(
            "Read values from a Google Sheets spreadsheet (online Google-native format). "
            "This does NOT work on .xlsx files — for those, use google__download_file "
            "then python_exec with openpyxl.\n\n"
            "Accepts friendly parameters (no A1 notation needed) OR an explicit A1 range string.\n"
            "Friendly params: sheet_name='Budget', columns='A' or columns='A:C', rows='1:50'\n"
            "A1 range: range_='Sheet1!A1:D10' or range_='Budget!A:A'\n\n"
            "If you only have the spreadsheet name (not ID), first call "
            "google__search_drive_files(name='<name>', file_type='spreadsheet').\n"
            "The search results will tell you whether the file is a Google Sheet or .xlsx.\n"
            "If you don't know tab names, call google__list_sheets(spreadsheet_id='...') first.\n\n"
            "If a column returns only 1 row (just a header), read ALL columns first to understand "
            "the spreadsheet structure: google__get_sheet_values(spreadsheet_id='...')\n\n"
            "Examples: sheet_name='Budget' columns='A' → column A from Budget tab; "
            "columns='A:C' → columns A through C; rows='1:50' → rows 1-50."
        ),
        handler=_h(google__get_sheet_values),
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range_": {
                    "type": "string",
                    "description": (
                        "A1 notation range e.g. Sheet1!A1:D10 or Budget!A:A. Optional if using sheet_name/columns/rows."
                    ),
                    "default": "",
                },
                "sheet_name": {
                    "type": "string",
                    "description": (
                        "Tab/sheet name e.g. 'Budget' or 'Q1 Data'. Use with columns/rows for friendly access."
                    ),
                    "default": "",
                },
                "columns": {
                    "type": "string",
                    "description": "Column(s) to read: 'A' for single column, 'A:C' for range. Use with sheet_name.",
                    "default": "",
                },
                "rows": {
                    "type": "string",
                    "description": "Row range to read: '1:50' for rows 1-50. Use with sheet_name/columns.",
                    "default": "",
                },
            },
            "required": ["spreadsheet_id"],
        },
    )
    registry.register(
        name="google__get_sheet_metadata",
        description="Get spreadsheet properties: title, locale, timezone, and named ranges.",
        handler=_h(google__get_sheet_metadata),
        parameters={
            "type": "object",
            "properties": {"spreadsheet_id": {"type": "string"}},
            "required": ["spreadsheet_id"],
        },
    )
    registry.register(
        name="google__search_sheet_values",
        description=(
            "Search for a value in a Google Sheets spreadsheet (online Google-native format). "
            "Does NOT work on .xlsx files. "
            "Finds all cells containing the search term and returns their cell references "
            "(e.g. 'Sheet1!C14') with the matching value and row context. "
            "Searches across ALL tabs by default unless sheet_name is specified. "
            "If you don't have the spreadsheet_id, call "
            "google__search_drive_files(name='<name>', file_type='spreadsheet') first."
        ),
        handler=_h(google__search_sheet_values),
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "search_value": {
                    "type": "string",
                    "description": "Value to search for (case-insensitive substring by default)",
                },
                "sheet_name": {
                    "type": "string",
                    "description": "Limit search to a specific tab. Leave empty to search all tabs.",
                    "default": "",
                },
                "match_case": {"type": "boolean", "default": False},
                "match_entire_cell": {"type": "boolean", "default": False},
            },
            "required": ["spreadsheet_id", "search_value"],
        },
    )
    registry.register(
        name="google__create_spreadsheet",
        description="Create a new Google Spreadsheet.",
        handler=_h(google__create_spreadsheet),
        parameters={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__update_sheet_values",
        description="Write values to a specific range in a Google Sheet.",
        handler=_h(google__update_sheet_values),
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range_": {"type": "string", "description": "Range like Sheet1!A1:C3"},
                "values": {"type": "array", "items": {"type": "array"}, "description": "2D array of values"},
                "value_input_option": {"type": "string", "enum": ["USER_ENTERED", "RAW"], "default": "USER_ENTERED"},
            },
            "required": ["spreadsheet_id", "range_", "values"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__append_sheet_values",
        description="Append rows to a Google Sheet (adds after the last row with data).",
        handler=_h(google__append_sheet_values),
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range_": {"type": "string", "description": "Sheet or range to append to (e.g. Sheet1)"},
                "values": {"type": "array", "items": {"type": "array"}, "description": "2D array of rows to append"},
                "value_input_option": {"type": "string", "enum": ["USER_ENTERED", "RAW"], "default": "USER_ENTERED"},
            },
            "required": ["spreadsheet_id", "range_", "values"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__clear_sheet_values",
        description="Clear all values in a cell range in a Google Sheet.",
        handler=_h(google__clear_sheet_values),
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range_": {"type": "string"},
            },
            "required": ["spreadsheet_id", "range_"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__add_sheet",
        description="Add a new sheet/tab to an existing Google Spreadsheet.",
        handler=_h(google__add_sheet),
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "title": {"type": "string", "description": "Name for the new sheet"},
            },
            "required": ["spreadsheet_id", "title"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__format_cells",
        description="Apply formatting to a cell range in a Google Sheet (bold, background color).",
        handler=_h(google__format_cells),
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "sheet_id": {"type": "integer", "description": "Numeric sheet ID (from google__list_sheets)"},
                "start_row": {"type": "integer", "description": "Start row index (0-based)"},
                "end_row": {"type": "integer", "description": "End row index (exclusive)"},
                "start_col": {"type": "integer", "description": "Start column index (0-based)"},
                "end_col": {"type": "integer", "description": "End column index (exclusive)"},
                "bold": {"type": "boolean", "default": False},
                "background_color": {"type": "string", "description": "Hex color code e.g. #FF0000", "default": ""},
            },
            "required": ["spreadsheet_id", "sheet_id", "start_row", "end_row", "start_col", "end_col"],
        },
        require_approval=True,
    )
    return 10
