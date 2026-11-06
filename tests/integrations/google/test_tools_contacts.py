"""
Tests for Contacts tools — covers all 5 bug-fixed tools.
"""

from __future__ import annotations

import pytest

from pincer.integrations.google import tools_contacts
from pincer.integrations.google.tools_contacts import (
    google__get_contact,
    google__list_contacts,
    google__search_contacts,
    google__update_contact,
)


def _person(
    resource_name="people/c1",
    display_name="John Doe",
    email="john@example.com",
    phone="+1555000",
    org="Acme",
    given="John",
    family="Doe",
    etag="etag_abc",
):
    return {
        "resourceName": resource_name,
        "etag": etag,  # top-level etag — this is what updateContact validates
        "names": [{"displayName": display_name, "givenName": given, "familyName": family}],
        "emailAddresses": [{"value": email, "type": "work"}],
        "phoneNumbers": [{"value": phone, "type": "mobile"}],
        "organizations": [{"name": org}],
        "metadata": {"sources": [{"type": "CONTACT", "etag": "SOURCE_" + etag}]},
    }


# ── list_contacts ─────────────────────────────────────────────────────────────


async def test_list_contacts_returns_names(mock_factory, mock_contacts_service):
    mock_contacts_service.people().connections().list().execute.return_value = {
        "connections": [_person()],
        "totalPeople": 1,
    }
    result = await google__list_contacts(mock_factory)
    assert "John Doe" in result
    assert "john@example.com" in result


async def test_list_contacts_person_fields_passed(mock_factory, mock_contacts_service):
    mock_contacts_service.people().connections().list().execute.return_value = {
        "connections": [_person()],
        "totalPeople": 1,
    }
    await google__list_contacts(mock_factory)
    call_kwargs = mock_contacts_service.people().connections().list.call_args[1]
    assert "personFields" in call_kwargs
    # resourceName must NOT be in personFields (causes 400)
    assert "resourceName" not in call_kwargs["personFields"]


async def test_list_contacts_empty(mock_factory, mock_contacts_service):
    mock_contacts_service.people().connections().list().execute.return_value = {"connections": []}
    result = await google__list_contacts(mock_factory)
    assert "No contacts" in result


async def test_list_contacts_pagination_token(mock_factory, mock_contacts_service):
    mock_contacts_service.people().connections().list().execute.return_value = {
        "connections": [_person()],
        "totalPeople": 2,
        "nextPageToken": "tok123",
    }
    result = await google__list_contacts(mock_factory)
    assert "tok123" in result or "more" in result.lower()


# ── search_contacts ───────────────────────────────────────────────────────────


async def test_search_contacts_uses_read_mask(mock_factory, mock_contacts_service):
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": [{"person": _person()}]}
    # Reset to clear the setup call so only production calls appear in call_args_list
    mock_contacts_service.people().searchContacts.reset_mock()
    await google__search_contacts(mock_factory, query="John")
    calls = mock_contacts_service.people().searchContacts.call_args_list
    assert calls, "searchContacts should have been called"
    for call in calls:
        kw = call[1]
        assert "readMask" in kw, f"searchContacts must use readMask, got kwargs={kw}"
        assert "personFields" not in kw, "searchContacts must NOT use personFields"


async def test_search_contacts_warmup_before_real_search(mock_factory, mock_contacts_service):
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": []}
    # Reset to clear the setup call so only production calls appear in call_args_list
    mock_contacts_service.people().searchContacts.reset_mock()
    await google__search_contacts(mock_factory, query="test")
    calls = mock_contacts_service.people().searchContacts.call_args_list
    queries = [c[1].get("query", "MISSING") for c in calls]
    # warmup, search, then (empty result) warmup + retry
    assert queries == ["", "test", "", "test"]


async def test_search_contacts_returns_results(mock_factory, mock_contacts_service):
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": [{"person": _person()}]}
    result = await google__search_contacts(mock_factory, query="John")
    assert "John Doe" in result


async def test_search_contacts_no_results(mock_factory, mock_contacts_service):
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": []}
    mock_contacts_service.people().connections().list().execute.return_value = {"connections": [_person()]}
    result = await google__search_contacts(mock_factory, query="nobody")
    assert 'No match in Google Contacts for "nobody"' in result
    assert "not a saved contact" in result


async def test_search_contacts_warmup_failure_is_nonfatal(mock_factory, mock_contacts_service):
    """If warmup throws, the real search should still be attempted."""
    call_count = 0

    def execute_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("warmup failed")
        return {"results": [{"person": _person()}]}

    mock_contacts_service.people().searchContacts().execute.side_effect = execute_side_effect
    result = await google__search_contacts(mock_factory, query="John")
    assert "John Doe" in result


async def test_search_contacts_retries_after_empty_result(mock_factory, mock_contacts_service):
    responses = iter([{}, {"results": []}, {}, {"results": [{"person": _person()}]}])
    mock_contacts_service.people().searchContacts().execute.side_effect = lambda: next(responses)
    result = await google__search_contacts(mock_factory, query="John")
    assert "Found 1 contact(s) in Google Contacts" in result
    assert "John Doe" in result


async def test_search_contacts_no_retry_when_first_search_hits(mock_factory, mock_contacts_service):
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": [{"person": _person()}]}
    mock_contacts_service.people().searchContacts.reset_mock()
    await google__search_contacts(mock_factory, query="John")
    assert len(mock_contacts_service.people().searchContacts.call_args_list) == 2


async def test_search_contacts_clamps_page_size(mock_factory, mock_contacts_service):
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": [{"person": _person()}]}
    mock_contacts_service.people().searchContacts.reset_mock()
    await google__search_contacts(mock_factory, query="John", max_results=100)
    sizes = [c[1]["pageSize"] for c in mock_contacts_service.people().searchContacts.call_args_list if c[1]["query"]]
    assert sizes == [30]


async def test_search_contacts_result_labels_source(mock_factory, mock_contacts_service):
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": [{"person": _person()}]}
    result = await google__search_contacts(mock_factory, query="John")
    assert "in Google Contacts" in result


def _jane():
    return _person(
        resource_name="people/c42",
        display_name="Jané Doe",
        given="Jané",
        family="Doe",
        email="jane.doe@example.com",
        phone="+49 170 1234567",
        org="Initech",
    )


@pytest.mark.parametrize(
    "query",
    [
        "Jane",
        "Doe",
        "jane doe",
        "Jané",
        "JANE",
        "doe jane",
        "J Doe",
        "jane.d",
        "jane.doe@exa",
        "jane.doe@example.com",
        "Initech",
        "1234567",
        "0170 1234567",
        "+49 170 1234567",
    ],
)
async def test_search_contacts_falls_back_to_connections(mock_factory, mock_contacts_service, query):
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": []}
    mock_contacts_service.people().connections().list().execute.return_value = {
        "connections": [_person(display_name="John Roe", family="Roe"), _jane()],
    }
    result = await google__search_contacts(mock_factory, query=query)
    assert "Found 1 contact(s) in Google Contacts" in result
    assert "people/c42" in result
    assert "people/c1\n" not in result


@pytest.mark.parametrize("query", ["com", "example", "oe", "ane", "1234", "jane.doe@other.org", "doe@example.com"])
async def test_search_contacts_fallback_rejects_loose_matches(mock_factory, mock_contacts_service, query):
    """Email domains, mid-word substrings and short phone fragments match nobody (review on #351)."""
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": []}
    mock_contacts_service.people().connections().list().execute.return_value = {"connections": [_jane()]}
    result = await google__search_contacts(mock_factory, query=query)
    assert "people/c42" not in result


async def test_search_contacts_fallback_phone_needs_suffix(mock_factory, mock_contacts_service):
    """Digits in the middle of a number are not a match; the end of the number is."""
    person = _jane()
    person["phoneNumbers"] = [{"value": "+1 (555) 123-4567"}]
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": []}
    mock_contacts_service.people().connections().list().execute.return_value = {"connections": [person]}
    assert "people/c42" not in await google__search_contacts(mock_factory, query="555123")
    assert "people/c42" in await google__search_contacts(mock_factory, query="555 123 4567")


@pytest.mark.parametrize(
    ("stored", "query", "matches"),
    [
        ("123456", "+44 20 7946 0123456", False),  # short stored number is not the tail of a longer query
        ("123456", "123456", True),
        ("0170 1234567", "+49 170 1234567", True),  # national number + country code in the query
        ("170 1234567", "0049 170 1234567", True),
    ],
)
async def test_search_contacts_fallback_reverse_phone_suffix_needs_national_number(
    mock_factory, mock_contacts_service, stored, query, matches
):
    person = _jane()
    person["phoneNumbers"] = [{"value": stored}]
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": []}
    mock_contacts_service.people().connections().list().execute.return_value = {"connections": [person]}
    result = await google__search_contacts(mock_factory, query=query)
    assert ("people/c42" in result) is matches


@pytest.mark.parametrize("query", ["", "   ", "?!."])
async def test_search_contacts_rejects_empty_query(mock_factory, mock_contacts_service, query):
    mock_contacts_service.people().searchContacts.reset_mock()
    mock_contacts_service.people().connections().list.reset_mock()
    result = await google__search_contacts(mock_factory, query=query)
    assert result.startswith("Error:")
    assert "empty" in result
    assert not mock_contacts_service.people().searchContacts.call_args_list
    assert not mock_contacts_service.people().connections().list.call_args_list


@pytest.mark.parametrize("query", ["王", "a", "123", "@example.com"])
async def test_search_contacts_short_query_still_asks_google(mock_factory, mock_contacts_service, query):
    """Short or domain-only queries go to searchContacts; Google's hits are returned (review on #351)."""
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": [{"person": _jane()}]}
    result = await google__search_contacts(mock_factory, query=query)
    assert "Found 1 contact(s) in Google Contacts" in result


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("王", "at least 2 characters"),
        ("a", "at least 2 characters"),
        ("123", "at least 6 digits"),
        ("@example.com", "email domains alone"),
    ],
)
async def test_search_contacts_short_query_skips_fallback_without_verdict(
    mock_factory, mock_contacts_service, query, reason
):
    """When Google finds nothing, a query too vague for the local scan is not scanned and gets no "not saved"."""
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": []}
    mock_contacts_service.people().searchContacts.reset_mock()
    mock_contacts_service.people().connections().list.reset_mock()
    result = await google__search_contacts(mock_factory, query=query)
    assert [c[1]["query"] for c in mock_contacts_service.people().searchContacts.call_args_list] == [
        "",
        query,
        "",
        query,
    ]
    assert not mock_contacts_service.people().connections().list.call_args_list
    assert not result.startswith("Error:")
    assert reason in result
    assert "Cannot confirm" in result
    assert "not a saved contact" not in result


async def test_search_contacts_fallback_page_cap_is_not_a_negative(mock_factory, mock_contacts_service):
    """Hitting the page cap with pages left must not claim the person isn't saved (review on #351)."""
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": []}
    mock_contacts_service.people().connections().list().execute.return_value = {
        "connections": [_person(display_name="John Roe", family="Roe")],
        "nextPageToken": "more",
    }
    mock_contacts_service.people().connections().list.reset_mock()
    result = await google__search_contacts(mock_factory, query="Jane")
    assert len(mock_contacts_service.people().connections().list.call_args_list) == tools_contacts._FALLBACK_MAX_PAGES
    assert "Cannot confirm" in result
    assert "not a saved contact" not in result


async def test_search_contacts_fallback_uses_connections_fields(mock_factory, mock_contacts_service):
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": []}
    mock_contacts_service.people().connections().list().execute.return_value = {"connections": []}
    mock_contacts_service.people().connections().list.reset_mock()
    await google__search_contacts(mock_factory, query="Jane")
    kw = mock_contacts_service.people().connections().list.call_args[1]
    assert kw["resourceName"] == "people/me"
    assert kw["pageSize"] == 1000
    assert "nicknames" in kw["personFields"]
    assert "resourceName" not in kw["personFields"]


async def test_search_contacts_fallback_pages_through_connections(mock_factory, mock_contacts_service):
    pages = iter(
        [
            {"connections": [_person()], "nextPageToken": "p2"},
            {"connections": [_jane()]},
        ]
    )
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": []}
    mock_contacts_service.people().connections().list().execute.side_effect = lambda: next(pages)
    mock_contacts_service.people().connections().list.reset_mock()
    result = await google__search_contacts(mock_factory, query="Jane")
    assert "people/c42" in result
    tokens = [c[1].get("pageToken") for c in mock_contacts_service.people().connections().list.call_args_list]
    assert tokens == [None, "p2"]


async def test_search_contacts_fallback_matches_nickname(mock_factory, mock_contacts_service):
    person = _jane()
    person["nicknames"] = [{"value": "JJ"}]
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": []}
    mock_contacts_service.people().connections().list().execute.return_value = {"connections": [person]}
    result = await google__search_contacts(mock_factory, query="jj")
    assert "people/c42" in result


async def test_search_contacts_fallback_respects_max_results(mock_factory, mock_contacts_service):
    people = [_person(resource_name=f"people/c{i}", display_name=f"Jane {i}", given="Jane") for i in range(5)]
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": []}
    mock_contacts_service.people().connections().list().execute.return_value = {"connections": people}
    result = await google__search_contacts(mock_factory, query="Jane", max_results=2)
    assert "Found 2 contact(s)" in result


async def test_search_contacts_fallback_failure_is_not_a_negative(mock_factory, mock_contacts_service):
    mock_contacts_service.people().searchContacts().execute.return_value = {"results": []}
    mock_contacts_service.people().connections().list().execute.side_effect = RuntimeError("boom")
    result = await google__search_contacts(mock_factory, query="Jane")
    assert "Cannot confirm" in result
    assert "not a saved contact" not in result


# ── get_contact ───────────────────────────────────────────────────────────────


async def test_get_contact_auto_prefixes_people_slash(mock_factory, mock_contacts_service):
    mock_contacts_service.people().get().execute.return_value = _person(resource_name="people/c123")
    await google__get_contact(mock_factory, resource_name="c123")
    call_kwargs = mock_contacts_service.people().get.call_args[1]
    assert call_kwargs["resourceName"] == "people/c123"


async def test_get_contact_full_resource_name_unchanged(mock_factory, mock_contacts_service):
    mock_contacts_service.people().get().execute.return_value = _person(resource_name="people/c456")
    await google__get_contact(mock_factory, resource_name="people/c456")
    call_kwargs = mock_contacts_service.people().get.call_args[1]
    assert call_kwargs["resourceName"] == "people/c456"


async def test_get_contact_includes_person_fields(mock_factory, mock_contacts_service):
    mock_contacts_service.people().get().execute.return_value = _person()
    await google__get_contact(mock_factory, resource_name="people/c1")
    call_kwargs = mock_contacts_service.people().get.call_args[1]
    assert "personFields" in call_kwargs
    assert "resourceName" not in call_kwargs["personFields"]


async def test_get_contact_returns_details(mock_factory, mock_contacts_service):
    mock_contacts_service.people().get().execute.return_value = _person()
    result = await google__get_contact(mock_factory, resource_name="people/c1")
    assert "John Doe" in result
    assert "john@example.com" in result


async def test_get_contact_not_found_suggests_search(mock_factory, mock_contacts_service):
    mock_contacts_service.people().get().execute.side_effect = Exception("404: not found")
    result = await google__get_contact(mock_factory, resource_name="people/c999")
    assert "not found" in result.lower()
    assert "google__search_contacts" in result


# ── update_contact ────────────────────────────────────────────────────────────


async def test_update_contact_fetches_etag_before_update(mock_factory, mock_contacts_service):
    mock_contacts_service.people().get().execute.return_value = _person(etag="etag_abc")
    mock_contacts_service.people().updateContact().execute.return_value = _person(
        given="New", display_name="New Doe", etag="etag_def"
    )
    result = await google__update_contact(mock_factory, resource_name="people/c1", given_name="New")
    assert "updated" in result.lower()
    update_call = mock_contacts_service.people().updateContact.call_args
    body = update_call[1]["body"]
    # Top-level etag must be used (not nested in metadata)
    assert body["etag"] == "etag_abc"
    assert "metadata" not in body


async def test_update_contact_update_person_fields_set(mock_factory, mock_contacts_service):
    mock_contacts_service.people().get().execute.return_value = _person()
    mock_contacts_service.people().updateContact().execute.return_value = _person()
    await google__update_contact(mock_factory, resource_name="people/c1", email="new@test.com")
    update_call = mock_contacts_service.people().updateContact.call_args
    assert "emailAddresses" in update_call[1]["updatePersonFields"]


async def test_update_contact_preserves_family_name(mock_factory, mock_contacts_service):
    """Updating only given_name keeps existing family_name."""
    mock_contacts_service.people().get().execute.return_value = _person(given="Old", family="Smith")
    mock_contacts_service.people().updateContact().execute.return_value = _person(given="New", family="Smith")
    await google__update_contact(mock_factory, resource_name="people/c1", given_name="New")
    body = mock_contacts_service.people().updateContact.call_args[1]["body"]
    assert body["names"][0]["familyName"] == "Smith"


async def test_update_contact_auto_prefix(mock_factory, mock_contacts_service):
    mock_contacts_service.people().get().execute.return_value = _person()
    mock_contacts_service.people().updateContact().execute.return_value = _person()
    await google__update_contact(mock_factory, resource_name="c1", email="x@y.com")
    get_call = mock_contacts_service.people().get.call_args
    assert get_call[1]["resourceName"] == "people/c1"


async def test_update_contact_organization_and_job_title(mock_factory, mock_contacts_service):
    mock_contacts_service.people().get().execute.return_value = _person()
    mock_contacts_service.people().updateContact().execute.return_value = _person()
    await google__update_contact(mock_factory, resource_name="people/c1", organization="NewCo", job_title="CTO")
    body = mock_contacts_service.people().updateContact.call_args[1]["body"]
    assert body["organizations"][0]["name"] == "NewCo"
    assert body["organizations"][0]["title"] == "CTO"
    assert "organizations" in mock_contacts_service.people().updateContact.call_args[1]["updatePersonFields"]


async def test_update_contact_notes(mock_factory, mock_contacts_service):
    mock_contacts_service.people().get().execute.return_value = _person()
    mock_contacts_service.people().updateContact().execute.return_value = _person()
    await google__update_contact(mock_factory, resource_name="people/c1", notes="VIP customer")
    body = mock_contacts_service.people().updateContact.call_args[1]["body"]
    assert body["biographies"][0]["value"] == "VIP customer"
    assert "biographies" in mock_contacts_service.people().updateContact.call_args[1]["updatePersonFields"]


async def test_update_contact_no_fields_returns_guidance(mock_factory, mock_contacts_service):
    result = await google__update_contact(mock_factory, resource_name="people/c1")
    assert "No fields to update" in result
    mock_contacts_service.people().get.assert_not_called()


async def test_update_contact_stale_etag_auto_retries(mock_factory, mock_contacts_service):
    """Stale etag with a different fresh etag triggers an automatic retry."""
    mock_contacts_service.people().get().execute.side_effect = [
        _person(etag="old_etag"),  # first fetch for current data
        _person(etag="fresh_etag"),  # retry fetch for fresh etag
    ]
    mock_contacts_service.people().updateContact().execute.side_effect = [
        Exception("400: failedPrecondition"),  # first attempt fails
        _person(given="Test", display_name="Test Doe"),  # retry succeeds
    ]
    result = await google__update_contact(mock_factory, resource_name="people/c1", given_name="Test")
    assert "updated" in result.lower()


async def test_update_contact_stale_etag_conflict_when_same(mock_factory, mock_contacts_service):
    """When retried etag is the same (read-only contact), returns conflict message."""
    mock_contacts_service.people().get().execute.return_value = _person(etag="same_etag")
    mock_contacts_service.people().updateContact().execute.side_effect = Exception("400: failedPrecondition")
    result = await google__update_contact(mock_factory, resource_name="people/c1", given_name="Test")
    assert "etag" in result.lower() or "conflict" in result.lower() or "read-only" in result.lower()


async def test_update_contact_no_contact_etag_returns_error(mock_factory, mock_contacts_service):
    """If no top-level etag and no CONTACT-type source with etag, return a helpful message."""
    mock_contacts_service.people().get().execute.return_value = {
        "resourceName": "people/c1",
        "metadata": {"sources": [{"type": "PROFILE"}]},
    }
    result = await google__update_contact(mock_factory, resource_name="people/c1", given_name="Test")
    assert "etag" in result.lower() or "read-only" in result.lower()


async def test_update_contact_prefers_top_level_etag(mock_factory, mock_contacts_service):
    """Top-level person.etag takes priority over metadata.sources[].etag."""
    mock_contacts_service.people().get().execute.return_value = {
        "resourceName": "people/c1",
        "etag": "TOP_ETAG",
        "names": [{"givenName": "Old", "familyName": "Name"}],
        "metadata": {"sources": [{"type": "CONTACT", "etag": "SOURCE_ETAG"}]},
    }
    mock_contacts_service.people().updateContact().execute.return_value = _person()
    await google__update_contact(mock_factory, resource_name="people/c1", given_name="New")
    body = mock_contacts_service.people().updateContact.call_args[1]["body"]
    assert body["etag"] == "TOP_ETAG"
    assert "metadata" not in body


async def test_update_contact_remove_email_keeps_others(mock_factory, mock_contacts_service):
    """remove_email removes the specified address and keeps all others."""
    mock_contacts_service.people().get().execute.return_value = {
        "resourceName": "people/c1",
        "etag": "e1",
        "emailAddresses": [
            {"value": "liliia.pugachova1986@gmail.com", "type": "home"},
            {"value": "liliiapugachova@gmail.com", "type": "work"},
        ],
    }
    mock_contacts_service.people().updateContact().execute.return_value = {
        "resourceName": "people/c1",
        "etag": "e2",
        "emailAddresses": [{"value": "liliiapugachova@gmail.com"}],
    }
    result = await google__update_contact(
        mock_factory,
        resource_name="people/c1",
        remove_email="liliia.pugachova1986@gmail.com",
    )
    body = mock_contacts_service.people().updateContact.call_args[1]["body"]
    emails = [e["value"] for e in body["emailAddresses"]]
    assert "liliiapugachova@gmail.com" in emails
    assert "liliia.pugachova1986@gmail.com" not in emails
    assert "emailAddresses" in mock_contacts_service.people().updateContact.call_args[1]["updatePersonFields"]
    assert "updated" in result.lower()


async def test_update_contact_remove_email_not_found(mock_factory, mock_contacts_service):
    """remove_email returns helpful error when address is not present."""
    mock_contacts_service.people().get().execute.return_value = {
        "resourceName": "people/c1",
        "etag": "e1",
        "emailAddresses": [{"value": "other@example.com"}],
    }
    result = await google__update_contact(mock_factory, resource_name="people/c1", remove_email="missing@example.com")
    assert "not found" in result.lower()
    assert "other@example.com" in result


async def test_update_contact_add_email_appends(mock_factory, mock_contacts_service):
    """Adding an email appends to existing list rather than replacing it."""
    mock_contacts_service.people().get().execute.return_value = {
        "resourceName": "people/c1",
        "etag": "e1",
        "emailAddresses": [{"value": "existing@test.com"}],
    }
    mock_contacts_service.people().updateContact().execute.return_value = {
        "resourceName": "people/c1",
        "etag": "e2",
    }
    await google__update_contact(mock_factory, resource_name="people/c1", email="new@test.com")
    body = mock_contacts_service.people().updateContact.call_args[1]["body"]
    emails = [e["value"] for e in body["emailAddresses"]]
    assert "existing@test.com" in emails
    assert "new@test.com" in emails


async def test_update_contact_remove_phone(mock_factory, mock_contacts_service):
    """remove_phone removes the specified number and keeps all others."""
    mock_contacts_service.people().get().execute.return_value = {
        "resourceName": "people/c1",
        "etag": "e1",
        "phoneNumbers": [
            {"value": "+49123456789"},
            {"value": "+49987654321"},
        ],
    }
    mock_contacts_service.people().updateContact().execute.return_value = {
        "resourceName": "people/c1",
        "etag": "e2",
    }
    result = await google__update_contact(mock_factory, resource_name="people/c1", remove_phone="+49123456789")
    body = mock_contacts_service.people().updateContact.call_args[1]["body"]
    phones = [p["value"] for p in body["phoneNumbers"]]
    assert "+49987654321" in phones
    assert "+49123456789" not in phones
    assert "updated" in result.lower()
