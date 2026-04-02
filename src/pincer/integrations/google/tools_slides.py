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
    """Get the text content and layout of a specific slide (0-indexed)."""
    svc = await factory.get("slides")
    pres = await with_backoff(lambda: svc.presentations().get(presentationId=presentation_id).execute())
    slides = pres.get("slides", [])
    if slide_index >= len(slides):
        return f"Slide index {slide_index} out of range (presentation has {len(slides)} slides)."
    slide = slides[slide_index]
    texts: list[str] = []
    for element in slide.get("pageElements", []):
        shape = element.get("shape", {})
        text_content = shape.get("text", {})
        for te in text_content.get("textElements", []):
            tr = te.get("textRun")
            if tr:
                texts.append(tr.get("content", "").rstrip())
    content = "\n".join(t for t in texts if t)
    return f"Slide {slide_index + 1} (id={slide.get('objectId', '')}):\n{content or '(no text)'}"


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
    """Add a new slide to a presentation."""
    svc = await factory.get("slides")
    # Use createSlide for new blank slides
    create_req: dict[str, Any] = {
        "slideLayoutReference": {"predefinedLayout": layout},
    }
    if insertion_index >= 0:
        create_req["insertionIndex"] = insertion_index
    requests: list[dict[str, Any]] = [{"createSlide": create_req}]
    result = await with_backoff(
        lambda: svc.presentations().batchUpdate(presentationId=presentation_id, body={"requests": requests}).execute()
    )
    replies = result.get("replies", [{}])
    new_slide_id = replies[0].get("createSlide", {}).get("objectId", "") if replies else ""
    return f"Slide added (id={new_slide_id}) to presentation {presentation_id}."


async def google__update_slide_text(
    factory: GoogleServiceFactory,
    presentation_id: str,
    object_id: str,
    new_text: str,
) -> str:
    """Replace all text in a shape/text box on a slide."""
    svc = await factory.get("slides")
    requests: list[dict[str, Any]] = [
        {"deleteText": {"objectId": object_id, "textRange": {"type": "ALL"}}},
        {"insertText": {"objectId": object_id, "insertionIndex": 0, "text": new_text}},
    ]
    await with_backoff(
        lambda: svc.presentations().batchUpdate(presentationId=presentation_id, body={"requests": requests}).execute()
    )
    return f"Text updated in object {object_id}."


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
        description="Get the text content and layout of a specific slide (0-indexed).",
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
        description="Add a new slide to a Google Slides presentation.",
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
        description="Replace the text in a shape or text box on a slide.",
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
