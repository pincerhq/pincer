"""
Google OAuth authentication for Workspace integrations.

Uses InstalledAppFlow with a credentials JSON file (downloaded from Google
Cloud Console) and a local token cache that auto-refreshes.

Credentials file:  ~/.pincer/google_credentials.json  (or data/ in repo)
Token cache:       ~/.pincer/google_workspace_token.json

Run ``pincer google setup`` to perform the one-time browser consent flow.

Alternatively the token JSON can be supplied via ``PINCER_GOOGLE_TOKEN_JSON``
(see :mod:`pincer.integrations.google.token_source`); it is then kept in memory
and never written to disk.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, cast

from pincer.integrations.google.token_source import TOKEN_ENV

logger = logging.getLogger(__name__)

# All scopes required by the full workspace integration.
ALL_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/contacts",
    # Google Meet — space management
    "https://www.googleapis.com/auth/meetings.space.created",
    "https://www.googleapis.com/auth/meetings.space.readonly",
    "https://www.googleapis.com/auth/meetings.space.settings",
]

# Scopes grouped by service — used when only a subset of APIs is needed.
SERVICE_SCOPES: dict[str, list[str]] = {
    "gmail": [
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.send",
    ],
    "calendar": ["https://www.googleapis.com/auth/calendar"],
    "drive": ["https://www.googleapis.com/auth/drive"],
    "docs": ["https://www.googleapis.com/auth/documents"],
    "sheets": ["https://www.googleapis.com/auth/spreadsheets"],
    "slides": ["https://www.googleapis.com/auth/presentations"],
    "tasks": ["https://www.googleapis.com/auth/tasks"],
    "contacts": ["https://www.googleapis.com/auth/contacts"],
    "meet": [
        "https://www.googleapis.com/auth/meetings.space.created",
        "https://www.googleapis.com/auth/meetings.space.readonly",
        "https://www.googleapis.com/auth/meetings.space.settings",
    ],
}


def scopes_for_services(services: list[str] | None = None) -> list[str]:
    """Return the deduplicated scope list for the given service names."""
    if services is None:
        return ALL_SCOPES
    seen: set[str] = set()
    result: list[str] = []
    for svc in services:
        for scope in SERVICE_SCOPES.get(svc, []):
            if scope not in seen:
                seen.add(scope)
                result.append(scope)
    return result


class GoogleAuth:
    """
    Manages Google OAuth credentials for the Workspace integration.

    Loads an existing token from *token_path* and refreshes it automatically.
    When no valid token exists, raises ``FileNotFoundError`` with a hint to
    run ``pincer google setup``.

    When *token_info* is given (token JSON from ``PINCER_GOOGLE_TOKEN_JSON``)
    the instance is in env mode: no files are needed or written, and refreshed
    access tokens live in memory only.

    This class intentionally does **not** open a browser — that belongs in the
    ``pincer google setup`` CLI command.
    """

    def __init__(
        self,
        credentials_path: str = "",
        token_path: str = "",
        services: list[str] | None = None,
        token_info: dict[str, Any] | None = None,
    ) -> None:
        self._credentials_path = Path(credentials_path)
        self._token_path = Path(token_path)
        self._token_info = token_info
        self._services = list(services) if services is not None else list(SERVICE_SCOPES)
        self._scopes = scopes_for_services(services)
        self._credentials: Any = None  # google.oauth2.credentials.Credentials

    # ── Public API ────────────────────────────────────────────────────────────

    def get_credentials(self) -> Any:  # -> google.oauth2.credentials.Credentials
        """Return valid credentials, refreshing if necessary.

        Raises:
            FileNotFoundError: if credentials or token file is missing (file mode).
            RuntimeError: if the token cannot be refreshed and re-auth is needed.
        """
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        if self._credentials and self._credentials.valid:
            return self._credentials

        if self._token_info is None:
            self._load_from_file()
        elif self._credentials is None:
            # Env mode: load once; later refreshes update the in-memory object.
            self._check_token_scopes()
            self._credentials = Credentials.from_authorized_user_info(  # type: ignore[no-untyped-call]
                self._token_info, self._scopes
            )

        if self._credentials.valid:
            return self._credentials

        if self._credentials.expired and self._credentials.refresh_token:
            try:
                self._credentials.refresh(Request())
                self._save_token(keep_granted_scopes=True)
                logger.debug("Google Workspace token refreshed")
                return self._credentials
            except Exception as exc:
                raise RuntimeError(f"Token refresh failed: {exc}. {self._reauth_hint()}") from exc

        raise RuntimeError(f"Google token is invalid (no refresh token). {self._reauth_hint()}")

    def _load_from_file(self) -> None:
        from google.oauth2.credentials import Credentials

        # The token JSON carries client_id / client_secret, so the client
        # credentials file is only needed by the consent flow, not here.
        if not self._token_path.exists():
            if not self._credentials_path.exists():
                raise FileNotFoundError(
                    f"Google Workspace token not found at {self._token_path} and no OAuth client at "
                    f"{self._credentials_path}. Download it from Google Cloud Console → APIs & Services → "
                    "Credentials → OAuth 2.0 Client IDs → Download JSON, save it there, then run "
                    "'pincer google setup'."
                )
            raise FileNotFoundError("Google Workspace token not found. Run 'pincer google setup' to authenticate.")

        self._credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
            str(self._token_path), self._scopes
        )
        self._check_token_scopes()

    def _check_token_scopes(self) -> None:
        """Detect scope mismatch early — avoids opaque 403s from Google APIs.

        Reads scopes from the raw token JSON because from_authorized_user_*(scopes=...)
        overrides credentials.scopes with the passed-in list, making the
        credentials.scopes check always pass regardless of what the token holds.
        """
        token_scopes = self._token_scopes()
        if token_scopes:
            missing = [s for s in self._scopes if s not in token_scopes]
            if missing:
                raise RuntimeError(
                    f"Google token is missing required scopes: {missing}. "
                    f"{self._reauth_hint()} to re-authorise with the full scope set."
                )

    def _reauth_hint(self) -> str:
        if self._token_info is not None:
            return f"Run 'pincer google setup --print-env' and update {TOKEN_ENV}"
        if self._token_path.name == "google_token.json":
            # Legacy calendar token — the built-in calendar_* tools still use it, don't delete it.
            return "Run 'pincer google setup' (creates google_workspace_token.json; keep google_token.json)"
        return f"Run 'pincer google setup' (re-authorises and replaces {self._token_path})"

    def run_auth_flow(
        self,
        open_browser: bool = True,
        save: bool = True,
        client_config: dict[str, Any] | None = None,
    ) -> Any:
        """Run the InstalledAppFlow consent flow (blocking — call from CLI only).

        Args:
            open_browser: When True, tries to open the system browser.
            save: When False, the token is returned but not written to *token_path*.
            client_config: OAuth client JSON to use instead of *credentials_path*.

        Returns:
            The authorised Credentials object.
        """
        from google_auth_oauthlib.flow import InstalledAppFlow

        if client_config is not None:
            flow = InstalledAppFlow.from_client_config(client_config, self._scopes)
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(self._credentials_path), self._scopes)
        try:
            creds = flow.run_local_server(port=0, open_browser=open_browser)
        except Exception:
            logger.warning("run_local_server failed, trying port 8080 without browser open")
            creds = flow.run_local_server(port=8080, open_browser=False)

        self._credentials = creds
        if save:
            self._save_token()
            logger.info("Google Workspace authentication successful, token saved to %s", self._token_path)
        return creds

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _save_token(self, keep_granted_scopes: bool = False) -> None:
        """Write the credentials to *token_path* (no-op in env mode).

        With *keep_granted_scopes* (after a refresh) the ``scopes`` already in the
        file are kept, or left absent if the file had none: ``to_json()`` writes
        the scopes this instance requested, which may be a subset of what the
        refresh token covers — saving those would make later
        ``granted_services()`` calls under-report.
        """
        if self._token_info is not None:
            return  # env mode — refreshed access tokens stay in memory
        data = json.loads(self._credentials.to_json())
        if keep_granted_scopes:
            granted = self._token_scopes()
            if granted:
                data["scopes"] = granted
            else:
                # The token never listed its scopes (treated as granting
                # everything) — keep it that way instead of writing the subset.
                data.pop("scopes", None)
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._token_path, "w") as fh:
            fh.write(json.dumps(data))
        os.chmod(self._token_path, 0o600)

    @property
    def token_path(self) -> Path:
        return self._token_path

    @property
    def source(self) -> str:
        """Where the token comes from: ``"env"`` or ``"file"``."""
        return "env" if self._token_info is not None else "file"

    @property
    def credentials_path(self) -> Path:
        return self._credentials_path

    @property
    def services(self) -> list[str]:
        """Service names this instance requests scopes for."""
        return list(self._services)

    def _token_data(self) -> dict[str, Any] | None:
        """Return the raw token JSON (env or file), or None if absent/unreadable."""
        if self._token_info is not None:
            return self._token_info
        if not self._token_path.exists():
            return None
        try:
            data = json.loads(self._token_path.read_text())
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _token_scopes(self) -> list[str]:
        """Return the scopes listed in the cached token ([] if absent/unreadable/unlisted)."""
        data = self._token_data()
        if data is None:
            return []
        scopes = data.get("scopes") or []
        if isinstance(scopes, str):  # google-auth also accepts a space-separated string
            scopes = scopes.split()
        return list(scopes)

    def granted_services(self) -> list[str]:
        """Return the services in ``SERVICE_SCOPES`` whose scopes are all in the cached token.

        Like :meth:`missing_scopes`, a token without a ``scopes`` field (older
        tokens) is treated as granting everything.
        """
        token_scopes = self._token_scopes()
        if not token_scopes:
            return list(SERVICE_SCOPES)
        return [svc for svc, scopes in SERVICE_SCOPES.items() if all(s in token_scopes for s in scopes)]

    def missing_scopes(self) -> list[str]:
        """Return scopes required by this instance that are absent from the cached token.

        Returns an empty list when the token is absent, unreadable, or has no
        ``scopes`` field (older tokens) — only flags a mismatch when the token
        explicitly lists scopes that are a subset of what is needed.
        """
        token_scopes = self._token_scopes()
        if not token_scopes:
            return []
        return [s for s in self._scopes if s not in token_scopes]

    def authenticated_email(self) -> str | None:
        """Return the email address from the stored token, or None."""
        data = self._token_data()
        if data is None:
            return None
        try:
            if "account" not in data:
                return str(data.get("client_id", "")).split("-")[0]
            return cast("str | None", data.get("account"))
        except Exception:
            return None
