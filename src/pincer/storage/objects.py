"""Reading, writing and presigning objects of a cloud storage root.

cloudpathlib maps every key to one file in its local cache, shared by every
request: a save that overlaps a download of the same key can upload the old
content, and two writes can see that file unlinked half-way. Here a read
streams from the provider and a write uploads from the caller's own buffer,
so requests share nothing.

A write can also be conditional, which cloudpathlib has no API for:
`if_version` (the version `read` returned) or `if_absent` makes the provider
itself refuse the write when the object changed or exists, so no other
writer, on any replica, can get between the check and the write. A refused
write raises KeyConflict.

Listing, stat, move and delete still go through cloudpathlib: none of them
touch its cache.

With a customer-provided encryption key (`pincer.storage.encryption`) every
request that carries content carries the key too, and an object stored
without it (written before encryption was enabled) is read without it.
Provider links cannot carry the key, so `presignable` is False and the
storage browser hands out its own signed links instead, which it serves
decrypted.
"""

from __future__ import annotations

import contextlib
import io
import logging
import shutil
from typing import IO, TYPE_CHECKING, Any
from urllib.parse import quote

from pincer.storage.encryption import keyed_request_hit_plain_object
from pincer.storage.errors import KeyConflict, KeyNotFound
from pincer.storage.keys import content_type_for, etag
from pincer.storage.limits import COPY_CHUNK

if TYPE_CHECKING:
    from cloudpathlib import CloudPath

    from pincer.storage.encryption import StorageKey
    from pincer.storage.signing import Disposition

logger = logging.getLogger(__name__)

#: What an S3 link may serve inline. Anything else could run script on the
#: bucket's origin, which sends none of the API's headers: HTML, SVG and every
#: `*+xml` / `*/xml` type (browsers render those as XML, XHTML script included).
#: So S3 links to anything else always download.
_INLINE_TYPES = frozenset(
    {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/csv",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/bmp",
        "image/x-icon",
        "image/vnd.microsoft.icon",
    }
)
#: Largest object S3 takes in one PUT.
_S3_MAX_PUT_BYTES = 5 * 1024**3


def _inline_safe(content_type: str) -> bool:
    return content_type in _INLINE_TYPES or content_type.startswith(("audio/", "video/"))


type ObjectIO = PathObjects | SdkObjects


def objects_for(root: CloudPath, *, keyed: bool = True) -> ObjectIO:
    """The object IO for `root`'s client; without its customer key when not `keyed`."""
    from cloudpathlib import AzureBlobClient, GSClient, S3Client

    client = root.client
    key: StorageKey | None = getattr(client, "encryption_key", None) if keyed else None
    if isinstance(client, S3Client):
        return S3Objects(client, key)
    if isinstance(client, AzureBlobClient):
        return AzureObjects(client, key)
    if isinstance(client, GSClient):
        return GSObjects(client, key)
    return PathObjects(key)


def _provider_link(path: CloudPath, ttl: int) -> str:
    return path.as_url(presign=True, expire_seconds=ttl)


class PathObjects:
    """Object IO through cloudpathlib's path API, for a client with no SDK of
    its own below it (cloudpathlib's local test clients). The conditions are
    checked before the write, not by the store, so only the browser's
    in-process lock keeps the check and the write together."""

    #: Largest object one write may store; None for no limit of the store's own.
    max_object_bytes: int | None = None

    def __init__(self, encryption_key: StorageKey | None = None) -> None:
        self.encryption_key = encryption_key

    @property
    def presignable(self) -> bool:
        """Whether `presign` gives a link the provider serves; False when objects
        are encrypted with a customer key, which a provider link cannot carry."""
        return self.encryption_key is None

    def read(self, path: CloudPath) -> tuple[bytes, object]:
        """The object's content, and a version to pass back as `if_version`."""
        try:
            with path.open("rb", force_overwrite_from_cloud=True) as f:
                data = f.read()
        except FileNotFoundError as e:
            raise KeyNotFound(f"No such file: {path}") from e
        return data, etag(data)

    def open(self, path: CloudPath) -> IO[bytes]:
        """The object's content as a stream, for the caller to close."""
        return path.open("rb", force_overwrite_from_cloud=True)  # type: ignore[no-any-return]

    def write(
        self,
        path: CloudPath,
        source: IO[bytes],
        *,
        content_type: str,
        if_version: object = None,
        if_absent: bool = False,
    ) -> None:
        if if_absent and path.exists():
            raise KeyConflict(f"{path} already exists")
        if if_version is not None and not if_absent:
            try:
                current = self.read(path)[1]
            except KeyNotFound:
                current = None
            if current != if_version:
                raise KeyConflict(f"{path} changed")
        with path.open("wb", force_overwrite_to_cloud=True) as out:
            shutil.copyfileobj(source, out, COPY_CHUNK)

    def presign(self, path: CloudPath, disposition: Disposition, ttl: int) -> str:
        """A link that serves `path` without credentials for `ttl` seconds."""
        return _provider_link(path, ttl)


class SdkObjects:
    """Object IO straight through a provider SDK, with conditions the store enforces.

    `write` decides the condition once: `if_absent` (create only) wins over
    `if_version`. Each provider sends it in `_put` and says in `_refused`
    whether an error is that condition failing.
    """

    #: Largest object one write may store; None for no limit of the store's own.
    max_object_bytes: int | None = None
    #: The customer key every request that carries content sends; None for none.
    encryption_key: StorageKey | None = None

    @property
    def presignable(self) -> bool:
        """False with a customer key: a provider link cannot carry it."""
        return self.encryption_key is None

    def read(self, path: CloudPath) -> tuple[bytes, object]:
        raise NotImplementedError

    def open(self, path: CloudPath) -> IO[bytes]:
        raise NotImplementedError

    def _put(self, path: CloudPath, source: IO[bytes], content_type: str, *, version: object, absent: bool) -> None:
        raise NotImplementedError

    def _refused(self, error: Exception) -> bool:
        raise NotImplementedError

    def write(
        self,
        path: CloudPath,
        source: IO[bytes],
        *,
        content_type: str,
        if_version: object = None,
        if_absent: bool = False,
    ) -> None:
        try:
            self._put(path, source, content_type, version=None if if_absent else if_version, absent=if_absent)
        except Exception as e:
            if self._refused(e):
                raise KeyConflict(f"{path} changed or already exists") from e
            raise

    def presign(self, path: CloudPath, disposition: Disposition, ttl: int) -> str:
        """A link that serves `path` without credentials for `ttl` seconds.
        Served with the content type the object was stored with."""
        return _provider_link(path, ttl)


class S3Objects(SdkObjects):
    """S3 through boto3: `IfMatch` on the ETag, `IfNoneMatch: *` for a new object.

    One PUT per write, so objects are capped at 5 GiB: multipart uploads would
    move the condition to CompleteMultipartUpload, and uploads that large do not
    pass through the API anyway (Starlette spools the whole request first).
    """

    max_object_bytes = _S3_MAX_PUT_BYTES

    def __init__(self, client: Any, encryption_key: StorageKey | None = None) -> None:
        self._client = client
        self._s3 = client.client
        self._unconditional = False
        self._presigner: Any = None
        self.encryption_key = encryption_key
        self._sse: dict[str, str] = encryption_key.s3_args() if encryption_key else {}

    def _keyed_then_plain(self, call: Any, path: CloudPath) -> Any:
        """`call(**sse)`, once more without the key for an object stored without one."""
        from botocore.exceptions import ClientError

        if not self._sse:
            return call()
        try:
            return call(**self._sse)
        except ClientError as e:
            if not keyed_request_hit_plain_object(e):
                raise
        return call()

    def _get(self, path: CloudPath) -> Any:
        from botocore.exceptions import ClientError

        try:
            return self._keyed_then_plain(
                lambda **sse: self._s3.get_object(Bucket=path.bucket, Key=path.key, **sse),  # type: ignore[attr-defined]
                path,
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise KeyNotFound(f"No such file: {path}") from e
            raise

    def read(self, path: CloudPath) -> tuple[bytes, object]:
        obj = self._get(path)
        with contextlib.closing(obj["Body"]) as body:
            return body.read(), obj["ETag"]

    def open(self, path: CloudPath) -> IO[bytes]:
        return self._get(path)["Body"]  # type: ignore[no-any-return]

    def _put(self, path: CloudPath, source: IO[bytes], content_type: str, *, version: object, absent: bool) -> None:
        from botocore.exceptions import ClientError, ParamValidationError

        condition: dict[str, Any] = {}
        if absent:
            condition["IfNoneMatch"] = "*"
        elif version is not None:
            condition["IfMatch"] = version
        start = source.tell()

        def put(**extra: Any) -> None:
            source.seek(start)
            self._s3.put_object(
                Bucket=path.bucket,  # type: ignore[attr-defined]
                Key=path.key,  # type: ignore[attr-defined]
                Body=source,
                ContentType=content_type,
                **self._sse,
                **extra,
            )

        if not condition:
            put()
            return
        if not self._unconditional:
            try:
                put(**condition)
                return
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") != "NotImplemented":
                    raise
                self._fall_back(path, e)
            except ParamValidationError as e:
                # A botocore from before conditional writes.
                self._fall_back(path, e)
        # The store cannot refuse the write, so check first: the browser holds
        # the file's lock, which keeps the check and the write together here.
        current = self._etag_of(path)
        if (absent and current is not None) or (version is not None and current != version):
            raise KeyConflict(f"{path} changed or already exists")
        put()

    def _etag_of(self, path: CloudPath) -> str | None:
        """The object's ETag, or None when there is no such object."""
        from botocore.exceptions import ClientError

        try:
            head = self._keyed_then_plain(
                lambda **sse: self._s3.head_object(Bucket=path.bucket, Key=path.key, **sse),  # type: ignore[attr-defined]
                path,
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        found: str = head["ETag"]
        return found

    def _refused(self, error: Exception) -> bool:
        from botocore.exceptions import ClientError

        return isinstance(error, ClientError) and error.response.get("Error", {}).get("Code") in (
            "PreconditionFailed",
            "ConditionalRequestConflict",
        )

    def _fall_back(self, path: CloudPath, error: Exception) -> None:
        """Some S3-compatible servers have no conditional writes: from now on
        check before writing, which only the browser's in-process lock keeps
        together with the write."""
        self._unconditional = True
        logger.warning(
            "Storage: %s does not take conditional writes (%s) — saves are only serialised within this process",
            path.bucket,  # type: ignore[attr-defined]
            error,
        )

    def presign(self, path: CloudPath, disposition: Disposition, ttl: int) -> str:
        """A SigV4 link carrying the response headers: cloudpathlib's presign
        has none, and S3 takes them as parameters, so a download link
        downloads instead of navigating."""
        content_type = content_type_for(path.name)
        if not _inline_safe(content_type):
            disposition = "attachment"
        url: str = self._s3_presigner().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": path.bucket,  # type: ignore[attr-defined]
                "Key": path.key,  # type: ignore[attr-defined]
                "ResponseContentDisposition": f"{disposition}; filename*=UTF-8''{quote(path.name)}",
                "ResponseContentType": content_type,
            },
            ExpiresIn=ttl,
        )
        return url

    def _s3_presigner(self) -> Any:
        """A boto3 client like the root's, but signing with SigV4.

        boto3 may presign with SigV2 against a custom endpoint, and S3-compatible
        servers (SeaweedFS, MinIO) reject a SigV2 link that carries response-*
        parameters. AWS itself has deprecated SigV2.
        """
        if self._presigner is None:
            from botocore.config import Config

            base = self._s3.meta
            config = Config(signature_version="s3v4")
            self._presigner = self._client.sess.client(
                "s3",
                endpoint_url=base.endpoint_url,
                region_name=base.region_name,
                config=base.config.merge(config) if base.config is not None else config,
            )
        return self._presigner


class _Reader(io.RawIOBase):
    """An Azure blob download as a file object."""

    def __init__(self, downloader: Any) -> None:
        self._downloader = downloader

    def readable(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> bytes:
        data: bytes = self._downloader.read(-1 if size is None else size)
        return data


class AzureObjects(SdkObjects):
    """Azure Blob through azure-storage-blob: `If-Match` on the ETag, no overwrite for a new blob."""

    def __init__(self, client: Any, encryption_key: StorageKey | None = None) -> None:
        self._service = client.service_client
        self.encryption_key = encryption_key

    def _blob(self, path: CloudPath) -> Any:
        return self._service.get_blob_client(container=path.container, blob=path.blob)  # type: ignore[attr-defined]

    def _cpk(self) -> dict[str, Any]:
        return {"cpk": self.encryption_key.azure_cpk()} if self.encryption_key else {}

    def _download(self, path: CloudPath) -> Any:
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

        blob = self._blob(path)
        try:
            try:
                return blob.download_blob(**self._cpk())
            except HttpResponseError as e:
                if not (self.encryption_key and keyed_request_hit_plain_object(e)):
                    raise
            return blob.download_blob()
        except ResourceNotFoundError as e:
            raise KeyNotFound(f"No such file: {path}") from e

    def read(self, path: CloudPath) -> tuple[bytes, object]:
        downloader = self._download(path)
        return downloader.readall(), downloader.properties.etag

    def open(self, path: CloudPath) -> IO[bytes]:
        return _Reader(self._download(path))  # type: ignore[return-value]

    def _put(self, path: CloudPath, source: IO[bytes], content_type: str, *, version: object, absent: bool) -> None:
        from azure.core import MatchConditions
        from azure.storage.blob import ContentSettings

        condition: dict[str, Any] = {}
        if version is not None:
            condition = {"etag": version, "match_condition": MatchConditions.IfNotModified}
        self._blob(path).upload_blob(
            source,
            overwrite=not absent,
            content_settings=ContentSettings(content_type=content_type),
            **self._cpk(),
            **condition,
        )

    def _refused(self, error: Exception) -> bool:
        from azure.core.exceptions import ResourceExistsError, ResourceModifiedError

        return isinstance(error, ResourceExistsError | ResourceModifiedError)


class GSObjects(SdkObjects):
    """Google Cloud Storage through google-cloud-storage: `ifGenerationMatch`, 0 for a new object."""

    def __init__(self, client: Any, encryption_key: StorageKey | None = None) -> None:
        self._gcs = client.client
        self.encryption_key = encryption_key

    def _blob(self, path: CloudPath) -> Any:
        """The blob to write: keyed when a customer key is set."""
        raw = self.encryption_key.raw if self.encryption_key else None
        return self._gcs.bucket(path.bucket).blob(path.blob, encryption_key=raw)  # type: ignore[attr-defined]

    def _readable(self, path: CloudPath) -> Any:
        """The blob to read: keyed only when it is stored with a customer key
        (its metadata, readable without the key, says so)."""
        if self.encryption_key is None:
            return self._gcs.bucket(path.bucket).blob(path.blob)  # type: ignore[attr-defined]
        found = self._gcs.bucket(path.bucket).get_blob(path.blob)  # type: ignore[attr-defined]
        if found is None:
            raise KeyNotFound(f"No such file: {path}")
        if (getattr(found, "_properties", None) or {}).get("customerEncryption"):
            found.encryption_key = self.encryption_key.raw
        return found

    def read(self, path: CloudPath) -> tuple[bytes, object]:
        from google.api_core.exceptions import NotFound

        blob = self._readable(path)
        try:
            data: bytes = blob.download_as_bytes()
        except NotFound as e:
            raise KeyNotFound(f"No such file: {path}") from e
        # Set from the download's response headers: the generation of what was read.
        return data, blob.generation

    def open(self, path: CloudPath) -> IO[bytes]:
        return self._readable(path).open("rb")  # type: ignore[no-any-return]

    def _put(self, path: CloudPath, source: IO[bytes], content_type: str, *, version: object, absent: bool) -> None:
        condition: dict[str, Any] = {}
        if absent:
            condition["if_generation_match"] = 0
        elif version is not None:
            condition["if_generation_match"] = version
        self._blob(path).upload_from_file(source, content_type=content_type, **condition)

    def _refused(self, error: Exception) -> bool:
        from google.api_core.exceptions import PreconditionFailed

        return isinstance(error, PreconditionFailed)
