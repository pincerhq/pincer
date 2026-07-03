"""Fernet key management for the per-identity token cache (issue #155)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cryptography.fernet import Fernet

    from ms365.config import MS365Settings

logger = logging.getLogger(__name__)


def load_or_generate_fernet(settings: MS365Settings) -> Fernet:
    """Return the Fernet used to encrypt/decrypt token cache files.

    Precedence: ``MS365_TOKEN_ENCRYPTION_KEY`` (raw key) wins over the on-disk
    key file entirely — when set, the file is never touched. Otherwise loads
    ``settings.token_encryption_key_path``, generating one (mode 0600) on
    first run. Mirrors the load-or-generate pattern used for the MCP OAuth
    signing key in ``pincer.mcp.auth.tokens.TokenService``.
    """
    from cryptography.fernet import Fernet

    raw_key = settings.token_encryption_key.get_secret_value()
    if raw_key:
        return Fernet(raw_key.encode())

    key_path = settings.token_encryption_key_path
    if key_path.exists():
        key_bytes = key_path.read_bytes()
    else:
        key_bytes = Fernet.generate_key()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(key_bytes)
        key_path.chmod(0o600)
        logger.info("Generated new token encryption key at %s", key_path)

    return Fernet(key_bytes)
