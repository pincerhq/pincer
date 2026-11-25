"""The one-way lock of storage encryption, and the checks Pincer runs before using encrypted storage.

The lock lives in the bucket itself; no database is involved. An encrypted
location carries two objects, both written with the key:

- `.pincer/encryption.json`, written when encryption is enabled: the key's
  fingerprint (an HMAC, never the key), and how far the encryption of the
  data stored before it got (see `pincer.storage.encrypt_job`).
- `.encrypted`, at the root, written once every object there is encrypted:
  only that HMAC. Reading it back with the key and finding the key's HMAC
  proves the key is right; finding it without the key proves the location is
  encrypted.

Either one is enough to keep encryption on: when one of them was deleted, the
other proves the key and the missing one is written again. Both are hidden
from the storage browser, which refuses every request for them
(`RESERVED_NAMES`).

The locations are the storage root, and a cloud skills dir outside it: every
cloud path Pincer opens (`clients.open_cloud_uri`) lies under one of them and
is checked against it (`check_location`), once per process (without a key,
only in the start sequence, `pincer doctor` and `pincer storage`, so opening
a path stays free of requests):

- Key set, neither object there yet: the marker is created, with a
  conditional write, so two replicas cannot race.
- Key set, an object there: it must have been written with this key. A key
  cannot change.
- No key, but the check file or the marker is there: the location is refused.
  Encryption cannot be turned off.
- Key set on an Azure hierarchical-namespace account: the Data Lake paths are
  probed with the key (`EncryptedAzureBlobClient.probe_hns`); a failed step
  refuses the location.

`verify_at_startup` runs the check for every location while Pincer starts, and
remembers what it refused, so every later open of that location fails fast
with the same reason.
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pincer.storage import clients
from pincer.storage.encryption import StorageKey, load_key
from pincer.storage.errors import KeyConflict, KeyNotFound, StorageEncryptionError, StorageUnavailable

if TYPE_CHECKING:
    from cloudpathlib import CloudPath

    from pincer.config import Settings

logger = logging.getLogger(__name__)

STATE_DIR = ".pincer"
MARKER = "encryption.json"
#: The check file at the root of a location whose data is all encrypted: only the key's HMAC.
SENTINEL = ".encrypted"
#: What Pincer keeps at the root of a cloud location: hidden from the storage
#: browser and refused by every one of its endpoints.
RESERVED_NAMES = frozenset({STATE_DIR, SENTINEL})
_HMAC_RE = re.compile(r"[0-9a-f]{64}")
JOB_LOCK = "encrypt.lock"
MARKER_VERSION = 1
#: Migration fields: when the job last rewrote an object in place (soft
#: delete's retention counts from it), and when the owner said the old plain
#: versions were deleted (`pincer storage old-versions-deleted`).
LAST_REWRITE = "last_rewrite_at"
OLD_VERSIONS_DELETED = "old_versions_deleted_at"

#: After an HNS probe that could not finish (not a refusal), ordinary opens of
#: the location fail fast with the same error for this long before probing again.
PROBE_RETRY_SECONDS = 30.0

_lock = threading.RLock()
#: location → (monotonic time, error) of its last HNS probe that could not finish.
_probe_failed: dict[str, tuple[float, str]] = {}
_verified: set[tuple[str, str | None]] = set()
_refused: dict[str, str] = {}
#: Locations the startup check could not finish (the store was unreachable):
#: their next open runs the full check, even without a key.
_unchecked: set[str] = set()


def reset_for_tests() -> None:
    with _lock:
        _verified.clear()
        _refused.clear()
        _unchecked.clear()
        _probe_failed.clear()


def is_not_found(error: Exception) -> bool:
    """A listing or read of something that is not there, in any SDK's words:
    cloudpathlib's FileNotFoundError, botocore's 404 / `NoSuchKey`, azure-core's
    ResourceNotFoundError, google-api-core's NotFound."""
    if isinstance(error, FileNotFoundError | NotADirectoryError | KeyNotFound):
        return True
    if type(error).__name__ in ("ResourceNotFoundError", "NotFound", "NoSuchKey"):
        return True
    response = getattr(error, "response", None)
    if isinstance(response, dict) and "Error" in response:  # botocore ClientError
        code = str(response.get("Error", {}).get("Code") or "")
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return status == 404 or code in ("404", "NoSuchKey", "NotFound")
    return False


# ── Locations ──────────────────────────────────────────────────────────


def _norm(uri: str) -> str:
    scheme, rest = clients.split_uri(uri)
    return f"{scheme}://{rest}"


def locations(settings: Settings) -> list[str]:
    """The cloud locations encryption covers: the storage root, and a cloud skills dir outside it."""
    from pincer.storage.layout import storage_root_for

    found: list[str] = []
    root = storage_root_for(settings)
    if clients.is_remote_uri(root):
        found.append(_norm(str(root)))
    skills = settings.skills_dir
    if isinstance(skills, str) and clients.is_remote_uri(skills) and guard_location(skills, settings) not in found:
        found.append(_norm(skills))
    return found


def guard_location(uri: str, settings: Settings) -> str:
    """The location whose lock covers `uri`: the storage root when `uri` lies under it, else `uri` itself."""
    from pincer.storage.layout import storage_root_for

    target = _norm(uri)
    root = storage_root_for(settings)
    if clients.is_remote_uri(root):
        base = _norm(str(root))
        if target == base or target.startswith(base + "/"):
            return base
    return target


# ── Marker ─────────────────────────────────────────────────────────────


def marker_path(location: CloudPath) -> CloudPath:
    return location / STATE_DIR / MARKER


def _objects(location: CloudPath, *, keyed: bool = True) -> Any:
    from pincer.storage.objects import objects_for

    return objects_for(location, keyed=keyed)


def marker_listed(location: CloudPath) -> bool:
    """Whether the marker object exists, by listing: works without the key."""
    folder = location / STATE_DIR
    try:
        return any(child.name == MARKER for child in folder.iterdir())
    except Exception as e:
        # A missing `.pincer/`: on an Azure HNS account, Data Lake says so
        # with ResourceNotFoundError rather than an empty listing.
        if is_not_found(e):
            return False
        raise


def read_marker(location: CloudPath) -> tuple[dict[str, Any], object] | None:
    """The marker and its version (for a conditional rewrite), or None when there is none.

    Raises StorageEncryptionError when it exists but this client cannot read
    it: no key, or another key.
    """
    try:
        data, version = _objects(location).read(marker_path(location))
    except KeyNotFound:
        return None
    except Exception as e:
        from pincer.storage.encryption import is_key_error

        # Only an error about the key itself means "missing or another key";
        # throttling, timeouts and 5xx are the store being unreachable.
        if is_key_error(e) and marker_listed(location):
            raise StorageEncryptionError(
                f"Storage encryption was enabled on {location}, and its marker cannot be read with the "
                f"configured PINCER_STORAGE_ENCRYPTION_KEY ({type(e).__name__}): the key is missing or is not "
                "the key encryption was enabled with. The key cannot be changed"
            ) from e
        raise
    try:
        marker = json.loads(data)
    except ValueError as e:
        raise StorageEncryptionError(f"The storage encryption marker of {location} is not valid JSON") from e
    if not isinstance(marker, dict):
        raise StorageEncryptionError(f"The storage encryption marker of {location} is not a JSON object")
    return marker, version


def new_marker(key: StorageKey) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "version": MARKER_VERSION,
        "algorithm": "AES256",
        "fingerprint": key.fingerprint,
        "enabled_at": now,
        "migration": {"state": "pending", "done": 0, "cursor": None, "updated_at": now},
    }


def write_marker(
    location: CloudPath, marker: dict[str, Any], *, if_version: object = None, if_absent: bool = False
) -> None:
    body = json.dumps(marker, indent=2, sort_keys=True).encode()
    _objects(location).write(
        marker_path(location),
        io.BytesIO(body),
        content_type="application/json",
        if_version=if_version,
        if_absent=if_absent,
    )


# ── Check file ─────────────────────────────────────────────────────────


def sentinel_path(location: CloudPath) -> CloudPath:
    return location / SENTINEL


def sentinel_text(key: StorageKey) -> str:
    """What the check file holds for `key`: only its HMAC fingerprint, never the key."""
    return key.fingerprint


def _is_hmac(text: str) -> bool:
    return bool(_HMAC_RE.fullmatch(text))


def sentinel_present(location: CloudPath) -> bool:
    """Whether Pincer's check file is there, read without the key.

    A read refused because the object is stored with a customer key means it
    is there. A plain file holding an HMAC is there too (a test store that
    does not encrypt); a plain file with anything else is someone else's
    `.encrypted`, not ours. Any other error is raised: unreachable storage is
    not a reason to refuse it as encrypted.
    """
    from pincer.storage.encryption import request_hit_encrypted_object

    try:
        data, _ = _objects(location, keyed=False).read(sentinel_path(location))
    except KeyNotFound:
        return False
    except Exception as e:
        if request_hit_encrypted_object(e):
            return True
        raise
    return _is_hmac(data.decode(errors="replace"))


def read_sentinel(location: CloudPath, key: StorageKey) -> bool:
    """True when the check file is there and holds `key`'s HMAC; False when there is none.

    Raises StorageEncryptionError when it holds another key's HMAC (or another
    key wrote it), or when a file of that name is not Pincer's.
    """
    name = _norm(str(location))
    try:
        data, _ = _objects(location).read(sentinel_path(location))
    except KeyNotFound:
        return False
    except Exception as e:
        from pincer.storage.encryption import is_key_error

        # It is there, and the provider refused the key: another key wrote it.
        # Anything else (throttling, a timeout, a 5xx) is the store being
        # unreachable, never a wrong key.
        if is_key_error(e) and sentinel_present(location):
            raise _wrong_key(name) from e
        raise
    text = data.decode(errors="replace")
    if text == sentinel_text(key):
        return True
    if _is_hmac(text):
        raise _wrong_key(name)
    raise StorageEncryptionError(
        f"{sentinel_path(location)} exists but is not Pincer's encryption check file; "
        f"move it away to use storage encryption on {name}"
    )


def write_sentinel(location: CloudPath, key: StorageKey) -> None:
    """Write the check file: done once every object of `location` is encrypted.

    A check file already there must hold this key's HMAC (raises otherwise).
    """
    try:
        _objects(location).write(
            sentinel_path(location),
            io.BytesIO(sentinel_text(key).encode()),
            content_type="text/plain",
            if_absent=True,
        )
    except KeyConflict:
        read_sentinel(location, key)


# ── Checks ─────────────────────────────────────────────────────────────


def _cannot_turn_off(location: str) -> StorageEncryptionError:
    return StorageEncryptionError(
        f"Storage encryption was enabled on {location} and cannot be turned off; "
        "set PINCER_STORAGE_ENCRYPTION_KEY to the key it was enabled with"
    )


def _wrong_key(location: str) -> StorageEncryptionError:
    return StorageEncryptionError(
        f"PINCER_STORAGE_ENCRYPTION_KEY does not match the key storage encryption was enabled with on {location}; "
        "the key cannot be changed"
    )


def ensure_enabled(
    location: CloudPath, key: StorageKey, *, create: bool = True, probe: bool = True
) -> dict[str, Any] | None:
    """Verify (or create) the lock of `location` for `key`; returns the marker.

    The marker is written when encryption is enabled; the check file once all
    data is encrypted. Each proves the key. When one of them was deleted, the
    other proves the key and the missing one is written again: the marker
    (as complete) when only the check file is left, the check file when the
    marker says the data is all encrypted.

    Not `create`: only verify, writing nothing (for read-only commands: a dry
    run, a status, `pincer doctor`); returns None when encryption was never
    enabled there, rather than enabling it. Not `probe`: skip the Azure
    hierarchical-namespace probe (the caller runs it itself).
    """
    name = _norm(str(location))
    has_sentinel = read_sentinel(location, key)
    found = read_marker(location)
    if found is not None and found[0].get("fingerprint") != key.fingerprint:
        raise _wrong_key(name)
    if not create:
        if found is not None:
            return found[0]
        return {**new_marker(key), "migration": {"state": "complete"}} if has_sentinel else None
    if found is None:
        marker = new_marker(key)
        if has_sentinel:
            logger.warning("Storage encryption: the marker of %s was deleted; writing it again", name)
            marker["migration"]["state"] = "complete"
            # When the rewrites happened is lost with the marker: count soft delete's retention from now.
            marker["migration"][LAST_REWRITE] = marker["migration"]["updated_at"]
        try:
            write_marker(location, marker, if_absent=True)
            if not has_sentinel:
                logger.info("Storage encryption enabled on %s", name)
        except KeyConflict:
            # Another replica wrote it in between: read theirs.
            found = read_marker(location)
            if found is None:
                raise StorageUnavailable(
                    f"The storage encryption marker of {name} vanished while it was created"
                ) from None
            marker = found[0]
            if marker.get("fingerprint") != key.fingerprint:
                raise _wrong_key(name) from None
    else:
        marker = found[0]
    if not has_sentinel and marker.get("migration", {}).get("state") == "complete":
        logger.warning("Storage encryption: the check file of %s is missing; writing it again", name)
        write_sentinel(location, key)
    if probe:
        _probe_if_hns(location)
    return marker


def _probe_if_hns(location: CloudPath) -> None:
    client = location.client
    if not (hasattr(client, "probe_hns") and client.is_hns(location)):
        return
    try:
        steps = client.probe_hns(location / STATE_DIR)
    except StorageUnavailable as e:
        # Not a refusal; back off instead of probing on every open.
        _probe_failed[_norm(str(location))] = (time.monotonic(), str(e))
        raise
    _probe_failed.pop(_norm(str(location)), None)
    for step, problem in steps:
        if problem is not None:
            raise StorageEncryptionError(
                f"Storage encryption check on the Azure hierarchical-namespace account of {location} failed at "
                f"step '{step}': {problem}. Pincer does not use this storage until the check passes"
            )


def ensure_disabled_allowed(location: CloudPath) -> None:
    """With no key set: refuse `location` when the check file or the marker says it is encrypted."""
    if sentinel_present(location) or marker_listed(location):
        raise _cannot_turn_off(_norm(str(location)))


def check_location(
    path: CloudPath, settings: Settings, *, thorough: bool = False, create: bool = True, probe: bool = True
) -> None:
    """The lock check for the location `path` lies under; called for every cloud path Pincer opens.

    Remembered per location and key, so it costs one round of requests per
    process (`thorough` checks again). Raises StorageEncryptionError.

    Without a key, an ordinary open makes no request (opening a cloud path
    stays free, as it is without encryption); only a `thorough` check reads
    the bucket for the lock. Pincer's start sequence, `pincer doctor` and
    `pincer storage` run it thoroughly, and a location they refused stays
    refused for the process, as does a location refused on an ordinary open.
    A location whose check could not finish (the store was unreachable) gets
    the full check on its next open.

    Not `create`: verify only, never write the lock (read-only commands).
    Not `probe`: skip the Azure hierarchical-namespace probe.
    """
    key = load_key(settings)
    location = guard_location(str(path), settings)
    cache_key = (location, key.fingerprint if key else None)
    with _lock:
        if location in _refused:
            raise StorageEncryptionError(_refused[location])
        failed = _probe_failed.get(location)
        if failed is not None and not thorough and time.monotonic() - failed[0] < PROBE_RETRY_SECONDS:
            raise StorageUnavailable(f"{failed[1]} (probed again in at most {PROBE_RETRY_SECONDS:.0f}s)")
        if cache_key in _verified and not thorough:
            return
        if key is None and not thorough and location not in _unchecked:
            return
        base = path.client.CloudPath(location)
        try:
            if key is None:
                ensure_disabled_allowed(base)
            elif ensure_enabled(base, key, create=create, probe=probe) is None:
                return  # read-only and never enabled: nothing verified to remember
        except StorageEncryptionError as e:
            # Refused (wrong or missing key, failed HNS probe): every later
            # open of the location fails fast with the same reason.
            _refused[location] = str(e)
            raise
        _unchecked.discard(location)
        if create:
            _verified.add(cache_key)


def verify_at_startup(settings: Settings) -> list[tuple[str, str | None]]:
    """The full check of every location, run in Pincer's start sequence.

    Each location is (uri, None) when it passed, or (uri, reason) when it was
    refused; a refused location stays refused for this process, so the
    storage browser, skills and workspace fail with that reason. Never raises:
    a refused storage must not stop the rest of Pincer from starting.
    """
    results: list[tuple[str, str | None]] = []
    for location in locations(settings):
        try:
            path = clients.open_cloud_uri(
                location, settings, None, error=StorageUnavailable, what="storage location", check=False
            )
            check_location(path, settings, thorough=True)
        except StorageEncryptionError as e:
            with _lock:
                _refused[location] = str(e)
            logger.error("Storage encryption: refusing %s: %s", location, e)
            results.append((location, str(e)))
            continue
        except Exception as e:
            # Unreachable storage is not an encryption problem: the usual errors
            # report it. Check it in full on its next open instead.
            with _lock:
                _unchecked.add(location)
            logger.warning("Storage encryption: could not check %s: %s", location, e)
            results.append((location, f"{type(e).__name__}: {e}"))
            continue
        results.append((location, None))
    return results


def status(path: CloudPath) -> dict[str, Any] | None:
    """The marker of the location `path`, or None when encryption was never enabled there."""
    found = read_marker(path)
    return None if found is None else found[0]
