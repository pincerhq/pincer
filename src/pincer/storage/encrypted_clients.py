"""cloudpathlib clients that encrypt with the customer-provided key.

`clients.build_client` returns one of these when `PINCER_STORAGE_ENCRYPTION_KEY`
is set, so the storage browser, skill roots and workspaces all get it:

- S3: SSE-C parameters on every upload, download, HEAD and copy.
- GCS: CSEK blobs (`encryption_key=`) for uploads, downloads and copies (as
  rewrites: `copy_blob` cannot copy a CSEK object).
- Azure Blob: `cpk=` on every upload, download, properties and metadata
  request; copies stream through Pincer (Copy Blob cannot read a CPK source).
  On a hierarchical-namespace (ADLS Gen2) account cloudpathlib uses the Data
  Lake client for properties, rename, mkdir, listing and folder delete: file
  properties carry the key; the others only change paths and need none. Every
  Data Lake call goes through `datalake_errors`.

Every write is encrypted. A read of an object stored without the key (before
encryption was enabled, or by another service sharing the bucket) is retried
once without it (`encryption.keyed_request_hit_plain_object`), so storage keeps
working while `pincer.storage.encrypt_job` encrypts what is left.

Each client also answers `is_encrypted(path)` and `encrypt_in_place(path)` for
that job.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cloudpathlib import AzureBlobClient, GSClient, S3Client

from pincer.storage.encryption import (
    TLS_WARNING_DOC,
    OldVersions,
    StorageKey,
    azure_error_code,
    is_key_error,
    keyed_request_hit_plain_object,
    note_plain,
    request_hit_encrypted_object,
)
from pincer.storage.errors import KeyConflict, StorageEncryptionError, StorageUnavailable

if TYPE_CHECKING:
    import os
    from collections.abc import Callable, Iterable, Iterator

    from cloudpathlib import CloudPath

logger = logging.getLogger(__name__)

#: Data Lake / Blob error codes that are about the customer-provided key.
_CPK_CODE_MARKERS = ("CustomerSpecifiedEncryption", "CustomerProvidedKey", "EncryptionKey")
#: Object metadata an in-place S3 rewrite carries over (a multipart copy would drop it).
_S3_KEPT_HEADERS = ("ContentType", "ContentEncoding", "ContentDisposition", "ContentLanguage", "CacheControl")

# ── S3 (SSE-C) ─────────────────────────────────────────────────────────


class EncryptedS3Client(S3Client):
    def __init__(self, *args: Any, encryption_key: StorageKey, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.encryption_key = encryption_key
        sse = encryption_key.s3_args()
        self._plain_dl_args = dict(self.boto3_dl_extra_args)
        self._keyed_dl_args = {**self.boto3_dl_extra_args, **sse}
        self._base_copy_args = {k: v for k, v in self.boto3_copy_extra_args.items() if "SSECustomer" not in k}
        self.boto3_ul_extra_args = {**self.boto3_ul_extra_args, **sse}
        # cloudpathlib's own HEADs (`_get_metadata`, `_s3_file_query`) are
        # overridden below; this keeps any other download path keyed.
        self.boto3_dl_extra_args = self._keyed_dl_args
        self.boto3_copy_extra_args = {**self._base_copy_args, **sse}

    def _head(self, bucket: str, key: str, *, log: bool = True) -> tuple[dict[str, Any], bool]:
        """The object's HEAD and whether it is stored with the key. `log`: note
        a plain object (reads and stats do; the encryption job's check does not)."""
        from botocore.exceptions import ClientError

        try:
            return self.client.head_object(Bucket=bucket, Key=key, **self._keyed_dl_args), True
        except ClientError as e:
            if e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 403:
                self._raise_if_another_key(bucket, key, e)
                raise
            if not keyed_request_hit_plain_object(e):
                raise
        data = self.client.head_object(Bucket=bucket, Key=key, **self._plain_dl_args)
        if data.get("SSECustomerAlgorithm"):
            # Stored with a customer key, but not this one.
            raise StorageEncryptionError(
                f"s3://{bucket}/{key} is encrypted with a different customer key than PINCER_STORAGE_ENCRYPTION_KEY"
            )
        if log:
            note_plain(f"s3://{bucket}/{key}")
        return data, False

    def _raise_if_another_key(self, bucket: str, key: str, keyed_error: Exception) -> None:
        """After a 403 on a keyed HEAD: StorageEncryptionError when the object is
        SSE-C under another key (a plain HEAD gets 400). A 403 there too is a
        missing permission, which the caller re-raises as it is."""
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=bucket, Key=key, **self._plain_dl_args)
        except ClientError as plain_error:
            if request_hit_encrypted_object(plain_error):
                raise StorageEncryptionError(
                    f"s3://{bucket}/{key} is encrypted with a different customer key than PINCER_STORAGE_ENCRYPTION_KEY"
                ) from keyed_error
            return
        # Readable without a key but not with it: the store refuses SSE-C here.
        raise StorageEncryptionError(
            f"s3://{bucket}/{key}: the store refused a request with the customer key (403)"
        ) from keyed_error

    def _get_metadata(self, cloud_path: Any) -> dict[str, Any]:
        data, _ = self._head(cloud_path.bucket, cloud_path.key)
        return {
            "last_modified": data["LastModified"],
            "size": data["ContentLength"],
            "etag": data.get("ETag", None),
            "content_type": data.get("ContentType", None),
            "extra": data["Metadata"],
        }

    def _download_file(self, cloud_path: Any, local_path: str | os.PathLike[str]) -> Path:
        from botocore.exceptions import ClientError

        local_path = Path(local_path)
        obj = self.s3.Object(cloud_path.bucket, cloud_path.key)
        try:
            obj.download_file(str(local_path), Config=self.boto3_transfer_config, ExtraArgs=self._keyed_dl_args)
        except ClientError as e:
            if not keyed_request_hit_plain_object(e):
                raise
            note_plain(cloud_path)
            obj.download_file(str(local_path), Config=self.boto3_transfer_config, ExtraArgs=self._plain_dl_args)
        return local_path

    def _s3_file_query(self, cloud_path: Any) -> str | None:
        from botocore.exceptions import ClientError

        try:
            self._head(cloud_path.bucket, cloud_path.key.rstrip("/"))
            return "file"
        except (ClientError, self.client.exceptions.NoSuchKey):
            key = cloud_path.key.rstrip("/") + "/"
            objects = self.s3.Bucket(cloud_path.bucket).objects.filter(Prefix=key, **self.boto3_list_extra_args)
            return next(("dir" for _ in objects.limit(1)), None)

    def _copy_args(self, source_keyed: bool) -> dict[str, Any]:
        args = {**self._base_copy_args, **self.encryption_key.s3_args()}
        if source_keyed:
            args.update(self.encryption_key.s3_copy_source_args())
        return args

    def _move_file(self, src: Any, dst: Any, remove_src: bool = True) -> Any:
        head, keyed = self._head(src.bucket, src.key)
        if src == dst:
            # A touch: copy onto itself with REPLACEd metadata, as cloudpathlib does.
            touch = {k: v for k, v in self._copy_args(keyed).items() if k not in ("Metadata", "MetadataDirective")}
            self.s3.Object(src.bucket, src.key).copy_from(
                CopySource={"Bucket": src.bucket, "Key": src.key},
                Metadata=head.get("Metadata", {}),
                MetadataDirective="REPLACE",
                **touch,
            )
            return dst
        self.s3.Object(dst.bucket, dst.key).copy(
            {"Bucket": src.bucket, "Key": src.key},
            ExtraArgs=self._copy_args(keyed),
            Config=self.boto3_transfer_config,
        )
        if remove_src:
            self._remove(src)
        return dst

    # ── For the encryption job ──

    def is_encrypted(self, path: CloudPath) -> bool:
        return self._head(path.bucket, path.key, log=False)[1]  # type: ignore[attr-defined]

    def old_versions_kept(self, root: CloudPath) -> list[OldVersions]:
        """Why plain versions replaced by an in-place rewrite stay readable: bucket versioning."""
        bucket = root.bucket  # type: ignore[attr-defined]
        status = self.client.get_bucket_versioning(Bucket=bucket).get("Status")
        if status in ("Enabled", "Suspended"):
            note = f"versioning is {status.lower()} on S3 bucket {bucket}, so the plain versions stay readable"
            return [OldVersions(note)]
        return []

    def encrypt_in_place(self, path: CloudPath) -> None:
        """Copy the plain object onto itself, encrypted, unless it changed since its HEAD.

        A managed copy: multipart above the transfer threshold, so objects over
        5 GB work and nothing leaves the provider. Content type, metadata and
        the other stored headers are carried over explicitly, because a
        multipart copy would drop them. Raises KeyConflict when the object
        changed in between (that write is already encrypted).
        """
        from botocore.exceptions import ClientError

        bucket, key = path.bucket, path.key  # type: ignore[attr-defined]
        head = self.client.head_object(Bucket=bucket, Key=key, **self._plain_dl_args)
        extra = {
            **self._copy_args(source_keyed=False),
            "Metadata": head.get("Metadata", {}),
            "MetadataDirective": "REPLACE",
            "CopySourceIfMatch": head["ETag"],
        }
        extra.update({name: head[name] for name in _S3_KEPT_HEADERS if head.get(name)})
        try:
            self.s3.Object(bucket, key).copy(
                {"Bucket": bucket, "Key": key}, ExtraArgs=extra, Config=self.boto3_transfer_config
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("PreconditionFailed", "412"):
                raise KeyConflict(f"{path} changed while it was being encrypted") from e
            raise


# ── Google Cloud Storage (CSEK) ────────────────────────────────────────


def _gcs_keyed(blob: Any) -> bool:
    """True when GCS reports the blob as stored with a customer-supplied key."""
    return bool((getattr(blob, "_properties", None) or {}).get("customerEncryption"))


class EncryptedGSClient(GSClient):
    def __init__(self, *args: Any, encryption_key: StorageKey, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.encryption_key = encryption_key

    def _readable_blob(self, bucket_name: str, name: str) -> Any:
        """The blob with its metadata (readable without the key), keyed when it is stored with one."""
        blob = self.client.bucket(bucket_name).get_blob(name, **self.blob_kwargs)
        if blob is None:
            return None
        if _gcs_keyed(blob):
            blob.encryption_key = self.encryption_key.raw
        else:
            note_plain(f"gs://{bucket_name}/{name}")
        return blob

    def _download_file(self, cloud_path: Any, local_path: str | os.PathLike[str]) -> Path:
        blob = self._readable_blob(cloud_path.bucket, cloud_path.blob)
        if blob is None:
            raise FileNotFoundError(f"File does not exist: {cloud_path}")
        local_path = Path(local_path)
        blob.download_to_filename(local_path, **self.blob_kwargs)
        return local_path

    def _upload_file(self, local_path: str | os.PathLike[str], cloud_path: Any) -> Any:
        blob = self.client.bucket(cloud_path.bucket).blob(cloud_path.blob, encryption_key=self.encryption_key.raw)
        extra: dict[str, Any] = {}
        if self.content_type_method is not None:
            content_type, _ = self.content_type_method(str(local_path))
            extra["content_type"] = content_type
        blob.upload_from_filename(str(local_path), **extra, **self.blob_kwargs)
        return cloud_path

    def _rewrite(self, source: Any, target: Any, **conditions: Any) -> None:
        token: str | None = None
        while True:
            token, _, _ = target.rewrite(source, token=token, **conditions, **self.blob_kwargs)
            if token is None:
                return

    def _move_file(self, src: Any, dst: Any, remove_src: bool = True) -> Any:
        if src == dst:
            return super()._move_file(src, dst, remove_src)  # a metadata patch: no key needed
        source = self._readable_blob(src.bucket, src.blob)
        if source is None:
            raise FileNotFoundError(f"File does not exist: {src}")
        target = self.client.bucket(dst.bucket).blob(dst.blob, encryption_key=self.encryption_key.raw)
        self._rewrite(source, target)
        if remove_src:
            source.delete()
        return dst

    # ── For the encryption job ──

    def is_encrypted(self, path: CloudPath) -> bool:
        blob = self.client.bucket(path.bucket).get_blob(path.blob, **self.blob_kwargs)  # type: ignore[attr-defined]
        if blob is None:
            raise FileNotFoundError(f"File does not exist: {path}")
        return _gcs_keyed(blob)

    def old_versions_kept(self, root: CloudPath) -> list[OldVersions]:
        """Why plain generations replaced by an in-place rewrite stay readable:
        object versioning (noncurrent generations) or soft delete."""
        name = root.bucket  # type: ignore[attr-defined]
        bucket = self.client.get_bucket(name)
        notes = []
        if bucket.versioning_enabled:
            notes.append(
                OldVersions(f"object versioning is on for GCS bucket {name}, so the plain noncurrent generations stay")
            )
        policy = getattr(bucket, "soft_delete_policy", None)
        seconds = int(getattr(policy, "retention_duration_seconds", None) or 0)
        if seconds:
            notes.append(
                OldVersions(
                    f"soft delete keeps the plain generations of GCS bucket {name} for {seconds // 86400} day(s)",
                    retention_seconds=seconds,
                )
            )
        return notes

    def encrypt_in_place(self, path: CloudPath) -> None:
        """Rewrite the plain object onto itself with the key, unless its generation changed.

        A rewrite keeps the content type and metadata and stays inside GCS.
        Raises KeyConflict when the object changed in between.
        """
        from google.api_core.exceptions import PreconditionFailed

        bucket = self.client.bucket(path.bucket)  # type: ignore[attr-defined]
        source = bucket.get_blob(path.blob, **self.blob_kwargs)  # type: ignore[attr-defined]
        if source is None:
            raise FileNotFoundError(f"File does not exist: {path}")
        target = bucket.blob(path.blob, encryption_key=self.encryption_key.raw)  # type: ignore[attr-defined]
        try:
            self._rewrite(source, target, if_source_generation_match=source.generation)
        except PreconditionFailed as e:
            raise KeyConflict(f"{path} changed while it was being encrypted") from e


# ── Azure Blob (CPK), with Data Lake for HNS accounts ──────────────────


@contextlib.contextmanager
def datalake_errors(operation: str, path: object) -> Iterator[None]:
    """Map an Azure Data Lake (or Blob) request's errors to Pincer's.

    - `ResourceNotFoundError` passes through: cloudpathlib and the callers
      already turn it into "no such file".
    - An error about the customer-provided key becomes StorageEncryptionError,
      naming the operation, the path and the service error code.
    - A key refused over plain HTTP becomes StorageEncryptionError too.
    - Any other service error becomes StorageUnavailable with the same context.

    Messages never carry the key.
    """
    from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

    try:
        yield
    except ResourceNotFoundError:
        raise
    except HttpResponseError as e:
        code = azure_error_code(e) or (f"HTTP {e.status_code}" if e.status_code else type(e).__name__)
        if any(marker in code for marker in _CPK_CODE_MARKERS):
            raise StorageEncryptionError(
                f"Azure Data Lake {operation} of {path} was refused for the customer-provided key ({code})"
            ) from e
        raise StorageUnavailable(f"Azure Data Lake {operation} of {path} failed ({code})") from e
    except ValueError as e:
        if "https" in str(e).lower():
            raise StorageEncryptionError(
                f"Azure Data Lake {operation} of {path}: a customer-provided key needs TLS ({e}); see {TLS_WARNING_DOC}"
            ) from e
        raise


#: HTTP statuses of a probe step that may pass on their own: a timeout, throttling,
#: or a missing permission (401/403, which the operator can grant without the
#: location being refused). Every 5xx is transient too.
_TRANSIENT_STATUSES = frozenset({401, 403, 408, 429})


def _probe_error_is_transient(error: BaseException) -> bool:
    """True when a failed HNS probe step may succeed when tried again.

    The HTTP status comes from the error or the error it wraps (a
    StorageUnavailable from `datalake_errors` carries the service error as its
    cause). No status at all (a connection error or a timeout before an
    answer) is transient; a 4xx other than those above is a deterministic
    refusal.
    """
    seen: BaseException | None = error
    while seen is not None:
        status = getattr(seen, "status_code", None)
        if isinstance(status, int):
            return status >= 500 or status in _TRANSIENT_STATUSES
        seen = seen.__cause__
    return True


class EncryptedAzureBlobClient(AzureBlobClient):
    def __init__(self, *args: Any, encryption_key: StorageKey, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.encryption_key = encryption_key

    def _keyed_then_plain[T](self, call: Callable[..., T], cpk: Any, path: object, *, log: bool = True) -> T:
        """`call(cpk=...)`; once more without the key when the object is stored
        without one. `log`: note the plain object (not for the encryption job's check)."""
        from azure.core.exceptions import HttpResponseError

        try:
            return call(cpk=cpk)
        except HttpResponseError as e:
            if not keyed_request_hit_plain_object(e):
                raise
        if log:
            note_plain(path)
        return call()

    def _blob_client(self, path: Any) -> Any:
        return self.service_client.get_blob_client(container=path.container, blob=path.blob)

    def _get_metadata(self, cloud_path: Any) -> Any:
        if self._check_hns(cloud_path):
            fsc = self.data_lake_client.get_file_system_client(cloud_path.container)  # type: ignore[union-attr]
            with datalake_errors("get file properties", cloud_path):
                properties = self._keyed_then_plain(
                    fsc.get_file_client(cloud_path.blob).get_file_properties,
                    self.encryption_key.datalake_cpk(),
                    cloud_path,
                )
            properties["content_type"] = properties.get("content_settings", {"content_type": None}).get("content_type")
            return properties
        properties = self._keyed_then_plain(
            self._blob_client(cloud_path).get_blob_properties, self.encryption_key.azure_cpk(), cloud_path
        )
        properties["content_type"] = properties.content_settings.content_type
        return properties

    def _download_file(self, cloud_path: Any, local_path: str | os.PathLike[str]) -> Path:
        downloader = self._keyed_then_plain(
            self._blob_client(cloud_path).download_blob, self.encryption_key.azure_cpk(), cloud_path
        )
        local_path = Path(local_path)
        local_path.parent.mkdir(exist_ok=True, parents=True)
        partial = self._partial_filename(local_path)
        try:
            with partial.open("wb") as out:
                downloader.readinto(out)
            partial.replace(local_path)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return local_path

    def _upload_file(self, local_path: str | os.PathLike[str], cloud_path: Any) -> Any:
        from azure.storage.blob import ContentSettings

        settings: dict[str, Any] = {}
        if self.content_type_method is not None:
            content_type, content_encoding = self.content_type_method(str(local_path))
            if content_type is not None:
                settings["content_type"] = content_type
            if content_encoding is not None:
                settings["content_encoding"] = content_encoding
        with Path(local_path).open("rb") as data:
            self._blob_client(cloud_path).upload_blob(
                data, overwrite=True, content_settings=ContentSettings(**settings), cpk=self.encryption_key.azure_cpk()
            )
        return cloud_path

    def _move_file(self, src: Any, dst: Any, remove_src: bool = True) -> Any:
        if src == dst:
            blob = self._blob_client(src)
            metadata = {"last_modified": str(datetime.now(UTC).timestamp())}
            self._keyed_then_plain(
                lambda **kw: blob.set_blob_metadata(metadata=metadata, **kw), self.encryption_key.azure_cpk(), src
            )
            return dst
        if remove_src and src.client is dst.client and self._check_hns(src):
            # A Data Lake rename changes the path only: no key.
            with datalake_errors("rename", src):
                return super()._move_file(src, dst, remove_src)
        # Neither Copy Blob nor Put Blob From URL can read a CPK source: stream it.
        downloader = self._keyed_then_plain(self._blob_client(src).download_blob, self.encryption_key.azure_cpk(), src)
        props = downloader.properties
        self._blob_client(dst).upload_blob(
            downloader.chunks(),
            overwrite=True,
            content_settings=props.content_settings,
            metadata=props.metadata,
            cpk=self.encryption_key.azure_cpk(),
        )
        if remove_src:
            self._remove(src)
        return dst

    def _mkdir(self, cloud_path: Any, parents: bool = False, exist_ok: bool = False) -> None:
        if self._check_hns(cloud_path):
            with datalake_errors("create directory", cloud_path):
                return super()._mkdir(cloud_path, parents=parents, exist_ok=exist_ok)
        return super()._mkdir(cloud_path, parents=parents, exist_ok=exist_ok)

    def _remove(self, cloud_path: Any, missing_ok: bool = True) -> None:
        if cloud_path.blob and self._check_hns(cloud_path):
            with datalake_errors("delete", cloud_path):
                return super()._remove(cloud_path, missing_ok=missing_ok)
        return super()._remove(cloud_path, missing_ok=missing_ok)

    def _list_dir(self, cloud_path: Any, recursive: bool = False) -> Iterable[tuple[Any, bool]]:
        if cloud_path.container and self._check_hns(cloud_path):
            with datalake_errors("list", cloud_path):
                yield from super()._list_dir(cloud_path, recursive=recursive)
            return
        yield from super()._list_dir(cloud_path, recursive=recursive)

    # ── For the encryption job ──

    def is_encrypted(self, path: CloudPath) -> bool:
        properties = self._keyed_then_plain(
            self._blob_client(path).get_blob_properties, self.encryption_key.azure_cpk(), path, log=False
        )
        return bool(properties.encryption_key_sha256)

    def old_versions_kept(self, root: CloudPath) -> list[OldVersions]:
        """Why plain blobs replaced by an in-place rewrite stay readable: blob
        versioning (seen as version ids in a listing) or blob soft delete.

        The two are checked independently: one that cannot be checked (soft
        delete needs the account's service properties) is reported as such
        next to the other's answer. Raises when neither can be checked."""
        container = root.container  # type: ignore[attr-defined]

        def versioning() -> OldVersions | None:
            listing = self.service_client.get_container_client(container).list_blobs(
                name_starts_with=root.blob or None,  # type: ignore[attr-defined]
                include=["versions"],
                results_per_page=1,
            )
            if any(getattr(blob, "version_id", None) for blob in itertools.islice(listing, 1)):
                return OldVersions(f"blob versioning is on for Azure container {container}, so the plain versions stay")
            return None

        def soft_delete() -> OldVersions | None:
            retention = self.service_client.get_service_properties().get("delete_retention_policy")
            if retention is not None and getattr(retention, "enabled", False):
                days = getattr(retention, "days", None)
                return OldVersions(
                    f"blob soft delete keeps the plain blobs of Azure container {container} for {days or '?'} day(s)",
                    retention_seconds=int(days) * 86400 if days else None,
                )
            return None

        notes: list[OldVersions] = []
        errors: list[Exception] = []
        for what, probe in (("blob versioning", versioning), ("blob soft delete", soft_delete)):
            try:
                note = probe()
            except Exception as e:
                errors.append(e)
                notes.append(
                    OldVersions(f"could not check {what} for Azure container {container} ({type(e).__name__}: {e})")
                )
                continue
            if note:
                notes.append(note)
        if len(errors) == 2:
            raise errors[0]
        return notes

    def encrypt_in_place(self, path: CloudPath) -> None:
        """Download the plain blob and upload it over itself with the key,
        unless its ETag changed. Content settings and metadata are kept.
        Raises KeyConflict when the blob changed in between."""
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceModifiedError

        blob = self._blob_client(path)
        downloader = blob.download_blob()
        props = downloader.properties
        try:
            blob.upload_blob(
                downloader.chunks(),
                overwrite=True,
                content_settings=props.content_settings,
                metadata=props.metadata,
                cpk=self.encryption_key.azure_cpk(),
                etag=props.etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except ResourceModifiedError as e:
            raise KeyConflict(f"{path} changed while it was being encrypted") from e

    # ── Startup check for HNS accounts ──

    def is_hns(self, path: CloudPath) -> bool:
        return bool(self._check_hns(path))  # type: ignore[arg-type]

    def probe_hns(self, base: CloudPath) -> list[tuple[str, str | None]]:
        """Exercise every Data Lake path Pincer uses with the key, on a probe file under `base`.

        Steps: write the file with the key, read its properties through Data
        Lake with the key, read it back, rename it, delete it. Returns each
        step and None, or the error that stopped it (later steps are not run):
        a refusal about the key or the content, or any other 4xx the service
        answers for good (an unsupported header or operation, say). An error
        that may pass (throttling, a timeout, a server error, a missing
        permission: see `_probe_error_is_transient`) raises StorageUnavailable
        instead, so the location is not refused for it and is probed again on
        its next use. The probe file is removed whatever happens.
        """
        token = uuid.uuid4().hex
        first = base / f"probe-{token}"
        second = base / f"probe-{token}-renamed"
        payload = f"pincer storage encryption probe {token}".encode()
        fsc = self.data_lake_client.get_file_system_client(first.container)  # type: ignore[attr-defined,union-attr]
        steps: list[tuple[str, Callable[[], None]]] = []

        def write() -> None:
            self._blob_client(first).upload_blob(payload, overwrite=True, cpk=self.encryption_key.azure_cpk())

        def properties() -> None:
            with datalake_errors("get file properties", first):
                props = fsc.get_file_client(first.blob).get_file_properties(  # type: ignore[attr-defined]
                    cpk=self.encryption_key.datalake_cpk()
                )
            if props.get("encryption_key_sha256") not in (None, self.encryption_key.sha256_b64):
                raise StorageEncryptionError(f"{first} reports another customer key")

        def read_back() -> None:
            data = self._blob_client(first).download_blob(cpk=self.encryption_key.azure_cpk()).readall()
            if data != payload:
                raise StorageEncryptionError(f"{first} read back different content")

        def rename() -> None:
            with datalake_errors("rename", first):
                fsc.get_file_client(first.blob).rename_file(  # type: ignore[attr-defined]
                    f"{second.container}/{second.blob}"  # type: ignore[attr-defined]
                )

        def delete() -> None:
            with datalake_errors("delete", second):
                fsc.get_file_client(second.blob).delete_file()  # type: ignore[attr-defined]

        steps = [
            ("write with the key", write),
            ("read properties through Data Lake with the key", properties),
            ("read back with the key", read_back),
            ("rename through Data Lake", rename),
            ("delete through Data Lake", delete),
        ]
        results: list[tuple[str, str | None]] = []
        try:
            for name, step in steps:
                try:
                    step()
                except StorageEncryptionError as e:
                    results.append((name, f"{type(e).__name__}: {e}"))
                    break
                except Exception as e:
                    if not is_key_error(e) and _probe_error_is_transient(e):
                        raise StorageUnavailable(
                            f"Storage encryption check on the Azure hierarchical-namespace account of {base} "
                            f"could not finish step '{name}': {type(e).__name__}: {e}"
                        ) from e
                    results.append((name, f"{type(e).__name__}: {e}"))
                    break
                results.append((name, None))
        finally:
            for leftover in (first, second):
                with contextlib.suppress(Exception):
                    self._blob_client(leftover).delete_blob()
        return results


def build_encrypted(scheme: str, encryption_key: StorageKey, **kwargs: Any) -> Any:
    """The encrypting client for `scheme`, with cloudpathlib's own constructor arguments."""
    classes: dict[str, type[Any]] = {
        "s3": EncryptedS3Client,
        "az": EncryptedAzureBlobClient,
        "gs": EncryptedGSClient,
    }
    return classes[scheme](encryption_key=encryption_key, **kwargs)


__all__ = [
    "EncryptedAzureBlobClient",
    "EncryptedGSClient",
    "EncryptedS3Client",
    "build_encrypted",
    "datalake_errors",
]
