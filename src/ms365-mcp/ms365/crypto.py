"""Fernet key resolution for the per-identity token cache (issue #155).

Encryption is opt-in: set MS365_TOKEN_ENCRYPTION_KEY to enable it. When unset,
token caches are stored in plaintext, matching pre-encryption behavior.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cryptography.fernet import Fernet

if TYPE_CHECKING:
    from ms365.config import MS365Settings


def resolve_fernet(settings: MS365Settings) -> Fernet | None:
    """Return the configured Fernet, or None if encryption is disabled."""
    raw_key = settings.token_encryption_key.get_secret_value()
    if not raw_key:
        return None
    try:
        return Fernet(raw_key.encode())
    except ValueError as e:
        raise ValueError(
            "MS365_TOKEN_ENCRYPTION_KEY is not a valid Fernet key. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        ) from e
