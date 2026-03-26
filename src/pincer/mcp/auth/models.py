"""OAuth 2.1 data models — frozen dataclasses, no business logic."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Optional


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_name: str
    redirect_uris: list[str]
    grant_types: list[str] = field(default_factory=lambda: ["authorization_code"])
    token_endpoint_auth_method: str = "none"
    client_secret: Optional[str] = None
    auto_consent: bool = False
    is_static: bool = False
    registered_at: int = field(default_factory=lambda: int(time.time()))


@dataclass(frozen=True)
class AuthorizationCode:
    code: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    scope: str
    resource: str
    expires_at: int
    used: bool = False


@dataclass(frozen=True)
class RefreshTokenData:
    token: str
    client_id: str
    scope: str
    resource: str
    expires_at: int


@dataclass(frozen=True)
class TokenClaims:
    iss: str
    sub: str
    aud: str
    exp: int
    iat: int
    jti: str
    scope: str
    client_id: str


@dataclass(frozen=True)
class AuthServerConfig:
    enabled: bool = True
    localhost_bypass: bool = True
    token_lifetime: int = 3600
    refresh_token_lifetime: int = 86400
    host: str = "127.0.0.1"
    port: int = 18800
    path: str = "/mcp"
    static_clients: list[dict[str, Any]] = field(default_factory=list)
    dcr_enabled: bool = True
    dcr_max_clients: int = 20
    default_scopes: str = "tools:read resources:read"
    max_scopes: str = "tools:all resources:read prompts:read admin:ask_user offline_access"


class GrantType(StrEnum):
    AUTHORIZATION_CODE = "authorization_code"
    CLIENT_CREDENTIALS = "client_credentials"
    REFRESH_TOKEN = "refresh_token"
