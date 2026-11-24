"""The customer-provided key that encrypts cloud storage on the provider side.

With `PINCER_STORAGE_ENCRYPTION_KEY` set, every object Pincer writes to a
cloud location is encrypted by the provider with that AES-256 key: SSE-C on
S3, CPK on Azure Blob, CSEK on GCS. The provider never stores the key; it is
sent with every request that reads or writes content, so without it the data
cannot be read at all. The clients that send it are in
`pincer.storage.encrypted_clients`; the one-way lock and the startup checks are
in `pincer.storage.encryption_state`; `pincer.storage.encrypt_job` encrypts
what was stored before.

Every provider refuses a customer key over plain HTTP, so encrypted data can
only be read over TLS (see `TLS_WARNING_DOC`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pincer.exceptions import PincerError
from pincer.storage.errors import StorageEncryptionError

if TYPE_CHECKING:
    from pincer.config import Settings

#: Where the docs explain why encrypted storage needs TLS; error messages point here.
TLS_WARNING_DOC = "docs/guides/storage-encryption.md#encrypted-data-can-only-be-read-over-tls"
KEY_BYTES = 32
_FINGERPRINT_LABEL = b"pincer-storage-key-check"


@dataclass(frozen=True)
class StorageKey:
    """A 32-byte AES key and the forms each provider takes it in. Its repr
    never shows the key."""

    raw: bytes = field(repr=False)

    @property
    def b64(self) -> str:
        return base64.b64encode(self.raw).decode()

    @property
    def md5_b64(self) -> str:
        """The key's MD5, base64: S3 checks it against the key (`SSECustomerKeyMD5`)."""
        return base64.b64encode(hashlib.md5(self.raw, usedforsecurity=False).digest()).decode()

    @property
    def sha256_b64(self) -> str:
        """The key's SHA-256, base64: Azure's `x-ms-encryption-key-sha256`, and what it reports back."""
        return base64.b64encode(hashlib.sha256(self.raw).digest()).decode()

    @property
    def fingerprint(self) -> str:
        """What the lock stores to recognise the key: an HMAC, never the key or a plain hash of it."""
        return hmac.new(self.raw, _FINGERPRINT_LABEL, hashlib.sha256).hexdigest()

    def s3_args(self) -> dict[str, str]:
        """The SSE-C parameters of an S3 request."""
        return {"SSECustomerAlgorithm": "AES256", "SSECustomerKey": self.b64, "SSECustomerKeyMD5": self.md5_b64}

    def s3_copy_source_args(self) -> dict[str, str]:
        """The SSE-C parameters that let S3 read an encrypted copy source."""
        return {
            "CopySourceSSECustomerAlgorithm": "AES256",
            "CopySourceSSECustomerKey": self.b64,
            "CopySourceSSECustomerKeyMD5": self.md5_b64,
        }

    def azure_cpk(self) -> Any:
        """The key as an azure-storage-blob `CustomerProvidedEncryptionKey`."""
        from azure.storage.blob import CustomerProvidedEncryptionKey

        return CustomerProvidedEncryptionKey(key_value=self.b64, key_hash=self.sha256_b64)

    def datalake_cpk(self) -> Any:
        """The key as an azure-storage-file-datalake `CustomerProvidedEncryptionKey`."""
        from azure.storage.filedatalake import CustomerProvidedEncryptionKey

        return CustomerProvidedEncryptionKey(key_value=self.b64, key_hash=self.sha256_b64)


class InvalidStorageKey(PincerError):
    """A `PINCER_STORAGE_ENCRYPTION_KEY` that is not 32 bytes of base64.

    Deliberately not a ValueError: pydantic wraps a ValueError raised in a
    validator into a ValidationError that prints the raw input, which here
    is the key. This one passes through pydantic as it is, and its message
    never carries the value.
    """


def parse_key(value: str) -> StorageKey:
    """`value` (base64 of 32 bytes) as a key. Raises InvalidStorageKey, which never echoes the value."""
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise InvalidStorageKey(
            "storage_encryption_key is not valid base64; generate one with `openssl rand -base64 32`"
        ) from None
    if len(raw) != KEY_BYTES:
        raise InvalidStorageKey(
            f"storage_encryption_key must decode to {KEY_BYTES} bytes (AES-256), not {len(raw)}; "
            "generate one with `openssl rand -base64 32`"
        )
    return StorageKey(raw)


def load_key(settings: Settings) -> StorageKey | None:
    """The configured key, or None when encryption is off."""
    from pydantic import SecretStr

    secret = getattr(settings, "storage_encryption_key", None)
    if isinstance(secret, SecretStr):
        secret = secret.get_secret_value()
    if not isinstance(secret, str) or not secret:
        return None
    return parse_key(secret)


def tls_problem(scheme: str, settings: Settings) -> str | None:
    """Why `scheme`'s endpoint cannot carry a customer key, or None.

    S3 SSE-C, Azure CPK and GCS CSEK are refused over plain HTTP: encrypted
    data can only be read over TLS.
    """
    if scheme == "s3":
        endpoint = settings.storage_s3_endpoint_url or ""
        if endpoint.lower().startswith("http://"):
            return f"the S3 endpoint {endpoint} is not TLS (http://)"
    elif scheme == "az":
        connection = settings.storage_azure_connection_string
        parts = {}
        for part in (connection.get_secret_value() if connection is not None else "").split(";"):
            name, _, value = part.partition("=")
            parts[name.strip().lower()] = value.strip()
        if parts.get("usedevelopmentstorage", "").lower() == "true":
            return "the Azure connection string uses UseDevelopmentStorage=true (the emulator over http://)"
        if parts.get("defaultendpointsprotocol", "").lower() == "http":
            return "the Azure connection string uses DefaultEndpointsProtocol=http"
        if parts.get("blobendpoint", "").lower().startswith("http://"):
            return "the Azure connection string's BlobEndpoint is not TLS (http://)"
        account_url = settings.storage_azure_account_url or ""
        if account_url.lower().startswith("http://"):
            return f"the Azure account URL {account_url} is not TLS (http://)"
    return None


def require_tls(scheme: str, settings: Settings) -> None:
    """Raise StorageEncryptionError when `scheme`'s endpoint is not TLS."""
    problem = tls_problem(scheme, settings)
    if problem:
        raise StorageEncryptionError(
            f"Storage encryption needs TLS, but {problem}. Encrypted data can only be read over TLS: "
            f"use an https:// endpoint (see {TLS_WARNING_DOC})."
        )


# ── Telling a plain object from an encrypted one, by the error a keyed request gets ──

_AZURE_PLAIN_CODES = frozenset({"BlobDoesNotUseCustomerSpecifiedEncryption"})


def keyed_request_hit_plain_object(error: Exception) -> bool:
    """True when `error` is a provider refusing a customer key for an object
    that was not stored with one (written before encryption was enabled, or by
    another service). The caller retries once without the key.

    - S3 answers a keyed GET/HEAD of a plain object with 400 (`InvalidRequest`,
      or a bodyless `400` for HEAD). A wrong key is 403, which is not retried.
    - GCS answers a keyed download of a plain object with 400 Bad Request.
    - Azure answers with `BlobDoesNotUseCustomerSpecifiedEncryption`.
    """
    response = getattr(error, "response", None)
    if isinstance(response, dict) and "Error" in response:  # botocore ClientError
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = response.get("Error", {}).get("Code")
        return status == 400 or code in ("400", "InvalidRequest")
    if type(error).__name__ == "BadRequest" and type(error).__module__.startswith("google.api_core"):
        return True
    return azure_error_code(error) in _AZURE_PLAIN_CODES


def request_hit_encrypted_object(error: Exception) -> bool:
    """True when `error` is a provider refusing a read without a key (or with
    another key) because the object is stored with a customer key.

    - S3: 400 for a GET/HEAD of an SSE-C object without its key.
    - GCS: 400 Bad Request for a download of a CSEK object without its key.
    - Azure: `BlobUsesCustomerSpecifiedEncryption`.
    """
    response = getattr(error, "response", None)
    if isinstance(response, dict) and "Error" in response:  # botocore ClientError
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return status == 400 or response.get("Error", {}).get("Code") in ("400", "InvalidRequest")
    if type(error).__name__ == "BadRequest" and type(error).__module__.startswith("google.api_core"):
        return True
    return azure_error_code(error) in _AZURE_ENCRYPTED_CODES


_AZURE_ENCRYPTED_CODES = frozenset({"BlobUsesCustomerSpecifiedEncryption"})
#: Azure error codes about the customer key itself: missing, wrong or not applicable.
_AZURE_KEY_CODE_MARKERS = ("CustomerSpecifiedEncryption", "CustomerProvidedKey", "EncryptionKey")


def is_key_error(error: Exception) -> bool:
    """True when `error` may be the provider refusing the key itself (a
    wrong key, or none), rather than the store being unreachable.

    - S3: 400 or 403 for a keyed request (a wrong SSE-C key is 403; 403 is
      also a missing permission, which the caller tells apart by reading
      without the key).
    - GCS: 400 Bad Request (a wrong CSEK key) or 403.
    - Azure: an error code about the customer-provided key.

    Throttling, timeouts and server errors (5xx) are never key errors.
    """
    response = getattr(error, "response", None)
    if isinstance(response, dict) and "Error" in response:  # botocore ClientError
        return response.get("ResponseMetadata", {}).get("HTTPStatusCode") in (400, 403)
    if type(error).__module__.startswith("google.api_core"):
        return type(error).__name__ in ("BadRequest", "Forbidden")
    code = azure_error_code(error)
    return code is not None and any(marker in code for marker in _AZURE_KEY_CODE_MARKERS)


def azure_error_code(error: Exception) -> str | None:
    """The storage error code of an azure-core error, as a string (the SDK may give an enum)."""
    code = getattr(error, "error_code", None)
    if code is None:
        return None
    return str(getattr(code, "value", code))
