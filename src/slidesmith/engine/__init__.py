"""slidesmith.engine - Edit Google Slides through SML (Slide Markup Language)."""

from slidesmith import __version__
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
    "__version__",
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
