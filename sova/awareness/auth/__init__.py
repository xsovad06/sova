"""Google OAuth2 and credential management for awareness providers."""

try:
    from sova.awareness.auth.google_oauth import (
        AWARENESS_GOOGLE_SCOPES,
        DEFAULT_CREDENTIALS_PATH,
        DEFAULT_TOKEN_PATH,
        authenticate_google,
    )
except ImportError:
    authenticate_google = None  # type: ignore[assignment]
    AWARENESS_GOOGLE_SCOPES = None  # type: ignore[assignment]
    DEFAULT_CREDENTIALS_PATH = None  # type: ignore[assignment]
    DEFAULT_TOKEN_PATH = None  # type: ignore[assignment]

__all__ = [
    "AWARENESS_GOOGLE_SCOPES",
    "DEFAULT_CREDENTIALS_PATH",
    "DEFAULT_TOKEN_PATH",
    "authenticate_google",
]
