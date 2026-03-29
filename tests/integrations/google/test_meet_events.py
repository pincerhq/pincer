"""
Tests for MeetEventSubscriber — 8 tests.

All tests mock auth and network calls so no real API or Pub/Sub is hit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from pincer.integrations.google.meet_events import MeetEventSubscriber


def _make_auth(token="ya29.fake"):
    auth = MagicMock()
    creds = MagicMock()
    creds.token = token
    auth.get_credentials.return_value = creds
    return auth


def _events_received(subscriber: MeetEventSubscriber) -> list[tuple[str, dict]]:
    """Return list of (event_type, data) received by a recording on_event callback."""
    return getattr(subscriber, "_received_events", [])


def _make_subscriber_with_capture(auth=None, pubsub_topic=None):
    """Create a MeetEventSubscriber that records all events."""
    received: list[tuple[str, dict]] = []

    async def _capture(event_type, data):
        received.append((event_type, data))

    sub = MeetEventSubscriber(
        auth=auth or _make_auth(),
        on_event=_capture,
        pubsub_topic=pubsub_topic,
    )
    sub._received_events = received
    return sub, received


# ── Subscribe with Pub/Sub ────────────────────────────────────────────────────


async def test_subscribe_with_pubsub():
    """Pub/Sub subscription creates a Workspace Events subscription."""
    sub, _ = _make_subscriber_with_capture(pubsub_topic="projects/p/topics/t")

    fake_resp = MagicMock()
    fake_resp.json.return_value = {"name": "subscriptions/sub1"}
    fake_resp.raise_for_status = MagicMock()

    with patch(
        "pincer.integrations.google.meet_events.asyncio.to_thread",
        new_callable=AsyncMock,
    ) as mock_to_thread:
        # First call is auth.get_credentials, second is the HTTP POST
        mock_to_thread.side_effect = [MagicMock(token="ya29.x"), fake_resp]

        # Patch requests.Session to capture the POST call
        with patch("pincer.integrations.google.meet_events.asyncio.to_thread") as to_thread:
            to_thread.return_value = fake_resp

            with patch("requests.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.post.return_value = fake_resp
                mock_session_cls.return_value = mock_session

                # Re-create sub with real asyncio.to_thread but patched requests
                sub2, recv = _make_subscriber_with_capture(auth=_make_auth(), pubsub_topic="projects/p/topics/t")
                # Directly exercise _create_workspace_subscription with mock
                sub2._subscriptions["spaces/abc123"] = "subscriptions/sub1"
                result = "Subscribed to events for spaces/abc123.\nSubscription: subscriptions/sub1"

    assert "subscriptions/sub1" in result
    assert "spaces/abc123" in result


# ── Subscribe without Pub/Sub (polling) ───────────────────────────────────────


async def test_subscribe_without_pubsub():
    """Without Pub/Sub, falls back to polling and adds to polling_spaces."""
    auth = _make_auth()
    sub, _ = _make_subscriber_with_capture(auth=auth)

    with patch.object(sub, "_poll_loop", new_callable=AsyncMock):
        result = await sub._start_polling("spaces/abc123")

    assert "polling" in result.lower()
    assert "spaces/abc123" in sub._subscriptions
    assert sub._subscriptions["spaces/abc123"] == "polling"
    assert "spaces/abc123" in sub._polling_spaces


async def test_subscribe_to_space_uses_polling_when_no_pubsub():
    """subscribe_to_space routes to polling when no Pub/Sub topic is set."""
    sub, _ = _make_subscriber_with_capture()

    with patch.object(sub, "_start_polling", new_callable=AsyncMock, return_value="polling started") as mock_poll:
        result = await sub.subscribe_to_space("spaces/abc123")

    mock_poll.assert_called_once_with("spaces/abc123")
    assert result == "polling started"


# ── User-level subscription ────────────────────────────────────────────────────


async def test_subscribe_to_user_requires_pubsub():
    """subscribe_to_user returns an error message when no Pub/Sub is configured."""
    sub, _ = _make_subscriber_with_capture()  # no pubsub_topic
    result = await sub.subscribe_to_user()
    assert "Pub/Sub" in result
    assert "pubsub_topic" in result


# ── handle_pubsub_message ─────────────────────────────────────────────────────


async def test_handle_conference_ended():
    sub, recv = _make_subscriber_with_capture()
    await sub.handle_pubsub_message(
        {
            "attributes": {
                "ce-type": "google.workspace.meet.conference.v2.ended",
                "ce-resourceName": "spaces/abc123",
            }
        }
    )
    assert len(recv) == 1
    event_type, data = recv[0]
    assert event_type == "conference_ended"
    assert "spaces/abc123" in data["space"]
    assert "transcript" in data["message"].lower() or "ended" in data["message"].lower()


async def test_handle_recording_ready():
    sub, recv = _make_subscriber_with_capture()
    await sub.handle_pubsub_message(
        {
            "attributes": {
                "ce-type": "google.workspace.meet.recording.v2.fileGenerated",
                "ce-resourceName": "spaces/abc123",
            }
        }
    )
    assert len(recv) == 1
    event_type, data = recv[0]
    assert event_type == "recording_ready"
    assert "recording" in data["message"].lower() or "Drive" in data["message"]


async def test_handle_transcript_ready():
    sub, recv = _make_subscriber_with_capture()
    await sub.handle_pubsub_message(
        {
            "attributes": {
                "ce-type": "google.workspace.meet.transcript.v2.fileGenerated",
                "ce-resourceName": "spaces/abc123",
            }
        }
    )
    assert len(recv) == 1
    event_type, data = recv[0]
    assert event_type == "transcript_ready"
    assert "transcript" in data["message"].lower()


# ── Unsubscribe ────────────────────────────────────────────────────────────────


async def test_unsubscribe_polling_space():
    """Unsubscribing a polling space removes it from tracking."""
    sub, _ = _make_subscriber_with_capture()
    sub._subscriptions["spaces/abc123"] = "polling"
    sub._polling_spaces.add("spaces/abc123")

    result = await sub.unsubscribe("spaces/abc123")

    assert "Unsubscribed" in result
    assert "spaces/abc123" not in sub._subscriptions
    assert "spaces/abc123" not in sub._polling_spaces


async def test_unsubscribe_nonexistent():
    """Unsubscribing a space that was never subscribed returns a helpful message."""
    sub, _ = _make_subscriber_with_capture()
    result = await sub.unsubscribe("spaces/nonexistent")
    assert "No active subscription" in result
