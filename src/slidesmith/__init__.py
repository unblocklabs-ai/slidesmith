"""slidesmith: agent+human co-editing for Google Slides.

Pull a deck to local SML files, edit them, diff locally, and push the
resulting batchUpdate requests back to the SAME deck in place.
"""

from slidesmith.workspace import materialize

__version__ = "0.1.0"
__all__ = ["materialize"]
