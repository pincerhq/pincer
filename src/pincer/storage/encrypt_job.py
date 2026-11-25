"""Encrypting what was stored before storage encryption was enabled.

`encrypt_location` lists every object under a location and rewrites each one
still stored without the customer key, in place and on the provider where it
can (see `encrypt_in_place` on each client in
`pincer.storage.encrypted_clients`). The rewrite is conditional on the object
not having changed since it was looked at; one that changed was rewritten by
a writer that already encrypts, and is left alone.

Progress is kept in the location's marker (`.pincer/encryption.json`): the
state, how many objects were done, and the last key handled. On S3 and GCS,
which list in key order, a restart resumes after that key; elsewhere it
lists again and skips what is already encrypted (one HEAD each). A finished
location is `complete`, and only then gets its `.encrypted` check file (see
`pincer.storage.encryption_state`). Running it again is always safe and picks up plain
objects another service dropped in since.

`start` is Pincer's start sequence: the lock check of every location
(`encryption_state.verify_at_startup`), then the job in a background thread
for each location not `complete`. A best-effort lease object,
`.pincer/encrypt.lock`, keeps two replicas from running it at the same time.
"""

from __future__ import annotations

import io
import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pincer.storage import clients
from pincer.storage.encryption import load_key
from pincer.storage.encryption_state import (
    JOB_LOCK,
    SENTINEL,
    STATE_DIR,
    is_not_found,
    read_marker,
    verify_at_startup,
    write_marker,
    write_sentinel,
)
from pincer.storage.errors import KeyConflict, KeyNotFound, StorageEncryptionError, StorageUnavailable

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloudpathlib import CloudPath

    from pincer.config import Settings

logger = logging.getLogger(__name__)

#: Save progress to the marker every this many objects.
PROGRESS_EVERY = 100
#: A job's lease lasts this long without a refresh; then another replica may take over.
LEASE_SECONDS = 15 * 60
#: Providers that list keys in lexical order, so a restart can resume after the cursor.
_ORDERED_LISTING = ("s3", "gs")
#: How many times an object that changed mid-rewrite, and is still plain, is tried again.
CONFLICT_RETRIES = 1

_started = False
_start_lock = threading.Lock()


@dataclass
class Progress:
    location: str
    encrypted: int = 0
    already: int = 0
    pending: int = 0
    changed: int = 0
    failed: list[str] = field(default_factory=list)
    state: str = "running"
    #: Why the plain versions from before may still be readable (bucket versioning, soft delete), or None.
    old_versions: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "location": self.location,
            "encrypted": self.encrypted,
            "already_encrypted": self.already,
            "pending": self.pending,
            "changed_meanwhile": self.changed,
            "failed": len(self.failed),
            "state": self.state,
            "old_versions": self.old_versions,
        }


def _relative(root: CloudPath, path: CloudPath) -> str:
    base = str(root).rstrip("/")
    return str(path)[len(base) + 1 :]


class _Marker:
    """The location's marker, rewritten conditionally as progress is made."""

    def __init__(self, root: CloudPath, *, required: bool = True) -> None:
        self.root = root
        found = read_marker(root)
        if found is None:
            if required:
                raise StorageEncryptionError(
                    f"Storage encryption is not enabled on {root}: set PINCER_STORAGE_ENCRYPTION_KEY first"
                )
            # A dry run before encryption was enabled: nothing to resume, nothing saved.
            found = ({"migration": {"state": "pending"}}, None)
        self.data, self.version = found

    @property
    def migration(self) -> dict[str, Any]:
        migration: dict[str, Any] = self.data.setdefault("migration", {})
        return migration

    def save(self, **changes: Any) -> None:
        for _ in range(3):
            self.migration.update(changes, updated_at=datetime.now(UTC).isoformat())
            try:
                write_marker(self.root, self.data, if_version=self.version)
            except KeyConflict:
                # Someone else rewrote it (another replica's progress): take theirs, apply ours on top.
                found = read_marker(self.root)
                if found is None:
                    raise StorageUnavailable(f"The storage encryption marker of {self.root} vanished") from None
                self.data, self.version = found
                continue
            found = read_marker(self.root)
            if found is not None:
                self.data, self.version = found
            return
        logger.warning("Storage encryption: could not save progress of %s", self.root)


def _encrypt_one(client: Any, path: CloudPath) -> str:
    """Encrypt one plain object in place: "encrypted", or "changed" when it was
    rewritten with the key meanwhile.

    A rewrite that loses a race (the object changed since it was read) is
    checked again: a writer in a shared bucket may be another service that
    writes plain, so the object is tried again while it is still plain. One
    that keeps changing, still plain, raises KeyConflict and counts as failed.
    """
    for _ in range(CONFLICT_RETRIES + 1):
        try:
            client.encrypt_in_place(path)
            return "encrypted"
        except KeyConflict:
            if client.is_encrypted(path):
                return "changed"
    raise KeyConflict(f"{path} kept changing, still without the key, while it was being encrypted")


def _old_versions(client: Any, root: CloudPath) -> str | None:
    """Why the plain versions an in-place rewrite replaces may stay readable, if they may."""
    check = getattr(client, "old_versions_kept", None)
    if check is None:
        return None
    try:
        notes: list[str] = check(root)
    except Exception as e:
        logger.debug("Storage encryption: could not check %s for old versions: %s", root, e)
        return None
    return "; ".join(notes) or None


def encrypt_location(
    root: CloudPath,
    *,
    dry_run: bool = False,
    on_object: Callable[[str, str], None] | None = None,
    stop: threading.Event | None = None,
    heartbeat: Callable[[], bool] | None = None,
) -> Progress:
    """Encrypt every object under `root` still stored without the key.

    `on_object(key, outcome)` hears about each object: "encrypted",
    "already", "pending" (dry run), "changed" or "failed". `stop` ends the
    run early, saving progress. A dry run only counts, and writes nothing:
    not even the lock, so it works before encryption was enabled.
    `heartbeat()` runs whenever progress is saved (it renews the lease); when
    it returns False the lease was lost to another process, and the run
    stops, saving progress.

    The saved cursor never passes an object that failed, so a run that ends
    early (restart, lost lease, stop) tries it again on resume. A dry run
    counts every object, whatever the cursor.
    """
    client = root.client
    if not hasattr(client, "encrypt_in_place"):
        raise StorageEncryptionError(
            f"{root} is not opened with a customer key: set PINCER_STORAGE_ENCRYPTION_KEY to encrypt it"
        )
    marker = _Marker(root, required=not dry_run)
    progress = Progress(_norm(root))
    migration = marker.migration
    resume_after = None
    scheme = clients.uri_scheme(str(root))
    if not dry_run and migration.get("state") != "complete" and scheme in _ORDERED_LISTING:
        resume_after = migration.get("cursor")
    done_before = int(migration.get("done") or 0) if resume_after else 0
    progress.old_versions = _old_versions(client, root)
    if progress.old_versions:
        logger.warning(
            "Storage encryption of %s: %s. Encrypting rewrites objects in place; delete the old versions "
            "to remove the plain copies",
            root,
            progress.old_versions,
        )
    if not dry_run:
        marker.save(state="running", old_versions=progress.old_versions)

    def report(key: str, outcome: str) -> None:
        if on_object is not None:
            on_object(key, outcome)

    since_save = 0
    cursor = resume_after
    for path, is_dir in client._list_dir(root, recursive=True):
        if is_dir:
            continue
        key = _relative(root, path)
        if not key or key in (SENTINEL, STATE_DIR) or key.startswith(STATE_DIR + "/"):
            continue  # the lock: written with the key already
        if resume_after and key <= resume_after:
            continue
        if stop is not None and stop.is_set():
            break
        try:
            if client.is_encrypted(path):
                progress.already += 1
                report(key, "already")
            elif dry_run:
                progress.pending += 1
                report(key, "pending")
            else:
                outcome = _encrypt_one(client, path)
                if outcome == "encrypted":
                    progress.encrypted += 1
                else:
                    progress.changed += 1
                report(key, outcome)
        except Exception as e:
            if is_not_found(e):
                continue  # deleted while the job ran
            logger.warning("Storage encryption: could not encrypt %s: %s: %s", path, type(e).__name__, e)
            progress.failed.append(key)
            report(key, "failed")
        if not progress.failed:
            cursor = key  # never past a failed object: a resumed run tries it again
        since_save += 1
        if not dry_run and since_save >= PROGRESS_EVERY:
            since_save = 0
            marker.save(state="running", cursor=cursor, done=done_before + progress.encrypted + progress.already)
            if heartbeat is not None and not heartbeat():
                logger.warning("Storage encryption of %s: lost the lease to another process; stopping", root)
                stop = stop or threading.Event()
                stop.set()

    stopped = stop is not None and stop.is_set()
    if dry_run:
        progress.state = migration.get("state", "pending")
        return progress
    if stopped:
        progress.state = "running"
        marker.save(state="running", cursor=cursor, done=done_before + progress.encrypted + progress.already)
    elif progress.failed:
        # Resume from the start next time: the failed objects come before the cursor.
        progress.state = "pending"
        marker.save(state="pending", cursor=None, done=done_before + progress.encrypted + progress.already)
    else:
        progress.state = "complete"
        marker.save(state="complete", cursor=None, done=done_before + progress.encrypted + progress.already)
        # Every object is encrypted: only now does the check file appear.
        storage_key = getattr(client, "encryption_key", None)
        if storage_key is not None:
            write_sentinel(root, storage_key)
    return progress


def _norm(root: CloudPath) -> str:
    scheme, rest = clients.split_uri(str(root))
    return f"{scheme}://{rest}"


# ── Lease, so two replicas do not run the job at once ─────────────────


class Lease:
    """`.pincer/encrypt.lock`: who runs the job on a location, until when.

    Acquired with a conditional create (or a conditional take-over of an
    expired one), renewed by `refresh` while the job runs, and released only
    by its owner: a process never deletes another process's lease.
    """

    def __init__(self, root: CloudPath) -> None:
        from pincer.storage.objects import objects_for

        self.path = root / STATE_DIR / JOB_LOCK
        self._objects = objects_for(root)
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"
        self._version: object = None

    def _body(self) -> io.BytesIO:
        return io.BytesIO(json.dumps({"owner": self.owner, "expires_at": time.time() + LEASE_SECONDS}).encode())

    def _read(self) -> tuple[dict[str, Any], object] | None:
        try:
            data, version = self._objects.read(self.path)
            held = json.loads(data)
        except (KeyNotFound, ValueError):
            return None
        return (held, version) if isinstance(held, dict) else None

    def _write(self, **condition: Any) -> bool:
        try:
            self._objects.write(self.path, self._body(), content_type="application/json", **condition)
        except KeyConflict:
            return False
        found = self._read()
        if found is None or found[0].get("owner") != self.owner:
            return False
        self._version = found[1]
        return True

    def acquire(self) -> bool:
        if self._write(if_absent=True):
            return True
        found = self._read()
        if found is None:
            return False
        held, version = found
        if float(held.get("expires_at", 0)) > time.time() and held.get("owner") != self.owner:
            return False
        return self._write(if_version=version)

    def refresh(self) -> bool:
        """Extend the lease; False when it is no longer ours (it expired and another process took it)."""
        if self._version is None:
            return False
        return self._write(if_version=self._version)

    def release(self) -> None:
        """Delete the lease, only while it is still ours."""
        found = self._read()
        if found is None or found[0].get("owner") != self.owner:
            return
        try:
            self.path.unlink(missing_ok=True)
        except Exception as e:
            logger.debug("Storage encryption: could not release %s: %s", self.path, e)
        self._version = None


def run_with_lease(root: CloudPath, stop: threading.Event | None = None) -> Progress | None:
    """`encrypt_location` while holding the lease; None when another process holds it."""
    lease = Lease(root)
    if not lease.acquire():
        logger.info("Storage encryption: another process is encrypting %s", root)
        return None
    try:
        progress = encrypt_location(root, stop=stop, heartbeat=lease.refresh)
    finally:
        lease.release()
    logger.info("Storage encryption of %s: %s", root, progress.as_dict())
    return progress


# ── Start sequence ─────────────────────────────────────────────────────


def _open(location: str, settings: Settings) -> CloudPath:
    return clients.open_cloud_uri(location, settings, None, error=StorageUnavailable, what="storage location")


def pending_locations(settings: Settings) -> list[CloudPath]:
    """The locations whose existing data is not fully encrypted yet."""
    from pincer.storage.encryption_state import locations, status

    pending = []
    for location in locations(settings):
        try:
            root = _open(location, settings)
            marker = status(root)
        except Exception as e:
            # Refused at startup, or unreachable: reported elsewhere.
            logger.debug("Storage encryption: skipping %s: %s", location, e)
            continue
        if marker is not None and marker.get("migration", {}).get("state") != "complete":
            pending.append(root)
    return pending


def start(settings: Settings) -> threading.Thread | None:
    """Pincer's start sequence for storage encryption; runs once per process.

    Checks every location (refusing those that fail), then starts the job for
    the ones not fully encrypted in a daemon thread. Never raises: errors are
    logged and never block startup.
    """
    global _started
    with _start_lock:
        if _started:
            return None
        _started = True
    try:
        verify_at_startup(settings)
        if load_key(settings) is None:
            return None
        pending = pending_locations(settings)
    except Exception as e:
        logger.warning("Storage encryption: start-up check failed: %s: %s", type(e).__name__, e)
        return None
    if not pending:
        return None

    def run() -> None:
        for root in pending:
            try:
                run_with_lease(root)
            except Exception as e:
                logger.warning("Storage encryption of %s stopped: %s: %s", root, type(e).__name__, e)

    thread = threading.Thread(target=run, name="pincer-storage-encrypt", daemon=True)
    thread.start()
    return thread


def reset_for_tests() -> None:
    global _started
    with _start_lock:
        _started = False
