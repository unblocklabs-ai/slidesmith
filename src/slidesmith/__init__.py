"""slidesmith: agent+human co-editing for Google Slides.

Pull a deck to local SML files, edit them, diff locally, and push the
resulting batchUpdate requests back to the SAME deck in place.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("slidesmith")
except PackageNotFoundError:
    # Source checkouts without an installed distribution have no package metadata.
    __version__ = "0+unknown-dev"


def __getattr__(name: str):
    """Load the workspace helper only for callers that request it."""
    if name != "materialize":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from slidesmith.workspace import materialize

    globals()[name] = materialize
    return materialize


__all__ = ["materialize"]
