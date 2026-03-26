"""Persistent token storage for MCPOAuthClient."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger("pincer.mcp.auth.token_store")

_TOKEN_FILE = Path.home() / ".pincer" / "mcp_tokens.json"


class TokenStore:
    """
    Stores OAuth tokens keyed by resource URI.

    Strategy priority:
    1. keyring (if installed)
    2. JSON file at ~/.pincer/mcp_tokens.json
    3. In-memory dict (fallback)
    """

    def __init__(self) -> None:
        self._memory: dict[str, dict[str, Any]] = {}
        self._strategy = self._detect_strategy()
        logger.debug("TokenStore using strategy: %s", self._strategy)

    def _detect_strategy(self) -> str:
        try:
            import keyring  # noqa: F401

            return "keyring"
        except ImportError:
            pass
        return "file"

    # ── Public API ─────────────────────────────────────────────────────────────

    def store(
        self,
        resource: str,
        access_token: str,
        refresh_token: str | None,
        expires_at: int,
    ) -> None:
        data = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
        }
        if self._strategy == "keyring":
            self._keyring_store(resource, data)
        elif self._strategy == "file":
            self._file_store(resource, data)
        else:
            self._memory[resource] = data

    def get_access_token(self, resource: str) -> str | None:
        data = self._load(resource)
        if not data:
            return None
        if data.get("expires_at", 0) < int(time.time()) + 60:
            return None  # Treat as expired if within 60s of expiry
        return data.get("access_token")

    def get_refresh_token(self, resource: str) -> str | None:
        data = self._load(resource)
        return data.get("refresh_token") if data else None

    def clear(self, resource: str) -> None:
        if self._strategy == "keyring":
            try:
                import keyring

                keyring.delete_password("pincer_mcp", resource)
            except Exception:
                pass
        elif self._strategy == "file":
            all_data = self._file_load_all()
            all_data.pop(resource, None)
            self._file_save_all(all_data)
        else:
            self._memory.pop(resource, None)

    def clear_all(self) -> None:
        if self._strategy == "file":
            with contextlib.suppress(Exception):
                _TOKEN_FILE.unlink(missing_ok=True)
        self._memory.clear()

    # ── Keyring strategy ───────────────────────────────────────────────────────

    def _keyring_store(self, resource: str, data: dict[str, Any]) -> None:
        try:
            import keyring

            keyring.set_password("pincer_mcp", resource, json.dumps(data))
        except Exception as e:
            logger.warning("keyring store failed, falling back to file: %s", e)
            self._strategy = "file"
            self._file_store(resource, data)

    def _keyring_load(self, resource: str) -> dict[str, Any] | None:
        try:
            import keyring

            raw = keyring.get_password("pincer_mcp", resource)
            if raw:
                return cast("dict[str, Any]", json.loads(raw))
        except Exception:
            pass
        return None

    # ── File strategy ──────────────────────────────────────────────────────────

    def _file_store(self, resource: str, data: dict[str, Any]) -> None:
        try:
            all_data = self._file_load_all()
            all_data[resource] = data
            self._file_save_all(all_data)
        except Exception as e:
            logger.warning("file store failed, using memory: %s", e)
            self._strategy = "memory"
            self._memory[resource] = data

    def _file_load(self, resource: str) -> dict[str, Any] | None:
        all_data = self._file_load_all()
        return all_data.get(resource)

    def _file_load_all(self) -> dict[str, Any]:
        try:
            if _TOKEN_FILE.exists():
                return cast("dict[str, Any]", json.loads(_TOKEN_FILE.read_text()))
        except Exception:
            pass
        return {}

    def _file_save_all(self, data: dict[str, Any]) -> None:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(json.dumps(data, indent=2))
        _TOKEN_FILE.chmod(0o600)

    # ── Load helper ────────────────────────────────────────────────────────────

    def _load(self, resource: str) -> dict[str, Any] | None:
        if self._strategy == "keyring":
            return self._keyring_load(resource)
        elif self._strategy == "file":
            return self._file_load(resource)
        else:
            return self._memory.get(resource)
