"""
Session management, stored through `pincer.services.sessions`.

A session = one conversation thread for one user on one channel.
Stores message history and provides context for the agent. A session is never cut by message
count: it is compacted by prompt tokens (`pincer.core.compaction`).

Each message is a row of its own (`pincer_session_messages`), ordered by (`created_at`, `id`). A
save writes only what changed since the last one: the messages added, the ones edited in place, and
the deletion of the ones dropped (`_plan`). The history in memory is the list as the agent left it;
the rows follow its order, so the provider output each assistant message replays (`raw`, see
`llm.replay`) and the latest compaction window come back where they were.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pincer.db.ids import uuid7_after, uuid7_ms
from pincer.db.types import epoch_ms, from_epoch_ms
from pincer.llm.base import ImageContent, LLMMessage, MessageRole, StoredRow
from pincer.models.sessions import MEMORY_FREE_KEY
from pincer.repositories.sessions import rekeyed_session_id
from pincer.services.onboarding import OnboardingService
from pincer.services.sessions import ImageSet, MessageChanges, SessionService, StoredImage, register_cache

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Collection
    from pathlib import Path

    from pincer.core.compaction import LastRequest

logger = logging.getLogger(__name__)

# Sessions held in memory at once; past it the least recently used is
# persisted and dropped, and reloads from the database on its next turn.
_DEFAULT_MAX_CACHED_SESSIONS = 1024
# The photos the cached sessions hold at once, in base64 characters (about 192 MB of image bytes): every cached
# session keeps its photos in memory, up to 5 MB each, so a count cap alone does not bound what the cache costs
_DEFAULT_MAX_CACHED_PHOTO_BYTES = 256 * 1024 * 1024
# Eviction tries this many sessions beyond the overflow per request, so a few
# that cannot be saved are stepped over without one request retrying them all.
_EVICTION_SPARE_ATTEMPTS = 2


def _session_key(user_id: str, channel: str) -> str:
    return f"{channel}:{user_id}"


def _resolve_session_key(user_id: str, channel: str, pincer_user_id: str = "", session_key: str | None = None) -> str:
    if session_key:
        return session_key
    if pincer_user_id:
        return _session_key(pincer_user_id, "unified")
    return _session_key(user_id, channel)


def channel_session_key(user_id: str, channel: str) -> str:
    """A user's main session on a channel (no conversation, no identity)."""
    return _session_key(user_id, channel)


def conversation_key(user_id: str, channel: str, conversation_id: str) -> str:
    """One conversation of a user's, nested under their channel session."""
    return f"{_session_key(user_id, channel)}:{conversation_id}"


# Session metadata the onboarding flow wrote before `pincer_onboarding`; now
# only read, for a user with no row there.
ONBOARDING_COMPLETE_KEY = "onboarding_complete"
ONBOARDING_PROMPT_SENT_KEY = "onboarding_prompt_sent"


@dataclass
class Session:
    """In-memory representation of a conversation session."""

    session_id: str
    user_id: str
    channel: str
    messages: list[LLMMessage] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: _now())
    updated_at: datetime = field(default_factory=lambda: _now())
    pincer_user_id: str = ""
    # The message rows as last saved (or loaded): each row's id and a fingerprint of its content, so a
    # save writes only the messages that changed, and an eviction skips a session where none did
    _saved_rows: dict[str, int] = field(default_factory=dict, init=False, repr=False, compare=False)
    # The rows a save gave new messages before it went through. A save that failed may have landed after
    # all: the next one writes those rows again (upserted by id) rather than new ones, and deletes the ones
    # no message holds any more, as it does saved rows
    _pending_rows: set[str] = field(default_factory=set, init=False, repr=False, compare=False)
    # The metadata as last saved or loaded (a new session's, as created), serialized
    _saved_metadata: str | None = field(default=None, init=False, repr=False, compare=False)
    # Set once an eviction's save has failed and been warned about, so a
    # session that keeps failing is not logged with a traceback every request.
    _eviction_failed: bool = field(default=False, init=False, repr=False, compare=False)
    # The hashes of the photos the image store holds for this session, as last saved or loaded;
    # `_persist` brings the store to the photos the messages hold now.
    _synced_images: frozenset[str] = field(default=frozenset(), init=False, repr=False, compare=False)
    # `photo_bytes()` as of the last save or load, for the cache's photo budget: summing it over every message of
    # every cached session on every cache miss would be the cost of the cache; stale only between an edit in memory
    # and the save that follows it
    _photo_bytes: int = field(default=0, init=False, repr=False, compare=False)
    # What the latest LLM request of the session's turn sent, for compaction; never saved
    last_request: LastRequest | None = field(default=None, init=False, repr=False, compare=False)

    # ── Onboarding (legacy flags, see ONBOARDING_COMPLETE_KEY) ──────────

    def is_fresh(self) -> bool:
        """True when the session has no non-system messages yet."""
        return not any(m.role != MessageRole.SYSTEM for m in self.messages)

    def is_onboarded(self) -> bool:
        return self.metadata.get(ONBOARDING_COMPLETE_KEY) == "true"

    def onboarding_prompt_already_sent(self) -> bool:
        return self.metadata.get(ONBOARDING_PROMPT_SENT_KEY) == "true"

    def is_memory_free(self) -> bool:
        """True once a turn without long-term memory has run in the session."""
        return self.metadata.get(MEMORY_FREE_KEY) == "true"

    def mark_memory_free(self) -> None:
        """Flag the session memory-free; saved with its next message."""
        self.metadata[MEMORY_FREE_KEY] = "true"

    def _save_plan(self, now: datetime) -> _SavePlan:
        """The rows a save writes, for the history as it is now, in one walk of it.

        A message whose row this session holds (saved, or given by a save that failed) keeps it while it
        sorts after the message before it: rewritten if its content changed, inserted again if only a
        failed save gave it. Any other message gets a new row: at its own time if it has one (a history
        taken over from an older key, or a stored message that has to move), else now when nothing stored
        comes after it, else just after the message before it (or, at the head, a millisecond before the
        first stored message). Its id carries that millisecond and sorts
        after the previous message's id, so (`created_at`, `id`) is the list's order. A stored message
        that would sort before the one in front of it (something new was put ahead of it with no room)
        moves to a new row the same way. Rows no message holds any more are deleted.
        """
        owned = self._saved_rows.keys() | self._pending_rows
        # The millisecond of the first row this session holds after each message, if any
        stored_after: list[int | None] = [None] * len(self.messages)
        later: int | None = None
        for index in range(len(self.messages) - 1, -1, -1):
            stored_after[index] = later
            message = self.messages[index]
            if message.stored is not None and _holds_row(message, owned):
                later = epoch_ms(message.stored.created_at)
        now_ms = epoch_ms(now)
        previous: StoredRow | None = None
        inserts: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        placed: list[tuple[LLMMessage, str, StoredRow]] = []
        rows: dict[str, int] = {}
        for index, message in enumerate(self.messages):
            fingerprint = _fingerprint(message)
            row = message.stored
            if (
                row is not None
                and row.id is not None
                and row.id in owned
                and row.id not in rows
                and _sorts_after(row, previous)
            ):
                if row.id not in self._saved_rows:
                    inserts.append(_as_stored(message, row))
                elif self._saved_rows[row.id] != fingerprint:
                    updates.append(_as_stored(message, row))
                rows[row.id] = fingerprint
                previous = row
                continue
            if row is not None:
                target_ms = epoch_ms(row.created_at)
            elif stored_after[index] is not None:
                # Before a stored message: at the previous message's millisecond, or, at the head, the
                # millisecond before the first stored one's, which sorts ahead of it whatever its id
                head_ms = max((stored_after[index] or 0) - 1, 0)
                target_ms = epoch_ms(previous.created_at) if previous is not None else head_ms
            else:
                target_ms = now_ms
            row_id = _id_after(target_ms, previous)
            new_row = StoredRow(row_id, from_epoch_ms(uuid7_ms(row_id)))
            inserts.append(_as_stored(message, new_row))
            placed.append((message, row_id, new_row))
            rows[row_id] = fingerprint
            previous = new_row
        deletes = [row_id for row_id in owned if row_id not in rows]
        return _SavePlan(MessageChanges(insert=inserts, update=updates, delete=deletes), placed, rows)

    def _place(self, plan: _SavePlan) -> None:
        """Before a save: the new messages take their rows, which a retry reuses."""
        for message, row_id, row in plan.placed:
            message.stored = row
            self._pending_rows.add(row_id)

    def _saved(self, plan: _SavePlan) -> None:
        """Once a save went through: its rows are the session's."""
        self._saved_rows = plan.rows
        self._pending_rows.clear()

    def _has_changes(self) -> bool:
        """True when a save would write something: a message or the metadata changed since the last one."""
        return bool(self._save_plan(_now()).changes) or json.dumps(self.metadata) != self._saved_metadata

    def photo_bytes(self) -> int:
        """What the session's photos take in memory, in base64 characters; a photo shown by several
        messages is one."""
        held = {image.sha256 or id(image): len(image.data) for m in self.messages for image in m.images}
        return sum(held.values())


class SessionManager:
    """Async SQLite-backed session store."""

    def __init__(
        self,
        db_path: Path,
        max_cached: int = _DEFAULT_MAX_CACHED_SESSIONS,
        max_cached_photo_bytes: int = _DEFAULT_MAX_CACHED_PHOTO_BYTES,
    ) -> None:
        self._db_path = db_path
        self._max_cached = max_cached
        self._max_cached_photo_bytes = max_cached_photo_bytes
        # Counts the cache hits (each moves a session to the recent end): see `_evict_overflow`
        self._touches = 0
        self._service: SessionService | None = None
        self._onboarding: OnboardingService | None = None
        self._cache: OrderedDict[str, Session] = OrderedDict()
        self._turn_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        register_cache(self)

    @asynccontextmanager
    async def turn(
        self, user_id: str, channel: str, session_key: str | None = None, pincer_user_id: str = ""
    ) -> AsyncIterator[Session]:
        """Run one agent turn on the session: serialize turns, pin the session while they are
        active or queued, and yield it. Every message of the turn is saved as it is added."""
        key = _resolve_session_key(user_id, channel, pincer_user_id, session_key)
        async with self._locked(key):
            yield await self.get_or_create(user_id, channel, pincer_user_id, session_key)

    @asynccontextmanager
    async def _locked(self, key: str) -> AsyncIterator[None]:
        """Hold the session's turn lock, pinning it in the cache while held or awaited."""
        lock, users = self._turn_locks.get(key, (asyncio.Lock(), 0))
        self._turn_locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            current_lock, users = self._turn_locks[key]
            if users == 1:
                del self._turn_locks[key]
            else:
                self._turn_locks[key] = (current_lock, users - 1)

    async def compact(
        self,
        session: Session,
        prefix: list[LLMMessage],
        replacement: LLMMessage,
        kept_ok: Callable[[list[LLMMessage]], bool] | None = None,
    ) -> bool:
        """Replace the session's oldest messages `prefix` by `replacement`, between turns.

        Applies only while `prefix` is still the start of the cached session's history (the
        same message objects), so a turn, repair, clear or cut since it was chosen wins; and
        only if `kept_ok` accepts the messages that would follow the replacement, judged under
        the turn lock, when no turn can add to them. True if the history changed.

        The replacement takes the row (and time) of the first message it replaces, so it is
        stored where that one was, before every message kept.
        """
        return await self._replace_prefix(session, prefix, [replacement], kept_ok)

    async def cut(self, session: Session, prefix: list[LLMMessage], keep: list[LLMMessage] | None = None) -> bool:
        """Drop the session's oldest messages `prefix` (the emergency cut), between turns, on the
        same terms as `compact`, except `keep`: the messages of the prefix that stay at the head (its
        summaries). The photos only the dropped messages showed are deleted with them."""
        return await self._replace_prefix(session, prefix, keep or [], None)

    async def _replace_prefix(
        self,
        session: Session,
        prefix: list[LLMMessage],
        replacement: list[LLMMessage],
        kept_ok: Callable[[list[LLMMessage]], bool] | None,
    ) -> bool:
        async with self._locked(session.session_id):
            if self._cache.get(session.session_id) is not session:
                return False
            head = session.messages[: len(prefix)]
            if len(head) != len(prefix) or any(a is not b for a, b in zip(head, prefix, strict=True)):
                return False
            if kept_ok is not None and not kept_ok(session.messages[len(prefix) :]):
                return False
            _take_place(replacement, prefix)
            session.messages = [*replacement, *session.messages[len(prefix) :]]
            session.updated_at = _now()
            await self._persist(session)
            return True

    async def initialize(self) -> None:
        """Bring the database to head and attach the store.

        Production DB discipline (Sprint 7, T7.4): WAL and a 5s busy_timeout
        are set on every connection the shared engine opens (see
        `pincer.db.engine`), so this process and the tasks worker both wait on
        a lock rather than failing. Schema DDL lives in the Alembic migrations.
        """
        self._service = await SessionService.for_path(self._db_path)
        self._onboarding = await OnboardingService.for_path(self._db_path)

    async def close(self) -> None:
        for session in list(self._cache.values()):
            await self._persist(session, only_if_changed=True)
        self._cache.clear()
        # The engine is shared; the process disposes it at shutdown.
        self._service = None
        self._onboarding = None

    @property
    def _store(self) -> SessionService:
        if self._service is None:
            raise RuntimeError("SessionManager not initialized")
        return self._service

    @property
    def onboarding(self) -> OnboardingService:
        """Each user's onboarding state per channel, in the same database."""
        if self._onboarding is None:
            raise RuntimeError("SessionManager not initialized")
        return self._onboarding

    async def has_stored_sessions(self, user_id: str, channel: str, *, created_before: datetime | None = None) -> bool:
        """True when the user has any session stored on the channel, conversations included
        (memory-free ones aside, see `Session.is_memory_free`); with `created_before`,
        only one created before that time."""
        return await self._store.has_any(user_id, channel, created_before=created_before)

    async def get_or_create(
        self,
        user_id: str,
        channel: str,
        pincer_user_id: str = "",
        session_key: str | None = None,
    ) -> Session:
        """Get existing session or create a new one.

        If pincer_user_id is provided, it's used as the session key prefix
        for cross-channel continuity; otherwise falls back to channel:user_id.
        ``session_key`` overrides both with an exact key (a phone call's
        session, which must not depend on how its user was resolved).
        """
        key = _resolve_session_key(user_id, channel, pincer_user_id, session_key)

        if key in self._cache:
            self._cache.move_to_end(key)
            self._touches += 1
            return self._cache[key]

        row = await self._store.load(key, user_id=user_id, channel=channel, legacy_fallback=not session_key)

        if row:
            photos = {
                photo.sha256: ImageContent(
                    data=base64.b64encode(photo.data).decode(), media_type=photo.media_type, sha256=photo.sha256
                )
                for photo in row["images"]
            }
            stored = row["messages"]
            missing = {ref["sha256"] for m in stored for ref in m.get("images", [])} - photos.keys()
            if missing:
                logger.warning("Session %s refers to %d photo(s) its image store lacks; left out", key, len(missing))
            messages = [_loaded(m, photos, own=row["session_id"] == key) for m in stored]
            session = Session(
                session_id=key,
                user_id=user_id,
                channel=channel,
                messages=messages,
                metadata=json.loads(row["metadata"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                pincer_user_id=pincer_user_id,
            )
        else:
            session = Session(
                session_id=key,
                user_id=user_id,
                channel=channel,
                pincer_user_id=pincer_user_id,
            )

        # As loaded, or the empty session's: a session nobody changed is never written
        session._saved_metadata = json.dumps(session.metadata)
        session._photo_bytes = session.photo_bytes()
        if row and row["session_id"] == key:
            session._saved_rows = {
                m.stored.id: _fingerprint(m) for m in session.messages if m.stored is not None and m.stored.id
            }
            session._synced_images = frozenset(photo.sha256 for photo in row["images"])
        # A session left under the old key stores its messages anew (their rows stay the old session's: the
        # save gives them new ones at their own times) and its photos anew
        self._cache[key] = session
        await self._evict_overflow(keep=key)
        return session

    async def add_message(self, session: Session, message: LLMMessage) -> None:
        """Add a message to the session and save it."""
        session.messages.append(message)
        session.updated_at = _now()
        await self._persist(session)

    async def add_messages(self, session: Session, messages: list[LLMMessage]) -> None:
        """Add several messages to the session and save them in one write (every save writes the whole
        session, so one call is cheaper than a call per message)."""
        if not messages:
            return
        session.messages.extend(messages)
        session.updated_at = _now()
        await self._persist(session)

    async def append_note(self, session_key: str, user_id: str, channel: str, text: str) -> None:
        """Add an assistant message to a session from outside any turn (a
        phone call's final report), in turn order with the user's own turns.

        A trailing assistant message is fine: the Anthropic serializer merges
        consecutive same-role messages, and OpenAI accepts them as they are.
        """
        async with self.turn(user_id, channel, session_key=session_key) as session:
            await self.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content=text))

    async def save(self, session: Session) -> None:
        """Write the session out after its messages were changed in place."""
        await self._persist(session)

    async def clear(self, session: Session) -> None:
        """Clear all messages from a session."""
        session.messages.clear()
        session.updated_at = _now()
        await self._persist(session, force_images=True)

    async def edit_message(
        self, session_key: str, channel_message_id: str, content: str, *, at: datetime | None = None
    ) -> bool:
        """Rewrite a user message its messenger reports edited (`channel_message_id`), between turns.

        The cached copy of the session is saved first and dropped after, so the next turn reads the
        edit instead of writing the old text back. False when the session has no such message.
        """
        async with self._locked(session_key):
            cached = self._cache.get(session_key)
            if cached is not None:
                await self._persist(cached, only_if_changed=True)
            edited = await self._store.edit(session_key, channel_message_id, content, at=at or _now())
            if edited and cached is not None and self._cache.get(session_key) is cached:
                del self._cache[session_key]
            return edited

    def forget_identity(self, url: str | None, old_user_id: str, new_user_id: str) -> None:
        """Drop, unsaved, the cached sessions of both ids of a rename on `url`'s database.

        `IdentityService.rename_identity` has moved the old id's rows to the
        new id and merged any that collided, so a cached copy of either side
        is stale: saved again, it would overwrite the merged row or bring back
        the removed one. The next turn loads the row as it now is. A turn
        already running on such a session still saves its own copy; renames
        run only during startup seeding, which turns wait for.
        """
        if self._service is None or self._service.url != url:
            return
        for key in [key for key in self._cache if _belongs_to(key, old_user_id) or _belongs_to(key, new_user_id)]:
            del self._cache[key]

    async def evict(self, key: str) -> None:
        """Persist and drop one session from the cache (a phone call's, at
        hangup — a key per call would otherwise grow the cache forever).

        The session stays cached until its save lands, so a request for it
        meanwhile gets this object rather than the row as it was before.
        """
        session = self._cache.get(key)
        if session is None:
            return
        await self._persist(session, only_if_changed=True)
        if self._cache.get(key) is session:
            del self._cache[key]

    async def _evict_overflow(self, keep: str) -> None:
        """Persist and drop least recently used sessions past the cap, or while their photos pass the
        byte budget (`max_cached_photo_bytes`; the session `keep` stays even if its own photos pass it).

        Runs inside the request for `keep`, which is never evicted here. A
        session whose save fails is logged and kept, moved to the recent end
        so the next one in line is tried; the request goes on, and the cache
        stays over the cap until later evictions succeed. A session hit or
        replaced while its save was awaited is kept too. At most the overflow
        (and the sessions that hold photos, when they are over budget) plus
        `_EVICTION_SPARE_ATTEMPTS` sessions are tried per request.

        Over the photo budget only, the sessions dropped are the oldest ones that hold photos: a session
        with none frees nothing. Nothing is dropped at all when `keep` and the sessions in a turn, which stay,
        hold more than the budget by themselves: dropping every other session could not bring the total under
        it, and those users would only reload their photos.
        """
        # Known race: a turn still holding a session evicted after its save
        # keeps writing that object while the next request loads a second
        # copy, and the last write wins. It takes more than `max_cached`
        # other keys within one turn, so only a client minting new
        # conversation ids gets near it.
        # Summed once per request (a cache hit never gets here), then reduced by each session dropped
        sizes = {key: cached._photo_bytes for key, cached in self._cache.items()}
        photos = sum(sizes.values())
        attempts = _EVICTION_SPARE_ATTEMPTS + max(len(self._cache) - self._max_cached, 0)
        # Dropping the others can bring the photos under the budget only if what stays does not pass it alone
        can_help = photos > self._max_cached_photo_bytes and (
            sum(size for key, size in sizes.items() if key == keep or key in self._turn_locks)
            <= self._max_cached_photo_bytes
        )

        def photo_sessions() -> list[tuple[str, Session]]:
            """The sessions that hold photos and can go, oldest first, found in one walk of the cache."""
            if not can_help:
                return []
            return [
                (key, cached)
                for key, cached in self._cache.items()
                if sizes.get(key) and key != keep and key not in self._turn_locks
            ]

        walked = photo_sessions()
        attempts += len(walked)
        candidates, touched = iter(walked), self._touches
        while attempts > 0:
            if len(self._cache) > self._max_cached:
                key, session = next(iter(self._cache.items()))
                if key == keep:
                    return  # everything older was tried
            elif photos > self._max_cached_photo_bytes:
                # Only the photos are over: drop the next session of those that hold some. One that was
                # dropped, replaced or put in a turn since the walk is passed over; a session used since it
                # (while a save was awaited) has moved in the cache, so the walk is made again (the live order)
                if touched != self._touches:
                    candidates, touched = iter(photo_sessions()), self._touches
                found = next(
                    (
                        (key, cached)
                        for key, cached in candidates
                        if self._cache.get(key) is cached and key not in self._turn_locks
                    ),
                    None,
                )
                if found is None:
                    return
                key, session = found
                self._cache.move_to_end(key, last=False)  # the next in line, for the checks below
            else:
                return
            attempts -= 1
            if key in self._turn_locks:
                self._cache.move_to_end(key)
                continue
            try:
                await self._persist(session, only_if_changed=True)
            except Exception:
                if session._eviction_failed:
                    logger.debug("Session %s still cannot be saved to evict it", key)
                else:
                    logger.warning("Could not save session %s to evict it; keeping it cached", key, exc_info=True)
                    session._eviction_failed = True
                if self._cache.get(key) is session:
                    self._cache.move_to_end(key)
                continue
            session._eviction_failed = False
            if (
                key not in self._turn_locks
                and next(iter(self._cache), None) == key
                and self._cache[key] is session
                and not session._has_changes()
            ):
                del self._cache[key]
                photos -= sizes.pop(key, 0)

    async def _persist(self, session: Session, *, only_if_changed: bool = False, force_images: bool = False) -> None:
        """Write the session out: its row, and the messages that changed since the last save (with
        ``only_if_changed``, nothing at all when neither they nor the metadata did).

        Its photos follow its messages: new ones are stored, and the ones no message holds any
        more are deleted, whichever code removed those messages. ``force_images`` also deletes
        what an earlier session of this process left behind (a clear).
        """
        plan = session._save_plan(_now())
        metadata = json.dumps(session.metadata)
        if only_if_changed and not plan.changes and metadata == session._saved_metadata:
            return
        session._place(plan)
        session._photo_bytes = session.photo_bytes()
        images = _image_set(session, force=force_images)
        await self._store.save(
            session_id=session.session_id,
            user_id=session.user_id,
            channel=session.channel,
            metadata=metadata,
            created_at=session.created_at,
            updated_at=session.updated_at,
            messages=plan.changes,
            images=images,
        )
        session._saved(plan)
        session._saved_metadata = metadata
        if images is not None:
            session._synced_images = images.keep


def _now() -> datetime:
    return datetime.now(UTC)


def _fingerprint(message: LLMMessage) -> int:
    """What a message's row holds, as a hash: a stored message whose fingerprint changed is rewritten.

    Its raw output, most of a history's size, is hashed once (`LLMMessage.raw_hash`), not on every save."""
    fields = message.to_dict()
    raw = fields.pop("raw", None)
    if raw is not None and message.raw_hash is None:
        message.raw_hash = hash(json.dumps(raw))
    return hash((json.dumps(fields), message.raw_hash if raw is not None else None))


def _loaded(stored: dict[str, Any], photos: dict[str, ImageContent], *, own: bool) -> LLMMessage:
    """A stored message (a dict from `SessionService.load`). With `own`, it keeps its row; one read from
    a session under an older key keeps only its time (`Session._save_plan` gives it a row of its own)."""
    message = LLMMessage.from_dict(stored, photos)
    message.stored = StoredRow(stored["id"] if own else None, stored["created_at"])
    return message


def _take_place(replacement: list[LLMMessage], prefix: list[LLMMessage]) -> None:
    """Give the first new message of `replacement` the row of the first message of `prefix`, which it
    replaces (a compaction summary takes the place of what it summarizes). Messages the replacement
    keeps from the prefix (a cut keeps its summaries) keep their own."""
    first = prefix[0] if prefix else None
    if first is None or first.stored is None or any(message is first for message in replacement):
        return
    for message in replacement:
        if message.stored is None:
            message.stored = first.stored
            return


@dataclass
class _SavePlan:
    """What a save does to the rows (`changes`), the row it gives each new message (`placed`, with its id), and the
    fingerprints of the session's rows once it went through (`rows`)."""

    changes: MessageChanges
    placed: list[tuple[LLMMessage, str, StoredRow]]
    rows: dict[str, int]


def _holds_row(message: LLMMessage, owned: Collection[str]) -> bool:
    return message.stored is not None and message.stored.id in owned


def _sorts_after(row: StoredRow, previous: StoredRow | None) -> bool:
    """True when `row` sorts after `previous` in (`created_at`, `id`) order. Both have ids: they are rows
    this session holds or was just given."""
    if previous is None:
        return True
    return (epoch_ms(row.created_at), row.id or "") > (epoch_ms(previous.created_at), previous.id or "")


def _id_after(target_ms: int, previous: StoredRow | None) -> str:
    """A new row's id at `target_ms` (or the previous row's millisecond, if that is later), sorting after
    the previous row's; the row's `created_at` is the millisecond it carries."""
    if previous is not None:
        target_ms = max(target_ms, epoch_ms(previous.created_at))
    return uuid7_after(target_ms, previous.id if previous else None)


def _as_stored(message: LLMMessage, row: StoredRow) -> dict[str, Any]:
    return message.to_dict() | {"id": row.id, "created_at": row.created_at}


def _image_set(session: Session, *, force: bool) -> ImageSet | None:
    """The photos the session's messages hold, if they differ from what its image store holds."""
    held: dict[str, ImageContent] = {}
    for message in session.messages:
        for image in message.images:
            if image.sha256:
                held.setdefault(image.sha256, image)
    keep = frozenset(held)
    if keep == session._synced_images and not force:
        return None
    add = tuple(
        StoredImage(sha, image.media_type, base64.b64decode(image.data))
        for sha, image in held.items()
        if sha not in session._synced_images
    )
    return ImageSet(keep, add)


def _belongs_to(key: str, user_id: str) -> bool:
    """True when `key` is one of `user_id`'s sessions, as a rename sees it (`rekeyed_session_id`)."""
    return rekeyed_session_id(key, user_id, user_id) is not None
