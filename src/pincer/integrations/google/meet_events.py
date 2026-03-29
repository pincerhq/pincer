"""
Google Meet event subscription and proactive notification support.

Uses the Google Workspace Events API to subscribe to Meet events for a
specific space or all spaces owned by the authenticated user.

Supported event types:
  - google.workspace.meet.conference.v2.started
  - google.workspace.meet.conference.v2.ended
  - google.workspace.meet.participant.v2.joined
  - google.workspace.meet.participant.v2.left
  - google.workspace.meet.recording.v2.fileGenerated
  - google.workspace.meet.transcript.v2.fileGenerated

Delivery modes:
  1. Push (Pub/Sub): create a Google Cloud Pub/Sub topic, configure
     ``pubsub_topic`` in ``[integrations.google.meet]`` in pincer.toml,
     and wire ``handle_pubsub_message()`` to your Pub/Sub subscriber.
  2. Polling fallback: if no Pub/Sub topic is configured, the subscriber
     polls the Meet Spaces API every 60 seconds and emits events based on
     changes to the active-conference state.

Proactive notifications are delivered by calling the ``on_event`` callback
you pass to the constructor. Wire this to your channel router to send
messages to the user (e.g. on Telegram, WhatsApp, etc.).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from pincer.integrations.google.auth import GoogleAuth

logger = logging.getLogger(__name__)

# All Meet event types supported by the Workspace Events API.
_MEET_EVENT_TYPES: list[str] = [
    "google.workspace.meet.conference.v2.started",
    "google.workspace.meet.conference.v2.ended",
    "google.workspace.meet.participant.v2.joined",
    "google.workspace.meet.participant.v2.left",
    "google.workspace.meet.recording.v2.fileGenerated",
    "google.workspace.meet.transcript.v2.fileGenerated",
]

_WORKSPACE_EVENTS_BASE = "https://workspaceevents.googleapis.com/v1"

# Default polling interval in seconds for the fallback mode.
_POLL_INTERVAL = 60


class MeetEventSubscriber:
    """Subscribe to Google Meet events and deliver them via callback.

    Args:
        auth: ``GoogleAuth`` instance for credential refresh.
        on_event: Async callable ``(event_type: str, data: dict) -> None``
            called when a Meet event is received. Defaults to logging.
        pubsub_topic: Full Pub/Sub topic resource name
            (e.g. ``projects/my-project/topics/pincer-meet-events``).
            If omitted, polling mode is used.
    """

    def __init__(
        self,
        auth: "GoogleAuth",
        on_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        pubsub_topic: str | None = None,
    ) -> None:
        self._auth = auth
        self._on_event: Callable[[str, dict[str, Any]], Awaitable[None]] = (
            on_event if on_event is not None else self._default_on_event
        )
        self._pubsub_topic = pubsub_topic
        # space_name → subscription resource name (or "polling")
        self._subscriptions: dict[str, str] = {}
        # Spaces currently being polled
        self._polling_spaces: set[str] = set()
        # Known active-conference state per space (space_name → conf record name | None)
        self._last_known_conference: dict[str, str | None] = {}
        self._polling_task: asyncio.Task[None] | None = None

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def active_subscriptions(self) -> dict[str, str]:
        """Return a snapshot of active subscriptions: {space_name: sub_id}."""
        return dict(self._subscriptions)

    async def subscribe_to_space(self, space_name: str) -> str:
        """Subscribe to all Meet events for a specific space.

        Uses Pub/Sub if configured; falls back to polling otherwise.
        """
        if self._pubsub_topic:
            return await self._create_workspace_subscription(space_name)
        return await self._start_polling(space_name)

    async def subscribe_to_user(self) -> str:
        """Subscribe to all Meet events for all spaces owned by the user.

        Requires a Pub/Sub topic — there is no polling equivalent for
        user-level subscriptions.
        """
        if not self._pubsub_topic:
            return (
                "User-level event subscription requires Google Cloud Pub/Sub. "
                "Configure pubsub_topic in [integrations.google.meet] in pincer.toml. "
                "No polling fallback is available for user-level subscriptions. "
                "Subscribe to individual spaces instead."
            )
        return await self._create_workspace_subscription_user()

    async def unsubscribe(self, space_name: str) -> str:
        """Delete the event subscription for *space_name*."""
        if space_name not in self._subscriptions:
            return f"No active subscription for {space_name}."

        sub_id = self._subscriptions.pop(space_name)
        self._polling_spaces.discard(space_name)
        self._last_known_conference.pop(space_name, None)

        if sub_id != "polling":
            try:
                await self._delete_workspace_subscription(sub_id)
            except Exception as exc:
                logger.warning("Failed to delete Workspace subscription %s: %s", sub_id, exc)

        return f"Unsubscribed from events for {space_name}."

    async def handle_pubsub_message(self, message: dict[str, Any]) -> None:
        """Process a Cloud Pub/Sub message containing a Meet event.

        Call this from your Pub/Sub subscriber / push endpoint.
        """
        attributes = message.get("attributes", {})
        event_type = attributes.get("ce-type", "")
        resource = attributes.get("ce-resourceName", "")

        if "conference" in event_type and "ended" in event_type:
            await self._on_event(
                "conference_ended",
                {
                    "space": resource,
                    "message": (
                        "Your meeting just ended. "
                        "Recordings and transcripts will be ready shortly. "
                        "Want me to summarize the transcript when it's available?"
                    ),
                },
            )
        elif "conference" in event_type and "started" in event_type:
            await self._on_event(
                "conference_started",
                {"space": resource, "message": f"A meeting has started at {resource}."},
            )
        elif "participant" in event_type and "joined" in event_type:
            await self._on_event(
                "participant_joined",
                {"space": resource, "message": "A participant joined the meeting."},
            )
        elif "participant" in event_type and "left" in event_type:
            await self._on_event(
                "participant_left",
                {"space": resource, "message": "A participant left the meeting."},
            )
        elif "recording" in event_type and "fileGenerated" in event_type:
            await self._on_event(
                "recording_ready",
                {
                    "space": resource,
                    "message": "Meeting recording is now available in your Google Drive.",
                },
            )
        elif "transcript" in event_type and "fileGenerated" in event_type:
            await self._on_event(
                "transcript_ready",
                {
                    "space": resource,
                    "message": "Meeting transcript is ready. Want me to summarize it?",
                },
            )
        else:
            logger.debug("Unhandled Meet event type: %s for %s", event_type, resource)

    # ── Pub/Sub subscription helpers ───────────────────────────────────────────

    async def _create_workspace_subscription(self, space_name: str) -> str:
        creds = await asyncio.to_thread(self._auth.get_credentials)
        body = {
            "targetResource": f"//meet.googleapis.com/{space_name}",
            "eventTypes": _MEET_EVENT_TYPES,
            "payloadOptions": {"includeResource": False},
            "notificationEndpoint": {"pubsubTopic": self._pubsub_topic},
            "ttl": "86400s",
        }
        try:
            import requests as req  # type: ignore[import-untyped]  # requests is a transitive dep

            session = req.Session()
            session.headers["Authorization"] = f"Bearer {creds.token}"
            resp = await asyncio.to_thread(
                lambda: session.post(
                    f"{_WORKSPACE_EVENTS_BASE}/subscriptions",
                    json=body,
                )
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            sub_name: str = data.get("name", "unknown")
            self._subscriptions[space_name] = sub_name
            return f"Subscribed to events for {space_name}.\nSubscription: {sub_name}"
        except Exception as exc:
            logger.warning("Workspace Events subscription failed: %s — falling back to polling", exc)
            return await self._start_polling(space_name)

    async def _create_workspace_subscription_user(self) -> str:
        creds = await asyncio.to_thread(self._auth.get_credentials)
        body = {
            "targetResource": "//meet.googleapis.com/users/me",
            "eventTypes": _MEET_EVENT_TYPES,
            "payloadOptions": {"includeResource": False},
            "notificationEndpoint": {"pubsubTopic": self._pubsub_topic},
            "ttl": "86400s",
        }
        import requests as req

        session = req.Session()
        session.headers["Authorization"] = f"Bearer {creds.token}"
        resp = await asyncio.to_thread(
            lambda: session.post(f"{_WORKSPACE_EVENTS_BASE}/subscriptions", json=body)
        )
        resp.raise_for_status()
        data = resp.json()
        sub_name = data.get("name", "unknown")
        self._subscriptions["users/me"] = sub_name
        return f"Subscribed to all Meet events for this user.\nSubscription: {sub_name}"

    async def _delete_workspace_subscription(self, sub_name: str) -> None:
        creds = await asyncio.to_thread(self._auth.get_credentials)
        import requests as req

        session = req.Session()
        session.headers["Authorization"] = f"Bearer {creds.token}"
        resp = await asyncio.to_thread(
            lambda: session.delete(f"{_WORKSPACE_EVENTS_BASE}/{sub_name}")
        )
        resp.raise_for_status()

    # ── Polling fallback ───────────────────────────────────────────────────────

    async def _start_polling(self, space_name: str) -> str:
        self._subscriptions[space_name] = "polling"
        self._polling_spaces.add(space_name)

        if self._polling_task is None or self._polling_task.done():
            self._polling_task = asyncio.create_task(self._poll_loop())

        return (
            f"Monitoring {space_name} via polling (no Pub/Sub configured). "
            f"Checking for conference changes every {_POLL_INTERVAL}s."
        )

    async def _poll_loop(self) -> None:
        """Background task: poll Meet Spaces API to detect conference state changes."""
        from googleapiclient.discovery import build  # type: ignore[import-untyped]

        while self._polling_spaces:
            await asyncio.sleep(_POLL_INTERVAL)

            try:
                creds = await asyncio.to_thread(self._auth.get_credentials)
            except Exception as exc:
                logger.warning("Meet polling: credential refresh failed: %s", exc)
                continue

            for space_name in list(self._polling_spaces):
                if space_name not in self._subscriptions:
                    self._polling_spaces.discard(space_name)
                    continue
                try:
                    _sn = space_name
                    svc = await asyncio.to_thread(
                        build, "meet", "v2", credentials=creds, cache_discovery=False
                    )
                    space = await asyncio.to_thread(
                        lambda s=_sn: svc.spaces().get(name=s).execute()  # type: ignore[misc]
                    )
                    active = space.get("activeConference", {})
                    conf_record: str | None = (
                        active.get("conferenceRecord") if active else None
                    )
                    prev_record = self._last_known_conference.get(space_name)

                    if prev_record and not conf_record:
                        await self._on_event(
                            "conference_ended",
                            {
                                "space": space_name,
                                "conference": prev_record,
                                "message": (
                                    f"Your meeting in {space_name} just ended. "
                                    "Recordings and transcripts will be ready shortly."
                                ),
                            },
                        )
                    elif conf_record and not prev_record:
                        await self._on_event(
                            "conference_started",
                            {
                                "space": space_name,
                                "conference": conf_record,
                                "message": f"A meeting has started in {space_name}.",
                            },
                        )

                    self._last_known_conference[space_name] = conf_record
                except Exception as exc:
                    logger.warning("Meet polling error for %s: %s", space_name, exc)

    # ── Default callback ───────────────────────────────────────────────────────

    @staticmethod
    async def _default_on_event(event_type: str, data: dict[str, Any]) -> None:
        logger.info(
            "Meet event: %s — %s",
            event_type,
            data.get("message", str(data)),
        )
