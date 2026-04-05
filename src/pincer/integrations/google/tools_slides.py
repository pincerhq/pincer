"""
Google Slides tools — 6 tools for reading and editing presentations.
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


def _placeholder_label(ptype: str) -> str:
    return {
        "TITLE": "Title",
        "CENTERED_TITLE": "Centered Title",
        "SUBTITLE": "Subtitle",
        "BODY": "Body",
        "HEADER": "Header",
        "FOOTER": "Footer",
        "SLIDE_NUMBER": "Slide Number",
        "OBJECT": "Content",
    }.get(ptype, f"Placeholder ({ptype})")


# ── Tool implementations ──────────────────────────────────────────────────────


async def google__list_slides(
    factory: GoogleServiceFactory,
    presentation_id: str,
) -> str:
    """List all slides in a presentation."""
    svc = await factory.get("slides")
    pres = await with_backoff(lambda: svc.presentations().get(presentationId=presentation_id).execute())
    title = pres.get("title", "(untitled)")
    slides = pres.get("slides", [])
    if not slides:
        return f"Presentation '{title}' has no slides."
    lines = []
    for i, slide in enumerate(slides, 1):
        slide_id = slide.get("objectId", "")
        layout = slide.get("slideProperties", {}).get("layoutObjectId", "")
        lines.append(f"  Slide {i}: id={slide_id}, layout={layout}")
    return f"Presentation: {title}\n{len(slides)} slide(s):\n" + "\n".join(lines)


async def google__get_slide_content(
    factory: GoogleServiceFactory,
    presentation_id: str,
    slide_index: int = 0,
) -> str:
    """Get the text content and placeholder object_ids of a specific slide (0-indexed).

    Returns each placeholder's object_id with an edit hint.
    IMPORTANT: The slide page ID is NOT the same as placeholder IDs — only
    placeholder IDs can be used with google__update_slide_text.
    """
    svc = await factory.get("slides")
    pres = await with_backoff(lambda: svc.presentations().get(presentationId=presentation_id).execute())
    slides = pres.get("slides", [])
    if slide_index < 0 or slide_index >= len(slides):
        return f"Invalid slide_index={slide_index}. Presentation has {len(slides)} slides (0-indexed)."
    slide = slides[slide_index]
    page_id = slide.get("objectId", "?")
    output = f"Slide {slide_index} (page ID: {page_id} — do NOT use this for text editing):\n\n"

    text_elements_found = False
    for elem in slide.get("pageElements", []):
        obj_id = elem.get("objectId", "?")
        shape = elem.get("shape", {})
        ph_type = shape.get("placeholder", {}).get("type", "")

        if ph_type:
            label = _placeholder_label(ph_type)
        elif shape.get("shapeType"):
            label = f"Shape ({shape['shapeType']})"
        else:
            continue

        text = ""
        for te in shape.get("text", {}).get("textElements", []):
            run = te.get("textRun", {})
            if run.get("content"):
                text += run["content"]
        text = text.strip() or "(empty)"

        if shape.get("text") is not None or ph_type:
            text_elements_found = True
            output += (
                f"  {label}\n"
                f"     object_id: \"{obj_id}\"\n"
                f"     text: \"{text[:200]}\"\n"
                f"     → google__update_slide_text(object_id=\"{obj_id}\", new_text=\"...\")\n\n"
            )

    if not text_elements_found:
        output += "  (No text-editable elements on this slide.)\n"

    return output


async def google__create_presentation(
    factory: GoogleServiceFactory,
    title: str,
) -> str:
    """Create a new Google Slides presentation."""
    svc = await factory.get("slides")
    result = await with_backoff(lambda: svc.presentations().create(body={"title": title}).execute())
    pres_id = result.get("presentationId", "")
    link = f"https://docs.google.com/presentation/d/{pres_id}/edit"
    return f"Presentation created: '{title}'\nID: {pres_id}\nLink: {link}"


async def google__add_slide(
    factory: GoogleServiceFactory,
    presentation_id: str,
    insertion_index: int = -1,
    layout: str = "BLANK",
) -> str:
    """Add a new slide and return its text placeholder object_ids.

    Use the returned placeholder object_ids with google__update_slide_text to
    edit text — do NOT use the slide page ID for text editing.
    """
    svc = await factory.get("slides")
    layout_upper = layout.upper().replace(" ", "_")
    create_req: dict[str, Any] = {
        "slideLayoutReference": {"predefinedLayout": layout_upper},
    }
    if insertion_index >= 0:
        create_req["insertionIndex"] = insertion_index
    requests: list[dict[str, Any]] = [{"createSlide": create_req}]
    try:
        result = await with_backoff(
            lambda: svc.presentations().batchUpdate(
                presentationId=presentation_id, body={"requests": requests}
            ).execute()
        )
    except Exception as e:
        error_msg = str(e)
        if "predefinedLayout" in error_msg or "invalid" in error_msg.lower():
            return (
                f"Invalid layout: '{layout}'. Valid layouts: BLANK, TITLE, TITLE_AND_BODY, "
                f"TITLE_ONLY, CAPTION_ONLY, SECTION_HEADER, MAIN_POINT, BIG_NUMBER."
            )
        return f"Failed to add slide: {error_msg}"

    replies = result.get("replies", [{}])
    new_slide_id = replies[0].get("createSlide", {}).get("objectId", "") if replies else ""

    if not new_slide_id:
        return "Slide created but could not determine its ID."

    # Fetch placeholder IDs from the new slide so the LLM can target them directly
    try:
        page = await with_backoff(
            lambda: svc.presentations().pages().get(
                presentationId=presentation_id, pageObjectId=new_slide_id
            ).execute()
        )
        placeholders = []
        for elem in page.get("pageElements", []):
            shape = elem.get("shape", {})
            ph_type = shape.get("placeholder", {}).get("type", "")
            if ph_type:
                placeholders.append((_placeholder_label(ph_type), elem["objectId"]))

        output = f"Slide added successfully (layout: {layout_upper}, slide ID: {new_slide_id}).\n\n"
        if placeholders:
            output += "Text placeholders — use these object_ids with google__update_slide_text:\n"
            for label, obj_id in placeholders:
                output += f"  {label}: object_id=\"{obj_id}\"\n"
        else:
            output += "(Layout has no text placeholders — BLANK slides have none.)\n"
        return output

    except Exception:
        return (
            f"Slide added (ID: {new_slide_id}). "
            f"Call google__get_slide_content to find placeholder IDs for text editing."
        )


async def google__update_slide_text(
    factory: GoogleServiceFactory,
    presentation_id: str,
    object_id: str,
    new_text: str,
) -> str:
    """Replace all text in a slide placeholder (Title, Subtitle, Body).

    object_id must be a PLACEHOLDER ID, not the slide page ID.
    Get placeholder IDs from google__add_slide or google__get_slide_content.
    If a slide page ID is passed by mistake, this tool auto-resolves to the
    first text placeholder (TITLE > SUBTITLE > BODY).
    """
    svc = await factory.get("slides")
    requests: list[dict[str, Any]] = [
        {"deleteText": {"objectId": object_id, "textRange": {"type": "ALL"}}},
        {"insertText": {"objectId": object_id, "insertionIndex": 0, "text": new_text}},
    ]
    try:
        await with_backoff(
            lambda: svc.presentations().batchUpdate(
                presentationId=presentation_id, body={"requests": requests}
            ).execute()
        )
        return f"Text updated: \"{new_text[:100]}\" on element {object_id}"

    except Exception as e:
        error_msg = str(e)

        # Empty placeholder: deleteText fails because there is no text to delete.
        # The API returns "startIndex 0 must be less than the endIndex 0" for empty placeholders.
        # Retry with insertText only.
        if (
            "no text" in error_msg.lower()
            or "empty" in error_msg.lower()
            or "there is no text content" in error_msg.lower()
            or "startindex" in error_msg.lower()
            or "must be less than the endindex" in error_msg.lower()
        ):
            try:
                await with_backoff(
                    lambda: svc.presentations().batchUpdate(
                        presentationId=presentation_id,
                        body={"requests": [{"insertText": {"objectId": object_id, "insertionIndex": 0, "text": new_text}}]},
                    ).execute()
                )
                return f"Text updated: \"{new_text[:100]}\" on element {object_id}"
            except Exception as insert_err:
                return f"Failed to update text: {insert_err}"

        if "does not allow text editing" not in error_msg:
            return f"Failed to update text: {error_msg}"

        # AUTO-RESOLVE: object_id appears to be a slide page ID — find the real placeholder
        try:
            page = await with_backoff(
                lambda: svc.presentations().pages().get(
                    presentationId=presentation_id, pageObjectId=object_id
                ).execute()
            )
        except Exception:
            return (
                f"Object '{object_id}' is not a text element and could not be resolved as a slide page.\n"
                f"Use google__get_slide_content to find the correct placeholder object_ids."
            )

        priority = ["TITLE", "CENTERED_TITLE", "SUBTITLE", "BODY", "OBJECT"]
        resolved_id = None
        resolved_label = ""

        for target_type in priority:
            for elem in page.get("pageElements", []):
                ph_type = elem.get("shape", {}).get("placeholder", {}).get("type", "")
                if ph_type == target_type:
                    resolved_id = elem["objectId"]
                    resolved_label = _placeholder_label(target_type)
                    break
            if resolved_id:
                break

        if not resolved_id:
            for elem in page.get("pageElements", []):
                if elem.get("shape", {}).get("text") is not None:
                    resolved_id = elem["objectId"]
                    resolved_label = "Shape"
                    break

        if not resolved_id:
            return (
                f"Slide '{object_id}' has no text-editable placeholders.\n"
                f"This slide may use a BLANK layout. Add a slide with text placeholders:\n"
                f"  google__add_slide(presentation_id=\"{presentation_id}\", layout=\"TITLE\")"
            )

        retry_requests: list[dict[str, Any]] = [
            {"deleteText": {"objectId": resolved_id, "textRange": {"type": "ALL"}}},
            {"insertText": {"objectId": resolved_id, "insertionIndex": 0, "text": new_text}},
        ]
        success_msg = (
            f"Text updated: \"{new_text[:100]}\" on {resolved_label} (auto-resolved).\n\n"
            f"TIP: Next time use the placeholder ID directly:\n"
            f"  google__update_slide_text(object_id=\"{resolved_id}\", new_text=\"...\")"
        )
        try:
            await with_backoff(
                lambda: svc.presentations().batchUpdate(
                    presentationId=presentation_id, body={"requests": retry_requests}
                ).execute()
            )
            return success_msg
        except Exception as retry_err:
            retry_err_msg = str(retry_err)
            if (
                "no text" in retry_err_msg.lower()
                or "empty" in retry_err_msg.lower()
                or "there is no text content" in retry_err_msg.lower()
                or "startindex" in retry_err_msg.lower()
                or "must be less than the endindex" in retry_err_msg.lower()
            ):
                try:
                    await with_backoff(
                        lambda: svc.presentations().batchUpdate(
                            presentationId=presentation_id,
                            body={"requests": [{"insertText": {"objectId": resolved_id, "insertionIndex": 0, "text": new_text}}]},
                        ).execute()
                    )
                    return success_msg
                except Exception as insert_err:
                    return f"Auto-resolve to {resolved_label} ({resolved_id}) also failed: {insert_err}"
            return f"Auto-resolve to {resolved_label} ({resolved_id}) also failed: {retry_err}"


async def google__add_image_to_slide(
    factory: GoogleServiceFactory,
    presentation_id: str,
    slide_id: str,
    image_url: str,
    left: float = 100.0,
    top: float = 100.0,
    width: float = 300.0,
    height: float = 200.0,
) -> str:
    """Insert an image from a URL onto a slide."""
    svc = await factory.get("slides")
    emu_per_pt = 12700
    requests: list[dict[str, Any]] = [
        {
            "createImage": {
                "url": image_url,
                "elementProperties": {
                    "pageObjectId": slide_id,
                    "size": {
                        "width": {"magnitude": width * emu_per_pt, "unit": "EMU"},
                        "height": {"magnitude": height * emu_per_pt, "unit": "EMU"},
                    },
                    "transform": {
                        "scaleX": 1,
                        "scaleY": 1,
                        "translateX": left * emu_per_pt,
                        "translateY": top * emu_per_pt,
                        "unit": "EMU",
                    },
                },
            }
        }
    ]
    result = await with_backoff(
        lambda: svc.presentations().batchUpdate(presentationId=presentation_id, body={"requests": requests}).execute()
    )
    replies = result.get("replies", [{}])
    img_id = replies[0].get("createImage", {}).get("objectId", "") if replies else ""
    return f"Image added (id={img_id}) to slide {slide_id}."


# ── Registry ──────────────────────────────────────────────────────────────────


def register_slides_tools(registry: ToolRegistry, factory: GoogleServiceFactory) -> int:
    """Register all 6 Slides tools. Returns count."""

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(**kwargs):  # type: ignore[no-untyped-def]
            return await fn(factory, **kwargs)

        return wrapper

    registry.register(
        name="google__list_slides",
        description="List all slides in a Google Slides presentation.",
        handler=_h(google__list_slides),
        parameters={
            "type": "object",
            "properties": {"presentation_id": {"type": "string"}},
            "required": ["presentation_id"],
        },
    )
    registry.register(
        name="google__get_slide_content",
        description=(
            "Get the text content and placeholder object_ids of a specific slide (0-indexed). "
            "Returns placeholder object_ids needed for google__update_slide_text. "
            "IMPORTANT: The slide page ID is NOT usable for text editing — only placeholder IDs are."
        ),
        handler=_h(google__get_slide_content),
        parameters={
            "type": "object",
            "properties": {
                "presentation_id": {"type": "string"},
                "slide_index": {"type": "integer", "description": "0-based slide index", "default": 0},
            },
            "required": ["presentation_id"],
        },
    )
    registry.register(
        name="google__create_presentation",
        description="Create a new Google Slides presentation.",
        handler=_h(google__create_presentation),
        parameters={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__add_slide",
        description=(
            "Add a new slide to a Google Slides presentation. "
            "Returns the new slide's text placeholder object_ids — use those with "
            "google__update_slide_text to edit text. "
            "Layouts: BLANK, TITLE, TITLE_AND_BODY, TITLE_ONLY, CAPTION_ONLY, "
            "SECTION_HEADER, MAIN_POINT, BIG_NUMBER."
        ),
        handler=_h(google__add_slide),
        parameters={
            "type": "object",
            "properties": {
                "presentation_id": {"type": "string"},
                "insertion_index": {"type": "integer", "description": "Position to insert (-1 = end)", "default": -1},
                "layout": {
                    "type": "string",
                    "enum": [
                        "BLANK",
                        "CAPTION_ONLY",
                        "TITLE",
                        "TITLE_AND_BODY",
                        "TITLE_AND_TWO_COLUMNS",
                        "TITLE_ONLY",
                        "ONE_COLUMN_TEXT",
                        "MAIN_POINT",
                        "BIG_NUMBER",
                    ],
                    "default": "BLANK",
                },
            },
            "required": ["presentation_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__update_slide_text",
        description=(
            "Replace the text in a slide placeholder (Title, Subtitle, Body). "
            "object_id must be a PLACEHOLDER ID from google__add_slide or google__get_slide_content — "
            "NOT the slide page ID. Auto-resolves to the title placeholder if a page ID is passed."
        ),
        handler=_h(google__update_slide_text),
        parameters={
            "type": "object",
            "properties": {
                "presentation_id": {"type": "string"},
                "object_id": {"type": "string", "description": "ID of the shape/text box to update"},
                "new_text": {"type": "string"},
            },
            "required": ["presentation_id", "object_id", "new_text"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__add_image_to_slide",
        description="Insert an image from a URL onto a slide.",
        handler=_h(google__add_image_to_slide),
        parameters={
            "type": "object",
            "properties": {
                "presentation_id": {"type": "string"},
                "slide_id": {"type": "string", "description": "Slide object ID (from google__list_slides)"},
                "image_url": {"type": "string", "description": "Publicly accessible image URL"},
                "left": {"type": "number", "description": "Left offset in points", "default": 100.0},
                "top": {"type": "number", "description": "Top offset in points", "default": 100.0},
                "width": {"type": "number", "description": "Width in points", "default": 300.0},
                "height": {"type": "number", "description": "Height in points", "default": 200.0},
            },
            "required": ["presentation_id", "slide_id", "image_url"],
        },
        require_approval=True,
    )
    return 6
