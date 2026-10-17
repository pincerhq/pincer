"""Skills: the one owner of a process's skill index (`SkillService`).

The agent, its skill tools, the dashboard API and `pincer skills rebuild` all go
through a SkillService instead of building their own SkillIndex, so a process
holds exactly one index and every change to it goes through one place (the
planned skill-upload API included: write the files, then `invalidate()`).

Index changes are announced across processes with a generation stamp file in
`skills_cache_dir`. Any change — a rebuild, an `invalidate()`, or a periodic
rescan that finds different skills — writes a new stamp. Other processes notice
it in one of two ways:

- With a `SkillIndexWatcher` (what `pincer run` starts), a background task checks
  the stamp every few minutes and rescans off the request path; agent turns and
  skill tools just read the current index.
- Without one (e.g. the standalone MCP server), every read checks the stamp
  itself (one small local file read) and rescans on the spot.

Caching is cloudpathlib's file cache (remote files only) plus the dashboard
API's bounded rescan interval (`ensure_current(max_age=...)`); the index itself
is state, not a cache.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import uuid
from time import monotonic
from typing import TYPE_CHECKING

from pincer.tools.skills import index as index_module
from pincer.tools.skills.index import SkillIndex
from pincer.tools.skills.storage import clear_remote_cache, open_user_skills_root

if TYPE_CHECKING:
    from pathlib import Path

    from pincer.config import Settings
    from pincer.tools.skills.storage import SkillPath

logger = logging.getLogger(__name__)

_STAMP_FILENAME = "index-generation"


class SkillService:
    """Owns a SkillIndex and the operations that change it."""

    def __init__(
        self,
        bundled_dir: SkillPath | None,
        user_dir: SkillPath | None,
        *,
        max_per_root: int = 100,
        cache_dir: Path | None = None,
    ) -> None:
        self.index = SkillIndex(bundled_dir=bundled_dir, user_dir=user_dir, max_per_root=max_per_root)
        self._user_dir = user_dir
        self._stamp_path = cache_dir / _STAMP_FILENAME if cache_dir else None
        self._generation: str | None = None
        self._scanned_at: float | None = None
        self._lock = threading.Lock()
        # Set while a SkillIndexWatcher owns stamp checks for this service.
        self._watched = False

    @classmethod
    def from_settings(cls, settings: Settings) -> SkillService:
        return cls(
            bundled_dir=index_module.BUNDLED_SKILLS_DIR,
            user_dir=open_user_skills_root(settings),
            max_per_root=settings.skills_max_loaded_per_root,
            cache_dir=settings.skills_cache_dir,
        )

    @property
    def user_dir(self) -> SkillPath | None:
        return self._user_dir

    @property
    def generation(self) -> str | None:
        """The announced change this process's index reflects (None before any)."""
        return self._generation

    def ensure_current(self, max_age: float | None = None) -> SkillIndex:
        """Return the index, rescanning first if it was never scanned, if another
        process announced a change, or if the last scan is older than `max_age`
        seconds. Blocking (may list a bucket) — call via `asyncio.to_thread`.

        While a watcher runs, calls without `max_age` return the current index
        immediately (no stamp read, no waiting on a scan in progress): the
        watcher picks up announced changes. Calls with `max_age` (the dashboard
        API) keep checking the stamp themselves.
        """
        if self._watched and max_age is None and self._scanned_at is not None:
            # discover() swaps the index contents in one assignment, so this is
            # safe to read while the watcher rescans.
            return self.index
        with self._lock:
            stamp = self._read_stamp()
            if (
                self._scanned_at is None
                or stamp != self._generation
                or (max_age is not None and monotonic() - self._scanned_at >= max_age)
            ):
                self._scan(stamp)
            return self.index

    def refresh_if_announced(self) -> bool:
        """Rescan if another process announced a change since the last scan.
        Returns whether it rescanned. Blocking — the watcher runs it in a thread."""
        with self._lock:
            stamp = self._read_stamp()
            if self._scanned_at is not None and stamp == self._generation:
                return False
            self._scan(stamp)
            return True

    def rebuild(self, clear_cache: bool = True) -> SkillIndex:
        """Rescan both roots now and announce the result to every process.
        With `clear_cache`, cloudpathlib's file cache is emptied first, so every
        remote file is fetched fresh from the bucket."""
        with self._lock:
            if clear_cache:
                clear_remote_cache(self._user_dir)
            self.index.discover()
            self._generation = self._write_stamp()
            self._scanned_at = monotonic()
            return self.index

    def apply_generation(self, generation: str, clear_cache: bool = True) -> bool:
        """Bring this process up to a rebuild another process announced as `generation`
        (the `rebuild_skill_index` background job). Idempotent; returns whether it rescanned.

        - Already at `generation` (e.g. the job ran in the process that rebuilt): no-op.
        - Shares the rebuilder's disk (the stamp already says `generation`): rescan only;
          the cache was cleared by the rebuilder.
        - Has its own disk: clear its own cache if asked, rescan, and record `generation`
          as its stamp so processes sharing *this* disk follow without another rescan wave.
        """
        with self._lock:
            if self._generation == generation:
                return False
            shares_disk = self._read_stamp() == generation
            if clear_cache and not shares_disk:
                clear_remote_cache(self._user_dir)
            self.index.discover()
            self._scanned_at = monotonic()
            if not shares_disk:
                self._write_stamp(generation)
            self._generation = generation
            return True

    def invalidate(self) -> None:
        """Mark the index stale in every process (this one included) without
        scanning now — e.g. after uploading skill files."""
        with self._lock:
            self._write_stamp()

    def _scan(self, stamp: str | None) -> None:
        before = self._snapshot() if self._scanned_at is not None else None
        self.index.discover()
        self._scanned_at = monotonic()
        if before is not None and before != self._snapshot():
            # The skills themselves changed (e.g. files edited in the bucket):
            # tell other processes, so their indexes don't lag behind ours.
            stamp = self._write_stamp()
        self._generation = stamp

    def _snapshot(self) -> dict[str, str]:
        return {entry.name: entry.file_hash for entry in self.index.all_skills()}

    def _read_stamp(self) -> str | None:
        if self._stamp_path is None:
            # No stamp file means no other process can announce anything.
            return self._generation
        try:
            return self._stamp_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def _write_stamp(self, stamp: str | None = None) -> str:
        """Write `stamp` (a new random one by default) and return it. Without a
        cache dir nothing is written, but the generation is still returned."""
        stamp = stamp or uuid.uuid4().hex
        if self._stamp_path is None:
            return stamp
        self._stamp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._stamp_path.with_name(f".{_STAMP_FILENAME}.{os.getpid()}.tmp")
        tmp.write_text(stamp, encoding="utf-8")
        os.replace(tmp, self._stamp_path)
        logger.debug("Skill index generation is now %s", stamp)
        return stamp


class SkillIndexWatcher:
    """Background task that applies index changes announced by other processes.

    Every `interval` seconds it checks the generation stamp and, if it changed,
    rescans in a worker thread. A change is therefore picked up within
    `interval` plus the scan time, and no request ever runs the scan itself.
    """

    def __init__(self, service: SkillService, *, interval: float = 300) -> None:
        self._service = service
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        # Scan before taking over stamp checks, so readers never see an unscanned index.
        await asyncio.to_thread(self._service.ensure_current)
        self._service._watched = True
        self._task = asyncio.create_task(self._loop(), name="pincer-skill-index-watcher")
        logger.info("Skill index watcher started (interval=%ss)", self._interval)

    async def stop(self) -> None:
        self._service._watched = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Skill index watcher stopped")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                if await asyncio.to_thread(self._service.refresh_if_announced):
                    logger.info(
                        "Skill index reloaded after an announced change: %d skill(s)",
                        len(self._service.index.all_skills()),
                    )
            except Exception:
                logger.exception("Skill index watcher: rescan failed, keeping the current index")


_service: SkillService | None = None
_service_lock = threading.Lock()


def get_skill_service(settings: Settings | None = None) -> SkillService:
    """The process-wide SkillService, created on first use from `settings`
    (default: the relaxed settings singleton)."""
    global _service
    with _service_lock:
        if _service is None:
            if settings is None:
                from pincer.config import get_settings_relaxed

                settings = get_settings_relaxed()
            _service = SkillService.from_settings(settings)
        return _service


def reset_skill_service() -> None:
    """Forget the process-wide SkillService (tests, settings reloads)."""
    global _service
    with _service_lock:
        _service = None
