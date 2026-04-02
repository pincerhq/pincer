"""
Tests for Slides (6 tools) + Tasks (8 tools) + Contacts (7 tools) = 21 tests.
"""

from __future__ import annotations

from pincer.integrations.google.tools_contacts import (
    google__create_contact,
    google__delete_contact,
    google__get_contact,
    google__list_contact_groups,
    google__list_contacts,
    google__search_contacts,
    google__update_contact,
)
from pincer.integrations.google.tools_slides import (
    google__add_image_to_slide,
    google__add_slide,
    google__create_presentation,
    google__get_slide_content,
    google__list_slides,
    google__update_slide_text,
)
from pincer.integrations.google.tools_tasks import (
    google__complete_task,
    google__create_task,
    google__create_task_list,
    google__delete_task,
    google__get_task,
    google__list_task_lists,
    google__list_tasks,
    google__update_task,
)

# ═══════════════════════════════════════════════
# Slides tests
# ═══════════════════════════════════════════════


def _presentation(pres_id="pres1", title="Q1 Roadmap"):
    return {
        "presentationId": pres_id,
        "title": title,
        "slides": [
            {
                "objectId": "slide1",
                "slideProperties": {"layoutObjectId": "BLANK"},
                "pageElements": [
                    {
                        "shape": {
                            "text": {
                                "textElements": [
                                    {"textRun": {"content": "Title Text"}},
                                    {"textRun": {"content": "Subtitle Text"}},
                                ]
                            }
                        }
                    }
                ],
            },
            {"objectId": "slide2", "slideProperties": {}, "pageElements": []},
        ],
    }


async def test_list_slides(mock_factory, mock_slides_service):
    mock_slides_service.presentations().get().execute.return_value = _presentation()
    result = await google__list_slides(mock_factory, presentation_id="pres1")
    assert "Q1 Roadmap" in result
    assert "slide1" in result
    assert "slide2" in result


async def test_list_slides_empty(mock_factory, mock_slides_service):
    mock_slides_service.presentations().get().execute.return_value = {
        "presentationId": "p1",
        "title": "Empty",
        "slides": [],
    }
    result = await google__list_slides(mock_factory, presentation_id="p1")
    assert "no slides" in result.lower()


async def test_get_slide_content(mock_factory, mock_slides_service):
    mock_slides_service.presentations().get().execute.return_value = _presentation()
    result = await google__get_slide_content(mock_factory, presentation_id="pres1", slide_index=0)
    assert "Title Text" in result
    assert "slide1" in result


async def test_get_slide_content_out_of_range(mock_factory, mock_slides_service):
    mock_slides_service.presentations().get().execute.return_value = _presentation()
    result = await google__get_slide_content(mock_factory, presentation_id="pres1", slide_index=99)
    assert "out of range" in result


async def test_create_presentation(mock_factory, mock_slides_service):
    mock_slides_service.presentations().create().execute.return_value = {
        "presentationId": "new_pres",
        "title": "New Deck",
    }
    result = await google__create_presentation(mock_factory, title="New Deck")
    assert "New Deck" in result
    assert "new_pres" in result


async def test_add_slide(mock_factory, mock_slides_service):
    mock_slides_service.presentations().batchUpdate().execute.return_value = {
        "replies": [{"createSlide": {"objectId": "new_slide_1"}}]
    }
    result = await google__add_slide(mock_factory, presentation_id="pres1")
    assert "new_slide_1" in result


async def test_update_slide_text(mock_factory, mock_slides_service):
    mock_slides_service.presentations().batchUpdate().execute.return_value = {"replies": []}
    result = await google__update_slide_text(
        mock_factory,
        presentation_id="pres1",
        object_id="slide1",
        new_text="Updated slide content",
    )
    assert "slide1" in result
    assert "updated" in result.lower()


async def test_add_image_to_slide(mock_factory, mock_slides_service):
    mock_slides_service.presentations().batchUpdate().execute.return_value = {
        "replies": [{"createImage": {"objectId": "img1"}}]
    }
    result = await google__add_image_to_slide(
        mock_factory,
        presentation_id="pres1",
        slide_id="slide1",
        image_url="https://example.com/image.png",
    )
    assert "img1" in result
    assert "slide1" in result


# ═══════════════════════════════════════════════
# Tasks tests
# ═══════════════════════════════════════════════


def _task(task_id="task1", title="Review PR", status="needsAction"):
    return {
        "id": task_id,
        "title": title,
        "status": status,
        "notes": "",
        "due": "",
    }


async def test_list_task_lists(mock_factory, mock_tasks_service):
    mock_tasks_service.tasklists().list().execute.return_value = {"items": [{"id": "@default", "title": "My Tasks"}]}
    result = await google__list_task_lists(mock_factory)
    assert "My Tasks" in result


async def test_list_task_lists_empty(mock_factory, mock_tasks_service):
    mock_tasks_service.tasklists().list().execute.return_value = {"items": []}
    result = await google__list_task_lists(mock_factory)
    assert "No task lists" in result


async def test_list_tasks(mock_factory, mock_tasks_service):
    mock_tasks_service.tasks().list().execute.return_value = {
        "items": [_task("t1", "Write tests"), _task("t2", "Ship feature")]
    }
    result = await google__list_tasks(mock_factory)
    assert "Write tests" in result
    assert "Ship feature" in result


async def test_list_tasks_empty(mock_factory, mock_tasks_service):
    mock_tasks_service.tasks().list().execute.return_value = {"items": []}
    result = await google__list_tasks(mock_factory)
    assert "No tasks" in result


async def test_get_task(mock_factory, mock_tasks_service):
    mock_tasks_service.tasks().get().execute.return_value = _task("t1", "Review PR")
    result = await google__get_task(mock_factory, task_id="t1")
    assert "Review PR" in result
    assert "t1" in result


async def test_create_task(mock_factory, mock_tasks_service):
    mock_tasks_service.tasks().insert().execute.return_value = _task("new_t", "New task")
    result = await google__create_task(mock_factory, title="New task")
    assert "New task" in result
    assert "new_t" in result


async def test_update_task(mock_factory, mock_tasks_service):
    mock_tasks_service.tasks().get().execute.return_value = _task("t1", "Old title")
    mock_tasks_service.tasks().update().execute.return_value = _task("t1", "New title")
    result = await google__update_task(mock_factory, task_id="t1", title="New title")
    assert "New title" in result or "updated" in result.lower()


async def test_complete_task(mock_factory, mock_tasks_service):
    mock_tasks_service.tasks().get().execute.return_value = _task("t1", "Ship feature")
    mock_tasks_service.tasks().update().execute.return_value = _task("t1", "Ship feature", status="completed")
    result = await google__complete_task(mock_factory, task_id="t1")
    assert "Ship feature" in result
    assert "completed" in result.lower() or "Task completed" in result


async def test_delete_task(mock_factory, mock_tasks_service):
    mock_tasks_service.tasks().delete().execute.return_value = None
    result = await google__delete_task(mock_factory, task_id="t1")
    assert "t1" in result
    assert "deleted" in result.lower()


async def test_create_task_list(mock_factory, mock_tasks_service):
    mock_tasks_service.tasklists().insert().execute.return_value = {"id": "list1", "title": "Sprint 9"}
    result = await google__create_task_list(mock_factory, title="Sprint 9")
    assert "Sprint 9" in result
    assert "list1" in result


# ═══════════════════════════════════════════════
# Contacts tests
# ═══════════════════════════════════════════════


def _person(resource="people/c123", given="Alice", family="Smith", email="alice@example.com"):
    return {
        "resourceName": resource,
        "etag": "abc123",
        "names": [{"displayName": f"{given} {family}", "givenName": given, "familyName": family}],
        "emailAddresses": [{"value": email}],
        "phoneNumbers": [{"value": "+1 555-1234"}],
        "organizations": [],
    }


async def test_list_contacts(mock_factory, mock_contacts_service):
    mock_contacts_service.people().connections().list().execute.return_value = {
        "connections": [_person(), _person("people/c456", "Bob", "Jones", "bob@example.com")]
    }
    result = await google__list_contacts(mock_factory)
    assert "Alice Smith" in result
    assert "Bob Jones" in result


async def test_list_contacts_empty(mock_factory, mock_contacts_service):
    mock_contacts_service.people().connections().list().execute.return_value = {"connections": []}
    result = await google__list_contacts(mock_factory)
    assert "No contacts" in result


async def test_search_contacts(mock_factory, mock_contacts_service):
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": [{"person": _person()}]}
    result = await google__search_contacts(mock_factory, query="Alice")
    assert "Alice Smith" in result
    assert "alice@example.com" in result


async def test_search_contacts_none(mock_factory, mock_contacts_service):
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": []}
    result = await google__search_contacts(mock_factory, query="Zephyr Nomatch")
    assert "No contacts" in result


async def test_get_contact(mock_factory, mock_contacts_service):
    mock_contacts_service.people().get().execute.return_value = _person()
    result = await google__get_contact(mock_factory, resource_name="people/c123")
    assert "Alice Smith" in result
    assert "alice@example.com" in result
    assert "people/c123" in result


async def test_create_contact(mock_factory, mock_contacts_service):
    mock_contacts_service.people().createContact().execute.return_value = _person(
        "people/c789", "Carol", "White", "carol@example.com"
    )
    result = await google__create_contact(
        mock_factory, given_name="Carol", family_name="White", email="carol@example.com"
    )
    assert "Carol White" in result or "Carol" in result
    assert "people/c789" in result


async def test_update_contact(mock_factory, mock_contacts_service):
    mock_contacts_service.people().get().execute.return_value = _person()
    mock_contacts_service.people().updateContact().execute.return_value = _person(given="Alice", family="Johnson")
    result = await google__update_contact(mock_factory, resource_name="people/c123", family_name="Johnson")
    assert "Alice Johnson" in result or "updated" in result.lower()


async def test_delete_contact(mock_factory, mock_contacts_service):
    mock_contacts_service.people().deleteContact().execute.return_value = None
    result = await google__delete_contact(mock_factory, resource_name="people/c123")
    assert "people/c123" in result
    assert "deleted" in result.lower()


async def test_list_contact_groups(mock_factory, mock_contacts_service):
    mock_contacts_service.contactGroups().list().execute.return_value = {
        "contactGroups": [
            {
                "name": "myContacts",
                "formattedName": "My Contacts",
                "memberCount": 42,
                "resourceName": "contactGroups/myContacts",
            },
            {"name": "starred", "formattedName": "Starred", "memberCount": 5, "resourceName": "contactGroups/starred"},
        ]
    }
    result = await google__list_contact_groups(mock_factory)
    assert "My Contacts" in result
    assert "42" in result
