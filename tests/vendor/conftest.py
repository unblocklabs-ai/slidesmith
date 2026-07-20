"""Compatibility injection for vendor tests without restoring shipped test code."""

import extraslide

from .helpers import LocalFileTransport


# The donor test imports this historic helper from extraslide. Keep that test
# untouched while exposing the helper only during tests, not in the package.
extraslide.LocalFileTransport = LocalFileTransport  # type: ignore[attr-defined]
