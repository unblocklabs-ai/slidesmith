"""extraslide - Edit Google Slides through SML (Slide Markup Language)."""

from extraslide.client import (
    ConflictError,
    SlidesClient,
    diff_folder,
)
from extraslide.transport import (
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
