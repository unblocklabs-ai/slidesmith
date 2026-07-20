"""Authentication building blocks for Slidesmith."""

from slidesmith.auth.discovery import OAuthClientCredentials
from slidesmith.auth.errors import AuthError, SessionExpiredError
from slidesmith.auth.stores import (
    FallbackSessionStore,
    FileSessionStore,
    InMemorySessionStore,
    KeyringSessionStore,
    SessionStore,
    SessionToken,
)

__all__ = [
    "AuthError",
    "FallbackSessionStore",
    "FileSessionStore",
    "InMemorySessionStore",
    "KeyringSessionStore",
    "OAuthClientCredentials",
    "SessionStore",
    "SessionToken",
    "SessionExpiredError",
]
