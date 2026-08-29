"""
Live listen-in — per-call media fan-out hub (Sprint 15).

Audio source: a SEPARATE Twilio media fork (`<Start><Stream track="both_tracks">`
in the call TwiML, see `twiml_builder`), NOT the conversation engine. That
makes the feature engine-independent (on ConversationRelay we never possess
the audio; the fork hands it to us without touching the conversation path)
and rx-only by protocol — a `<Start><Stream>` cannot inject audio into the
call, so barge-in / whisper is impossible by construction, not by discipline.

    Twilio ──WSS /api/apps/twilio/monitor/{sid}──► MonitorHub.publish()
                                                      │ per-call fan-out
                                                      ▼
              browser ◄── WSS /api/voice/listen/{sid} ── Subscription.queue

Frames are relayed untranscoded (base64 μ-law 8 kHz, as Twilio sends them)
and are NEVER written to disk or the database by this module: the hub holds
at most `queue_size` frames per subscriber (~1 s) and drops the OLDEST on
overflow, so a slow listener hears a skip and memory stays bounded.

The hub is a module singleton (hotfix-3 lesson: the ingress handler in
`twiml_server` and the egress handler in `api/voice` must see the same
object whichever module imports first).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Per-subscriber queue depth. Twilio sends one frame per 20 ms → 50 frames ≈ 1 s.
DEFAULT_QUEUE_SIZE = 50
DEFAULT_MAX_LISTENERS = 2

TRACK_INBOUND = "inbound"  # the caller / callee (the other party)
TRACK_OUTBOUND = "outbound"  # the agent
TRACKS = (TRACK_INBOUND, TRACK_OUTBOUND)

# End reasons sent to listeners as {"type": "end", "reason": ...}
END_CALL_ENDED = "call_ended"
END_CAPACITY = "capacity"
END_UNAVAILABLE = "unavailable"
END_ERROR = "error"
END_STOPPED = "stopped"  # the listener hung up (never sent, used for audit)

# WebSocket close codes on the listener side (application range 4000-4999).
CLOSE_CAPACITY = 4001
CLOSE_UNAVAILABLE = 4004


class MonitorError(Exception):
    """Base class for hub errors."""


class ListenerCapacityError(MonitorError):
    """The per-call listener cap is reached."""


class MonitorUnavailableError(MonitorError):
    """No media source is attached for this call (call unknown, ended, or
    listen-in disabled when the call's TwiML was built)."""


@dataclass
class Subscription:
    """One dashboard listener on one call.

    `queue` carries media frame dicts (`{"type": "media", ...}`) and exactly
    one terminal dict (`{"type": "end", "reason": ...}`) after which nothing
    more is ever enqueued.
    """

    call_sid: str
    user: str
    queue: asyncio.Queue[dict[str, Any]]
    started_at: float = field(default_factory=time.time)
    frames: int = 0
    dropped: int = 0
    ended: bool = False
    end_reason: str = ""

    def _finish(self, reason: str) -> None:
        if self.ended:
            return
        self.ended = True
        self.end_reason = reason
        self._put_dropping_oldest({"type": "end", "reason": reason})

    def _put_dropping_oldest(self, item: dict[str, Any]) -> bool:
        """Enqueue, evicting the oldest frame when full. Returns True when a
        frame was dropped."""
        dropped = False
        while True:
            try:
                self.queue.put_nowait(item)
                return dropped
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()
                    dropped = True
                except asyncio.QueueEmpty:  # pragma: no cover — racy, retry
                    continue


@dataclass
class _Channel:
    """Per-call state: the Twilio source plus its subscribers."""

    call_sid: str
    source: Any = None
    stream_sid: str = ""
    subscribers: list[Subscription] = field(default_factory=list)
    attached_at: float = field(default_factory=time.time)
    frames: int = 0


class MonitorHub:
    """Per-call fan-out of Twilio monitor frames to dashboard listeners."""

    def __init__(self, max_listeners: int = DEFAULT_MAX_LISTENERS, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self.max_listeners = max(1, int(max_listeners))
        self.queue_size = max(1, int(queue_size))
        self._channels: dict[str, _Channel] = {}

    # ── configuration ────────────────────────────────────────────

    def configure(self, settings: Any) -> None:
        """Apply `listen_in_max_listeners` from settings (idempotent)."""
        self.max_listeners = max(1, int(getattr(settings, "listen_in_max_listeners", DEFAULT_MAX_LISTENERS) or 1))

    # ── Twilio side ──────────────────────────────────────────────

    def attach_source(self, call_sid: str, source: Any, stream_sid: str = "") -> None:
        """Register the Twilio monitor socket for `call_sid`.

        A second attach for the same call (Twilio reconnect) replaces the
        source and keeps the existing subscribers.
        """
        channel = self._channels.get(call_sid)
        if channel is None:
            channel = _Channel(call_sid=call_sid)
            self._channels[call_sid] = channel
        channel.source = source
        if stream_sid:
            channel.stream_sid = stream_sid
        logger.info("Listen-in source attached [%s] (%d listener(s))", call_sid, len(channel.subscribers))

    def source_attached(self, call_sid: str) -> bool:
        channel = self._channels.get(call_sid)
        return channel is not None and channel.source is not None

    def publish(self, call_sid: str, track: str, payload_b64: str, ts: Any = None) -> int:
        """Fan one media frame out to every subscriber. Returns the number of
        frames dropped across subscribers (slow consumers).

        Hot path: no I/O, no persistence — frames only ever live in the
        per-subscriber queues.
        """
        channel = self._channels.get(call_sid)
        if channel is None or not payload_b64:
            return 0
        channel.frames += 1
        frame = {"type": "media", "track": track, "payload": payload_b64, "ts": ts}
        dropped_total = 0
        for sub in channel.subscribers:
            if sub.ended:
                continue
            sub.frames += 1
            if sub._put_dropping_oldest(frame):
                sub.dropped += 1
                dropped_total += 1
        if dropped_total:
            from pincer.observability.metrics import record_listen_frames_dropped

            record_listen_frames_dropped(dropped_total, track=track)
        return dropped_total

    def end(self, call_sid: str, reason: str = END_CALL_ENDED) -> int:
        """Source stopped / call ended: close every subscriber with `reason`.
        Returns the number of subscribers notified. Idempotent."""
        channel = self._channels.pop(call_sid, None)
        if channel is None:
            return 0
        notified = 0
        for sub in channel.subscribers:
            if not sub.ended:
                sub._finish(reason)
                notified += 1
        channel.subscribers.clear()
        channel.source = None
        logger.info("Listen-in source ended [%s] reason=%s listeners=%d", call_sid, reason, notified)
        return notified

    # ── Listener side ────────────────────────────────────────────

    async def subscribe(self, call_sid: str, user: str) -> Subscription:
        """Attach a listener. Raises `MonitorUnavailableError` when no source
        is attached and `ListenerCapacityError` at the per-call cap."""
        channel = self._channels.get(call_sid)
        if channel is None or channel.source is None:
            raise MonitorUnavailableError(call_sid)
        live = [s for s in channel.subscribers if not s.ended]
        if len(live) >= self.max_listeners:
            raise ListenerCapacityError(call_sid)
        sub = Subscription(call_sid=call_sid, user=user, queue=asyncio.Queue(maxsize=self.queue_size))
        channel.subscribers.append(sub)
        logger.info("Listen-in subscriber joined [%s] user=%s listeners=%d", call_sid, user, len(live) + 1)
        return sub

    def unsubscribe(self, sub: Subscription, reason: str = END_STOPPED) -> None:
        """Detach a listener (listener hung up or its socket errored)."""
        channel = self._channels.get(sub.call_sid)
        if channel is not None:
            channel.subscribers = [s for s in channel.subscribers if s is not sub]
        if not sub.ended:
            sub.ended = True
            sub.end_reason = reason

    # ── Introspection (API / tests) ──────────────────────────────

    def listener_count(self, call_sid: str) -> int:
        channel = self._channels.get(call_sid)
        if channel is None:
            return 0
        return sum(1 for s in channel.subscribers if not s.ended)

    def active_calls(self) -> list[str]:
        return [sid for sid, ch in self._channels.items() if ch.source is not None]

    def reset(self) -> None:
        for sid in list(self._channels):
            self.end(sid, END_ERROR)
        self._channels.clear()


# ── Module singleton ─────────────────────────────────────────────────

_hub: MonitorHub | None = None


def get_monitor_hub() -> MonitorHub:
    """The process-wide hub (created on first use)."""
    global _hub  # noqa: PLW0603
    if _hub is None:
        _hub = MonitorHub()
    return _hub


def configure_monitor_hub(settings: Any) -> MonitorHub:
    hub = get_monitor_hub()
    hub.configure(settings)
    return hub


def reset_monitor_hub_for_tests() -> None:
    global _hub  # noqa: PLW0603
    if _hub is not None:
        _hub.reset()
    _hub = None


def listen_in_enabled(settings: Any) -> bool:
    # `is True` on purpose: MagicMock settings in tests have every attribute.
    return settings is not None and getattr(settings, "listen_in_enabled", False) is True


# ── Twilio frame parsing (shared by the ingress handler and tests) ──


def parse_monitor_frame(msg: dict[str, Any]) -> tuple[str, str, str, Any] | None:
    """Reduce a Twilio Media Streams message to (event, track, payload, ts).

    Returns None for anything that is not a media frame. `event` is
    "media"; `track` is normalised to inbound/outbound (Twilio uses
    "inbound"/"outbound" on the media object and "inbound_track"/
    "outbound_track" in the start event's `tracks` list).
    """
    if str(msg.get("event", "")) != "media":
        return None
    media = msg.get("media") or {}
    payload = str(media.get("payload", "") or "")
    if not payload:
        return None
    track = str(media.get("track", TRACK_INBOUND) or TRACK_INBOUND).replace("_track", "")
    if track not in TRACKS:
        track = TRACK_INBOUND
    return "media", track, payload, media.get("timestamp")
