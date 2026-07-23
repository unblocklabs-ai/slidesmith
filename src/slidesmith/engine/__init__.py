"""slidesmith.engine - Edit Google Slides through SML (Slide Markup Language).

The public names remain available from ``slidesmith.engine`` but are resolved
only when requested.  Local-only modules such as ``engine.advisor`` therefore
do not import the HTTP transport merely because they share this package.
"""

from importlib import import_module

from slidesmith import __version__


_LAZY_EXPORTS = {
    "APIError": ("slidesmith.engine.transport", "APIError"),
    "AuthenticationError": ("slidesmith.engine.transport", "AuthenticationError"),
    "ConflictError": ("slidesmith.engine.conflicts", "ConflictError"),
    "GoogleSlidesTransport": ("slidesmith.engine.transport", "GoogleSlidesTransport"),
    "NotFoundError": ("slidesmith.engine.transport", "NotFoundError"),
    "PresentationData": ("slidesmith.engine.transport", "PresentationData"),
    "SlidesClient": ("slidesmith.engine.client", "SlidesClient"),
    "Transport": ("slidesmith.engine.transport", "Transport"),
    "TransportError": ("slidesmith.engine.transport", "TransportError"),
    "diff_folder": ("slidesmith.engine.client", "diff_folder"),
}


def __getattr__(name: str):
    """Resolve the legacy package exports on first attribute access."""
    try:
        module_name, attribute_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value

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
