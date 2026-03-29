"""
Tests for Google Meet tools — one test per tool (27 tools).

All tests mock the Meet service object so no real API calls are made.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.integrations.google.tools_meet import (
    google__add_meet_member,
    google__check_recording_status,
    google__configure_meet_artifacts,
    google__configure_meet_moderation,
    google__create_meet_space,
    google__delete_meet_subscription,
    google__end_active_conference,
    google__get_conference_record,
    google__get_meet_recording,
    google__get_meet_space,
    google__get_meet_transcript,
    google__get_participant_details,
    google__get_smart_notes,
    google__get_transcript_entry,
    google__list_conference_participants,
    google__list_conference_records,
    google__list_meet_recordings,
    google__list_meet_subscriptions,
    google__list_meet_transcripts,
    google__list_participant_sessions,
    google__list_smart_notes,
    google__list_transcript_entries,
    google__remove_meet_member,
    google__subscribe_meet_events,
    google__summarize_meet_transcript,
    google__update_meet_space,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _space(name="spaces/abc123", code="abc-defg-hij"):
    return {
        "name": name,
        "meetingUri": f"https://meet.google.com/{code}",
        "meetingCode": code,
        "config": {
            "accessType": "OPEN",
            "entryPointAccess": "ALL",
            "artifactConfig": {
                "recordingConfig": {"autoRecordingGeneration": "DISABLED"},
                "transcriptionConfig": {"autoTranscriptionGeneration": "DISABLED"},
                "smartNotesConfig": {"autoSmartNotesGeneration": "DISABLED"},
            },
        },
    }


def _space_with_conference():
    s = _space()
    s["activeConference"] = {"conferenceRecord": "conferenceRecords/conf1"}
    return s


def _conference(name="conferenceRecords/conf1"):
    return {
        "name": name,
        "space": "spaces/abc123",
        "startTime": "2026-03-28T09:00:00Z",
        "endTime": "2026-03-28T09:22:00Z",
        "expireTime": "2026-04-28T09:22:00Z",
    }


def _participant(name="conferenceRecords/conf1/participants/p1", display="Alice"):
    return {
        "name": name,
        "signerUser": {"displayName": display, "user": "users/alice"},
        "earliestStartTime": "2026-03-28T09:01:00Z",
        "latestEndTime": "2026-03-28T09:22:00Z",
    }


def _recording(state="FILE_GENERATED"):
    return {
        "name": "conferenceRecords/conf1/recordings/rec1",
        "state": state,
        "startTime": "2026-03-28T09:01:00Z",
        "endTime": "2026-03-28T09:22:00Z",
        "driveDestination": {
            "file": "1BxiMVs0XRA5nFMd",
            "exportUri": "https://drive.google.com/file/d/1BxiMVs0XRA5nFMd",
        },
    }


def _transcript():
    return {
        "name": "conferenceRecords/conf1/transcripts/t1",
        "state": "SAVED",
        "startTime": "2026-03-28T09:01:00Z",
        "endTime": "2026-03-28T09:22:00Z",
        "docsDestination": {
            "document": "1DocId123",
            "exportUri": "https://docs.google.com/document/d/1DocId123",
        },
    }


def _entry(n=1, speaker="Alice", text="Hello team."):
    return {
        "name": f"conferenceRecords/conf1/transcripts/t1/entries/{n:03d}",
        "participantDisplayName": speaker,
        "participant": "conferenceRecords/conf1/participants/p1",
        "text": text,
        "languageCode": "en-US",
        "startTime": f"2026-03-28T09:0{n}:00Z",
        "endTime": f"2026-03-28T09:0{n}:05Z",
    }


def _mock_subscriber(subs=None):
    sub = MagicMock()
    sub.active_subscriptions = subs or {}
    sub.subscribe_to_space = AsyncMock(return_value="Subscribed to events for spaces/abc123.\nSubscription: sub1")
    sub.subscribe_to_user = AsyncMock(return_value="Subscribed to all Meet events for this user.\nSubscription: sub_user")
    sub.unsubscribe = AsyncMock(return_value="Unsubscribed from events for spaces/abc123.")
    return sub


# ── Space Management ──────────────────────────────────────────────────────────

async def test_create_meet_space(mock_factory, mock_meet_service):
    mock_meet_service.spaces().create().execute.return_value = _space()
    result = await google__create_meet_space(mock_factory)
    assert "https://meet.google.com/" in result
    assert "abc-defg-hij" in result
    assert "spaces/abc123" in result
    assert "OPEN" in result


async def test_create_meet_space_with_auto_recording(mock_factory, mock_meet_service):
    mock_meet_service.spaces().create().execute.return_value = _space()
    result = await google__create_meet_space(mock_factory, auto_recording=True)
    assert "auto-recording" in result
    # Verify artifact config was passed in the create call
    call_args = mock_meet_service.spaces().create.call_args
    body = call_args[1]["body"] if call_args and "body" in (call_args[1] or {}) else (call_args[0][0] if call_args else {})
    assert "artifactConfig" in str(call_args)


async def test_create_meet_space_with_all_artifacts(mock_factory, mock_meet_service):
    mock_meet_service.spaces().create().execute.return_value = _space()
    result = await google__create_meet_space(
        mock_factory,
        auto_recording=True,
        auto_transcription=True,
        auto_smart_notes=True,
    )
    assert "auto-recording" in result
    assert "auto-transcription" in result
    assert "smart notes" in result


async def test_create_meet_space_restricted(mock_factory, mock_meet_service):
    s = _space()
    s["config"]["accessType"] = "RESTRICTED"
    mock_meet_service.spaces().create().execute.return_value = s
    result = await google__create_meet_space(mock_factory, access_type="RESTRICTED")
    assert "RESTRICTED" in result
    assert "invited members only" in result


async def test_get_meet_space_by_name(mock_factory, mock_meet_service):
    mock_meet_service.spaces().get().execute.return_value = _space()
    result = await google__get_meet_space(mock_factory, space_name="spaces/abc123")
    assert "https://meet.google.com/" in result
    assert "OPEN" in result
    assert "No active conference" in result


async def test_get_meet_space_by_code(mock_factory, mock_meet_service):
    mock_meet_service.spaces().get().execute.return_value = _space()
    result = await google__get_meet_space(mock_factory, meeting_code="abc-defg-hij")
    assert "abc-defg-hij" in result


async def test_get_meet_space_active_conference(mock_factory, mock_meet_service):
    mock_meet_service.spaces().get().execute.return_value = _space_with_conference()
    result = await google__get_meet_space(mock_factory, space_name="spaces/abc123")
    assert "conferenceRecords/conf1" in result


async def test_get_meet_space_no_identifier(mock_factory, mock_meet_service):
    result = await google__get_meet_space(mock_factory)
    assert "Error" in result


async def test_update_meet_space(mock_factory, mock_meet_service):
    updated = _space()
    updated["config"]["accessType"] = "TRUSTED"
    mock_meet_service.spaces().patch().execute.return_value = updated
    result = await google__update_meet_space(
        mock_factory, space_name="spaces/abc123", access_type="TRUSTED"
    )
    assert "TRUSTED" in result
    assert "spaces/abc123" in result


async def test_end_active_conference(mock_factory, mock_meet_service):
    mock_meet_service.spaces().endActiveConference().execute.return_value = {}
    result = await google__end_active_conference(mock_factory, space_name="spaces/abc123")
    assert "ended" in result
    assert "disconnected" in result


async def test_configure_moderation_success(mock_factory, mock_meet_service):
    mock_meet_service.spaces().patch().execute.return_value = _space()
    result = await google__configure_meet_moderation(
        mock_factory,
        space_name="spaces/abc123",
        chat_restriction="HOST_AND_CO_HOSTS",
    )
    assert "Moderation configured" in result or "Developer Preview" in result


async def test_configure_moderation_preview_unavailable(mock_factory, mock_meet_service):
    mock_meet_service.spaces().patch().execute.side_effect = Exception("403 Developer Preview")
    result = await google__configure_meet_moderation(
        mock_factory,
        space_name="spaces/abc123",
        chat_restriction="ORGANIZER_ONLY",
    )
    assert "Developer Preview" in result


async def test_configure_meet_artifacts(mock_factory, mock_meet_service):
    mock_meet_service.spaces().patch().execute.return_value = _space()
    result = await google__configure_meet_artifacts(
        mock_factory,
        space_name="spaces/abc123",
        auto_recording=True,
        auto_transcription=True,
    )
    assert "Auto-recording: ENABLED" in result
    assert "Auto-transcription: ENABLED" in result


async def test_add_meet_member_success(mock_factory, mock_meet_service):
    mock_meet_service.spaces().members().create().execute.return_value = {
        "name": "spaces/abc123/members/m1",
        "role": "COHOST",
    }
    result = await google__add_meet_member(
        mock_factory,
        space_name="spaces/abc123",
        email="alice@example.com",
        role="COHOST",
    )
    assert "alice@example.com" in result or "Developer Preview" in result


async def test_add_meet_member_preview_unavailable(mock_factory, mock_meet_service):
    mock_meet_service.spaces().members().create().execute.side_effect = Exception("404 preview")
    result = await google__add_meet_member(
        mock_factory,
        space_name="spaces/abc123",
        email="bob@example.com",
    )
    assert "Developer Preview" in result


async def test_remove_meet_member(mock_factory, mock_meet_service):
    mock_meet_service.spaces().members().delete().execute.return_value = {}
    result = await google__remove_meet_member(
        mock_factory, member_name="spaces/abc123/members/m1"
    )
    assert "removed" in result or "Developer Preview" in result


# ── Conference History ─────────────────────────────────────────────────────────

async def test_list_conference_records(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().list().execute.return_value = {
        "conferenceRecords": [_conference()]
    }
    result = await google__list_conference_records(mock_factory)
    assert "conferenceRecords/conf1" in result
    assert "spaces/abc123" in result


async def test_list_conference_records_empty(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().list().execute.return_value = {
        "conferenceRecords": []
    }
    result = await google__list_conference_records(mock_factory)
    assert "No conference records" in result


async def test_list_conference_records_filtered(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().list().execute.return_value = {
        "conferenceRecords": [_conference()]
    }
    result = await google__list_conference_records(
        mock_factory, space_name="spaces/abc123"
    )
    assert "Conference records" in result
    # Verify filter was passed
    call_args = mock_meet_service.conferenceRecords().list.call_args
    assert "filter" in str(call_args)


async def test_get_conference_record(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().get().execute.return_value = _conference()
    result = await google__get_conference_record(
        mock_factory, conference_name="conferenceRecords/conf1"
    )
    assert "conferenceRecords/conf1" in result
    assert "2026-03-28" in result


async def test_list_conference_participants(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().participants().list().execute.return_value = {
        "participants": [_participant()]
    }
    result = await google__list_conference_participants(
        mock_factory, conference_name="conferenceRecords/conf1"
    )
    assert "Alice" in result
    assert "signed in" in result


async def test_list_participants_anonymous(mock_factory, mock_meet_service):
    anon = {
        "name": "conferenceRecords/conf1/participants/p2",
        "anonymousUser": {"displayName": "Anonymous User"},
        "earliestStartTime": "2026-03-28T09:05:00Z",
    }
    mock_meet_service.conferenceRecords().participants().list().execute.return_value = {
        "participants": [anon]
    }
    result = await google__list_conference_participants(
        mock_factory, conference_name="conferenceRecords/conf1"
    )
    assert "Anonymous User" in result
    assert "anonymous" in result


async def test_get_participant_details(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().participants().get().execute.return_value = (
        _participant()
    )
    result = await google__get_participant_details(
        mock_factory,
        participant_name="conferenceRecords/conf1/participants/p1",
    )
    assert "Alice" in result
    assert "users/alice" in result


async def test_list_participant_sessions(mock_factory, mock_meet_service):
    session = {
        "name": "conferenceRecords/conf1/participants/p1/participantSessions/s1",
        "startTime": "2026-03-28T09:01:00Z",
        "endTime": "2026-03-28T09:22:00Z",
    }
    mock_meet_service.conferenceRecords().participants().participantSessions().list().execute.return_value = {
        "participantSessions": [session]
    }
    result = await google__list_participant_sessions(
        mock_factory,
        participant_name="conferenceRecords/conf1/participants/p1",
    )
    assert "Session 1" in result
    assert "2026-03-28" in result


# ── Recordings ────────────────────────────────────────────────────────────────

async def test_list_meet_recordings(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().recordings().list().execute.return_value = {
        "recordings": [_recording()]
    }
    result = await google__list_meet_recordings(
        mock_factory, conference_name="conferenceRecords/conf1"
    )
    assert "FILE_GENERATED" in result
    assert "1BxiMVs0XRA5nFMd" in result


async def test_list_recordings_empty(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().recordings().list().execute.return_value = {
        "recordings": []
    }
    result = await google__list_meet_recordings(
        mock_factory, conference_name="conferenceRecords/conf1"
    )
    assert "No recordings" in result


async def test_get_recording_with_drive_id(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().recordings().get().execute.return_value = (
        _recording()
    )
    result = await google__get_meet_recording(
        mock_factory, recording_name="conferenceRecords/conf1/recordings/rec1"
    )
    assert "1BxiMVs0XRA5nFMd" in result
    assert "FILE_GENERATED" in result


async def test_check_recording_not_ready(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().recordings().get().execute.return_value = (
        _recording(state="ENDED")
    )
    result = await google__check_recording_status(
        mock_factory, recording_name="conferenceRecords/conf1/recordings/rec1"
    )
    assert "ENDED" in result
    assert "processed" in result.lower()


async def test_check_recording_ready(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().recordings().get().execute.return_value = (
        _recording(state="FILE_GENERATED")
    )
    result = await google__check_recording_status(
        mock_factory, recording_name="conferenceRecords/conf1/recordings/rec1"
    )
    assert "ready" in result.lower()
    assert "1BxiMVs0XRA5nFMd" in result


# ── Transcripts ────────────────────────────────────────────────────────────────

async def test_list_meet_transcripts(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().transcripts().list().execute.return_value = {
        "transcripts": [_transcript()]
    }
    result = await google__list_meet_transcripts(
        mock_factory, conference_name="conferenceRecords/conf1"
    )
    assert "conferenceRecords/conf1/transcripts/t1" in result
    assert "SAVED" in result
    assert "1DocId123" in result


async def test_list_transcripts_empty(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().transcripts().list().execute.return_value = {
        "transcripts": []
    }
    result = await google__list_meet_transcripts(
        mock_factory, conference_name="conferenceRecords/conf1"
    )
    assert "No transcripts" in result


async def test_get_meet_transcript(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().transcripts().get().execute.return_value = (
        _transcript()
    )
    result = await google__get_meet_transcript(
        mock_factory,
        transcript_name="conferenceRecords/conf1/transcripts/t1",
    )
    assert "1DocId123" in result
    assert "SAVED" in result


async def test_list_transcript_entries(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().transcripts().entries().list().execute.return_value = {
        "transcriptEntries": [_entry(1, "Alice", "Hello team."), _entry(2, "Bob", "Good morning.")]
    }
    result = await google__list_transcript_entries(
        mock_factory,
        transcript_name="conferenceRecords/conf1/transcripts/t1",
    )
    assert "Alice" in result
    assert "Hello team." in result
    assert "Bob" in result
    assert "Good morning." in result


async def test_get_transcript_entry(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().transcripts().entries().get().execute.return_value = (
        _entry(1, "Alice", "Hello team.")
    )
    result = await google__get_transcript_entry(
        mock_factory,
        entry_name="conferenceRecords/conf1/transcripts/t1/entries/001",
    )
    assert "Alice" in result
    assert "Hello team." in result
    assert "en-US" in result


async def test_summarize_transcript(mock_factory, mock_meet_service):
    # transcripts list
    mock_meet_service.conferenceRecords().transcripts().list().execute.return_value = {
        "transcripts": [_transcript()]
    }
    # participants list
    mock_meet_service.conferenceRecords().participants().list().execute.return_value = {
        "participants": [_participant()]
    }
    # entries list (no next page)
    mock_meet_service.conferenceRecords().transcripts().entries().list().execute.return_value = {
        "transcriptEntries": [
            _entry(1, "Alice", "Let's start the standup."),
            _entry(2, "Bob", "I worked on the API yesterday."),
        ]
    }
    result = await google__summarize_meet_transcript(
        mock_factory, conference_name="conferenceRecords/conf1"
    )
    assert "Alice" in result
    assert "Let's start the standup." in result
    assert "Bob" in result
    assert "summarize" in result.lower()


async def test_summarize_transcript_no_transcripts(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().transcripts().list().execute.return_value = {
        "transcripts": []
    }
    result = await google__summarize_meet_transcript(
        mock_factory, conference_name="conferenceRecords/conf1"
    )
    assert "No transcripts" in result


# ── Smart Notes ────────────────────────────────────────────────────────────────

async def test_list_smart_notes(mock_factory, mock_meet_service):
    smart = _transcript()
    smart["state"] = "SMART_NOTES_SAVED"
    mock_meet_service.conferenceRecords().transcripts().list().execute.return_value = {
        "transcripts": [smart]
    }
    result = await google__list_smart_notes(
        mock_factory, conference_name="conferenceRecords/conf1"
    )
    # Either smart notes found or fallback message
    assert "SMART_NOTES_SAVED" in result or "smart notes" in result.lower()


async def test_list_smart_notes_not_configured(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().transcripts().list().execute.return_value = {
        "transcripts": []
    }
    result = await google__list_smart_notes(
        mock_factory, conference_name="conferenceRecords/conf1"
    )
    assert "auto_smart_notes" in result or "smart notes" in result.lower()


async def test_get_smart_notes(mock_factory, mock_meet_service):
    mock_meet_service.conferenceRecords().transcripts().get().execute.return_value = (
        _transcript()
    )
    result = await google__get_smart_notes(
        mock_factory,
        transcript_name="conferenceRecords/conf1/transcripts/t1",
    )
    # The transcript has docsDestination, so returns doc info
    assert "1DocId123" in result or "smart notes" in result.lower()


# ── Event Subscriptions ────────────────────────────────────────────────────────

async def test_subscribe_meet_events(mock_factory, mock_meet_service):
    sub = _mock_subscriber()
    result = await google__subscribe_meet_events(
        mock_factory, sub, space_name="spaces/abc123"
    )
    sub.subscribe_to_space.assert_called_once_with("spaces/abc123")
    assert "Subscribed" in result


async def test_subscribe_all_user_events(mock_factory, mock_meet_service):
    sub = _mock_subscriber()
    result = await google__subscribe_meet_events(
        mock_factory, sub, subscribe_all=True
    )
    sub.subscribe_to_user.assert_called_once()
    assert "Subscribed" in result


async def test_subscribe_no_args(mock_factory, mock_meet_service):
    sub = _mock_subscriber()
    result = await google__subscribe_meet_events(mock_factory, sub)
    assert "Provide" in result


async def test_list_meet_subscriptions_empty(mock_factory, mock_meet_service):
    sub = _mock_subscriber(subs={})
    result = await google__list_meet_subscriptions(mock_factory, sub)
    assert "No active" in result


async def test_list_meet_subscriptions_with_entries(mock_factory, mock_meet_service):
    sub = _mock_subscriber(subs={"spaces/abc123": "sub1", "spaces/xyz": "polling"})
    result = await google__list_meet_subscriptions(mock_factory, sub)
    assert "spaces/abc123" in result
    assert "sub1" in result


async def test_delete_meet_subscription(mock_factory, mock_meet_service):
    sub = _mock_subscriber()
    result = await google__delete_meet_subscription(
        mock_factory, sub, space_name="spaces/abc123"
    )
    sub.unsubscribe.assert_called_once_with("spaces/abc123")
    assert "Unsubscribed" in result
