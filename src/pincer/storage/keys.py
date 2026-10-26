"""Keys, names and listing cursors of the storage browser.

A key is a POSIX path relative to the storage root ("" is the root).
`normalize_key` is the first line of path safety: it refuses anything that is
not a plain name under the root before any path is built from it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes

from pincer.storage import clients
from pincer.storage.errors import InvalidKey

#: Folders before files, then case-insensitive by name, then the exact name.
type SortKey = tuple[int, str, str]

#: Object stores have no directories: an empty folder is this zero-byte marker.
FOLDER_MARKER = ".keep"
#: Local writes go to a file with this prefix first, then are renamed into place.
UPLOAD_TMP_PREFIX = ".pincer-upload-"

mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("text/markdown", ".markdown")


def normalize_key(key: str, *, allow_root: bool = True) -> str:
    """`key` as a clean root-relative POSIX path ("" is the root).

    Absolute, home-relative and scheme-qualified keys, `..` segments and NUL
    bytes are refused outright rather than resolved: a key is a name under
    the root, never a path computation.
    """
    raw = key.replace("\\", "/")
    if raw.startswith(("/", "~")) or clients.ANY_SCHEME_RE.match(raw) or "\x00" in raw:
        raise InvalidKey(f"Invalid key: {key!r}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise InvalidKey(f"Invalid key: {key!r}")
    normalized = "/".join(parts)
    if not normalized and not allow_root:
        raise InvalidKey("A key below the storage root is required")
    return normalized


def join_key(prefix: str, name: str) -> str:
    return f"{prefix}/{name}" if prefix else name


def etag(data: bytes) -> str:
    """The etag the text editor round-trips: a hash of the content."""
    return hashlib.sha256(data).hexdigest()[:32]


def is_hidden(name: str) -> bool:
    return name == FOLDER_MARKER or name.startswith(UPLOAD_TMP_PREFIX)


def content_type_for(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sort_key(name: str, is_dir: bool) -> SortKey:
    return (0 if is_dir else 1, name.lower(), name)


def encode_cursor(key: SortKey) -> str:
    return b64encode(json.dumps([key[0], key[2]], separators=(",", ":")).encode())


def decode_cursor(cursor: str) -> SortKey:
    try:
        kind, name = json.loads(b64decode(cursor))
        if kind not in (0, 1) or not isinstance(name, str):
            raise ValueError(cursor)
    except (ValueError, TypeError, binascii.Error) as e:
        raise InvalidKey("Invalid cursor") from e
    return (kind, name.lower(), name)
