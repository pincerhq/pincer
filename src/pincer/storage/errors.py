"""Errors of the storage browser; each carries the HTTP status the API answers with."""

from __future__ import annotations

from pincer.exceptions import PincerError


class StorageError(PincerError):
    """A storage request that cannot be served; `status` is its HTTP status."""

    status = 500


class InvalidKey(StorageError):
    status = 400


class InvalidSignature(StorageError):
    status = 403


class ProtectedKey(StorageError):
    status = 403


class KeyNotFound(StorageError):
    status = 404


class KeyConflict(StorageError):
    status = 409


class LinkExpired(StorageError):
    status = 410


class TooLarge(StorageError):
    status = 413


class NotText(StorageError):
    status = 415


class StorageUnavailable(StorageError):
    status = 503


def describe(error: Exception) -> str:
    """`error` for a client: an OS error without the server path it names."""
    if isinstance(error, OSError) and error.strerror:
        return f"{type(error).__name__}: {error.strerror}"
    return f"{type(error).__name__}: {error}"
