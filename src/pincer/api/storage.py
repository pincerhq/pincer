"""Storage API — browse, preview, edit, upload and manage files under the storage root.

The root is `PINCER_STORAGE_ROOT`, else the bucket of a cloud skills dir, else
`<data_dir>/storage` (see `pincer.storage.browser.resolve_storage_root`). Keys
are POSIX paths relative to it. The skills directory is protected from delete
and move when it lives under the root.

Downloads and previews go through presigned links (`/presign`): the browser
can then use them in `<img src>` or a plain navigation, which cannot carry a
bearer token. A cloud root signs with the provider; a local root signs with
the JWT secret and serves the file from the public `/signed/{token}` route.

Reading needs any authenticated identity; changing files needs a signed-in
session, not an API key (see `InteractiveIdentity`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from typing import IO, Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from starlette.responses import Response

from pincer.api.auth import InteractiveIdentity
from pincer.config import get_settings_relaxed
from pincer.storage.browser import Download, StorageBrowser, get_storage_browser
from pincer.storage.clients import STORAGE_HINTS
from pincer.storage.errors import KeyNotFound, StorageError, describe
from pincer.storage.limits import COPY_CHUNK, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, MAX_TEXT_BYTES
from pincer.storage.signing import Disposition

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/storage", tags=["storage"])

#: Served files may be HTML or SVG: never let one run script on the API's origin.
_SAFE_FILE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'",
}


# ── Models ───────────────────────────────────────────────────────────────────


class StorageInfoOut(BaseModel):
    backend: str = Field(description="file, s3, az or gs")
    root: str
    protected: list[str] = Field(description="Root-relative prefixes that cannot be deleted or moved")
    presign_ttl: int = Field(description="Lifetime of presigned links, in seconds")
    max_text_bytes: int
    max_upload_bytes: int = Field(
        description="Largest file an upload may carry: PINCER_STORAGE_UPLOAD_FILE_MAX_SIZE, at most 5 GiB on S3"
    )


class FolderOut(BaseModel):
    key: str
    name: str
    protected: bool


class FileOut(BaseModel):
    key: str
    name: str
    size: int | None
    last_modified: str | None
    content_type: str
    protected: bool


class ListingOut(BaseModel):
    prefix: str
    folders: list[FolderOut]
    files: list[FileOut]
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page; null on the last page")
    truncated: bool = Field(description="True when the folder has more entries than the browser can page through")


class PresignOut(BaseModel):
    url: str = Field(description="Absolute for cloud roots; a path on this API for local roots")
    expires_at: str


class TextOut(BaseModel):
    key: str
    content: str
    etag: str
    size: int


class TextIn(BaseModel):
    key: str = Field(min_length=1)
    content: str = Field(description=f"UTF-8 text, at most {MAX_TEXT_BYTES} bytes encoded (else 413)")
    etag: str | None = Field(default=None, description="The etag that was read; omit to overwrite unconditionally")


class TextSavedOut(BaseModel):
    key: str
    etag: str
    size: int


class UploadOut(BaseModel):
    files: list[FileOut]


class FolderIn(BaseModel):
    prefix: str = Field(min_length=1)


class MoveIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)
    overwrite: bool = False


class MoveOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str


class DeletedOut(BaseModel):
    key: str
    kind: Literal["file", "folder"]


# ── Plumbing ─────────────────────────────────────────────────────────────────


def _http_error(e: Exception, scheme: str) -> HTTPException:
    """The HTTP error a failed storage operation answers with."""
    if isinstance(e, StorageError):
        return HTTPException(status_code=e.status, detail=str(e))
    if isinstance(e, FileNotFoundError):
        return HTTPException(status_code=404, detail="Not found")
    if isinstance(e, FileExistsError | IsADirectoryError | NotADirectoryError):
        # The OS message names the server's absolute path: leave it out.
        return HTTPException(status_code=409, detail="Conflicts with an existing file or folder")
    # SDK errors: credentials, a missing bucket, an unreachable endpoint.
    logger.error("Storage operation failed", exc_info=e)
    hint = STORAGE_HINTS.get(scheme, "")
    return HTTPException(status_code=503, detail=f"Storage unavailable: {describe(e)}. {hint}".strip())


def storage_browser() -> StorageBrowser:
    """The browser for the current settings. A dependency, so tests can swap it
    through `app.dependency_overrides`; FastAPI runs it in a worker thread,
    since building a cloud client blocks."""
    settings = get_settings_relaxed()
    try:
        return get_storage_browser(settings)
    except Exception as e:
        raise _http_error(e, "") from e


StorageBrowserDep = Annotated[StorageBrowser, Depends(storage_browser)]


async def _run[T](browser: StorageBrowser, op: Callable[[StorageBrowser], T]) -> T:
    """Run a blocking storage operation in a worker thread, mapping failures to HTTP errors."""
    try:
        return await asyncio.to_thread(op, browser)
    except Exception as e:
        raise _http_error(e, browser.scheme) from e


def _content_disposition(disposition: Disposition, name: str) -> str:
    return f"{disposition}; filename*=utf-8''{quote(name)}"


def _iter_stream(stream: IO[bytes]) -> Iterator[bytes]:
    try:
        while chunk := stream.read(COPY_CHUNK):
            yield chunk
    finally:
        stream.close()


def _send(download: Download, disposition: Disposition) -> Response:
    headers = {**_SAFE_FILE_HEADERS, "Content-Disposition": _content_disposition(disposition, download.name)}
    if download.path is not None:
        return FileResponse(download.path, media_type=download.content_type, headers=headers)
    assert download.stream is not None
    if download.size is not None:
        headers["Content-Length"] = str(download.size)
    return StreamingResponse(
        _iter_stream(download.stream),
        media_type=download.content_type,
        headers=headers,
        background=BackgroundTask(download.stream.close),
    )


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("", response_model=StorageInfoOut)
async def storage_info(browser: StorageBrowserDep) -> dict[str, object]:
    """The storage backend, its root, the protected prefixes and the presign TTL."""
    return await _run(browser, lambda b: b.info())


@router.get("/list", response_model=ListingOut)
async def list_folder(
    browser: StorageBrowserDep,
    prefix: str = Query("", description="Folder key; empty for the root"),
    cursor: str | None = Query(None, description="`next_cursor` of the previous page"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Entries per page"),
) -> dict[str, object]:
    """One page of the folders and files directly inside `prefix`: folders first, then files, by name."""
    return await _run(browser, lambda b: b.list_dir(prefix, cursor=cursor, limit=limit))


@router.get("/download")
async def download(browser: StorageBrowserDep, key: str = Query(..., min_length=1)) -> Response:
    """Download a file with the bearer token (fallback for clients that cannot use `/presign`)."""
    return _send(await _run(browser, lambda b: b.open_read(key)), "attachment")


@router.get("/presign", response_model=PresignOut)
async def presign(
    browser: StorageBrowserDep,
    key: str = Query(..., min_length=1),
    disposition: Annotated[Disposition, Query()] = "attachment",
) -> dict[str, object]:
    """A link to `key` that needs no bearer token, valid for `presign_ttl` seconds."""
    return await _run(browser, lambda b: b.presign(key, disposition))


@router.get("/signed/{token}")
async def signed(token: str, browser: StorageBrowserDep) -> Response:
    """Serve a file from a local root through a link minted by `/presign`.

    Public: the token is the credential (HMAC over key, expiry and disposition).
    """

    def open_signed(b: StorageBrowser) -> tuple[Download, Disposition]:
        if b.is_cloud:
            raise KeyNotFound("Signed links of a cloud root are served by the storage provider")
        key, disposition = b.verify(token)
        return b.open_read(key), disposition

    download_, disposition = await _run(browser, open_signed)
    return _send(download_, disposition)


@router.get("/text", response_model=TextOut)
async def read_text(browser: StorageBrowserDep, key: str = Query(..., min_length=1)) -> dict[str, object]:
    """A UTF-8 text file (up to 2 MB) and its etag, for the editor."""
    return await _run(browser, lambda b: b.read_text(key))


@router.put("/text", response_model=TextSavedOut)
async def save_text(body: TextIn, identity: InteractiveIdentity, browser: StorageBrowserDep) -> dict[str, object]:
    """Save a text file. With `etag`, 409 if it changed on the server since it was read."""
    result = await _run(browser, lambda b: b.write_text(body.key, body.content, body.etag))
    logger.info("Storage: %s saved %s", identity.pincer_user_id, body.key)
    return result


@router.post("/upload", response_model=UploadOut)
async def upload(
    identity: InteractiveIdentity,
    browser: StorageBrowserDep,
    files: Annotated[list[UploadFile], File(description="Files to store; a name may carry subfolders")],
    prefix: str = Query("", description="Folder to upload into"),
    overwrite: bool = Query(False, description="Replace files that already exist (else 409)"),
) -> dict[str, object]:
    """Upload files into `prefix`. Every file is checked first: one that already exists (409, unless
    `overwrite`) or is larger than PINCER_STORAGE_UPLOAD_FILE_MAX_SIZE (413) stores none of them."""
    sources: list[tuple[str, IO[bytes]]] = [(file.filename or "", file.file) for file in files]
    stored = await _run(browser, lambda b: b.upload_files(prefix, sources, overwrite=overwrite))
    logger.info("Storage: %s uploaded %s", identity.pincer_user_id, [s["key"] for s in stored])
    return {"files": stored}


@router.post("/folders", response_model=FolderOut, status_code=201)
async def create_folder(body: FolderIn, identity: InteractiveIdentity, browser: StorageBrowserDep) -> dict[str, object]:
    """Create a folder (an empty `.keep` marker on object stores)."""
    result = await _run(browser, lambda b: b.mkdir(body.prefix))
    logger.info("Storage: %s created folder %s", identity.pincer_user_id, result["key"])
    return result


@router.post("/move", response_model=MoveOut, response_model_by_alias=True)
async def move(body: MoveIn, identity: InteractiveIdentity, browser: StorageBrowserDep) -> dict[str, object]:
    """Move or rename a file or folder. 409 if the target exists and `overwrite` is false, or if the
    target contains the source. A source or target in a protected prefix is refused (403)."""
    result = await _run(browser, lambda b: b.move(body.from_, body.to, overwrite=body.overwrite))
    logger.info("Storage: %s moved %s to %s", identity.pincer_user_id, result["from"], result["to"])
    return result


@router.delete("/object", response_model=DeletedOut)
async def delete(
    identity: InteractiveIdentity,
    browser: StorageBrowserDep,
    key: str | None = Query(None, description="A file to delete"),
    prefix: str | None = Query(None, description="A folder to delete, with everything in it"),
) -> dict[str, object]:
    """Delete a file (`key`) or a folder (`prefix`). Protected prefixes and their parents are refused."""
    if (key is None) == (prefix is None):
        raise HTTPException(status_code=400, detail="Pass exactly one of key or prefix")
    target = key if key is not None else prefix
    assert target is not None
    result = await _run(browser, lambda b: b.delete(target, folder=prefix is not None))
    logger.info("Storage: %s deleted %s %s", identity.pincer_user_id, result["kind"], result["key"])
    return result
