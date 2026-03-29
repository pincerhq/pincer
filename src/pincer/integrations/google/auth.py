"""
Google OAuth authentication for Workspace integrations.

Uses InstalledAppFlow with a credentials JSON file (downloaded from Google
Cloud Console) and a local token cache that auto-refreshes.

Credentials file:  ~/.pincer/google_credentials.json  (or data/ in repo)
Token cache:       ~/.pincer/google_workspace_token.json

Run ``pincer setup google`` to perform the one-time browser consent flow.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional, cast

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
    run ``pincer setup google``.

    This class intentionally does **not** open a browser — that belongs in the
    ``pincer setup google`` CLI command.
    """

    def __init__(
        self,
        credentials_path: str,
        token_path: str,
        services: list[str] | None = None,
    ) -> None:
        self._credentials_path = Path(credentials_path)
        self._token_path = Path(token_path)
        self._scopes = scopes_for_services(services)
        self._credentials: Any = None  # google.oauth2.credentials.Credentials

    # ── Public API ────────────────────────────────────────────────────────────

    def get_credentials(self) -> Any:  # -> google.oauth2.credentials.Credentials
        """Return valid credentials, refreshing if necessary.

        Raises:
            FileNotFoundError: if credentials or token file is missing.
            RuntimeError: if the token cannot be refreshed and re-auth is needed.
        """
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        if self._credentials and self._credentials.valid:
            return self._credentials

        if not self._credentials_path.exists():
            raise FileNotFoundError(
                f"Google OAuth credentials not found at {self._credentials_path}. "
                "Download from Google Cloud Console → APIs & Services → Credentials "
                "→ OAuth 2.0 Client IDs → Download JSON, then save it there. "
                "Run 'pincer setup google' to complete setup."
            )

        if not self._token_path.exists():
            raise FileNotFoundError(
                "Google Workspace token not found. "
                "Run 'pincer setup google' to authenticate."
            )

        self._credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
            str(self._token_path), self._scopes
        )

        # Detect scope mismatch early — avoids opaque 403s from Google APIs.
        # Read scopes from raw JSON because from_authorized_user_file(scopes=...)
        # overrides credentials.scopes with the passed-in list, making the
        # credentials.scopes check always pass regardless of what the token holds.
        try:
            token_data = json.loads(self._token_path.read_text())
            token_scopes = token_data.get("scopes") or []
        except Exception:
            token_scopes = []
        if token_scopes:
            missing = [s for s in self._scopes if s not in token_scopes]
            if missing:
                raise RuntimeError(
                    f"Google token is missing required scopes: {missing}. "
                    f"Delete {self._token_path} and run 'pincer setup-google' "
                    "to re-authorise with the full scope set."
                )

        if self._credentials.valid:
            return self._credentials

        if self._credentials.expired and self._credentials.refresh_token:
            try:
                self._credentials.refresh(Request())
                self._save_token()
                logger.debug("Google Workspace token refreshed")
                return self._credentials
            except Exception as exc:
                raise RuntimeError(
                    f"Token refresh failed: {exc}. "
                    f"Delete {self._token_path} and run 'pincer setup-google'."
                ) from exc

        raise RuntimeError(
            "Google token is invalid (no refresh token). "
            f"Delete {self._token_path} and run 'pincer setup-google'."
        )

    def run_auth_flow(self, open_browser: bool = True) -> Any:
        """Run the InstalledAppFlow consent flow (blocking — call from CLI only).

        Args:
            open_browser: When True, tries to open the system browser.

        Returns:
            The authorised Credentials object.
        """
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            str(self._credentials_path), self._scopes
        )
        try:
            creds = flow.run_local_server(port=0, open_browser=open_browser)
        except Exception:
            logger.warning("run_local_server failed, trying port 8080 without browser open")
            creds = flow.run_local_server(port=8080, open_browser=False)

        self._credentials = creds
        self._save_token()
        logger.info("Google Workspace authentication successful, token saved to %s", self._token_path)
        return creds

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _save_token(self) -> None:
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._token_path, "w") as fh:
            fh.write(self._credentials.to_json())
        os.chmod(self._token_path, 0o600)

    @property
    def token_path(self) -> Path:
        return self._token_path

    @property
    def credentials_path(self) -> Path:
        return self._credentials_path

    def missing_scopes(self) -> list[str]:
        """Return scopes required by this instance that are absent from the cached token.

        Returns an empty list when the token is absent, unreadable, or has no
        ``scopes`` field (older tokens) — only flags a mismatch when the token
        explicitly lists scopes that are a subset of what is needed.
        """
        if not self._token_path.exists():
            return []
        try:
            token_data = json.loads(self._token_path.read_text())
            token_scopes = token_data.get("scopes") or []
        except Exception:
            return []
        if not token_scopes:
            return []
        return [s for s in self._scopes if s not in token_scopes]

    def authenticated_email(self) -> str | None:
        """Return the email address from the stored token, or None."""
        if not self._token_path.exists():
            return None
        try:
            data = json.loads(self._token_path.read_text())
            return data.get("client_id", "").split("-")[0] if "account" not in data else cast(Optional[str], data.get("account"))
        except Exception:
            return None
