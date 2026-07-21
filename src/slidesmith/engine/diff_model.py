"""Shared data types for content diffs."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from slidesmith.engine.content_parser import (
    ElementStyles,
    ParagraphStyles,
    ParsedElement,
    ParsedRun,
)


class ChangeType(Enum):
    """Types of changes detected."""

    # Element was deleted
    DELETE = "delete"

    # Element position changed
    MOVE = "move"

    # Element text changed
    TEXT_UPDATE = "text_update"

    # Element was copied from another element
    COPY = "copy"

    # Truly new element (no source)
    CREATE = "create"

    # Class-derived styles changed on an existing element
    STYLE_UPDATE = "style_update"

    # Explicit defaults on one or more <P class> attributes changed.
    PARAGRAPH_STYLE_UPDATE = "paragraph_style_update"

    # An existing image source or fit mode changed.
    IMAGE_UPDATE = "image_update"


@dataclass
class ParagraphClassUpdate:
    """One changed paragraph's scoped text/paragraph defaults."""

    paragraph_index: int
    old_styles: ParagraphStyles | None
    new_styles: ParagraphStyles | None


@dataclass
class Change:
    """A single change operation."""

    change_type: ChangeType

    # Target element ID (clean_id)
    target_id: str

    # For COPY: source element ID
    source_id: str | None = None

    # For MOVE/COPY: new position (x, y, and optionally w, h)
    new_position: dict[str, float] | None = None

    # For MOVE/COPY: pristine absolute SML position. styles.json positions may
    # be relative to a visual parent and are not a valid delta basis.
    old_position: dict[str, float] | None = None

    # For COPY: translation from original position (dx, dy)
    # Used to calculate child positions: child_new = child_orig + translation
    translation: dict[str, float] | None = None

    # For TEXT_UPDATE: new text
    new_text: list[str] | None = None

    # For TEXT_UPDATE: pristine text/runs (basis for minimal range edits)
    old_text: list[str] | None = None
    old_runs: list[list[ParsedRun]] | None = None

    # For CREATE/STYLE_UPDATE: class-derived styles from the edited element
    new_styles: ElementStyles | None = None

    # For STYLE_UPDATE: fields to clear when an entire authored class group was
    # removed. None means unchanged; a list means reset those fields to the
    # Slides inherited/default values with an empty field-masked update.
    text_style_reset_fields: list[str] | None = None
    paragraph_style_reset_fields: list[str] | None = None
    stroke_reset_fields: list[str] | None = None
    reset_content_alignment: bool = False

    # For CREATE/TEXT_UPDATE: styled text runs (one list per paragraph)
    new_runs: list[list[ParsedRun]] | None = None

    # For CREATE: explicit <P class> defaults, parallel to new_text.
    new_paragraph_styles: list[ParagraphStyles | None] | None = None

    # For COPY: pristine paragraph defaults used to apply only authored deltas
    # after duplicateObject preserves dynamic autoText.
    old_paragraph_styles: list[ParagraphStyles | None] | None = None

    # For PARAGRAPH_STYLE_UPDATE: only paragraphs whose class changed.
    paragraph_style_updates: list[ParagraphClassUpdate] | None = None

    # Slide index where this change occurs
    slide_index: str | None = None

    # For COPY: slide containing the pristine source element.
    source_slide_index: str | None = None

    # Parent element ID (for hierarchy reconstruction)
    parent_id: str | None = None

    # For GROUP COPY: list of child elements (recursive structure)
    # Each child is a dict with: id, tag, position (absolute), text, children
    children: list[dict[str, Any]] | None = None

    # Element tag (for creates/copies)
    tag: str | None = None

    # Authored Image CREATE/IMAGE_UPDATE metadata. Pulled images do not
    # populate these.
    src: str | None = None
    fit: str | None = None
    image_pixel_width: int | None = None
    image_pixel_height: int | None = None
    image_dimensions_fetch_failed: bool = False


@dataclass
class DiffResult:
    """Result of diffing pristine vs edited content."""

    changes: list[Change] = field(default_factory=list)

    # Elements by ID in edited version (for reconstruction)
    edited_elements: dict[str, ParsedElement] = field(default_factory=dict)

    # Styles from pristine (for copy operations)
    pristine_styles: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Lossy-but-safe request-generation decisions surfaced by push/CLI.
    warnings: list[str] = field(default_factory=list)
