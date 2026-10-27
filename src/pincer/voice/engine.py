"""
Voice engine abstraction layer.

Provides a common interface for both ConversationRelay (Phase 1)
and Media Streams (Phase 2) Twilio integrations.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from pincer.voice.audio import MULAW_BYTES_PER_SECOND
from pincer.voice.twilio_rest import TwilioRest, outcome_unknown

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from pincer.config import Settings
    from pincer.observability.failure_codes import FailureCode

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _looks_like_base64(s: str) -> bool:
    """Heuristic: base64 strings are longer and use A-Za-z0-9+/=."""
    if len(s) < 20:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    return all(c in allowed or c.isspace() for c in s[:100])


class CallDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


# An answering machine's outgoing greeting is transcribed like any speech, so
# text alone can't distinguish "human replied to us" from "machine talked at
# us". Twilio's sync AMD verdict lands within the first seconds of the call;
# only speech after this window counts as a demonstrably conversing human.
AMD_GRACE_SECONDS = 10.0

#: Metadata key for WHY a call ended: a `FailureCode` value, set by whoever learns it.
END_CODE_KEY = "end_code"
#: Metadata key for the provider status that set `end_code`, when one did: the
#: report wording can be more specific than the code (`canceled` vs `failed`).
PROVIDER_STATUS_KEY = "provider_status"
#: Metadata key for the token of the call's latest <Dial>. `transfer_call` puts it
#: in the action URL (`?dial=…`, covered by the Twilio signature), so a dial
#: result names the dial it belongs to even when Twilio sends no DialCallSid.
#: Unlike the `transfer` record it survives a redirect that raised.
DIAL_ID_KEY = "dial_id"
#: Metadata key for one record per <Dial> the call has had, keyed by
#: `dial_key()`. `transfer_call` creates a dial's record and nothing replaces
#: it; /transfer-result writes the dial's result and answer into it. The
#: `transfer` key is the current dial's record (the same dict), not a copy.
DIALS_KEY = "dials"
#: Metadata key set on a call whose ConversationRelay events reach the
#: deprecated HTTP /relay-webhook: it has no media socket, so a transfer
#: redirect could never be told applied or not. Such a call is never transferred.
RELAY_WEBHOOK_KEY = "relay_webhook"
#: Metadata key on a pending outbound state: the CallSid a relay `setup`
#: claimed it as (see `claim_pending_outbound`). The dialer branches on this,
#: not on a live lookup — a claimed call may already be over.
CLAIMED_SID_KEY = "claimed_sid"

_ULAW_NS_PER_BYTE = 1_000_000_000 // MULAW_BYTES_PER_SECOND

# Per-utterance STT stamps left in call metadata (Media Streams). Cleared once
# the utterance is handed off; the channel snapshots the same keys first.
UTTERANCE_MARKERS = ("speech_end_ns", "speech_start_ns", "first_partial_ns")

# Media Streams barge-in: a VAD onset this soon after our audio started is
# ignored — line echo of our own first syllable, or the TTS onset click.
BARGE_IN_ARM_DELAY_MS = 250
# A VAD onset alone never stops the agent: a cough or a line click fires
# SpeechStarted too, and cancelling the turn on one drops the reply for good.
# Recognised words within this window confirm it — any number of words, which
# is what the onset buys over `interim` and its min-words threshold.
BARGE_IN_CONFIRM_WINDOW_MS = 1500

# How long a torn-down call stays recognisable to late status webhooks. Twilio
# retries a status callback for minutes, so this outlives the post-call
# pipeline rather than just the create_task hand-off.
_ENDED_CALL_TTL_S = 900.0
_MAX_ENDED_CALLS = 500

# Bound on the Twilio call-status fetch, tighter than twilio_rest's 10 s: it
# runs on the response path of a webhook Twilio waits ~15 s for.
CALL_STATUS_FETCH_TIMEOUT_S = 5.0
# How long after a timed-out transfer redirect the evidence is read: an update
# Twilio applies just after our timeout has closed the call's media socket by then.
UNCONFIRMED_REDIRECT_SETTLE_S = 5.0
# A redirect settled as not applied can still go live late; until its dial
# timeout plus this margin has passed, a closed media socket is not a hang-up
# but a reason to ask Twilio.
POSSIBLY_LIVE_MARGIN_S = 30.0
# When a timed-out redirect's evidence decided nothing (no socket known, Twilio
# unreachable, or the socket gone while Twilio still says in-progress): re-check
# Twilio's call status after these delays, and finalize on a terminal one.
TRANSFER_CONFIRM_BACKOFF_S: tuple[float, ...] = (10.0, 30.0, 60.0)
# A terminal call status says the call is over, not how. Twilio posts a dial's
# action (/transfer-result) before the call's final status, so wait this long
# for one already on its way to decide before finalizing on the status alone.
TERMINAL_STATUS_GRACE_S = 2.0
# Call statuses after which nobody is on the line any more.
_ENDED_CALL_STATUSES = frozenset({"completed", "canceled", "failed", "busy", "no-answer"})


@dataclass
class CallState:
    """Runtime state for an active voice call."""

    call_sid: str
    direction: CallDirection
    caller_number: str
    target_number: str = ""
    target_name: str = ""
    purpose: str = ""
    instructions: str = ""  # extra guidance from the user, shown to the agent on the call
    language: str = "en"
    engine_type: str = "conversation_relay"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    user_id: str = ""
    pincer_user_id: str = ""
    session_id: str = ""
    recording_consent: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> int:
        end = self.ended_at or datetime.now(UTC)
        return int((end - self.started_at).total_seconds())

    def mark_caller_spoke(self) -> None:
        """Flag a demonstrably conversing counterpart — shields the call from
        late/false AMD "machine" verdicts in the status webhook. Speech inside
        the AMD grace window does not count: a voicemail greeting is
        transcribed too, and it must not suppress the AMD hangup."""
        if (datetime.now(UTC) - self.started_at).total_seconds() > AMD_GRACE_SECONDS:
            self.metadata["caller_spoke"] = True


def set_end_cause(state: CallState, code: FailureCode | str) -> None:
    """Record why the call ended. First writer wins: the earliest authoritative
    signal (Twilio status, AMD) must not be overwritten by a later generic one
    (our own end_call, shutdown)."""
    state.metadata.setdefault(END_CODE_KEY, str(code))


@dataclass
class _SpeechProgress:
    """How much of one utterance has reached Twilio.

    Shared across the synthesis attempt and its retry so the retry resumes
    instead of repeating. Segment-level rather than byte-level: neural TTS is
    not byte-reproducible, so re-synthesizing and skipping N bytes would cut
    mid-phoneme rather than resume cleanly.
    """

    segments_done: int = 0
    wrote_audio: bool = False
    first_chunk_ms: float | None = None
    #: Failed synthesis attempts so far. Spans are keyed on it so a resumed
    #: retry shows up as its own bar rather than silently extending the first.
    attempts: int = 0
    #: The call's speech epoch when the utterance started. A barge-in bumps
    #: the epoch, which stops this utterance (and only this call's) mid-stream.
    epoch: int = 0
    #: A barge-in stopped the utterance before all of it was sent.
    cut: bool = False


class VoiceEngine(ABC):
    """Abstract interface for voice call handling.

    Both ConversationRelay and Media Streams implement this so the
    agent brain, state machine, and compliance layers are engine-agnostic.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._active_calls: dict[str, CallState] = {}
        self._on_speech_callback: Callable | None = None
        self._on_call_end_callback: Callable | None = None
        # Sprint 12: lets the channel start per-call tracking (silence rule,
        # receptionist session) the moment a call is registered, not on the
        # first caller utterance.
        self._on_call_start_callback: Callable | None = None
        # The caller talked over the agent: lets the channel cancel the turn
        # that is speaking. See on_caller_barge_in().
        self._on_barge_in_callback: Callable | None = None
        # The channel owns the call's phase: it moves it out of TRANSFERRING
        # when end_transfer hands the call back, so flag and phase agree.
        self._on_transfer_end_callback: Callable | None = None
        # Told how a timed-out transfer redirect settled (see
        # _settle_unconfirmed_redirect): the channel speaks or records it.
        self._on_transfer_settled_callback: Callable | None = None
        # call_sid -> the pending settle of its unconfirmed transfer redirect
        self._transfer_settles: dict[str, asyncio.Task[None]] = {}
        # Outbound calls briefed and registered BEFORE the Twilio REST dial,
        # keyed by a temporary id until the real CallSid comes back. See
        # register_pending_outbound().
        self._pending_outbound: dict[str, CallState] = {}
        # call_sid -> monotonic time of teardown. See was_recently_ended().
        self._ended_calls: dict[str, float] = {}
        # Optional VoiceMetricsRegistry, injected by VoiceChannel.set_engine
        self.metrics_registry: Any = None
        # Hangup, transfer, DTMF, fallback <Say>: awaited, never blocking the loop.
        self._rest = TwilioRest(settings)

    def set_on_speech(self, callback: Callable) -> None:
        self._on_speech_callback = callback

    def set_on_call_end(self, callback: Callable) -> None:
        self._on_call_end_callback = callback

    def set_on_call_start(self, callback: Callable) -> None:
        self._on_call_start_callback = callback

    def set_on_barge_in(self, callback: Callable) -> None:
        self._on_barge_in_callback = callback

    async def on_caller_barge_in(self, call_sid: str, heard: str | None = None) -> None:
        """The caller started speaking over the agent.

        The channel cancels the in-flight turn and calls `interrupt_speech`
        once; with no channel wired, the audio is at least silenced. `heard` is
        what the provider reports the caller heard before cutting in, when it
        reports that (ConversationRelay); None when it does not.
        """
        if self._on_barge_in_callback:
            await self._on_barge_in_callback(call_sid, heard)
        else:
            await self.interrupt_speech(call_sid)

    def set_on_transfer_end(self, callback: Callable) -> None:
        self._on_transfer_end_callback = callback

    def set_on_transfer_settled(self, callback: Callable) -> None:
        """``callback(call_sid, outcome)`` once a timed-out transfer redirect is
        settled: ``not_applied`` (the caller is still with us and needs the
        apology), ``hung_up`` (record the abandonment; the engine finalizes the
        call afterwards), ``applied`` or ``unknown``."""
        self._on_transfer_settled_callback = callback

    def pending_transfer_settle(self, call_sid: str) -> asyncio.Task[None] | None:
        """The transfer check (settle or re-checks) still running for this call, if any."""
        task = self._transfer_settles.get(call_sid)
        return task if task is not None and not task.done() else None

    @abstractmethod
    async def on_call_start(
        self,
        call_sid: str,
        caller: str,
        direction: CallDirection,
        target_number: str = "",
        target_name: str = "",
        purpose: str = "",
        language: str = "",
        instructions: str = "",
    ) -> CallState: ...

    @abstractmethod
    async def on_speech_input(self, call_sid: str, text_or_audio: Any) -> None: ...

    @abstractmethod
    async def send_speech(self, call_sid: str, text_or_audio: Any, *, last: bool = True) -> bool:
        """Speak on the call. Returns True only when the audio was actually
        handed to the transport — a False return means the caller heard
        nothing (transcripts must record that honestly).

        ``last=False`` marks a partial utterance in a streamed turn (Sprint 5):
        ConversationRelay buffers tokens until a ``last=True`` token closes the
        reply; engines with per-utterance synthesis ignore the flag."""
        ...

    @abstractmethod
    async def interrupt_speech(self, call_sid: str, *, by_caller: bool = True) -> None:
        """Silence the agent. ``by_caller=False`` is our own cut (shutdown):
        the audio stops, but no caller interruption is counted."""

    async def wait_for_playout(self, call_sid: str) -> None:
        """Return once the speech already sent for the call has been heard.
        `send_speech` returning means Twilio HAS the audio, not that the
        caller heard it. Engines that hand Twilio text have nothing to wait
        for; Media Streams sleeps out its playout estimate."""
        return None

    async def transfer_call(
        self,
        call_sid: str,
        target_number: str,
        *,
        timeout_s: int = 30,
        action_url: str = "",
        announce: str = "",
        language: str = "",
    ) -> None:
        """Redirect the live call into <Dial>. With ``action_url`` (Sprint 12
        receptionist) Twilio reports the dial result there, so a busy / no-answer
        target can fall back to message-taking instead of dropping the caller.

        The bridged call is no longer ours to time, so we attach a status
        callback: inbound calls get none by default, and without it nothing
        would tell us when the caller and the human hang up. ``timeLimit`` is
        the ceiling for the bridge.

        Each dial gets a token (`DIAL_ID_KEY`) appended to ``action_url``, so
        /transfer-result can tell this dial's result from a late copy of an
        earlier one's.

        Raises ValueError for an ``action_url`` with a ``#`` fragment, before
        anything is marked: that is where Twilio connection overrides go, and
        read-timeout retries (``#rp=all``, ``#rt=…``) would let Twilio execute a
        copy of a dial result after the first, which /transfer-result's replay
        answers rule out. The dial token appended after it would be lost too."""
        if "#" in action_url:
            raise ValueError(f"transfer action_url must not carry a '#' connection override: {action_url!r}")
        dial_id = uuid.uuid4().hex
        if action_url:
            action_url = f"{action_url}{'&' if '?' in action_url else '?'}{urlencode({'dial': dial_id})}"
        # Built before anything is marked: a <Dial> that cannot be built
        # leaves no trace of a transfer.
        twiml = build_dial_twiml(
            target_number,
            timeout_s=timeout_s,
            action_url=action_url,
            announce=announce,
            language=language,
            time_limit_s=int(self._settings.voice_transfer_max_duration_s),
        )
        update: dict[str, Any] = {"twiml": twiml}
        base = str(getattr(self._settings, "voice_webhook_base_url", "") or "").strip().rstrip("/")
        if base.startswith(("http://", "https://")):
            update["status_callback"] = f"{base}/api/apps/twilio/status"
            update["status_callback_method"] = "POST"
        # the media socket closes on the <Dial> redirect; that is not a hangup
        live = self._active_calls.get(call_sid)
        media_before = live.metadata.get("websocket") if live is not None else None
        record: dict[str, Any] = {
            "target": target_number,
            "started_mono": time.monotonic(),
            "dial_timeout_s": timeout_s,
            "dial_id": dial_id,
        }
        if live is not None:
            live.metadata["transferring"] = True
            live.metadata[DIAL_ID_KEY] = dial_id
            live.metadata.setdefault(DIALS_KEY, {})[dial_key(dial_id, "")] = record
            live.metadata["transfer"] = record
        try:
            await self._rest.update_call(call_sid, **update)
            logger.info("Call %s transferred to %s", call_sid, target_number)
        except Exception as exc:
            if outcome_unknown(exc) and live is not None:
                # Twilio may have applied it (the same rule as a dial whose
                # outcome is unknown). Keep the call in flight and settle it
                # from evidence once the turn that asked for it has returned:
                # the media socket's close needs the transport loop to see it.
                reason = type(exc).__name__
                logger.warning("Transfer redirect for call %s unconfirmed (%s) — settling", call_sid, reason)
                record["redirect_unconfirmed"] = True
                self._track_transfer_task(call_sid, self._settle_unconfirmed_redirect(call_sid, record, media_before))
                return
            logger.exception("Transfer failed for call %s", call_sid)
            # the redirect did not happen: a definite error, or the media
            # socket is still up. The call is still ours. DIAL_ID_KEY and the dial's
            # record stay: a redirect can raise although Twilio applied it,
            # and then this dial's result still arrives and must be recognised.
            record["redirect_failed"] = True
            if live is not None:
                live.metadata.pop("transferring", None)
                live.metadata.pop("transfer", None)
            raise
        note_transfer_started(call_sid, record)

    async def _settle_unconfirmed_redirect(self, call_sid: str, record: dict[str, Any], media_before: Any) -> None:
        """Decide a timed-out transfer redirect once the turn that asked for it
        has returned, from evidence (the call status alone cannot tell: it is
        ``in-progress`` whether or not the redirect applied):

        - the call's own media socket is still open: not applied, since applying
          it ends the <Connect>/<Stream>. The call is ours again;
        - Twilio reports the call over: the caller hung up. Finalized;
        - Twilio reports it in progress and the socket we had is gone: applied;
        - anything else (no socket was known, Twilio cannot be asked): unknown.
          /transfer-result or /status decides, the stuck-call reaper bounds it.

        A dial result that arrived meanwhile has decided already. The decision
        runs under the call's lock (`one_call_update_at_a_time`, shared with
        /transfer-result and /status) and is re-checked after every await, so
        it never acts on a dial one of them is deciding. A caller turn during
        the wait is swallowed (the phase is TRANSFERRING)."""
        from pincer.voice.call_locks import one_call_update_at_a_time

        hung_up = False
        try:
            await asyncio.sleep(UNCONFIRMED_REDIRECT_SETTLE_S)
            async with one_call_update_at_a_time(call_sid):
                live = self._unsettled_dial(call_sid, record)
                if live is None:
                    return
                outcome = await self._unconfirmed_redirect_outcome(call_sid, live, media_before)
                if self._unsettled_dial(call_sid, record) is None:
                    return  # decided while Twilio was asked
                logger.info("Unconfirmed transfer redirect for call %s settled: %s", call_sid, outcome)
                if outcome == "hung_up":
                    hung_up = True
                elif outcome == "not_applied":
                    record["redirect_failed"] = True
                    dial_timeout_s = float(record.get("dial_timeout_s") or 0)
                    record["possibly_live_until"] = time.monotonic() + dial_timeout_s + POSSIBLY_LIVE_MARGIN_S
                    # The call is ours again BEFORE the apology is spoken: a
                    # caller hanging up during it must end the call, not be
                    # ignored as a transfer's closing socket.
                    await self.end_transfer(call_sid)
                if outcome != "hung_up" and self._on_transfer_settled_callback is not None:
                    try:
                        await self._on_transfer_settled_callback(call_sid, outcome)
                    except Exception:
                        logger.exception("transfer-settled callback failed [%s]", call_sid)
                if outcome == "not_applied":
                    return
            if hung_up:
                # the caller hung up before any <Dial>: unless a dial result
                # on its way says otherwise
                await self._finish_after_grace(call_sid, record, reason="caller_hangup_during_transfer", abandoned=True)
                return
            # "applied" (the socket is gone while Twilio says in-progress: a
            # transfer, or a hang-up Twilio has not reported yet) and "unknown"
            # are not decided: ask Twilio again.
            await self._confirm_transfer(call_sid, record)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Settling the transfer redirect failed [%s]", call_sid)

    async def _confirm_transfer(self, call_sid: str, record: dict[str, Any]) -> None:
        """Re-check Twilio's call status for a transfer the evidence could not
        decide, after each of TRANSFER_CONFIRM_BACKOFF_S: a terminal status
        means the caller is gone (finalize); in-progress or no answer, keep
        waiting. A dial result or the call's end stops it. After the last
        re-check the call stays in flight; the stuck-call reaper bounds it."""
        from pincer.voice.call_locks import one_call_update_at_a_time

        for delay in TRANSFER_CONFIRM_BACKOFF_S:
            await asyncio.sleep(delay)
            async with one_call_update_at_a_time(call_sid):
                if self._unsettled_dial(call_sid, record) is None:
                    return
                try:
                    status = await self.fetch_call_status(call_sid)
                except Exception:
                    logger.warning("Transfer for call %s: Twilio status re-check failed", call_sid, exc_info=True)
                    continue
                if self._unsettled_dial(call_sid, record) is None:
                    return
            if status not in _ENDED_CALL_STATUSES:
                continue
            logger.info("Transfer for call %s: Twilio reports %r — the call is over", call_sid, status)
            # A terminal status does not say how it ended: a plain hang-up for a
            # close Twilio could not explain at the time, else neutral.
            reason = "" if record.get("close_unexplained") else "transfer_outcome_unknown"
            await self._finish_after_grace(call_sid, record, reason=reason, abandoned=False)
            return
        logger.warning("Transfer for call %s still unconfirmed after the re-checks — left in flight", call_sid)

    async def _finish_after_grace(self, call_sid: str, record: dict[str, Any], *, reason: str, abandoned: bool) -> None:
        """Twilio says the call is over. Give a dial result already on its way
        (Twilio posts the dial's action before the call's final status)
        TERMINAL_STATUS_GRACE_S to decide first; if none did, finalize with
        ``reason``, recording the transfer as abandoned only when ``abandoned``."""
        from pincer.voice.call_locks import one_call_update_at_a_time

        await asyncio.sleep(TERMINAL_STATUS_GRACE_S)
        async with one_call_update_at_a_time(call_sid):
            if self._unsettled_dial(call_sid, record) is None:
                return  # a dial result decided
            if abandoned and self._on_transfer_settled_callback is not None:
                try:
                    await self._on_transfer_settled_callback(call_sid, "hung_up")
                except Exception:
                    logger.exception("transfer-settled callback failed [%s]", call_sid)
            await self.finish_remote_ended(call_sid, reason=reason)

    def _track_transfer_task(self, call_sid: str, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Run one of the call's transfer checks (the settle, the re-checks) as
        its only one: a previous one still running is cancelled, the task leaves
        the slot when it ends, and ending the call cancels it (_unregister_call)."""
        previous = self._transfer_settles.get(call_sid)
        if previous is not None and previous is not asyncio.current_task() and not previous.done():
            previous.cancel()
        task = asyncio.create_task(coro, name=f"voice-transfer-{call_sid}")
        self._transfer_settles[call_sid] = task

        def _forget(done: asyncio.Task[None]) -> None:
            if self._transfer_settles.get(call_sid) is done:
                self._transfer_settles.pop(call_sid, None)

        task.add_done_callback(_forget)
        return task

    def _unsettled_dial(self, call_sid: str, record: dict[str, Any]) -> CallState | None:
        """The live call, if ``record`` is still its current dial, in flight and
        without a result; else None (ended, or decided by a dial result)."""
        live = self._active_calls.get(call_sid)
        if live is None or not live.metadata.get("transferring") or live.metadata.get("transfer") is not record:
            return None
        return None if "dial_status" in record else live

    async def _unconfirmed_redirect_outcome(self, call_sid: str, live: CallState, media_before: Any) -> str:
        media_now = live.metadata.get("websocket")
        if media_before is not None and media_now is media_before and _socket_open(media_now):
            return "not_applied"
        try:
            status = await self.fetch_call_status(call_sid)
        except Exception:
            logger.warning("Transfer redirect for call %s: Twilio status check failed", call_sid, exc_info=True)
            return "unknown"
        if status in _ENDED_CALL_STATUSES:
            return "hung_up"
        if status == "in-progress" and media_before is not None and media_now is None:
            return "applied"
        return "unknown"

    async def fetch_call_status(self, call_sid: str) -> str:
        """Twilio's current status of the call (``in-progress``, ``completed``,
        …), through the engine's async REST session and bounded by
        ``CALL_STATUS_FETCH_TIMEOUT_S``. Raises on any Twilio or network error;
        the caller decides what an unknown status means."""
        return await self._rest.fetch_call_status(call_sid, timeout=CALL_STATUS_FETCH_TIMEOUT_S)

    async def end_call(self, call_sid: str) -> None:
        try:
            await self._rest.update_call(call_sid, status="completed")
        except Exception:
            logger.exception("Failed to end call %s", call_sid)
        finally:
            state = await self._unregister_call(call_sid)
            if state and self._on_call_end_callback:
                await self._on_call_end_callback(call_sid, state)

    async def send_dtmf(self, call_sid: str, digits: str) -> None:
        try:
            twiml = f'<Response><Play digits="{digits}"/></Response>'
            await self._rest.update_call(call_sid, twiml=twiml)
            logger.info("DTMF sent [%s]: %s", call_sid, digits)
        except Exception:
            logger.exception("DTMF failed for call %s", call_sid)
            raise

    async def create_call(self, **kwargs: Any) -> Any:
        """Place an outbound call on this engine's REST session."""
        return await self._rest.create_call(**kwargs)

    async def find_call(self, *, to: str, from_: str, since: datetime) -> Any | None:
        """Look up a call whose `create_call` timed out (TwilioRest.find_call)."""
        return await self._rest.find_call(to=to, from_=from_, since=since)

    async def aclose(self) -> None:
        """Release the Twilio REST session (VoiceChannel.stop)."""
        await self._rest.aclose()

    def get_call_state(self, call_sid: str) -> CallState | None:
        return self._active_calls.get(call_sid)

    @abstractmethod
    async def close_media_stream(self, call_sid: str) -> None:
        """Override in MediaStreamEngine to close STT stream and consumer."""

    async def end_transfer(self, call_sid: str) -> None:
        """The <Dial> is over and the call is ours again (failed transfer →
        reconnect). The one place the ``transferring`` flag is cleared once
        ``transfer_call`` succeeded: from here the call counts as an AI call for
        the stuck-call reaper and ``on_media_closed``, and the channel takes its
        phase out of TRANSFERRING (untimed) so the phase timeouts apply again."""
        state = self._active_calls.get(call_sid)
        if state is None or not state.metadata.pop("transferring", None):
            return
        if self._on_transfer_end_callback is not None:
            await self._on_transfer_end_callback(call_sid)

    async def on_media_closed(self, call_sid: str) -> None:
        """The media WebSocket closed: the call is over on Twilio's side.

        Inbound calls get no status callback unless one is configured in the
        console, so this is their end signal. No-op if we already ended the
        call or it is being transferred; never issues a REST hangup.
        """
        state = self._active_calls.get(call_sid)
        if state is None or state.metadata.get("transferring"):
            return
        record = state.metadata.get("transfer")
        if isinstance(record, dict) and self._possibly_live(state, record):
            await self._media_closed_while_possibly_live(call_sid, record)
            return
        logger.info("Media session closed by Twilio [%s] — ending call", call_sid)
        await self.finish_remote_ended(call_sid)

    @staticmethod
    def _possibly_live(state: CallState, record: Any) -> bool:
        """A redirect settled as not applied that could still go live: its
        dial has no result and its dial timeout plus margin has not passed."""
        return (
            not state.metadata.get("transferring")
            and isinstance(record, dict)
            and "dial_status" not in record
            and float(record.get("possibly_live_until") or 0) > time.monotonic()
        )

    async def _media_closed_while_possibly_live(self, call_sid: str, record: dict[str, Any]) -> None:
        """The media closed for a call whose redirect was settled as not
        applied: the caller hung up, or the redirect went live late (the caller
        is in the <Dial>). Ask Twilio once, now, under the call's lock:

        - the call is over: a plain remote end (no transfer outcome recorded);
        - in progress: a late apply. Back to transferring (and the untimed
          phase, so no watchdog hangs up a bridged call); the dial's result or
          the re-checks decide;
        - Twilio cannot be asked: held the same way, but a terminal status a
          re-check finds later is still the plain hang-up it most likely was."""
        from pincer.voice.call_locks import one_call_update_at_a_time

        async with one_call_update_at_a_time(call_sid):
            state = self._active_calls.get(call_sid)
            if state is None or state.metadata.get("transfer") is not record or not self._possibly_live(state, record):
                return  # ended, or decided, while waiting for the lock
            try:
                status: str | None = await self.fetch_call_status(call_sid)
            except Exception:
                logger.warning("Media closed [%s] in a possibly live transfer: Twilio status check failed", call_sid)
                status = None
            state = self._active_calls.get(call_sid)
            if state is None or not self._possibly_live(state, record):
                return
            if status in _ENDED_CALL_STATUSES:
                logger.info("Media session closed by Twilio [%s] — ending call", call_sid)
                await self.finish_remote_ended(call_sid)
                return
            if status != "in-progress":
                record["close_unexplained"] = True
            logger.warning("Media closed [%s] for a transfer given up as not applied — held as a late apply", call_sid)
            state.metadata["transferring"] = True
            record["applied_late"] = True
            if self._on_transfer_settled_callback is not None:
                try:
                    await self._on_transfer_settled_callback(call_sid, "applied_late")
                except Exception:
                    logger.exception("transfer-settled callback failed [%s]", call_sid)
        self._track_transfer_task(call_sid, self._confirm_transfer(call_sid, record))

    async def finish_remote_ended(self, call_sid: str, reason: str = "") -> bool:
        """Twilio says the call is over: close media, unregister, run the
        end-of-call callback. Never issues a REST hangup. Idempotent — returns
        False if the call was already finalized.

        ``reason`` is a technical code for the trace. It is not ``end_reason``:
        that one is localized text the post-call report shows to the user."""
        state = self._active_calls.get(call_sid)
        if state is None:
            return False
        if reason:
            state.metadata.setdefault("remote_end_reason", reason)
        with contextlib.suppress(Exception):
            await self.close_media_stream(call_sid)
        ended = await self._unregister_call(call_sid)
        if ended and self._on_call_end_callback:
            await self._on_call_end_callback(call_sid, ended)
        return ended is not None

    def get_active_calls(self) -> dict[str, CallState]:
        return dict(self._active_calls)

    async def fallback_and_end(self, call_sid: str) -> None:
        """TTS is broken mid-call: apologize via Twilio's own <Say> (basic TTS,
        always available) and end the call cleanly — never dead air. Shared by
        both engines (Media Streams synthesis failure, ConversationRelay
        repeated 64111 token-to-speech errors)."""
        state = self._active_calls.get(call_sid)
        if not state:
            return

        from pincer.voice.language import de_formality, relay_language
        from pincer.voice.prompts import get_prompt

        apology = get_prompt("TTS_FAILURE_GOODBYE", state.language, de_formality(self._settings))
        try:
            twiml = f'<Response><Say language="{relay_language(state.language)}">{apology}</Say><Hangup/></Response>'
            await self._rest.update_call(call_sid, twiml=twiml)
        except Exception:
            logger.exception("TTS fallback <Say> failed [%s] — ending call directly", call_sid)
            with contextlib.suppress(Exception):
                await self.end_call(call_sid)
            return

        # Twilio speaks the apology and hangs up; clean up our side now.
        await self.finish_remote_ended(call_sid)

    # ── Outbound registration, before the dial ────────────
    #
    # The briefing used to be registered AFTER `client.calls.create()`
    # returned. Twilio can connect the ConversationRelay socket before that
    # call even returns, and the setup handler that then found no state
    # invented a fresh INBOUND one — an unbriefed generic persona on a call
    # the user had given a task. The briefed state was orphaned, and nothing
    # anywhere reported a problem.
    #
    # So the state is now built first, parked under a temporary key, and
    # re-keyed to the real CallSid when the REST call returns. A setup that
    # arrives in between waits for the promotion (await_call_state) instead of
    # improvising.

    async def register_pending_outbound(
        self,
        briefing: Any,
        target_number: str,
        language: str = "",
        instructions: str = "",
    ) -> CallState:
        """Register a briefed outbound call before it is dialled.

        Returns a CallState under a temporary ``pending:<uuid>`` key. It is NOT
        in `_active_calls` and the on_call_start callback has NOT fired: the
        per-call machinery (state machine, transcript, thread context) is keyed
        by CallSid, and starting it under a temporary id would produce tracking
        for a call that may never exist.
        """
        import uuid

        from pincer.voice.language import resolve_call_language

        pending_sid = f"pending:{uuid.uuid4().hex}"
        state = CallState(
            call_sid=pending_sid,
            direction=CallDirection.OUTBOUND,
            caller_number=target_number,
            target_number=target_number,
            target_name=str(getattr(briefing, "target_name", "") or ""),
            purpose=str(getattr(briefing, "task", "") or ""),
            instructions=instructions or str(getattr(briefing, "instructions", "") or ""),
            language=resolve_call_language(self._settings, language),
            engine_type=self.engine_name,
        )
        state.metadata["briefing"] = briefing
        self._pending_outbound[pending_sid] = state
        logger.info("Outbound call pre-registered %s -> %s", pending_sid, target_number)
        return state

    async def promote_pending(self, pre_state: CallState, call_sid: str) -> CallState | None:
        """Re-key a pre-registered outbound call to its real CallSid.

        Publishing the state is all this does — a relay `setup` racing the
        promotion then finds the briefed call instead of inventing an
        unbriefed one. The call is still RINGING at this point, so no clock
        starts here; see mark_call_answered(). Idempotent: a second promotion
        of the same pending state is a no-op that returns the live call.
        """
        if not call_sid:
            return None
        pending = self._pending_outbound.pop(pre_state.call_sid, None)
        state = pending or self._active_calls.get(call_sid)
        if state is None:
            return self._active_calls.get(call_sid)
        state.call_sid = call_sid
        self._active_calls[call_sid] = state
        logger.info("Outbound call promoted to %s (language=%s)", call_sid, state.language)
        # NOTE: the on_call_start callback is deliberately NOT fired here.
        # Promotion happens the moment the REST dial returns — the phone is
        # still ringing. Starting per-call tracking now would start the
        # conversation phase clock during the ring, and the watchdog would
        # speak its timeout goodbye and hang up before the callee had even
        # answered. Tracking starts in mark_call_answered().
        return state

    async def mark_call_answered(self, call_sid: str) -> CallState | None:
        """The callee picked up: start per-call tracking, once.

        This is the moment every clock should start from — the state machine's
        phase timeouts, the talk-time accumulator, the call-started metric. For
        a pre-registered outbound call the state has existed since before the
        dial, so "registered" and "answered" are different events and only this
        one means a conversation is happening.

        Idempotent: a `setup` that arrives again (coming back from a <Dial>
        transfer, or a reconnect) must not restart the clocks.
        """
        state = self._active_calls.get(call_sid)
        if state is None or state.metadata.get("answered_at") is not None:
            return state

        answered = datetime.now(UTC)
        state.metadata["answered_at"] = answered
        from pincer.voice.telemetry import hooks as telemetry

        telemetry.call_answered(call_sid, engine=self.engine_name, direction=str(state.direction))
        # An outbound state exists from before client.calls.create(), so
        # started_at has been running through the whole ring. Re-anchor it to
        # the pickup, keeping the dial time for diagnostics. Both things that
        # read started_at break otherwise: duration_seconds bills and reports
        # the ring as conversation, and the AMD grace window in
        # mark_caller_spoke() is already spent at pickup on any call that rang
        # longer than AMD_GRACE_SECONDS — so a voicemail greeting sets
        # caller_spoke and disarms the AMD shield in _handle_amd_verdict.
        state.metadata.setdefault("dialed_at", state.started_at)
        state.started_at = answered
        # Ringing is not silence: the talk-time clock is re-anchored here so
        # the wait for an answer does not land in the call's silence figure.
        from pincer.voice.analytics import TalkTimeAccumulator

        state.metadata["talktime"] = TalkTimeAccumulator(method=self.analytics_method)

        if self._on_call_start_callback is not None:
            try:
                await self._on_call_start_callback(call_sid, state)
            except Exception:
                logger.exception("on_call_start callback failed [%s]", call_sid)
        return state

    async def claim_pending_outbound(self, call_sid: str, target_number: str) -> CallState | None:
        """Bind a relay `setup` to the pending dial it belongs to, by callee number.

        Twilio can connect the callee before it has even answered the REST
        POST: the dial is still awaited in make_phone_call, the CallSid is not
        known on our side, and promote_pending() cannot run yet. The `to` of
        the setup message is the one key both sides share before that, so the
        setup claims the pending dial to that number and promotes it itself —
        only when exactly one is pending; with several it cannot tell them
        apart. The later REST return (or lookup) then finds the promotion
        already done. Telemetry is re-keyed here for the same reason: the
        answer/media events for the real SID follow immediately.
        """
        number = str(target_number or "").strip()
        if not call_sid or not number:
            return None
        matches = [pending for pending in self._pending_outbound.values() if pending.target_number == number]
        if len(matches) != 1:
            if matches:
                # Two dials to one callee (a double request, a retry next to a
                # chat request): the number cannot say which this setup is,
                # and a wrong guess runs each call with the other's task. The
                # REST return binds each one by its own SID instead.
                logger.warning("%d pending dials to %s — setup %s not claimed", len(matches), number, call_sid)
            return None
        pending = matches[0]
        pending_key = pending.call_sid
        pending.metadata[CLAIMED_SID_KEY] = call_sid
        state = await self.promote_pending(pending, call_sid)
        if state is not None:
            from pincer.voice.telemetry import hooks as telemetry

            telemetry.dial_accepted(pending_key, call_sid)
            logger.info("Relay setup claimed pending outbound %s as %s", pending_key, call_sid)
        return state

    def discard_pending(self, pre_state: CallState) -> None:
        """Drop a pre-registration whose dial never happened (Twilio raised)."""
        if self._pending_outbound.pop(pre_state.call_sid, None) is not None:
            logger.info("Outbound pre-registration discarded: %s", pre_state.call_sid)

    async def await_call_state(self, call_sid: str, timeout: float = 3.0, interval: float = 0.1) -> CallState | None:
        """Wait up to `timeout` for a call's state to be registered.

        For the setup-before-registration race: the dial is in flight and the
        promotion is microseconds away, so a short poll turns a lost briefing
        into a slightly later one.
        """
        state = self._active_calls.get(call_sid)
        if state is not None or timeout <= 0:
            return state
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(interval)
            state = self._active_calls.get(call_sid)
            if state is not None:
                logger.info("Outbound state for %s arrived after the relay setup", call_sid)
                return state
        return None

    async def _register_call(
        self,
        call_sid: str,
        caller: str,
        direction: CallDirection,
        target_number: str = "",
        target_name: str = "",
        purpose: str = "",
        language: str = "",
        instructions: str = "",
    ) -> CallState:
        from pincer.voice.language import resolve_call_language

        state = CallState(
            call_sid=call_sid,
            direction=direction,
            caller_number=caller,
            target_number=target_number,
            target_name=target_name,
            purpose=purpose,
            instructions=instructions,
            language=resolve_call_language(self._settings, language),
            engine_type=self.engine_name,
        )
        self._active_calls[call_sid] = state
        # This path registers a call that is already live (inbound, or an
        # outbound state recovered at setup), so it counts as answered and
        # fires the callback below; mark_call_answered() then no-ops.
        state.metadata["answered_at"] = datetime.now(UTC)
        # Conversation analytics start counting the moment the call exists.
        # `analytics_method` is a per-engine class attribute, so the record
        # carries its own provenance rather than being labelled later by
        # whoever happens to read it.
        from pincer.voice.analytics import ensure_accumulator

        ensure_accumulator(state, self.analytics_method)
        logger.info("Call registered: %s (%s) from %s, language=%s", call_sid, direction, caller, state.language)
        if self._on_call_start_callback is not None:
            try:
                await self._on_call_start_callback(call_sid, state)
            except Exception:
                logger.exception("on_call_start callback failed [%s]", call_sid)
        return state

    async def _unregister_call(self, call_sid: str) -> CallState | None:
        state = self._active_calls.pop(call_sid, None)
        settle = self._transfer_settles.pop(call_sid, None)
        if settle is not None and settle is not asyncio.current_task():
            settle.cancel()  # the call is over: nothing left to settle
        if state:
            state.ended_at = datetime.now(UTC)
            self._remember_ended(call_sid)
            logger.info(
                "Call ended: %s duration=%ds",
                call_sid,
                state.duration_seconds,
            )
        return state

    def _remember_ended(self, call_sid: str) -> None:
        """Record that this call HAD state and has been torn down.

        Teardown schedules the post-call pipeline as a background task, so for
        a moment afterwards `get_call_state()` returns None for a call that is
        very much accounted for. A status webhook arriving in that window must
        not read the miss as "this call never connected".
        """
        self._ended_calls[call_sid] = time.monotonic()
        cutoff = time.monotonic() - _ENDED_CALL_TTL_S
        for sid in [s for s, at in self._ended_calls.items() if at < cutoff]:
            self._ended_calls.pop(sid, None)
        while len(self._ended_calls) > _MAX_ENDED_CALLS:
            self._ended_calls.pop(next(iter(self._ended_calls)), None)

    def was_recently_ended(self, call_sid: str) -> bool:
        """Whether `end_call` already tore this call down.

        `end_call` always owns the final user message from that point on —
        either through the post-call pipeline it schedules or through the
        fallback status it sends directly — so a caller that finds no live
        state should stay quiet rather than send its own generic ending.
        """
        started = self._ended_calls.get(call_sid)
        return started is not None and (time.monotonic() - started) < _ENDED_CALL_TTL_S

    @property
    @abstractmethod
    def engine_name(self) -> str: ...

    # Talk-time provenance for this engine (analytics.METHOD_EXACT/ESTIMATED).
    # Media Streams sees real μ-law byte counts and STT word timings;
    # ConversationRelay hands audio to Twilio and never reports playout, so
    # its numbers can only ever be estimates from character counts.
    analytics_method: str = "estimated"


def note_transfer_started(call_sid: str, record: dict[str, Any]) -> None:
    """Record the transfer start once its redirect is confirmed, once per dial."""
    if record.get("started_emitted"):
        return
    record["started_emitted"] = True
    from pincer.voice.telemetry import hooks as telemetry

    telemetry.transfer_started(
        call_sid,
        target=str(record.get("target", "")),
        dial_timeout_s=record.get("dial_timeout_s"),
        recovered=bool(record.get("recovered")),
    )


def _socket_open(ws: Any) -> bool:
    """Whether a media WebSocket is still connected (Starlette's client state)."""
    return getattr(getattr(ws, "client_state", None), "name", "") == "CONNECTED"


def dial_key(dial_id: str, dial_call_sid: str) -> str:
    """What identifies a dial and its result in `DIALS_KEY`: our own dial token
    (from the action URL) when there is one, else Twilio's DialCallSid, which is
    empty when the dial never created a call (an invalid or unreachable number)."""
    return f"dial:{dial_id}" if dial_id else dial_call_sid


def build_dial_twiml(
    target_number: str,
    *,
    timeout_s: int = 30,
    action_url: str = "",
    announce: str = "",
    language: str = "",
    time_limit_s: int | None = None,
) -> str:
    """<Response>[<Say>announce</Say>]<Dial timeout=".." [timeLimit=".."] action="..">+49…</Dial></Response>.

    The announcement rides in the same TwiML (Twilio's <Say>) so it cannot be
    cut off by the redirect — the relay socket is gone the moment the call is
    updated."""
    from xml.sax.saxutils import escape

    from pincer.voice.language import relay_language

    say = f'<Say language="{relay_language(language or "en")}">{escape(announce)}</Say>' if announce else ""
    action = f' action="{escape(action_url, {chr(34): "&quot;"})}" method="POST"' if action_url else ""
    limit = f' timeLimit="{int(time_limit_s)}"' if time_limit_s is not None else ""
    return f'<Response>{say}<Dial timeout="{int(timeout_s)}"{limit}{action}>{escape(target_number)}</Dial></Response>'


class ConversationRelayEngine(VoiceEngine):
    """Phase 1: Twilio ConversationRelay — text in/out, Twilio handles audio.

    Twilio performs STT and TTS; we only exchange text via a webhook.
    Fastest to ship, higher latency (~2-3s).
    """

    @property
    def engine_name(self) -> str:
        return "conversation_relay"

    analytics_method = "estimated"

    async def on_call_start(
        self,
        call_sid: str,
        caller: str,
        direction: CallDirection,
        target_number: str = "",
        target_name: str = "",
        purpose: str = "",
        language: str = "",
        instructions: str = "",
    ) -> CallState:
        state = await self._register_call(
            call_sid,
            caller,
            direction,
            target_number,
            target_name,
            purpose,
            language,
            instructions,
        )
        logger.info("ConversationRelay call started: %s", call_sid)
        return state

    async def on_speech_input(self, call_sid: str, text_or_audio: Any) -> None:
        """Process text input from ConversationRelay webhook."""
        text = str(text_or_audio)
        logger.info("CR speech input [%s]: %s", call_sid, text[:80])
        state = self._active_calls.get(call_sid)
        if state and text.strip():
            state.mark_caller_spoke()
            from pincer.voice.analytics import get_accumulator

            accumulator = get_accumulator(state)
            if accumulator is not None:
                accumulator.caller_text(text, state.language)
        if self._on_speech_callback:
            await self._on_speech_callback(call_sid, text)

    async def send_speech(self, call_sid: str, text_or_audio: Any, *, last: bool = True) -> bool:
        """Send text response — Twilio converts to speech.

        Returns True only when the token was handed to the ConversationRelay
        WebSocket; a False return means the caller heard nothing.
        ``last=False`` streams a partial sentence of the current reply
        (Sprint 5): CR synthesizes it immediately while the LLM keeps writing.
        """
        text = str(text_or_audio)
        state = self._active_calls.get(call_sid)
        if not state:
            logger.warning("send_speech for unknown call: %s", call_sid)
            return False

        delivered = False
        ws = state.metadata.get("websocket")
        if ws:
            from pincer.voice.language import relay_language

            # Per-token language attribute pins TTS to the call language
            # (state.language is the single source of truth; it only changes
            # via language_guard.perform_switch on explicit caller request).
            msg = json.dumps({"type": "text", "token": text, "last": last, "lang": relay_language(state.language)})
            try:
                from pincer.voice.telemetry.tracer import current_turn_tracer

                turn = current_turn_tracer()
                if turn is not None and text:
                    turn.stamp("audio.queued")
                await ws.send_text(msg)
                delivered = True
                if turn is not None and text:
                    # ConversationRelay synthesises inside Twilio, so the first
                    # TEXT TOKEN handed over is the earliest observable point of
                    # the response. It is not audio and is never labelled as such.
                    turn.first_audio(kind="text_token", engine="conversation_relay")
                from pincer.voice.analytics import get_accumulator

                accumulator = get_accumulator(state)
                if accumulator is not None:
                    accumulator.agent_text(text, state.language)
            except Exception:
                logger.exception("CR websocket send failed [%s] — agent output DROPPED", call_sid)
        else:
            logger.warning("send_speech with no CR websocket [%s] — agent output DROPPED", call_sid)
        logger.info("CR speech output [%s] delivered=%s: %s", call_sid, delivered, text[:80])
        return delivered

    async def interrupt_speech(self, call_sid: str, *, by_caller: bool = True) -> None:
        # ConversationRelay stops its own playback on barge-in; the channel
        # cancels the turn behind it. Sending Media-Streams-style
        # {"type": "clear"} here is an invalid CR message (Twilio error
        # 64107) — nothing to send.
        # Our own cut (shutdown) has nothing to send either: CR cannot drop
        # text it was given, and the cancelled turn closes its open utterance.
        if not by_caller:
            return
        # The count is exact on both engines even though the timing is not.
        from pincer.voice.analytics import get_accumulator
        from pincer.voice.telemetry import hooks as telemetry

        accumulator = get_accumulator(self._active_calls.get(call_sid))
        if accumulator is not None:
            accumulator.interruption()
        # Reported, not performed: Twilio already stopped the audio. There is no
        # cancellation of ours to time, which is why interruption latency is
        # `Unavailable` on this engine.
        telemetry.interruption_reported(call_sid, engine="conversation_relay")
        logger.debug("CR interrupt [%s]", call_sid)

    async def close_media_stream(self, call_sid: str) -> None:
        """No-op for ConversationRelay; MediaStreamEngine overrides."""
        pass


class MediaStreamEngine(VoiceEngine):
    """Phase 2: Twilio Media Streams — raw mu-law audio via WebSocket.

    We run our own STT (Deepgram) and TTS (ElevenLabs) pipeline
    for lower latency (~0.8-1.5s) and custom voices.
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._stt_provider = None
        self._tts_provider = None

    @property
    def engine_name(self) -> str:
        return "media_streams"

    analytics_method = "exact"

    async def _ensure_providers(self) -> None:
        if self._stt_provider is None:
            from pincer.voice.stt import DeepgramSTT

            api_key = self._settings.deepgram_api_key.get_secret_value()
            if api_key:
                self._stt_provider = DeepgramSTT(api_key=api_key)

        if self._tts_provider is None:
            from pincer.voice.language import elevenlabs_model_for
            from pincer.voice.tts import OUTPUT_ULAW_8000, ElevenLabsTTS

            api_key = self._settings.elevenlabs_api_key.get_secret_value()
            if api_key:
                self._tts_provider = ElevenLabsTTS(
                    api_key=api_key,
                    voice_id=self._settings.elevenlabs_voice_id or None,
                    model=elevenlabs_model_for(self._settings),
                    stability=_safe_float(getattr(self._settings, "elevenlabs_stability", 0.5), 0.5),
                    similarity=_safe_float(getattr(self._settings, "elevenlabs_similarity", 0.75), 0.75),
                    speed=_safe_float(getattr(self._settings, "elevenlabs_speed", 1.0), 1.0),
                    style=_safe_float(getattr(self._settings, "elevenlabs_style", 0.0), 0.0),
                    output_format=str(getattr(self._settings, "elevenlabs_output_format", "") or OUTPUT_ULAW_8000),
                )

    async def on_call_start(
        self,
        call_sid: str,
        caller: str,
        direction: CallDirection,
        target_number: str = "",
        target_name: str = "",
        purpose: str = "",
        language: str = "",
        instructions: str = "",
    ) -> CallState:
        await self._ensure_providers()
        state = await self._register_call(
            call_sid,
            caller,
            direction,
            target_number,
            target_name,
            purpose,
            language,
            instructions,
        )
        logger.info("MediaStream call started: %s", call_sid)
        return state

    # Consecutive low-confidence transcripts tolerated before passing input
    # through anyway (avoids an ask-to-repeat loop with a noisy line).
    MAX_LOW_CONFIDENCE_RETRIES = 2

    async def setup_media_stream_stt(self, call_sid: str, stream_sid: str) -> None:
        """Create STT stream and transcript consumer when Media Streams WebSocket starts."""
        state = self._active_calls.get(call_sid)
        if not state or not self._stt_provider:
            return
        # A stream socket that took over the call starts STT again: close the
        # previous stream and its consumer, or they run until the provider
        # times them out (the replaced socket leaves the call's STT alone).
        await self._close_stt(state)

        state.metadata["stream_sid"] = stream_sid

        from pincer.voice.stt import stt_config_for_language

        config = stt_config_for_language(state.language, self._settings)
        # Both STT encodings assume Twilio's mu-law 8 kHz mono source (linear16
        # only upsamples it). A <Stream> configured otherwise would feed
        # Deepgram audio it misreads — say so instead of transcribing noise.
        media_format = state.metadata.get("media_format") or {}
        if media_format and (
            media_format.get("encoding"),
            media_format.get("sampleRate"),
            media_format.get("channels"),
        ) != ("audio/x-mulaw", 8000, 1):
            logger.warning(
                "Media Streams format %s is not mu-law 8 kHz mono; STT expects it [%s]", media_format, call_sid
            )
        from pincer.voice.telemetry.clock import mono_ns as _mono_ns

        stt_started_ns = _mono_ns()
        stt_stream = await self._stt_provider.start_stream(config)
        state.metadata["stt_stream"] = stt_stream
        # Deepgram word times are offsets into the AUDIO timeline of this
        # stream. Anchoring them to the monotonic instant the stream opened is
        # what turns "the caller stopped talking at 4.12 s" into a stamp on the
        # same clock every other stage is measured on.
        state.metadata["stt_started_ns"] = stt_started_ns
        # what on_speech_input puts on the wire; re-derived from settings on a
        # reconnect (_recover_stt_stream calls back in here)
        state.metadata["stt_encoding"] = config.encoding
        state.metadata["stt_endpointing_ms"] = float(config.endpointing)
        state.metadata["stt_utterance_end_ms"] = float(config.utterance_end_ms)

        # Both constrained by Settings (pattern / ge=1); a bad value fails there.
        trigger = self._settings.voice_barge_in_trigger
        min_words = self._settings.voice_barge_in_min_words

        def _agent_audible() -> bool:
            return _mono_ns() < int(state.metadata.get("playout_until_ns", 0))

        def _armed() -> bool:
            started = int(state.metadata.get("playout_started_ns", 0))
            return (_mono_ns() - started) >= BARGE_IN_ARM_DELAY_MS * 1_000_000

        def _echo_of_our_onset(transcript: Any) -> bool:
            # The same guard for recognised words, judged by when the speech
            # began rather than when the transcript arrived: Deepgram reports
            # an interim a few hundred ms after the audio, so an echo of our
            # first syllable would almost always arrive "armed". Speech that
            # began before our audio did cannot be its echo, and neither can
            # speech that goes on past the window: an echo is one or two
            # words at our onset, while a caller who started talking at the
            # same moment as us keeps producing words. Every interim of that
            # utterance shares the same first word, so the first word alone
            # would mute the caller for the whole reply.
            started = int(state.metadata.get("playout_started_ns", 0))
            window_end = started + BARGE_IN_ARM_DELAY_MS * 1_000_000
            words = list(getattr(transcript, "words", None) or [])
            if not words:
                return started <= _mono_ns() < window_end
            stt_started = int(state.metadata.get("stt_started_ns", 0))
            return all(started <= stt_started + word.start * 1_000_000_000 < window_end for word in words)

        def _vad_onset_confirmed(transcript: Any) -> bool:
            # The onset lives for one reply: a final pops it, and so does the
            # next reply's first chunk (see _stream_tts). Within the reply it
            # survives a new stretch of audio — a caller who said "Stop" during
            # sentence 1 still stops sentence 2. What it must not confirm is
            # the echo of that new stretch's first syllable: such an interim
            # leaves the onset in place for the caller's real words.
            onset = int(state.metadata.get("vad_onset_ns", 0))
            if not onset:
                return False
            if (_mono_ns() - onset) > BARGE_IN_CONFIRM_WINDOW_MS * 1_000_000:
                state.metadata.pop("vad_onset_ns", None)
                return False
            if _echo_of_our_onset(transcript):
                return False
            state.metadata.pop("vad_onset_ns", None)
            return True

        async def _cancel_reask() -> None:
            task = state.metadata.pop("aux_speech_task", None)
            if task is None:
                return
            if not task.done():
                task.cancel()
            # Cancelling stops only what is left to synthesize. Audio the re-ask
            # already sent sits in Twilio's buffer and would play ahead of the
            # reply; with no turn in flight, nothing else clears it.
            if _agent_audible():
                try:
                    await self.interrupt_speech(call_sid)
                except Exception:
                    logger.exception("Clearing the re-ask failed [%s]", call_sid)

        async def _handle_final_transcript(transcript: Any) -> None:
            text = transcript.text.strip()
            if not text or not self._on_speech_callback:
                return
            # The caller said something new: a re-ask still playing is moot.
            await _cancel_reask()
            state.mark_caller_spoke()
            # Deepgram word timings are real measurements of when the caller
            # spoke, which is what makes this engine's talk time `exact`.
            # Recorded before the confidence gate: a mumbled utterance the
            # agent asks to repeat was still time the caller spent talking.
            from pincer.voice.analytics import get_accumulator

            accumulator = get_accumulator(state)
            words = list(getattr(transcript, "words", None) or [])
            if accumulator is not None and words:
                accumulator.caller_span(words[0].start, words[-1].end)
            if words:
                # The one engine where "when did the caller stop speaking" is an
                # observation rather than a guess.
                state.metadata["speech_end_ns"] = int(
                    state.metadata.get("stt_started_ns", 0) + words[-1].end * 1_000_000_000
                )
                state.metadata["speech_start_ns"] = int(
                    state.metadata.get("stt_started_ns", 0) + words[0].start * 1_000_000_000
                )
            else:
                state.metadata.pop("speech_end_ns", None)
            # Misheard-input policy (Sprint 1): on low STT confidence ask to
            # repeat instead of acting on a guess. Confidence 0.0 means the
            # provider sent none — treat as trustworthy rather than looping.
            threshold = float(getattr(self._settings, "voice_stt_min_confidence", 0.0) or 0.0)
            confidence = float(getattr(transcript, "confidence", 0.0) or 0.0)
            low_count = int(state.metadata.get("low_confidence_count", 0))
            if 0.0 < confidence < threshold and low_count < self.MAX_LOW_CONFIDENCE_RETRIES:
                state.metadata["low_confidence_count"] = low_count + 1
                logger.info(
                    "Low STT confidence [%s]: %.2f < %.2f — asking to repeat",
                    call_sid,
                    confidence,
                    threshold,
                )
                from pincer.voice.language import de_formality
                from pincer.voice.prompts import get_prompt

                # A task, not awaited: the consumer has to keep reading (and
                # detecting barge-in) while the re-ask plays. The next final
                # transcript cancels it, so it never plays over the next reply.
                state.metadata["aux_speech_task"] = asyncio.create_task(
                    self.send_speech(
                        call_sid,
                        get_prompt("LOW_CONFIDENCE_REPLY", state.language, de_formality(self._settings)),
                    ),
                    name=f"voice-reask-{call_sid}",
                )
                return
            state.metadata["low_confidence_count"] = 0
            await self._on_speech_callback(call_sid, text)
            # Per-utterance markers are consumed by the turn that just started;
            # leaving them would date the NEXT turn from this utterance.
            for marker in UTTERANCE_MARKERS:
                state.metadata.pop(marker, None)

        async def _barge_in(transcript: Any, source: str) -> None:
            # One barge-in per stretch of agent audio; interrupt_speech resets
            # the playout estimate too, this only guards the window before it.
            state.metadata["playout_until_ns"] = 0
            state.metadata["barge_in_source"] = source
            # Speech onset: when SpeechStarted reached us, else Deepgram's word
            # timing (same anchoring as speech_end_ns), else detection time.
            words = list(getattr(transcript, "words", None) or [])
            onset_ns = int(state.metadata.pop("caller_onset_ns", 0))
            if onset_ns < int(state.metadata.get("playout_started_ns", 0)):
                onset_ns = 0  # an onset from before this audio began is an older utterance's
            if not onset_ns and words:
                onset_ns = int(state.metadata.get("stt_started_ns", 0) + words[0].start * 1_000_000_000)
            onset_ns = onset_ns or _mono_ns()
            logger.info("Barge-in detected [%s] via %s", call_sid, source)
            try:
                await self.on_caller_barge_in(call_sid)
            except Exception:
                # Never let it reach the consumer's handler: that one restarts STT.
                logger.exception("Barge-in handling failed [%s]", call_sid)
                return
            metrics = self.metrics_registry.get(call_sid) if self.metrics_registry else None
            if metrics:
                metrics.record_barge_in_reaction(max(0.0, (_mono_ns() - onset_ns) / 1_000_000.0))

        async def _consume_transcripts() -> None:
            try:
                async for transcript in stt_stream.receive_transcripts():
                    # Barge-in: the caller talking while our audio is still
                    # playing. VAD onset is the fast trigger; recognised words
                    # are the confirmation (and the only one in "interim" mode,
                    # for lines where VAD reacts to noise or echo).
                    if getattr(transcript, "speech_started", False):
                        state.metadata["caller_onset_ns"] = _mono_ns()
                        if trigger == "vad" and _agent_audible() and _armed():
                            # A candidate only, confirmed by the words below.
                            state.metadata["vad_onset_ns"] = _mono_ns()
                        continue
                    if transcript.text and trigger != "off" and _agent_audible():
                        if _vad_onset_confirmed(transcript):
                            # VAD already placed the onset; any recognised word
                            # confirms it, so min_words deliberately does not apply.
                            await _barge_in(transcript, "vad")
                        elif len(transcript.words or transcript.text.split()) >= min_words and not _echo_of_our_onset(
                            transcript
                        ):
                            await _barge_in(transcript, "interim")
                    if transcript.is_final:
                        # The utterance the onset belonged to is over (UtteranceEnd
                        # arrives as an empty final); it must not confirm a later one.
                        state.metadata.pop("vad_onset_ns", None)
                        if transcript.text:
                            # Whatever we say next is a new reply to it.
                            state.metadata["new_reply_pending"] = True
                        await _handle_final_transcript(transcript)
                    elif transcript.text and "first_partial_ns" not in state.metadata:
                        # Interim results are not acted on, but the FIRST one
                        # dates recognition independently of the endpointing
                        # wait that follows it.
                        state.metadata["first_partial_ns"] = _mono_ns()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("STT transcript consumer error [%s]", call_sid)
                await self._recover_stt_stream(call_sid, stream_sid)
            finally:
                await stt_stream.close()
                if state.metadata.get("stt_stream") is stt_stream:
                    state.metadata.pop("stt_stream", None)

        task = asyncio.create_task(_consume_transcripts())
        state.metadata["stt_consumer_task"] = task
        logger.info("Media Stream STT started [%s]", call_sid)

    async def _recover_stt_stream(self, call_sid: str, stream_sid: str) -> None:
        """STT died mid-call: reconnect once, else end the call gracefully (T1.3)."""
        state = self._active_calls.get(call_sid)
        if not state:
            return
        from pincer.voice.telemetry import hooks as telemetry

        if not state.metadata.get("stt_reconnected"):
            state.metadata["stt_reconnected"] = True
            telemetry.reconnect(call_sid, "stt", attempt=1)
            logger.warning("STT stream died [%s] — attempting one reconnect", call_sid)
            try:
                await self.setup_media_stream_stt(call_sid, stream_sid)
                return
            except Exception:
                telemetry.error(call_sid, "stt_error", stage="stt", detail="reconnect_failed")
                logger.exception("STT reconnect failed [%s]", call_sid)
        # Second failure: speak an honest goodbye and end the call.
        from pincer.voice.language import de_formality
        from pincer.voice.prompts import get_prompt

        with contextlib.suppress(Exception):
            await self.send_speech(
                call_sid,
                get_prompt("STT_FAILURE_GOODBYE", state.language, de_formality(self._settings)),
            )
        with contextlib.suppress(Exception):
            await self.end_call(call_sid)

    async def on_speech_input(self, call_sid: str, text_or_audio: Any) -> None:
        """Process raw audio from Media Streams WebSocket.

        Twilio sends base64-encoded mu-law 8kHz, which Deepgram ingests as-is
        (the stream is opened with encoding=mulaw, see setup_media_stream_stt).
        Decoding to PCM16 16 kHz in Python cost ~90 µs per 20 ms frame on the
        event loop and 4x the bytes upstream; it now only runs on the legacy
        voice_stt_encoding=linear16 path.
        """
        state = self._active_calls.get(call_sid)
        if not state:
            return

        # Pre-transcribed text (e.g. from fallback path) — pass directly to callback
        if isinstance(text_or_audio, str) and not _looks_like_base64(text_or_audio):
            if self._on_speech_callback:
                await self._on_speech_callback(call_sid, text_or_audio)
            return

        # Decode base64 payload from Twilio media event
        raw = base64.b64decode(text_or_audio) if isinstance(text_or_audio, str) else text_or_audio
        if not raw:
            return

        stt_stream = state.metadata.get("stt_stream")
        if stt_stream is None:
            return
        if state.metadata.get("stt_encoding", "mulaw") == "linear16":
            from pincer.voice.audio import mulaw8k_to_pcm16k

            raw = mulaw8k_to_pcm16k(raw)
        await stt_stream.send_audio(raw)

    async def _clear_twilio_buffer(self, state: CallState) -> None:
        """Drop whatever Twilio still has buffered for playout.

        Note this flushes the WHOLE outbound buffer, not just the tail — so it
        is only safe when nothing the caller still needs to hear is in it.
        """
        ws = state.metadata.get("websocket")
        if not ws:
            return
        msg = json.dumps({"event": "clear", "streamSid": state.metadata.get("stream_sid", "")})
        await ws.send_text(msg)

    async def send_speech(self, call_sid: str, text_or_audio: Any, *, last: bool = True) -> bool:
        """Synthesize text to speech and send audio to Twilio.

        T4.5: a failed synthesis is retried once; a second failure takes the
        fallback path (spoken apology via Twilio <Say>, graceful hangup) —
        never dead air. Returns True only when audio was streamed.
        ``last`` is accepted for interface parity (Sprint 5 streaming) — each
        utterance is synthesized independently here, so it is a no-op.

        The retry RESUMES: delivery is tracked per sentence-segment, so a
        synthesis that dies mid-utterance restarts at the first segment the
        caller has not been sent, never from the top. Re-speaking the whole
        utterance made the caller hear the opening sentences twice while the
        transcript recorded them once.
        """
        state = self._active_calls.get(call_sid)
        if not state:
            logger.warning("send_speech for unknown call: %s", call_sid)
            return False

        text = str(text_or_audio)

        if not self._tts_provider:
            logger.warning("send_speech with no TTS provider [%s] — agent output DROPPED", call_sid)
            return False

        from pincer.voice.sentence_stream import split_into_segments

        segments = split_into_segments(text)
        if not segments:
            return False
        progress = _SpeechProgress(epoch=int(state.metadata.get("speech_epoch", 0)))

        try:
            delivered = await self._synthesize_with_retry(call_sid, state, segments, progress)
        except asyncio.CancelledError:
            # The turn was cancelled mid-utterance. The channel logs the line
            # as cut off only if any of it reached Twilio.
            state.metadata["last_speech_cut"] = progress.wrote_audio
            raise
        if not delivered:
            return False

        # Read by the channel right after this returns: a cut utterance was
        # only partly heard, which "delivered" alone cannot say.
        state.metadata["last_speech_cut"] = progress.cut
        logger.info("MS speech output [%s] delivered=True cut=%s: %s", call_sid, progress.cut, text[:80])
        return True

    async def _synthesize_with_retry(
        self, call_sid: str, state: CallState, segments: list[str], progress: _SpeechProgress
    ) -> bool:
        """One utterance through `_stream_tts`, resumed once after a TTS failure."""
        try:
            return await self._stream_tts(call_sid, state, segments, progress)
        except Exception:
            from pincer.voice.telemetry import hooks as telemetry

            progress.attempts += 1
            telemetry.error(call_sid, "tts_error", stage="tts", attempt=progress.attempts)
            if progress.segments_done == 0 and progress.wrote_audio:
                # Only a fragment of the first segment is buffered, so dropping
                # the buffer costs the caller nothing and spares them hearing
                # that fragment before the retry repeats it. Once a segment has
                # completed we must NOT clear: `clear` flushes everything still
                # queued, which would silently swallow sentences the caller has
                # not heard yet. Losing content is worse than one stutter.
                with contextlib.suppress(Exception):
                    await self._clear_twilio_buffer(state)
            logger.warning(
                "TTS synthesis failed [%s] after %d/%d segment(s) — resuming utterance",
                call_sid,
                progress.segments_done,
                len(segments),
            )
            try:
                return await self._stream_tts(call_sid, state, segments, progress)
            except Exception:
                logger.exception("TTS retry failed [%s] — spoken fallback + hangup", call_sid)
                await self.fallback_and_end(call_sid)
                return False

    async def _stream_tts(
        self,
        call_sid: str,
        state: CallState,
        segments: list[str],
        progress: _SpeechProgress,
    ) -> bool:
        """Stream an utterance's segments to the Twilio WebSocket.

        Picks up at ``progress.segments_done``, so a retry after a mid-stream
        failure delivers only what the caller has not been sent. A segment is
        marked done only once its whole stream has been written, so the segment
        that failed is re-synthesized from its own start — the finest boundary
        neural TTS can restart on without cutting mid-phoneme.

        Returns True when any audio has been written for this utterance — no
        socket means no synthesis (and no ElevenLabs spend). With ulaw_8000
        output the ElevenLabs bytes go to Twilio as-is; the Python resample
        only runs on the legacy pcm_16000 fallback path.
        """
        from pincer.voice.language import elevenlabs_model_for, voice_for
        from pincer.voice.tts import OUTPUT_PCM_16000

        ws = state.metadata.get("websocket")
        if not ws:
            logger.warning("send_speech with no MS websocket [%s] — agent output DROPPED", call_sid)
            return False
        stream_sid = state.metadata.get("stream_sid", "")
        voice_id = voice_for(self._settings, state.language)
        tts_model = elevenlabs_model_for(self._settings, state.language)
        needs_resample = getattr(self._tts_provider, "output_format", OUTPUT_PCM_16000) == OUTPUT_PCM_16000

        from pincer.voice.analytics import get_accumulator

        accumulator = get_accumulator(state)
        metrics = self.metrics_registry.get(call_sid) if self.metrics_registry else None

        from pincer.voice.telemetry.schema import EventName, SpanName, SpanStatus
        from pincer.voice.telemetry.tracer import current_turn_tracer

        turn = current_turn_tracer()

        from pincer.voice.telemetry.clock import mono_ns as _mono_ns

        def interrupted() -> bool:
            return int(state.metadata.get("speech_epoch", 0)) != progress.epoch

        def bill(segment: str) -> None:
            # ElevenLabs bills the characters requested, not the audio we kept,
            # so a segment cut or cancelled mid-stream is still charged in full.
            state.metadata["tts_characters"] = int(state.metadata.get("tts_characters", 0)) + len(segment)
            if metrics:
                metrics.record_tts_characters(len(segment))

        started = time.monotonic()
        for index in range(progress.segments_done, len(segments)):
            if interrupted():
                # Barge-in between segments: interrupt_speech cleared what
                # Twilio buffered, and no further segment may start.
                progress.cut = True
                return progress.wrote_audio
            segment = segments[index]
            # One span per segment AND per attempt: a resumed retry is a second
            # synthesis of that segment and reads as one on the waterfall.
            tts_span = (
                turn.open_span(
                    SpanName.TTS,
                    attempt=progress.attempts + 1,
                    segment=index,
                    characters=len(segment),
                    voice=voice_id,
                    model=tts_model,
                )
                if turn is not None
                else None
            )
            if turn is not None and index == progress.segments_done:
                turn.stamp(EventName.TTS_REQUEST, provider="elevenlabs", model=tts_model)
            # aclosing: leaving the loop early (barge-in) closes the synthesis
            # stream now, not whenever the generator is garbage-collected.
            cut = False
            try:
                async with contextlib.aclosing(
                    self._tts_provider.synthesize_stream(segment, voice=voice_id, model=tts_model)
                ) as audio_stream:
                    async for audio_chunk in audio_stream:
                        if interrupted():
                            cut = True
                            break
                        if progress.first_chunk_ms is None:
                            # Measured once for the utterance: playout begins when
                            # Twilio receives the first chunk, whichever attempt sent it.
                            progress.first_chunk_ms = (time.monotonic() - started) * 1000.0
                            if accumulator is not None:
                                accumulator.agent_audio_begin()
                            if metrics:
                                metrics.record_tts_first_chunk(progress.first_chunk_ms)
                            if turn is not None:
                                turn.stamp(EventName.TTS_FIRST_AUDIO, first_chunk_ms=round(progress.first_chunk_ms, 1))
                                turn.stamp(EventName.AUDIO_QUEUED, kind="audio")
                        mulaw_data = audio_chunk
                        if needs_resample:
                            from pincer.voice.audio import pcm16k_to_mulaw8k

                            mulaw_data = pcm16k_to_mulaw8k(audio_chunk)
                        if mulaw_data:
                            payload = base64.b64encode(mulaw_data).decode("ascii")
                            msg = json.dumps(
                                {
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": payload},
                                }
                            )
                            await ws.send_text(msg)
                            if turn is not None and not progress.wrote_audio:
                                # Written to the provider socket. SENT, not heard: Twilio
                                # buffers outbound audio and acknowledges nothing.
                                turn.first_audio(kind="audio", engine="media_streams")
                            progress.wrote_audio = True
                            state.metadata["audio_since_clear"] = True
                            if state.metadata.pop("new_reply_pending", False):
                                # A new reply's first audio: an onset taken
                                # before it belongs to the old reply.
                                state.metadata.pop("vad_onset_ns", None)
                            # Twilio plays out in real time; the end of what it
                            # holds is what barge-in detection checks against.
                            now_ns = _mono_ns()
                            playing_until = int(state.metadata.get("playout_until_ns", 0))
                            if playing_until <= now_ns:
                                # A new stretch of agent audio: barge-in arms from here.
                                state.metadata["playout_started_ns"] = now_ns
                            state.metadata["playout_until_ns"] = (
                                max(playing_until, now_ns) + len(mulaw_data) * _ULAW_NS_PER_BYTE
                            )
                        if accumulator is not None and mulaw_data:
                            # μ-law at 8 kHz: one byte is 0.125 ms of audio. This is the
                            # measurement the `exact` method is named for.
                            accumulator.agent_audio_bytes(len(mulaw_data))
            except asyncio.CancelledError:
                # The turn was cancelled (barge-in) while this segment streamed.
                bill(segment)
                raise
            if cut:
                # Barge-in mid-segment: the rest of it is never synthesized.
                progress.cut = True
                bill(segment)
                if tts_span is not None:
                    tts_span.close(SpanStatus.CANCELLED)
                return progress.wrote_audio

            # Charged per segment actually synthesized, so a resumed retry does
            # not bill the segments it skipped (the old whole-utterance retry
            # counted every character twice).
            bill(segment)
            progress.segments_done = index + 1
            if tts_span is not None:
                tts_span.close(SpanStatus.OK)

        if turn is not None:
            turn.stamp(EventName.TTS_DONE, segments=len(segments))
        return progress.wrote_audio

    async def interrupt_speech(self, call_sid: str, *, by_caller: bool = True) -> None:
        state = self._active_calls.get(call_sid)
        if not state:
            return

        # Twilio's `clear` drops whatever is still buffered, so only the audio
        # that had time to play counts as agent speech.
        from pincer.voice.analytics import get_accumulator
        from pincer.voice.telemetry import hooks as telemetry

        # One interruption per stretch of agent audio: clearing a re-ask and
        # the channel's cancel of the prior turn both land here for the same
        # audio, and only the first of them may count or report it.
        interrupted = by_caller and bool(state.metadata.pop("audio_since_clear", False))
        source = str(state.metadata.pop("barge_in_source", "next_utterance"))
        accumulator = get_accumulator(state)
        if accumulator is not None:
            accumulator.agent_audio_cancelled()
            if interrupted:
                accumulator.interruption()

        detected_ns = (
            telemetry.interruption_detected(call_sid, engine="media_streams", source=source) if interrupted else None
        )
        # Per call, not per provider: bumping the epoch stops this call's
        # in-flight synthesis at its next chunk (see _stream_tts). The provider
        # is shared by every call, so cancelling it silenced all of them.
        state.metadata["speech_epoch"] = int(state.metadata.get("speech_epoch", 0)) + 1
        state.metadata["playout_until_ns"] = 0

        await self._clear_twilio_buffer(state)
        if interrupted:
            # Twilio does not acknowledge a `clear`, so this is when WE stopped
            # sending — audio already in its playout buffer may still be heard.
            telemetry.buffer_cleared(call_sid, since_ns=detected_ns, acknowledged=False)

        logger.debug("MS interrupt [%s]", call_sid)

    async def wait_for_playout(self, call_sid: str) -> None:
        state = self._active_calls.get(call_sid)
        if not state:
            return
        from pincer.voice.telemetry.clock import mono_ns

        # Audio is written faster than real time; `playout_until_ns` is when
        # Twilio runs out of it (zero after a `clear`).
        remaining_ns = int(state.metadata.get("playout_until_ns", 0)) - mono_ns()
        if remaining_ns > 0:
            await asyncio.sleep(remaining_ns / 1_000_000_000)

    async def end_call(self, call_sid: str) -> None:
        await self.close_media_stream(call_sid)
        await super().end_call(call_sid)

    async def close_media_stream(self, call_sid: str) -> None:
        """Close STT stream and cancel transcript consumer."""
        state = self._active_calls.get(call_sid)
        if not state:
            return
        await self._close_stt(state)
        logger.debug("Media stream STT closed [%s]", call_sid)

    async def _close_stt(self, state: CallState) -> None:
        stt_stream = state.metadata.pop("stt_stream", None)
        task = state.metadata.pop("stt_consumer_task", None)
        reask = state.metadata.pop("aux_speech_task", None)
        if reask is not None and not reask.done() and reask is not asyncio.current_task():
            reask.cancel()
        # The consumer task itself may drive call teardown (STT failure path);
        # never cancel-and-await the task we're currently running inside.
        if task and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if stt_stream:
            await stt_stream.close()


def get_voice_engine(settings: Settings) -> VoiceEngine:
    """Factory: return the configured voice engine implementation."""
    engine_type = settings.voice_engine.lower().strip()
    if engine_type == "media_streams":
        return MediaStreamEngine(settings)
    return ConversationRelayEngine(settings)
