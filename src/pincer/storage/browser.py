"""Storage browser: list, read, write, upload, move and delete files under one root.

The root is a local directory or a cloud prefix (see `pincer.storage.root`).
Every key is a POSIX path relative to it (see `pincer.storage.keys`);
`confine` refuses anything that would leave it, and the skills directory, when
it lives under the root, is protected from delete and move.

Everything here blocks — the API router runs it in a worker thread.
"""

from __future__ import annotations

import bisect
import hmac
import io
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from pincer.storage import clients, signing
from pincer.storage.errors import (
    InvalidKey,
    KeyConflict,
    KeyNotFound,
    NotText,
    ProtectedKey,
    StorageError,
    StorageUnavailable,
    TooLarge,
    describe,
)
from pincer.storage.keys import (
    FOLDER_MARKER,
    UPLOAD_TMP_PREFIX,
    content_type_for,
    decode_cursor,
    encode_cursor,
    etag,
    is_hidden,
    join_key,
    normalize_key,
    sort_key,
)
from pincer.storage.limits import (
    COPY_CHUNK,
    DEFAULT_PAGE_SIZE,
    DEFAULT_UPLOAD_MAX_BYTES,
    MAX_LIST_ENTRIES,
    MAX_PAGE_SIZE,
    MAX_TEXT_BYTES,
)
from pincer.storage.objects import objects_for
from pincer.storage.root import StoragePath, resolve_storage_root
from pincer.tools.path_sandbox import SandboxDenied, confine_path

if TYPE_CHECKING:
    from pincer.config import Settings
    from pincer.storage.objects import ObjectIO
    from pincer.storage.signing import Disposition

logger = logging.getLogger(__name__)

_STAT_WORKERS = 16
#: A save or upload holds the lock of its key, so a check and the write that
#: depends on it (an etag compare, "does it exist yet") cannot interleave with
#: another request's in this process. A fixed set, picked by the folded key:
#: case and Unicode variants of a name, one file on some disks, share a lock.
_KEY_LOCKS = tuple(threading.Lock() for _ in range(64))


def _iso(mtime: float | None) -> str | None:
    return datetime.fromtimestamp(mtime, UTC).isoformat() if mtime else None


def _fold(key: str) -> str:
    return unicodedata.normalize("NFC", key).casefold()


def _file_id(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _up_to(path: Path, root: Path) -> list[Path]:
    """`path` and its ancestors, up to and including `root`."""
    chain = [path]
    while chain[-1] != root and chain[-1] != chain[-1].parent:
        chain.append(chain[-1].parent)
    return chain


class _LimitedReader(io.RawIOBase):
    """Reads `source`, raising TooLarge once more than `limit` bytes came through."""

    def __init__(self, source: IO[bytes], limit: int) -> None:
        self._source, self._left, self._limit = source, limit, limit

    def readable(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> bytes:
        data = self._source.read(size if size is not None and size >= 0 else self._left + 1)
        self._left -= len(data)
        if self._left < 0:
            raise TooLarge(f"Uploads are limited to {self._limit} bytes")
        return data


@dataclass
class Download:
    """A file to send: a local `path`, or an open cloud `stream`."""

    name: str
    size: int | None
    content_type: str
    path: Path | None = None
    stream: IO[bytes] | None = None


class StorageBrowser:
    def __init__(
        self,
        root: StoragePath,
        protected: list[str],
        presign_ttl: int,
        signing_key: bytes,
        upload_max_bytes: int = DEFAULT_UPLOAD_MAX_BYTES,
    ) -> None:
        self.root = root
        self.protected = protected
        self.presign_ttl = presign_ttl
        self.upload_max_bytes = upload_max_bytes
        self._signing_key = signing_key
        self._objects: ObjectIO | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> StorageBrowser:
        root, protected = resolve_storage_root(settings)
        return cls(
            root,
            protected,
            settings.storage_presign_ttl,
            signing.signing_key(settings),
            upload_max_bytes=settings.storage_upload_file_max_size,
        )

    @property
    def is_cloud(self) -> bool:
        return not isinstance(self.root, Path)

    @property
    def scheme(self) -> str:
        return clients.uri_scheme(str(self.root)) if self.is_cloud else "file"

    def info(self) -> dict[str, Any]:
        return {
            "backend": self.scheme,
            "root": str(self.root),
            "protected": self.protected,
            "presign_ttl": self.presign_ttl,
            "max_text_bytes": MAX_TEXT_BYTES,
            "max_upload_bytes": self._upload_limit(),
        }

    # ── Paths ──

    def confine(self, key: str, *, allow_root: bool = True) -> StoragePath:
        """The path for `key`, guaranteed to be inside the root."""
        normalized = normalize_key(key, allow_root=allow_root)
        try:
            if isinstance(self.root, Path):
                return confine_path(self.root, normalized or ".", label="the storage root")
            return clients.confine_cloud_path(self.root, normalized, label="the storage root")
        except SandboxDenied as e:
            raise InvalidKey(str(e)) from e

    def _entry_path(self, key: str) -> StoragePath:
        """Like `confine`, but a local symlink is the link itself, not its target:
        deleting or moving a link must not touch what it points at."""
        path = self.confine(key, allow_root=False)
        if isinstance(self.root, Path):
            raw = self.root / normalize_key(key)
            if raw.is_symlink():
                return raw
        return path

    def is_protected(self, key: str) -> bool:
        """True for a protected prefix, anything inside one, and every ancestor
        of one (removing a parent folder would take the skills dir with it).

        Compares keys, which is exact on object stores. On a local disk the
        same folder can have other names; `_require_unprotected` checks those.
        """
        normalized = normalize_key(key)
        for prefix in self.protected:
            if (
                not prefix
                or not normalized
                or normalized == prefix
                or normalized.startswith(prefix + "/")
                or prefix.startswith(normalized + "/")
            ):
                return True
        return False

    def _overlaps_protected(self, path: Path) -> bool:
        """True when the local `path` is, is inside or holds a protected folder.

        Compared by file identity, not by name: on a case-insensitive disk
        `TEAM/Skills` is `team/skills`, a Unicode variant of a name is the same
        folder on APFS, and a symlink can lead into one. A symlink that is
        itself the entry is moved or deleted alone, never what it points at,
        so only the folders around it count.
        """
        assert isinstance(self.root, Path)
        link = path.is_symlink()
        around = {fid for p in _up_to(path.parent if link else path, self.root) if (fid := _file_id(p))}
        held = None if link else _file_id(path)
        for prefix in self.protected:
            guarded = self.root / prefix if prefix else self.root
            guarded_id = _file_id(guarded)
            if guarded_id is None:
                continue
            if guarded_id in around:
                return True
            if held is not None and any(_file_id(p) == held for p in _up_to(guarded, self.root)):
                return True
        return False

    def _require_unprotected(self, key: str, path: StoragePath) -> None:
        if self.is_protected(key) or (isinstance(path, Path) and self._overlaps_protected(path)):
            raise ProtectedKey(f"'{key}' is protected: it is, contains or is inside the skills directory")

    def _holds(self, outer: StoragePath, inner: StoragePath) -> bool:
        """True when the local folder `outer` contains `inner`, under whatever name."""
        if not isinstance(outer, Path) or not isinstance(inner, Path) or not isinstance(self.root, Path):
            return False
        outer_id = _file_id(outer)
        return outer_id is not None and any(_file_id(p) == outer_id for p in _up_to(inner.parent, self.root))

    def _file(self, key: str) -> StoragePath:
        path = self.confine(key, allow_root=False)
        if not path.is_file():
            raise KeyNotFound(f"No such file: {key}")
        return path

    # ── Listing ──

    def _children(self, folder: StoragePath) -> list[tuple[StoragePath, bool]]:
        """Up to MAX_LIST_ENTRIES + 1 (child, is_dir) pairs, visible entries only."""
        source: Any
        if isinstance(folder, Path):
            source = ((child, child.is_dir()) for child in folder.iterdir())
        elif list_dir := getattr(folder.client, "_list_dir", None):
            # Private, but `iterdir` drops the is-dir flag the listing already
            # has, and asking again costs a request per child. Should a
            # cloudpathlib release drop it, listing gets slower, not broken.
            source = list_dir(folder, recursive=False)
        else:
            source = ((child, child.is_dir()) for child in folder.iterdir())
        children: list[tuple[StoragePath, bool]] = []
        for child, is_dir in source:
            if child == folder or is_hidden(child.name):
                continue
            children.append((child, is_dir))
            if len(children) > MAX_LIST_ENTRIES:
                break
        return children

    @staticmethod
    def _stat(path: StoragePath) -> tuple[int | None, float | None]:
        try:
            st = path.stat()
        except Exception:
            return None, None
        return st.st_size, st.st_mtime

    def list_dir(
        self, prefix: str = "", *, cursor: str | None = None, limit: int = DEFAULT_PAGE_SIZE
    ) -> dict[str, Any]:
        """One page of a folder: folders first, then files, each by name.

        `cursor` is the `next_cursor` of the page before (None for the first).
        It names the last entry served rather than a position, so entries
        added or removed in between neither repeat nor skip the rest.
        """
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise InvalidKey(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        after = decode_cursor(cursor) if cursor else None
        normalized = normalize_key(prefix)
        folder = self.confine(normalized)
        if not folder.is_dir():
            # A fresh bucket root has no objects and so "is" no directory yet.
            if normalized:
                raise KeyNotFound(f"No such folder: {prefix}")
            return {"prefix": "", "folders": [], "files": [], "next_cursor": None, "truncated": False}

        children = self._children(folder)
        truncated = len(children) > MAX_LIST_ENTRIES
        ordered = sorted(
            ((sort_key(c.name, is_dir), c) for c, is_dir in children[:MAX_LIST_ENTRIES]), key=lambda e: e[0]
        )
        start = bisect.bisect_right([key for key, _ in ordered], after) if after else 0
        page = ordered[start : start + limit]
        has_more = start + limit < len(ordered)

        dirs = [c for key, c in page if key[0] == 0]
        files = [c for key, c in page if key[0] == 1]
        if isinstance(folder, Path) or len(files) < 2:
            stats = [self._stat(f) for f in files]
        else:
            # One HEAD request per object: run them side by side.
            with ThreadPoolExecutor(max_workers=_STAT_WORKERS) as pool:
                stats = list(pool.map(self._stat, files))

        def entry(name: str) -> tuple[str, bool]:
            key = join_key(normalized, name)
            return key, self.is_protected(key)

        return {
            "prefix": normalized,
            "folders": [
                {"key": key, "name": d.name, "protected": protected} for d in dirs for key, protected in [entry(d.name)]
            ],
            "files": [
                {
                    "key": key,
                    "name": f.name,
                    "size": size,
                    "last_modified": _iso(mtime),
                    "content_type": content_type_for(f.name),
                    "protected": protected,
                }
                for f, (size, mtime) in zip(files, stats, strict=True)
                for key, protected in [entry(f.name)]
            ],
            "next_cursor": encode_cursor(page[-1][0]) if has_more else None,
            "truncated": truncated,
        }

    def _file_entry(self, key: str, path: StoragePath) -> dict[str, Any]:
        size, mtime = self._stat(path)
        return {
            "key": key,
            "name": path.name,
            "size": size,
            "last_modified": _iso(mtime),
            "content_type": content_type_for(path.name),
            "protected": self.is_protected(key),
        }

    # ── Reading ──

    def _cloud(self) -> ObjectIO:
        """The object IO of a cloud root, built on first use."""
        assert not isinstance(self.root, Path)
        if self._objects is None:
            self._objects = objects_for(self.root)
        return self._objects

    def open_read(self, key: str) -> Download:
        """The file at `key`, ready to send. A cloud file is a stream from the
        provider, which the caller must close."""
        path = self._file(key)
        size, _ = self._stat(path)
        content_type = content_type_for(path.name)
        if isinstance(path, Path):
            return Download(path.name, size, content_type, path=path)
        return Download(path.name, size, content_type, stream=self._cloud().open(path))

    def _read(self, path: StoragePath) -> tuple[bytes, object]:
        """The content of `path`, and the version a conditional write checks
        (None for a local file: only the key lock guards it)."""
        if isinstance(path, Path):
            try:
                return path.read_bytes(), None
            except FileNotFoundError as e:
                raise KeyNotFound(f"No such file: {path.name}") from e
        return self._cloud().read(path)

    def read_text(self, key: str) -> dict[str, Any]:
        """A UTF-8 text file with its etag, for the editor."""
        path = self._file(key)
        size, _ = self._stat(path)
        if size is not None and size > MAX_TEXT_BYTES:
            raise TooLarge(f"{key} is larger than {MAX_TEXT_BYTES} bytes; download it instead")
        data, _version = self._read(path)
        if len(data) > MAX_TEXT_BYTES:
            raise TooLarge(f"{key} is larger than {MAX_TEXT_BYTES} bytes; download it instead")
        if b"\x00" in data:
            raise NotText(f"{key} is not a text file")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise NotText(f"{key} is not UTF-8 text") from e
        return {"key": normalize_key(key), "content": content, "etag": etag(data), "size": len(data)}

    # ── Writing ──

    @staticmethod
    def _lock(key: str, path: StoragePath) -> threading.Lock:
        """The lock of one file. A local file is named by its confined path,
        where symlinks are resolved: `alias/doc.md` with `alias -> team` is
        `team/doc.md`. Folding covers the case and Unicode variants a
        case-insensitive disk also treats as one name."""
        name = str(path) if isinstance(path, Path) else key
        return _KEY_LOCKS[hash(_fold(name)) % len(_KEY_LOCKS)]

    def _store(
        self, path: StoragePath, source: IO[bytes], *, if_version: object = None, if_absent: bool = False
    ) -> None:
        """Write `source` to `path`. `if_absent` refuses (KeyConflict) when the
        file exists; `if_version` when a cloud object is no longer that version."""
        if not isinstance(path, Path):
            self._cloud().write(
                path, source, content_type=content_type_for(path.name), if_version=if_version, if_absent=if_absent
            )
            return
        # Written next to the target and renamed over it: a reader never sees half a file.
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{UPLOAD_TMP_PREFIX}{secrets.token_hex(6)}")
        try:
            with tmp.open("wb") as out:
                shutil.copyfileobj(source, out, COPY_CHUNK)
            if not if_absent:
                tmp.replace(path)
                return
            try:
                # A hard link is created only if the name is free: no other
                # writer, in this process or not, can slip in between.
                os.link(tmp, path)
            except FileExistsError as e:
                raise KeyConflict(f"{path.name} already exists") from e
            except OSError:
                # A filesystem without hard links: check, then rename.
                if path.exists():
                    raise KeyConflict(f"{path.name} already exists") from None
                tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

    def write_text(self, key: str, content: str, expected_etag: str | None = None) -> dict[str, Any]:
        """Save `content` to `key`. With `expected_etag`, refuse (409) unless the
        file is still the version that was read — someone else's save in between
        must not be overwritten silently. Without it the write is unconditional."""
        data = content.encode("utf-8")
        if len(data) > MAX_TEXT_BYTES:
            raise TooLarge(f"Text files are limited to {MAX_TEXT_BYTES} bytes")
        normalized = normalize_key(key, allow_root=False)
        path = self.confine(normalized, allow_root=False)
        if path.is_dir():
            raise KeyConflict(f"{key} is a folder")
        with self._lock(normalized, path):
            if expected_etag is None:
                self._store(path, io.BytesIO(data))
                return {"key": normalized, "etag": etag(data), "size": len(data)}
            try:
                current, version = self._read(path)
            except KeyNotFound as e:
                raise KeyConflict(f"{key} was deleted on the server") from e
            # Bytes, not str: compare_digest refuses a str with non-ASCII characters.
            if not hmac.compare_digest(etag(current).encode(), expected_etag.encode()):
                raise KeyConflict(f"{key} changed on the server since it was opened")
            try:
                self._store(path, io.BytesIO(data), if_version=version)
            except KeyConflict as e:
                raise KeyConflict(f"{key} changed on the server since it was opened") from e
        return {"key": normalized, "etag": etag(data), "size": len(data)}

    def upload(self, prefix: str, filename: str, source: IO[bytes], *, overwrite: bool = False) -> dict[str, Any]:
        """Store one uploaded file; see `upload_files`."""
        return self.upload_files(prefix, [(filename, source)], overwrite=overwrite)[0]

    def upload_files(
        self, prefix: str, files: list[tuple[str, IO[bytes]]], *, overwrite: bool = False
    ) -> list[dict[str, Any]]:
        """Store uploaded files under `prefix`. A name may carry subfolders
        ("reports/q1.pdf", from a folder pick); they are kept.

        Every file is checked before any is written, and any of these refuses
        the whole upload: a bad name (400); a file that exists without
        `overwrite`, a folder in the way, a file where a folder is needed (an
        existing one, or another file of the same upload: `x` and `x/y`) or the
        same name twice (409); one over the size limit (413). Only a failure
        while writing (another writer taking a name first, an unavailable
        store) can leave earlier files stored; its message names them.
        """
        base = normalize_key(prefix)
        keys: list[str] = []
        for filename, _source in files:
            relative = normalize_key(filename or "")
            if not relative:
                raise InvalidKey("An uploaded file has no name")
            keys.append(join_key(base, relative))
        folded = {_fold(key) for key in keys}
        if len(folded) < len(keys):
            twice = next(key for i, key in enumerate(keys) if _fold(key) in {_fold(k) for k in keys[:i]})
            raise KeyConflict(f"{twice} is in the upload twice")

        planned: list[tuple[str, StoragePath, IO[bytes]]] = []
        is_file: dict[str, bool] = {}
        for key, (_filename, source) in zip(keys, files, strict=True):
            parts = key.split("/")
            for depth in range(1, len(parts)):
                folder = "/".join(parts[:depth])
                if _fold(folder) in folded:
                    raise KeyConflict(f"{folder} is both a file and a folder in this upload")
                if folder not in is_file:
                    is_file[folder] = self.confine(folder).is_file()
                if is_file[folder]:
                    raise KeyConflict(f"{folder} is a file, so {key} cannot be stored under it")
            path = self.confine(key, allow_root=False)
            if path.is_dir():
                raise KeyConflict(f"{key} is a folder")
            if not overwrite and path.exists():
                raise KeyConflict(f"{key} already exists")
            planned.append((key, path, source))

        bodies: list[IO[bytes]] = []
        try:
            for _key, _path, source in planned:
                bodies.append(self._within_upload_limit(source))
            stored: list[dict[str, Any]] = []
            for (key, path, _source), body in zip(planned, bodies, strict=True):
                try:
                    with self._lock(key, path):
                        self._store(path, body, if_absent=not overwrite)
                except Exception as e:
                    if not stored:
                        raise
                    names = ", ".join(entry["key"] for entry in stored)
                    error: type[StorageError] = StorageUnavailable
                    if isinstance(e, StorageError):
                        error = type(e)
                    elif isinstance(e, FileExistsError | IsADirectoryError | NotADirectoryError):
                        error = KeyConflict
                    reason = str(e) if isinstance(e, StorageError) else describe(e)
                    raise error(f"Storing {key} failed ({reason}); stored before it: {names}") from e
                stored.append(self._file_entry(key, path))
            return stored
        finally:
            for body, (_key, _path, source) in zip(bodies, planned, strict=False):
                if body is not source:
                    body.close()

    def _upload_limit(self) -> int:
        """`upload_max_bytes`, or less where the store takes less in one write (S3: 5 GiB)."""
        if isinstance(self.root, Path):
            return self.upload_max_bytes
        store_max = self._cloud().max_object_bytes
        return self.upload_max_bytes if store_max is None else min(self.upload_max_bytes, store_max)

    def _within_upload_limit(self, source: IO[bytes]) -> IO[bytes]:
        """`source`, refused with TooLarge if it is over the upload limit.

        Checked before anything is written: a failure half-way would store half
        a file. A source that cannot tell its size is spooled first, up to the limit.
        """
        limit = self._upload_limit()
        too_large = TooLarge(f"Uploads are limited to {limit} bytes")
        if source.seekable():
            start = source.tell()
            size = source.seek(0, os.SEEK_END) - start
            source.seek(start)
            if size > limit:
                raise too_large
            return source
        spool = tempfile.SpooledTemporaryFile(max_size=COPY_CHUNK * 8)  # noqa: SIM115 — the caller closes it
        shutil.copyfileobj(_LimitedReader(source, limit), spool, COPY_CHUNK)
        spool.seek(0)
        return spool  # type: ignore[return-value]

    def mkdir(self, prefix: str) -> dict[str, Any]:
        key = normalize_key(prefix, allow_root=False)
        path = self.confine(key, allow_root=False)
        if path.exists():
            raise KeyConflict(f"{key} already exists")
        if isinstance(path, Path):
            path.mkdir(parents=True)
        else:
            self._store(path / FOLDER_MARKER, io.BytesIO(b""))
        return {"key": key, "name": path.name, "protected": self.is_protected(key)}

    def _remove(self, path: StoragePath) -> None:
        if isinstance(path, Path):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        elif path.is_dir():
            path.rmtree()
        else:
            path.unlink()

    def delete(self, key: str, *, folder: bool) -> dict[str, Any]:
        """Delete the file `key`, or with `folder` the folder `key` and everything in it."""
        normalized = normalize_key(key, allow_root=False)
        path = self._entry_path(normalized)
        self._require_unprotected(normalized, path)
        is_link = isinstance(path, Path) and path.is_symlink()
        if folder:
            if not path.is_dir() or is_link:
                raise KeyNotFound(f"No such folder: {key}")
        elif not (path.is_file() or is_link):
            raise KeyNotFound(f"No such file: {key}")
        self._remove(path)
        return {"key": normalized, "kind": "folder" if folder else "file"}

    def move(self, src: str, dst: str, *, overwrite: bool = False) -> dict[str, Any]:
        """Move or rename a file or folder. Cloud stores copy then delete.

        A destination inside a protected folder is refused like a source in
        one: the skills dir changes only through upload and the editor.
        """
        src_key = normalize_key(src, allow_root=False)
        dst_key = normalize_key(dst, allow_root=False)
        if src_key == dst_key:
            raise InvalidKey("Source and destination are the same")
        source = self._entry_path(src_key)
        target = self._entry_path(dst_key)
        if dst_key.startswith(src_key + "/") or self._holds(source, target):
            raise InvalidKey("A folder cannot be moved into itself")
        if src_key.startswith(dst_key + "/") or self._holds(target, source):
            # Replacing a folder with something inside it would delete the source too.
            raise KeyConflict(f"{dst_key} contains {src_key}")
        self._require_unprotected(src_key, source)
        self._require_unprotected(dst_key, target)

        source_is_link = isinstance(source, Path) and source.is_symlink()
        if not (source.exists() or source_is_link):
            raise KeyNotFound(f"No such file or folder: {src}")
        # One file under two names that differ in case or Unicode form, on a
        # disk that treats them as one: a rename, not a replacement.
        renaming = (
            isinstance(source, Path)
            and isinstance(target, Path)
            and _fold(src_key) == _fold(dst_key)
            and target.exists()
            and os.path.samefile(source, target)
        )
        if target.exists() and not renaming:
            if not overwrite:
                raise KeyConflict(f"{dst_key} already exists")
            if target.is_dir() != source.is_dir():
                raise KeyConflict(f"{dst_key} exists and is a {'folder' if target.is_dir() else 'file'}")
            self._remove(target)

        if isinstance(source, Path):
            assert isinstance(target, Path)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        elif source.is_dir():
            assert not isinstance(target, Path)
            for dirpath, _dirs, files in source.walk():
                rel = dirpath.parts[len(source.parts) :]
                for name in files:
                    (dirpath / name).rename(target.joinpath(*rel, name))
            if source.exists():
                # Leftovers no file listing shows, such as the "dir/" placeholder
                # objects some tools create.
                source.rmtree()
        else:
            assert not isinstance(target, Path)
            source.rename(target)
        return {"from": src_key, "to": dst_key}

    # ── Links ──

    def presign(self, key: str, disposition: Disposition = "attachment") -> dict[str, Any]:
        """A link that serves `key` without a bearer token, valid for `presign_ttl` seconds.

        Cloud roots sign with the provider (see `pincer.storage.objects`); local
        roots get a relative `/api/storage/signed/<token>` URL, HMAC-signed (see
        `pincer.storage.signing`). Only the API's own responses carry the
        nosniff and sandbox headers: a cloud link is served by the provider,
        from its own origin.
        """
        path = self._file(key)
        normalized = normalize_key(key)
        expires = int(time.time()) + self.presign_ttl
        if isinstance(path, Path):
            url = f"/api/storage/signed/{self.sign(normalized, expires, disposition)}"
        else:
            url = self._cloud().presign(path, disposition, self.presign_ttl)
        return {"url": url, "expires_at": datetime.fromtimestamp(expires, UTC).isoformat()}

    def sign(self, key: str, expires: int, disposition: Disposition) -> str:
        return signing.sign(self._signing_key, key, expires, disposition)

    def verify(self, token: str) -> tuple[str, Disposition]:
        return signing.verify(self._signing_key, token)


_browser: tuple[Any, StorageBrowser] | None = None


def get_storage_browser(settings: Settings) -> StorageBrowser:
    """The browser for `settings`, built once per settings object (clients are
    not free to build: an S3 client starts a boto3 session)."""
    global _browser
    if _browser is None or _browser[0] is not settings:
        _browser = (settings, StorageBrowser.from_settings(settings))
    return _browser[1]
