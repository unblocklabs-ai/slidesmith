"""slidesmith.engine - Edit Google Slides through SML (Slide Markup Language)."""

from slidesmith.engine.client import SlidesClient, diff_folder
from slidesmith.engine.conflicts import ConflictError
from slidesmith.engine.transport import (
    APIError,
    AuthenticationError,
    GoogleSlidesTransport,
    NotFoundError,
    PresentationData,
    Transport,
    TransportError,
)

__all__ = [
    "APIError",
    "AuthenticationError",
    "ConflictError",
    "GoogleSlidesTransport",
    "NotFoundError",
    "PresentationData",
    "SlidesClient",
    "Transport",
    "TransportError",
    "diff_folder",
]

__version__ = "0.1.0"
