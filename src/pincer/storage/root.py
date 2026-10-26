"""Which root the storage browser serves, and which prefixes under it are protected."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from pincer.storage import clients
from pincer.storage.errors import StorageUnavailable

if TYPE_CHECKING:
    from cloudpathlib import CloudPath

    from pincer.config import Settings

type StoragePath = Path | CloudPath


def open_location(location: str | Path, settings: Settings) -> StoragePath:
    """A local directory (created if missing) or a cloud prefix, as a path."""
    if not clients.is_remote_uri(location):
        root = Path(location).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()
    # Cached copies are only a transfer buffer here (see pincer.storage.objects):
    # each one is dropped as soon as its file is closed.
    return clients.open_cloud_uri(
        str(location),
        settings,
        settings.data_dir / "cache" / "storage",
        error=StorageUnavailable,
        what="storage root",
        file_cache_mode="close_file",
    )


def _uri(value: str) -> str:
    scheme, rest = clients.split_uri(value)
    return f"{scheme}://{rest}"


def relative_prefix(root: StoragePath, location: str | Path) -> str | None:
    """`location` relative to `root` ("" when they are the same), or None when outside it."""
    if isinstance(root, Path):
        if clients.is_remote_uri(location):
            return None
        target = Path(location).expanduser().resolve()
        if not target.is_relative_to(root):
            return None
        rel = target.relative_to(root).as_posix()
        return "" if rel == "." else rel
    if not clients.is_remote_uri(location):
        return None
    base_uri, target_uri = _uri(str(root)), _uri(str(location))
    if target_uri == base_uri:
        return ""
    return target_uri[len(base_uri) + 1 :] if target_uri.startswith(base_uri + "/") else None


def data_dir_conflict(root: Path, data_dir: Path) -> str | None:
    """Why the local `root` must not be served, or None.

    The data dir holds the JWT secret and the database: a root that contains
    it would let any identity download the secret and forge sessions, or
    overwrite the database. Both paths must be resolved. The folders are
    compared on disk too, by file identity: `resolve()` keeps the case it was
    given, and on a case-insensitive disk `/srv/APP` is `/srv/app`.
    """
    contains = data_dir.is_relative_to(root)
    if not contains and root.exists():
        contains = any(p.exists() and os.path.samefile(p, root) for p in (data_dir, *data_dir.parents))
    if contains:
        return f"The storage root {root} contains the data dir {data_dir}, which holds the JWT secret and the database"
    return None


def resolve_storage_root(settings: Settings) -> tuple[StoragePath, list[str]]:
    """The root the storage browser serves, and the prefixes under it that are protected.

    - `storage_root` set: that location. The skills dir is protected when it lies under it.
    - Unset, with a cloud `skills_dir`: the root of that bucket / container, with
      the skills prefix protected.
    - Unset, with a local `skills_dir`: `<data_dir>/storage`. The skills dir is
      not under it, so nothing is protected.

    A local root that contains the data dir is refused (see `data_dir_conflict`).
    """
    skills_dir = settings.skills_dir
    if settings.storage_root:
        root = open_location(settings.storage_root, settings)
    elif clients.is_remote_uri(skills_dir):
        root = open_location(clients.bucket_root(str(skills_dir)), settings)
    else:
        root = open_location(settings.data_dir / "storage", settings)
    if isinstance(root, Path):
        problem = data_dir_conflict(root, Path(settings.data_dir).expanduser().resolve())
        if problem:
            raise StorageUnavailable(f"{problem}. Set PINCER_STORAGE_ROOT to a folder outside it.")
    protected = relative_prefix(root, skills_dir)
    return root, [] if protected is None else [protected]
