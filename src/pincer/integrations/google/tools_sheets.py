"""
Google Sheets tools — 10 tools for reading and writing spreadsheets.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any, Callable

from pincer.integrations.google.models import fmt_sheet_values
from pincer.integrations.google.quota import with_backoff

if TYPE_CHECKING:
    from pincer.integrations.google.service_factory import GoogleServiceFactory
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ── Tool implementations ──────────────────────────────────────────────────────

async def google__list_sheets(
    factory: "GoogleServiceFactory",
    spreadsheet_id: str,
) -> str:
    """List all sheets/tabs in a spreadsheet."""
    svc = await factory.get("sheets")
    result = await with_backoff(
        lambda: svc.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="properties,sheets.properties"
        ).execute()
    )
    title = result.get("properties", {}).get("title", "?")
    sheets = result.get("sheets", [])
    lines = [
        f"  {s['properties'].get('title', '?')} "
        f"(index={s['properties'].get('index', '?')}, "
        f"id={s['properties'].get('sheetId', '?')})"
        for s in sheets
    ]
    return f"Spreadsheet: {title}\n{len(lines)} sheet(s):\n" + "\n".join(lines)


async def google__get_sheet_values(
    factory: "GoogleServiceFactory",
    spreadsheet_id: str,
    range_: str = "Sheet1",
) -> str:
    """Read cell values from a range (e.g. 'Sheet1!A1:D10')."""
    svc = await factory.get("sheets")
    result = await with_backoff(
        lambda: svc.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=range_
        ).execute()
    )
    values = result.get("values", [])
    return fmt_sheet_values(values, range_name=result.get("range", range_))


async def google__get_sheet_metadata(
    factory: "GoogleServiceFactory",
    spreadsheet_id: str,
) -> str:
    """Get spreadsheet properties: title, locale, timezone, named ranges."""
    svc = await factory.get("sheets")
    result = await with_backoff(
        lambda: svc.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="properties, namedRanges",
        ).execute()
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
    factory: "GoogleServiceFactory",
    spreadsheet_id: str,
    search_value: str,
    sheet_name: str = "Sheet1",
) -> str:
    """Find cells in a sheet that match a value (case-insensitive substring)."""
    svc = await factory.get("sheets")
    result = await with_backoff(
        lambda: svc.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=sheet_name
        ).execute()
    )
    values = result.get("values", [])
    matches: list[str] = []
    search_lower = search_value.lower()
    for row_idx, row in enumerate(values):
        for col_idx, cell in enumerate(row):
            if search_lower in str(cell).lower():
                col_letter = _col_letter(col_idx)
                matches.append(f"  {col_letter}{row_idx + 1}: {cell}")
    if not matches:
        return f"No cells matching '{search_value}' in {sheet_name}."
    return f"Found {len(matches)} cell(s) matching '{search_value}':\n" + "\n".join(matches)


def _col_letter(index: int) -> str:
    """Convert zero-based column index to letter(s) (0→A, 25→Z, 26→AA)."""
    result = ""
    while index >= 0:
        result = chr(index % 26 + ord("A")) + result
        index = index // 26 - 1
    return result


async def google__create_spreadsheet(
    factory: "GoogleServiceFactory",
    title: str,
) -> str:
    """Create a new Google Spreadsheet."""
    svc = await factory.get("sheets")
    result = await with_backoff(
        lambda: svc.spreadsheets().create(
            body={"properties": {"title": title}}
        ).execute()
    )
    ss_id = result.get("spreadsheetId", "")
    link = result.get("spreadsheetUrl", f"https://docs.google.com/spreadsheets/d/{ss_id}/edit")
    return f"Spreadsheet created: '{title}'\nID: {ss_id}\nLink: {link}"


async def google__update_sheet_values(
    factory: "GoogleServiceFactory",
    spreadsheet_id: str,
    range_: str,
    values: list[list[Any]],
    value_input_option: str = "USER_ENTERED",
) -> str:
    """Write values to a specific range in a spreadsheet."""
    svc = await factory.get("sheets")
    result = await with_backoff(
        lambda: svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_,
            valueInputOption=value_input_option,
            body={"values": values},
        ).execute()
    )
    updated = result.get("updatedCells", 0)
    return f"Updated {updated} cell(s) in {range_}."


async def google__append_sheet_values(
    factory: "GoogleServiceFactory",
    spreadsheet_id: str,
    range_: str,
    values: list[list[Any]],
    value_input_option: str = "USER_ENTERED",
) -> str:
    """Append rows to a sheet (adds after last row with data)."""
    svc = await factory.get("sheets")
    result = await with_backoff(
        lambda: svc.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range_,
            valueInputOption=value_input_option,
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()
    )
    updates = result.get("updates", {})
    updated = updates.get("updatedCells", 0)
    updated_range = updates.get("updatedRange", range_)
    return f"Appended {len(values)} row(s) → {updated_range} ({updated} cells updated)."


async def google__clear_sheet_values(
    factory: "GoogleServiceFactory",
    spreadsheet_id: str,
    range_: str,
) -> str:
    """Clear all values in a cell range."""
    svc = await factory.get("sheets")
    await with_backoff(
        lambda: svc.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id, range=range_, body={}
        ).execute()
    )
    return f"Cleared range {range_} in spreadsheet {spreadsheet_id}."


async def google__add_sheet(
    factory: "GoogleServiceFactory",
    spreadsheet_id: str,
    title: str,
) -> str:
    """Add a new sheet/tab to an existing spreadsheet."""
    svc = await factory.get("sheets")
    requests: list[dict[str, Any]] = [
        {"addSheet": {"properties": {"title": title}}}
    ]
    result = await with_backoff(
        lambda: svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}
        ).execute()
    )
    replies = result.get("replies", [{}])
    sheet_id = replies[0].get("addSheet", {}).get("properties", {}).get("sheetId", "") if replies else ""
    return f"Sheet '{title}' added (sheetId={sheet_id})."


async def google__format_cells(
    factory: "GoogleServiceFactory",
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
        lambda: svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}
        ).execute()
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

def register_sheets_tools(registry: "ToolRegistry", factory: "GoogleServiceFactory") -> int:
    """Register all 10 Sheets tools. Returns count."""

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(**kwargs):  # type: ignore[no-untyped-def]
            return await fn(factory, **kwargs)
        return wrapper

    registry.register(
        name="google__list_sheets",
        description="List all sheets/tabs in a Google Spreadsheet.",
        handler=_h(google__list_sheets),
        parameters={
            "type": "object",
            "properties": {"spreadsheet_id": {"type": "string"}},
            "required": ["spreadsheet_id"],
        },
    )
    registry.register(
        name="google__get_sheet_values",
        description="Read cell values from a Google Sheet range (e.g. 'Sheet1!A1:D10' or just 'Sheet1' for all data).",
        handler=_h(google__get_sheet_values),
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range_": {"type": "string", "description": "Range notation e.g. Sheet1!A1:Z100", "default": "Sheet1"},
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
        description="Search for cells matching a value in a sheet (case-insensitive substring match).",
        handler=_h(google__search_sheet_values),
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "search_value": {"type": "string"},
                "sheet_name": {"type": "string", "default": "Sheet1"},
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
