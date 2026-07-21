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

from slidesmith.workspace import materialize

__all__ = ["materialize"]
