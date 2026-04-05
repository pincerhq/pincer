"""
Tests for Google Slides tools — covers the add-slide + edit-title workflow
that was broken by the LLM passing a slide page ID to google__update_slide_text.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from pincer.integrations.google.tools_slides import (
    _placeholder_label,
    google__add_slide,
    google__get_slide_content,
    google__list_slides,
    google__update_slide_text,
)

# ── _placeholder_label ────────────────────────────────────────────────────────


def test_placeholder_label_known_types():
    assert _placeholder_label("TITLE") == "Title"
    assert _placeholder_label("SUBTITLE") == "Subtitle"
    assert _placeholder_label("BODY") == "Body"
    assert _placeholder_label("CENTERED_TITLE") == "Centered Title"


def test_placeholder_label_unknown_type():
    result = _placeholder_label("CUSTOM_TYPE")
    assert "CUSTOM_TYPE" in result


# ── google__add_slide ─────────────────────────────────────────────────────────


async def test_add_slide_returns_placeholder_ids(mock_slides_service, mock_factory):
    """add_slide must return Title/Subtitle object_ids so LLM can target them."""
    mock_slides_service.presentations().batchUpdate().execute.return_value = {
        "replies": [{"createSlide": {"objectId": "slide_0"}}],
    }
    mock_slides_service.presentations().pages().get().execute.return_value = {
        "pageElements": [
            {"objectId": "title_1", "shape": {"placeholder": {"type": "TITLE"}, "text": {}}},
            {"objectId": "sub_2", "shape": {"placeholder": {"type": "SUBTITLE"}, "text": {}}},
        ],
    }

    result = await google__add_slide(mock_factory, presentation_id="p1", layout="TITLE")

    assert "title_1" in result
    assert "sub_2" in result
    assert "google__update_slide_text" in result or "object_id" in result
    assert "Title" in result
    assert "Subtitle" in result


async def test_add_slide_blank_no_placeholders(mock_slides_service, mock_factory):
    """BLANK layout returns a note that there are no text placeholders."""
    mock_slides_service.presentations().batchUpdate().execute.return_value = {
        "replies": [{"createSlide": {"objectId": "slide_blank"}}],
    }
    mock_slides_service.presentations().pages().get().execute.return_value = {
        "pageElements": [],
    }

    result = await google__add_slide(mock_factory, presentation_id="p1", layout="BLANK")

    assert "no text placeholders" in result.lower() or "none" in result.lower()


async def test_add_slide_normalises_layout_case(mock_slides_service, mock_factory):
    """Layout name is uppercased before sending to API."""
    mock_slides_service.presentations().batchUpdate().execute.return_value = {
        "replies": [{"createSlide": {"objectId": "s1"}}],
    }
    mock_slides_service.presentations().pages().get().execute.return_value = {"pageElements": []}

    result = await google__add_slide(mock_factory, presentation_id="p1", layout="title_and_body")

    assert "TITLE_AND_BODY" in result


async def test_add_slide_no_id_in_reply(mock_slides_service, mock_factory):
    """Graceful message when reply contains no objectId."""
    mock_slides_service.presentations().batchUpdate().execute.return_value = {"replies": [{}]}

    result = await google__add_slide(mock_factory, presentation_id="p1")

    assert "could not determine" in result.lower() or "id" in result.lower()


async def test_add_slide_api_error_falls_back(mock_slides_service, mock_factory):
    """API error while fetching page falls back to generic 'call get_slide_content' message."""
    mock_slides_service.presentations().batchUpdate().execute.return_value = {
        "replies": [{"createSlide": {"objectId": "s1"}}],
    }
    mock_slides_service.presentations().pages().get().execute.side_effect = Exception("network error")

    result = await google__add_slide(mock_factory, presentation_id="p1", layout="TITLE")

    assert "s1" in result
    assert "get_slide_content" in result or "placeholder" in result.lower()


# ── google__get_slide_content ─────────────────────────────────────────────────


async def test_get_slide_content_shows_placeholder_ids(mock_slides_service, mock_factory):
    """get_slide_content must show placeholder IDs with edit hints."""
    mock_slides_service.presentations().get().execute.return_value = {
        "slides": [
            {
                "objectId": "page_0",
                "pageElements": [
                    {
                        "objectId": "title_1",
                        "shape": {
                            "placeholder": {"type": "TITLE"},
                            "text": {"textElements": [{"textRun": {"content": "Old Title"}}]},
                        },
                    }
                ],
            }
        ],
    }

    result = await google__get_slide_content(mock_factory, presentation_id="p1", slide_index=0)

    assert "title_1" in result
    assert "google__update_slide_text" in result
    assert "Old Title" in result


async def test_get_slide_content_warns_page_id_not_for_text_editing(mock_slides_service, mock_factory):
    """The slide page ID must be labelled as NOT usable for text editing."""
    mock_slides_service.presentations().get().execute.return_value = {
        "slides": [
            {
                "objectId": "page_0",
                "pageElements": [
                    {
                        "objectId": "title_1",
                        "shape": {
                            "placeholder": {"type": "TITLE"},
                            "text": {},
                        },
                    }
                ],
            }
        ],
    }

    result = await google__get_slide_content(mock_factory, presentation_id="p1", slide_index=0)

    assert "do NOT use this for text editing" in result


async def test_get_slide_content_out_of_range(mock_slides_service, mock_factory):
    mock_slides_service.presentations().get().execute.return_value = {
        "slides": [{"objectId": "s0", "pageElements": []}],
    }

    result = await google__get_slide_content(mock_factory, presentation_id="p1", slide_index=5)

    assert "Invalid" in result or "out of range" in result.lower()


async def test_get_slide_content_no_text_elements(mock_slides_service, mock_factory):
    mock_slides_service.presentations().get().execute.return_value = {
        "slides": [{"objectId": "s0", "pageElements": []}],
    }

    result = await google__get_slide_content(mock_factory, presentation_id="p1", slide_index=0)

    assert "No text-editable" in result or "no text" in result.lower()


# ── google__update_slide_text ─────────────────────────────────────────────────


async def test_update_text_with_correct_id_succeeds(mock_slides_service, mock_factory):
    """Correct placeholder ID → text updated, clean response."""
    mock_slides_service.presentations().batchUpdate().execute.return_value = {}

    result = await google__update_slide_text(
        mock_factory, presentation_id="p1", object_id="title_1", new_text="Test Results"
    )

    assert "updated" in result.lower()
    assert "Test Results" in result


async def test_update_text_with_page_id_auto_resolves(mock_slides_service, mock_factory):
    """Slide page ID passed → auto-resolves to title placeholder, succeeds."""
    from googleapiclient.errors import HttpError

    call_count = 0

    def side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise HttpError(
                resp=MagicMock(status=400),
                content=b"The object does not allow text editing.",
            )
        return {}

    mock_slides_service.presentations().batchUpdate().execute.side_effect = side_effect
    mock_slides_service.presentations().pages().get().execute.return_value = {
        "pageElements": [
            {"objectId": "title_1", "shape": {"placeholder": {"type": "TITLE"}, "text": {}}},
        ],
    }

    result = await google__update_slide_text(
        mock_factory, presentation_id="p1", object_id="page_0", new_text="Auto Title"
    )

    assert "auto-resolved" in result.lower()
    assert "title_1" in result
    assert "Auto Title" in result


async def test_update_text_page_id_prefers_title_over_body(mock_slides_service, mock_factory):
    """Auto-resolve picks TITLE before BODY."""
    from googleapiclient.errors import HttpError

    call_count = 0

    def side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise HttpError(
                resp=MagicMock(status=400),
                content=b"does not allow text editing",
            )
        return {}

    mock_slides_service.presentations().batchUpdate().execute.side_effect = side_effect
    mock_slides_service.presentations().pages().get().execute.return_value = {
        "pageElements": [
            {"objectId": "body_2", "shape": {"placeholder": {"type": "BODY"}, "text": {}}},
            {"objectId": "title_1", "shape": {"placeholder": {"type": "TITLE"}, "text": {}}},
        ],
    }

    result = await google__update_slide_text(
        mock_factory, presentation_id="p1", object_id="page_0", new_text="My Title"
    )

    assert "title_1" in result
    assert "body_2" not in result.split("auto-resolved")[0] if "auto-resolved" in result else True


async def test_update_text_blank_slide_gives_guidance(mock_slides_service, mock_factory):
    """Page ID on BLANK slide (no placeholders) → helpful error with add_slide hint."""
    from googleapiclient.errors import HttpError

    mock_slides_service.presentations().batchUpdate().execute.side_effect = HttpError(
        resp=MagicMock(status=400),
        content=b"does not allow text editing",
    )
    mock_slides_service.presentations().pages().get().execute.return_value = {
        "pageElements": [],
    }

    result = await google__update_slide_text(
        mock_factory, presentation_id="p1", object_id="blank_slide", new_text="text"
    )

    assert "no text-editable" in result.lower()
    assert "google__add_slide" in result


async def test_update_text_non_slide_object_id_error(mock_slides_service, mock_factory):
    """Non-slide page ID that also fails pages.get → clear guidance message."""
    from googleapiclient.errors import HttpError

    mock_slides_service.presentations().batchUpdate().execute.side_effect = HttpError(
        resp=MagicMock(status=400),
        content=b"does not allow text editing",
    )
    mock_slides_service.presentations().pages().get().execute.side_effect = Exception("not found")

    result = await google__update_slide_text(
        mock_factory, presentation_id="p1", object_id="bad_id", new_text="text"
    )

    assert "get_slide_content" in result or "not a text element" in result.lower()


async def test_update_text_empty_placeholder_retries_without_delete(mock_slides_service, mock_factory):
    """Empty placeholder: deleteText fails → retries with insertText only → succeeds."""
    from googleapiclient.errors import HttpError

    call_count = 0

    def side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise HttpError(
                resp=MagicMock(status=400),
                content=b"There is no text content in the given range.",
            )
        return {}

    mock_slides_service.presentations().batchUpdate().execute.side_effect = side_effect

    result = await google__update_slide_text(
        mock_factory, presentation_id="p1", object_id="title_empty", new_text="Hello"
    )

    assert "updated" in result.lower()
    assert "Hello" in result
    assert call_count == 2


async def test_update_text_start_index_error_retries_without_delete(mock_slides_service, mock_factory):
    """Empty placeholder: 'startIndex 0 must be less than the endIndex 0' → retries insert-only."""
    from googleapiclient.errors import HttpError

    call_count = 0

    def side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise HttpError(
                resp=MagicMock(status=400),
                content=b"startIndex 0 must be less than the endIndex 0",
            )
        return {}

    mock_slides_service.presentations().batchUpdate().execute.side_effect = side_effect

    result = await google__update_slide_text(
        mock_factory, presentation_id="p1", object_id="title_empty", new_text="My Title"
    )

    assert "updated" in result.lower()
    assert "My Title" in result
    assert call_count == 2


async def test_update_text_other_api_error_propagated(mock_slides_service, mock_factory):
    """Non-text-editing errors are returned as-is without auto-resolve."""
    from googleapiclient.errors import HttpError

    mock_slides_service.presentations().batchUpdate().execute.side_effect = HttpError(
        resp=MagicMock(status=403),
        content=b"The caller does not have permission",
    )

    result = await google__update_slide_text(
        mock_factory, presentation_id="p1", object_id="title_1", new_text="text"
    )

    assert "Failed" in result or "permission" in result.lower()
    assert "auto-resolved" not in result


# ── Full workflow integration ─────────────────────────────────────────────────


async def test_full_workflow_add_then_edit(mock_slides_service, mock_factory):
    """Integration: add_slide returns placeholder ID → update_slide_text uses it → success."""
    # Step 1: add_slide returns placeholder IDs
    mock_slides_service.presentations().batchUpdate().execute.return_value = {
        "replies": [{"createSlide": {"objectId": "new_slide"}}],
    }
    mock_slides_service.presentations().pages().get().execute.return_value = {
        "pageElements": [
            {"objectId": "new_title", "shape": {"placeholder": {"type": "TITLE"}, "text": {}}},
        ],
    }

    add_result = await google__add_slide(mock_factory, presentation_id="p1", layout="TITLE")
    assert "new_title" in add_result

    # Step 2: use the placeholder ID from step 1 directly
    mock_slides_service.presentations().batchUpdate().execute.side_effect = None
    mock_slides_service.presentations().batchUpdate().execute.return_value = {}

    edit_result = await google__update_slide_text(
        mock_factory, presentation_id="p1", object_id="new_title", new_text="Test Results"
    )

    assert "updated" in edit_result.lower()
    assert "Test Results" in edit_result
    # No auto-resolve needed — it should be a clean success
    assert "auto-resolved" not in edit_result


# ── google__list_slides ───────────────────────────────────────────────────────


async def test_list_slides_basic(mock_slides_service, mock_factory):
    mock_slides_service.presentations().get().execute.return_value = {
        "title": "My Deck",
        "slides": [
            {"objectId": "s1", "slideProperties": {"layoutObjectId": "L1"}},
            {"objectId": "s2", "slideProperties": {"layoutObjectId": "L2"}},
        ],
    }

    result = await google__list_slides(mock_factory, presentation_id="p1")

    assert "My Deck" in result
    assert "s1" in result
    assert "s2" in result


async def test_list_slides_empty(mock_slides_service, mock_factory):
    mock_slides_service.presentations().get().execute.return_value = {
        "title": "Empty",
        "slides": [],
    }

    result = await google__list_slides(mock_factory, presentation_id="p1")

    assert "no slides" in result.lower()
