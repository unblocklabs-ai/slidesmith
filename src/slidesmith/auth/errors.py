"""Typed exceptions raised by Slidesmith authentication flows."""


class AuthError(Exception):
    """Base class for authentication flow failures."""


class SessionExpiredError(AuthError):
    """A stored session can no longer be exchanged for credentials."""
