"""The agent's workspace: `<storage root>/workspace` (see `pincer.storage.layout`).

A `Workspace` has two sides:

- `remote`, the workspace itself: a `Path` for a local root, a `CloudPath` for
  an s3://, az:// or gs:// root. For a cloud root the bucket is the source of
  truth: what a user uploads in Documents is there, and only there.
- `local`, the directory tools see. For a local root it is `remote` itself.
  For a cloud root it is cloudpathlib's cache folder for the prefix, under
  `<data_dir>/cache/workspace`: disposable, refreshed from the bucket by
  `fetch`, and pushed back to it by `save` and `publish`.

Tools that need a real file (python_exec's working directory, OCR, send_file,
paths named in the prompt) work in `local`; everything else goes through the
methods here, which are plain `Path` operations for a local root.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pincer.storage import clients, layout
from pincer.storage.errors import StorageUnavailable
from pincer.storage.keys import FOLDER_MARKER, is_hidden
from pincer.storage.root import data_dir_conflict, open_location
from pincer.tools.path_sandbox import confine_path

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable, Iterator

    from pincer.config import Settings
    from pincer.storage.root import StoragePath

logger = logging.getLogger(__name__)


def safe_upload_path(directory: Path, filename: str) -> Path:
    """Resolve `filename` under `directory`, rejecting traversal/absolute paths.

    `filename` comes straight off attacker-controlled incoming-message metadata
    (e.g. Telegram's doc.file_name, WhatsApp's fileName) — joining it onto
    the directory unchecked lets a sender write outside it.
    """
    safe_name = Path(filename).name or f"file_{uuid.uuid4().hex[:8]}"
    save_path = directory / safe_name
    if directory.resolve() not in save_path.resolve().parents:
        raise ValueError(f"unsafe attachment filename: {filename!r}")
    return save_path


@dataclass(frozen=True)
class Entry:
    """One child of a workspace folder."""

    name: str
    is_dir: bool
    size: int = 0


class Workspace:
    """`<storage root>/workspace`, and the local directory tools see (see the module docstring)."""

    def __init__(self, remote: StoragePath, local: Path) -> None:
        self.remote = remote
        self.local = local

    @property
    def is_cloud(self) -> bool:
        return not isinstance(self.remote, Path)

    @classmethod
    def from_settings(cls, settings: Settings) -> Workspace:
        """Open the workspace under the storage root.

        A local root that contains the data dir is refused, which keeps the JWT
        secret and the database out of the agent's reach. A cloud root is
        opened on a client of its own, whose cache is `<data_dir>/cache/workspace`.
        Raises StorageUnavailable.
        """
        location = layout.storage_root_for(settings)
        if clients.is_remote_uri(location):
            remote = clients.open_cloud_uri(
                str(layout.join(location, layout.WORKSPACE)),
                settings,
                settings.data_dir / "cache" / "workspace",
                error=StorageUnavailable,
                what="workspace",
            )
            return cls(remote, remote._local)
        root = open_location(location, settings)
        assert isinstance(root, Path)
        problem = data_dir_conflict(root, Path(settings.data_dir).expanduser().resolve())
        if problem:
            raise StorageUnavailable(f"{problem}. Set PINCER_STORAGE_ROOT to a folder outside it.")
        workspace = root / layout.WORKSPACE
        return cls(workspace, workspace)

    # ── Folders ──────────────────────────────────────────────────────

    def ensure(self) -> None:
        """Create the workspace and its sub-folders if missing (`.keep` markers on a cloud root)."""
        for sub in ("", *layout.WORKSPACE_SUBDIRS):
            (self.local / sub).mkdir(parents=True, exist_ok=True)
            if self.is_cloud:
                _ensure_marker(self.remote / sub if sub else self.remote)

    def ensure_sibling(self, name: str) -> StoragePath:
        """Create `<storage root>/<name>` if missing (a `.keep` marker on a cloud root) and return it."""
        folder = self.remote.parent / name
        if self.is_cloud:
            _ensure_marker(folder)
        else:
            assert isinstance(folder, Path)
            folder.mkdir(parents=True, exist_ok=True)
        return folder

    # ── Paths ────────────────────────────────────────────────────────

    def confine(self, requested: str | Path, label: str = "workspace") -> Path:
        """`requested` (relative to `local`, or absolute within it) as a resolved local path.
        Raises SandboxDenied if it would escape."""
        return confine_path(self.local, str(requested), label=label)

    def relative(self, local_path: str | Path) -> str:
        """The POSIX path of `local_path` relative to the workspace ("" for the workspace itself)."""
        rel = self.confine(local_path).relative_to(self.local.resolve()).as_posix()
        return "" if rel == "." else rel

    def to_remote(self, local_path: str | Path) -> StoragePath:
        """The workspace object (or folder) at the same place as `local_path`. Raises SandboxDenied."""
        rel = self.relative(local_path)
        return self.remote / rel if rel else self.remote

    def contains(self, path: str | Path) -> bool:
        """Whether `path` lies in the local workspace directory."""
        try:
            self.confine(path)
        except ValueError:
            return False
        return True

    # ── Files ────────────────────────────────────────────────────────

    def save(self, subdir: str, filename: str, data: bytes) -> Path:
        """Store `data` as `<subdir>/<filename>` and return its local path.

        `filename` is reduced to its last component; one that still escapes
        raises ValueError. On a cloud root the object is uploaded, and the
        returned path is the cached copy.
        """
        path = safe_upload_path(self.local / subdir, filename)
        if self.is_cloud:
            self.to_remote(path).write_bytes(data)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return path

    def write_text(self, local_path: str | Path, content: str) -> Path:
        """Write `content` (UTF-8) at `local_path` in the workspace and return the local path."""
        target = self.confine(local_path)
        remote = self.to_remote(target)
        if isinstance(remote, Path):
            remote.parent.mkdir(parents=True, exist_ok=True)
        remote.write_text(content, encoding="utf-8")
        return target

    def fetch(self, local_path: str | Path, *, known_file: bool = False) -> Path:
        """`local_path`, with the cached copy brought in line with the bucket.

        On a local root this is the path itself. On a cloud root the bucket is
        the source of truth: a cached copy changed locally is replaced, and one
        whose object is gone is removed, so the returned path does not exist.
        `known_file` skips the existence check when the caller just made it.
        """
        target = self.confine(local_path)
        remote = self.to_remote(target)
        if isinstance(remote, Path):
            return target
        if known_file or remote.is_file():
            return clients.cached_copy(remote)
        if target.is_file():
            logger.debug("Dropping the cached copy of %s: the object is gone", remote)
            target.unlink(missing_ok=True)
        return target

    def file_size(self, local_path: str | Path) -> int | None:
        """The size of the file at `local_path` in the workspace, or None when there is
        no file there. One request on a cloud root."""
        remote = self.to_remote(local_path)
        if isinstance(remote, Path):
            return remote.stat().st_size if remote.is_file() else None
        from cloudpathlib.exceptions import NoStatError

        try:
            return remote.stat().st_size
        except (NoStatError, FileNotFoundError, IsADirectoryError):
            return None  # what a real bucket answers for a missing key or a folder prefix

    def sync(self, subdir: str) -> None:
        """Bring the local copy of `<subdir>/` in line with the bucket: objects newer
        than their cached copy are downloaded, cached files whose object is gone
        are removed. A no-op on a local root. One listing, then one request per file
        (run in parallel)."""
        if isinstance(self.remote, Path):
            return
        remote_dir, local_dir = self.remote / subdir, self.local / subdir
        base_depth = len(remote_dir.parts)
        rels = [
            "/".join((*dirpath.parts[base_depth:], name))
            for dirpath, _dirnames, filenames in remote_dir.walk()
            for name in filenames
        ]
        _parallel(lambda rel: clients.cached_copy(remote_dir / rel), rels)
        keep = set(rels)
        if local_dir.is_dir():
            for dirpath, _dirnames, filenames in local_dir.walk():
                for name in filenames:
                    path = dirpath / name
                    if path.relative_to(local_dir).as_posix() not in keep:
                        path.unlink(missing_ok=True)

    def is_file(self, local_path: str | Path) -> bool:
        """Whether a file exists at `local_path` in the workspace (in the bucket, for a cloud root)."""
        return self.to_remote(local_path).is_file()

    def is_dir(self, local_path: str | Path) -> bool:
        return self.to_remote(local_path).is_dir()

    def publish(self, local_path: str | Path) -> None:
        """Upload a file a tool wrote straight into `local`. A no-op on a local root."""
        source = self.confine(local_path)
        remote = self.to_remote(source)
        if isinstance(remote, Path):
            return
        remote.upload_from(source, force_overwrite_to_cloud=True)
        # Align the cached copy's time with the object's, so the next read does not
        # take it for a local edit (cloudpathlib refuses to overwrite a newer copy).
        mtime = remote.stat().st_mtime
        os.utime(source, times=(mtime, mtime))

    def list(self, local_dir: str | Path = "") -> list[Entry]:
        """The children of a workspace folder, listed from the bucket for a cloud root
        (one listing, then the file sizes in parallel). Markers and upload temp
        files are hidden."""
        remote = self.to_remote(local_dir or ".")
        if isinstance(remote, Path):
            entries = [
                Entry(child.name, True) if child.is_dir() else Entry(child.name, False, child.stat().st_size)
                for child in remote.iterdir()
                if not is_hidden(child.name)
            ]
            return sorted(entries, key=lambda e: e.name)
        _top, dirnames, filenames = next(remote.walk(lazy=True), (remote, [], []))
        names = [name for name in filenames if not is_hidden(name)]
        sizes = _parallel(lambda name: (remote / name).stat().st_size, names)
        entries = [Entry(name, True) for name in dirnames if not is_hidden(name)]
        entries += [Entry(name, False, size) for name, size in zip(names, sizes, strict=True)]
        return sorted(entries, key=lambda e: e.name)

    def sizes(self, rels: builtins.list[str]) -> builtins.list[int]:
        """The sizes of the workspace files `rels` (0 for one that can't be read), in parallel."""

        def size(rel: str) -> int:
            try:
                return self.to_remote(rel).stat().st_size
            except Exception:
                return 0

        return _parallel(size, rels)

    def files(self) -> Iterator[tuple[str, Path]]:
        """Every file in the workspace, recursively: (relative POSIX path, local path).

        Hidden names (dot files, markers) and dot folders are skipped. The local
        path is not fetched: call `fetch` before reading it.
        """
        base = self.remote
        base_depth = len(base.parts)
        for dirpath, dirnames, filenames in base.walk():
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            rel_dir = dirpath.parts[base_depth:]
            if any(part.startswith(".") for part in rel_dir):
                continue
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                if isinstance(dirpath, Path) and (dirpath / name).is_dir():
                    continue
                rel = "/".join((*rel_dir, name))
                yield rel, self.local / rel


_PARALLEL_REQUESTS = 16


def _parallel[T, R](fn: Callable[[T], R], items: list[T]) -> list[R]:
    """`fn` over `items` in order, with up to 16 at a time (bucket round trips)."""
    if len(items) <= 1:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(_PARALLEL_REQUESTS, len(items))) as pool:
        return list(pool.map(fn, items))


def _ensure_marker(folder: Any) -> None:
    marker = folder / FOLDER_MARKER
    if not marker.exists():
        marker.write_bytes(b"")


_workspace: tuple[Any, Workspace] | None = None


def get_workspace(settings: Settings) -> Workspace:
    """The workspace for `settings`, built once per settings object (a cloud
    client is not free to build). Raises StorageUnavailable."""
    global _workspace
    if _workspace is None or _workspace[0] is not settings:
        ws = Workspace.from_settings(settings)
        ws.local.mkdir(parents=True, exist_ok=True)
        _workspace = (settings, ws)
    return _workspace[1]


def reset_workspace() -> None:
    """Forget the cached workspace (tests)."""
    global _workspace
    _workspace = None


# ── Boot ─────────────────────────────────────────────────────────────


def prepare_storage(settings: Settings, *, migrate: bool = True) -> Workspace:
    """Create Pincer's folders under the storage root and, with `migrate`, move
    legacy content in once.

    Runs at boot, before the skills service and the channels. The main process
    migrates; a tasks worker passes `migrate=False`, so two processes never
    copy the same folders at once. Raises StorageUnavailable when the root
    can't be reached or is refused.
    """
    ws = get_workspace(settings)
    try:
        ws.ensure()
        if settings.skills_dir_defaulted:
            ws.ensure_sibling(layout.SKILLS)
    except StorageUnavailable:
        raise
    except Exception as e:
        raise StorageUnavailable(f"Cannot prepare the storage root {ws.remote.parent}: {type(e).__name__}: {e}") from e
    if migrate:
        migrate_legacy(settings, ws)
    return ws


def legacy_home_dir() -> Path:
    """`~/.pincer`, where tools wrote files (and user skills lived) before the storage root."""
    return Path.home() / ".pincer"


def legacy_skills_dir() -> Path:
    """Where user skills lived before they defaulted to `<storage root>/skills`."""
    return legacy_home_dir() / "skills"


_MIGRATION_STATE = "storage-migration.json"


def migrate_legacy(settings: Settings, ws: Workspace) -> dict[str, int]:
    """Copy content from the pre-storage-root locations, once.

    - `<data_dir>/workspace/**` and `~/.pincer/workspace/**` → `workspace/`
    - `<data_dir>/downloads/**` and `~/.pincer/downloads/**` → `workspace/downloads/`
    - `~/.pincer/skills/**` → `<root>/skills/`, only when PINCER_SKILLS_DIR is
      unset and the target holds no skills yet.

    Keys that already exist are skipped. A migrated source is recorded in
    `<data_dir>/storage-migration.json` and renamed to `<name>.migrated`; a
    recorded one is never copied again, even when the rename failed (a mount
    point, a read-only folder), so files deleted since are not brought back.
    A source that fails to copy is not recorded and is retried at the next
    start. Returns the copied counts per label.
    """
    data_dir = Path(settings.data_dir).expanduser()
    home = legacy_home_dir()
    plan: list[tuple[str, Path, StoragePath]] = [
        ("workspace", data_dir / "workspace", ws.remote),
        ("downloads", data_dir / "downloads", ws.remote / "downloads"),
        ("workspace", home / "workspace", ws.remote),
        ("downloads", home / "downloads", ws.remote / "downloads"),
    ]
    if settings.skills_dir_defaulted:
        skills_target = ws.remote.parent / layout.SKILLS
        if not _has_skills(skills_target):
            plan.append(("skills", legacy_skills_dir(), skills_target))

    state_path = data_dir / _MIGRATION_STATE
    done = _read_state(state_path)
    seen: set[str] = set()
    copied: dict[str, int] = {}
    for label, source, target in plan:
        if not source.is_dir() or _same_place(source, target):
            continue
        key = str(source.resolve())
        if key in seen or key in done:
            continue
        seen.add(key)
        try:
            n_copied, n_skipped = _copy_tree(source, target)
        except Exception:
            logger.warning("Could not migrate %s from %s; will retry at the next start", label, source, exc_info=True)
            continue
        done.add(key)
        _write_state(state_path, done)
        try:
            moved: Path | str = _mark_migrated(source)
        except OSError as e:
            moved = f"left in place ({type(e).__name__}: {e}); recorded in {state_path}, so it is not copied again"
        copied[label] = copied.get(label, 0) + n_copied
        logger.info(
            "Migrated %s: %d file(s) copied from %s to %s, %d already there; the old folder is now %s",
            label,
            n_copied,
            source,
            target,
            n_skipped,
            moved,
        )
    if copied.get("skills"):
        from pincer.services.skills import announce_index_change

        if settings.skills_cache_dir is not None:
            announce_index_change(settings.skills_cache_dir)
    return copied


def _read_state(path: Path) -> set[str]:
    try:
        return set(json.loads(path.read_text(encoding="utf-8")).get("migrated", []))
    except FileNotFoundError:
        return set()
    except (OSError, ValueError, AttributeError):
        logger.warning("Ignoring an unreadable %s", path, exc_info=True)
        return set()


def _write_state(path: Path, done: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"migrated": sorted(done)}, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _has_skills(folder: StoragePath) -> bool:
    try:
        return any(True for _ in folder.glob("*/SKILL.md"))
    except (FileNotFoundError, NotADirectoryError):
        return False


def _same_place(source: Path, target: StoragePath) -> bool:
    if not isinstance(target, Path):
        return False
    source, target = source.resolve(), target.resolve()
    return source == target or target.is_relative_to(source) or source.is_relative_to(target)


def _copy_tree(source: Path, target: StoragePath) -> tuple[int, int]:
    """Copy every file under `source` to the same relative key under `target`,
    skipping keys that exist. Returns (copied, skipped)."""
    copied = skipped = 0
    for dirpath, _dirnames, filenames in source.walk():
        for name in filenames:
            path = dirpath / name
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(source).as_posix()
            dest = target / rel
            if dest.exists():
                skipped += 1
                continue
            if isinstance(dest, Path):
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
            else:
                dest.upload_from(path, force_overwrite_to_cloud=True)
            copied += 1
    return copied, skipped


def _mark_migrated(source: Path) -> Path:
    """Rename `source` to `<name>.migrated` (or `.migrated.N` when taken) and return the new path."""
    dest = source.with_name(f"{source.name}.migrated")
    n = 1
    while dest.exists():
        n += 1
        dest = source.with_name(f"{source.name}.migrated.{n}")
    source.rename(dest)
    return dest
