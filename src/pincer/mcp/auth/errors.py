"""OAuth 2.1 error types."""

from __future__ import annotations


class OAuthError(Exception):
    """RFC 6749 error response."""

    def __init__(self, error: str, description: str, status_code: int) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code

    def to_dict(self) -> dict[str, str]:
        return {"error": self.error, "error_description": self.description}


def invalid_request(desc: str) -> OAuthError:
    return OAuthError("invalid_request", desc, 400)


def invalid_client(desc: str) -> OAuthError:
    return OAuthError("invalid_client", desc, 401)


def invalid_grant(desc: str) -> OAuthError:
    return OAuthError("invalid_grant", desc, 400)


def unauthorized_client(desc: str) -> OAuthError:
    return OAuthError("unauthorized_client", desc, 400)


def unsupported_grant_type() -> OAuthError:
    return OAuthError("unsupported_grant_type", "Unsupported grant type", 400)


def insufficient_scope(required: str) -> OAuthError:
    err = OAuthError("insufficient_scope", f"Required scope: {required}", 403)
    err.required_scope = required  # type: ignore[attr-defined]
    return err
