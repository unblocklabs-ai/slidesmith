"""Diff algorithm for the copy-based workflow.

Compares pristine vs edited content and generates change operations.

Copy detection:
1. Element with same ID but missing w/h = COPY (new convention)
2. Duplicate IDs at different positions = COPY (legacy detection)

For copies, calculates translation from original position to apply to children.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from slidesmith.engine.assets import (
    image_source_kind,
    inspect_local_image,
    resolve_local_image_path,
)
from slidesmith.engine.classes import Fill, ParagraphStyle, PropertyState, Stroke, TextStyle
from slidesmith.engine.content_parser import (
    ElementStyles,
    ParagraphStyles,
    ParsedElement,
    ParsedRun,
    flatten_elements,
    parse_slide_content,
    validate_authored_image_geometry,
)
from slidesmith.engine.image_fetch import fetch_image_dimensions


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

    # Authored Image CREATE metadata. Pulled images do not populate these.
    src: str | None = None
    fit: str | None = None


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


def format_diff_summary(diff_result: DiffResult, request_count: int) -> str:
    """Render a compact, slide-grouped description of semantic changes."""
    changes_by_slide: dict[str, list[Change]] = {}
    for change in diff_result.changes:
        changes_by_slide.setdefault(change.slide_index or "?", []).append(change)

    lines: list[str] = []
    for slide_index in sorted(changes_by_slide, key=_slide_sort_key):
        changes = changes_by_slide[slide_index]
        lines.append(f"Slide {slide_index}")

        deleted_ids = [
            change.target_id
            for change in changes
            if change.change_type == ChangeType.DELETE
        ]
        if deleted_ids:
            lines.append(f"  DELETE {', '.join(deleted_ids)}")

        for change_type in (
            ChangeType.CREATE,
            ChangeType.MOVE,
            ChangeType.COPY,
            ChangeType.STYLE_UPDATE,
            ChangeType.PARAGRAPH_STYLE_UPDATE,
            ChangeType.TEXT_UPDATE,
        ):
            for change in changes:
                if change.change_type == change_type:
                    lines.append(f"  {_format_summary_change(change)}")

    if lines:
        lines.append("")
    lines.append(f"{request_count} requests total")
    return "\n".join(lines)


def _slide_sort_key(slide_index: str) -> tuple[int, int | str]:
    if slide_index.isdigit():
        return (0, int(slide_index))
    return (1, slide_index)


def _format_summary_change(change: Change) -> str:
    if change.change_type == ChangeType.CREATE:
        tag = change.tag or "Element"
        details = f" ({tag}{_format_frame(change.new_position)})"
        additions: list[str] = []
        if change.new_styles is not None:
            if change.new_styles.fill is not None:
                additions.append("+fill")
            if change.new_styles.stroke is not None:
                additions.append("+stroke")
        if change.new_text:
            count = len(change.new_text)
            noun = "paragraph" if count == 1 else "paragraphs"
            additions.append(f"+{count} {noun}")
        suffix = f" {' '.join(additions)}" if additions else ""
        return f"CREATE {change.target_id}{details}{suffix}"

    if change.change_type == ChangeType.MOVE:
        return f"MOVE {change.target_id}{_format_frame(change.new_position)}"

    if change.change_type == ChangeType.COPY:
        source_id = change.source_id or change.target_id
        return f"COPY {source_id} -> {change.target_id}{_format_frame(change.new_position)}"

    if change.change_type == ChangeType.STYLE_UPDATE:
        return f"STYLE {change.target_id}: {_format_style_delta(change.new_styles)}"

    if change.change_type == ChangeType.PARAGRAPH_STYLE_UPDATE:
        count = len(change.paragraph_style_updates or [])
        noun = "edit" if count == 1 else "edits"
        return f"STYLE {change.target_id}: {count} paragraph range {noun}"

    if change.change_type == ChangeType.TEXT_UPDATE:
        return f"TEXT {change.target_id}: 1 range edit"

    return f"{change.change_type.value.upper()} {change.target_id}"


def _format_frame(position: dict[str, float] | None) -> str:
    if not position:
        return ""
    x = _format_number(position.get("x"))
    y = _format_number(position.get("y"))
    if position.get("w") is None or position.get("h") is None:
        return f" @{x},{y}"
    width = _format_number(position.get("w"))
    height = _format_number(position.get("h"))
    return f" {width}x{height} @{x},{y}"


def _format_number(value: float | None) -> str:
    if value is None:
        return "?"
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _format_style_delta(styles: ElementStyles | None) -> str:
    if styles is None:
        return "style"
    parts: list[str] = []
    if styles.fill is not None:
        parts.append("fill")
    if styles.stroke is not None:
        parts.append("stroke")
    if styles.text_style is not None:
        parts.append("textStyle")
    if styles.paragraph_style is not None:
        parts.append("paragraphStyle")
    if styles.content_alignment is not None:
        value = getattr(styles.content_alignment, "value", styles.content_alignment)
        parts.append(f"contentAlignment {value}")
    return ", ".join(parts) or "style"


def diff_presentation(
    pristine_slides: dict[str, list[ParsedElement]],
    edited_slides: dict[str, list[ParsedElement]],
    pristine_styles: dict[str, dict[str, Any]],
    _id_mapping: dict[str, str],
    *,
    workspace_root: Path | None = None,
) -> DiffResult:
    """Diff pristine and edited presentation content.

    Detects:
    - Deleted elements
    - Moved elements (position changed)
    - Text updates
    - Copied elements (duplicate IDs in edited)
    - New elements

    Args:
        pristine_slides: Original slide content (from pull)
        edited_slides: Modified slide content (after LLM edits)
        pristine_styles: Original styles.json
        id_mapping: Original id_mapping.json

    Returns:
        DiffResult with all detected changes
    """
    result = DiffResult(pristine_styles=pristine_styles)

    # Flatten pristine elements
    pristine_elements: dict[str, ParsedElement] = {}
    pristine_slide_map: dict[str, str] = {}  # element_id -> slide_index
    for slide_idx, roots in pristine_slides.items():
        flattened = flatten_elements(roots)
        for elem_id, elem in flattened.items():
            pristine_elements[elem_id] = elem
            pristine_slide_map[elem_id] = slide_idx

    # Flatten edited elements, tracking duplicates
    # Note: We can't use flatten_elements because it returns a dict which loses duplicates
    edited_elements: dict[
        str, list[tuple[str, ParsedElement]]
    ] = {}  # id -> [(slide_idx, elem)]
    for slide_idx, roots in edited_slides.items():
        all_elements = _collect_all_elements(roots)
        for elem in all_elements:
            if elem.clean_id:
                if elem.clean_id not in edited_elements:
                    edited_elements[elem.clean_id] = []
                edited_elements[elem.clean_id].append((slide_idx, elem))

    # Store first instance of each edited element for reconstruction
    for elem_id, instances in edited_elements.items():
        if instances:
            result.edited_elements[elem_id] = instances[0][1]

    # Known IDs from pristine (these are original elements)
    known_ids = set(pristine_elements.keys())

    # First pass: identify which elements are being copied as groups
    # We need to skip children of copied groups to avoid duplicates
    copied_descendant_instances: set[int] = set()

    for elem_id, instances in edited_elements.items():
        if elem_id not in known_ids:
            continue
        pristine_elem = pristine_elements[elem_id]
        if len(instances) == 1:
            copy_instances = (
                instances if _is_copy_by_missing_dimensions(instances[0][1]) else []
            )
        else:
            _, copy_instances = _split_original_and_copies(
                instances,
                pristine_slide_map[elem_id],
                pristine_elem,
            )
        for _, edited_elem in copy_instances:
            if edited_elem.children:
                # Suppress only descendants belonging to this copy. The
                # original child instance must still be compared for same-diff
                # edits to text, geometry, and styles.
                _collect_descendant_instances(
                    edited_elem, copied_descendant_instances
                )

    # Detect changes
    retained_pristine_ids: set[str] = set()
    for elem_id, instances in edited_elements.items():
        instances = [
            instance
            for instance in instances
            if id(instance[1]) not in copied_descendant_instances
        ]
        if not instances:
            continue

        if elem_id in known_ids:
            # A non-suppressed instance retains the pristine source. Explicit
            # copy roots are intentionally included: authoring a copy does not
            # delete its source. Descendants found only inside that copy were
            # suppressed above and therefore do not retain their originals.
            retained_pristine_ids.add(elem_id)
            # This ID existed in pristine
            pristine_elem = pristine_elements[elem_id]

            if len(instances) == 1:
                # Single instance - check if it's a copy (missing w/h) or modification
                slide_idx, edited_elem = instances[0]

                # NEW CONVENTION: Missing w/h indicates a copy
                if _is_copy_by_missing_dimensions(edited_elem):
                    # This is a copy - element has x,y but no w,h
                    result.changes.append(
                        _make_copy_change(
                            elem_id,
                            0,
                            slide_idx,
                            edited_elem,
                            pristine_elem,
                            pristine_slide_map[elem_id],
                        )
                    )
                else:
                    # Normal modification check
                    changes = _compare_elements(pristine_elem, edited_elem, slide_idx)
                    result.changes.extend(changes)
            else:
                # Multiple instances - identify original vs copies
                original_instance, copy_instances = _split_original_and_copies(
                    instances,
                    pristine_slide_map[elem_id],
                    pristine_elem,
                )

                # Handle original (if it still exists on its original slide)
                if original_instance:
                    slide_idx, edited_elem = original_instance
                    changes = _compare_elements(pristine_elem, edited_elem, slide_idx)
                    result.changes.extend(changes)

                # Handle copies
                for i, (slide_idx, edited_elem) in enumerate(copy_instances):
                    result.changes.append(
                        _make_copy_change(
                            elem_id,
                            i,
                            slide_idx,
                            edited_elem,
                            pristine_elem,
                            pristine_slide_map[elem_id],
                        )
                    )
        else:
            # ID doesn't exist in pristine
            # Check if it looks like it was copied from another element
            for slide_idx, edited_elem in instances:
                # For now, treat as new element
                # Could enhance to detect copies by content similarity
                result.changes.append(
                    Change(
                        change_type=ChangeType.CREATE,
                        target_id=elem_id,
                        slide_index=slide_idx,
                        parent_id=edited_elem.parent_id,
                        new_position=_get_create_position(
                            edited_elem, workspace_root=workspace_root
                        ),
                        new_text=edited_elem.paragraphs
                        if edited_elem.paragraphs
                        else None,
                        new_styles=edited_elem.styles,
                        new_runs=edited_elem.runs if edited_elem.runs else None,
                        new_paragraph_styles=edited_elem.paragraph_styles
                        if edited_elem.paragraph_styles
                        else None,
                        tag=edited_elem.tag,
                        src=edited_elem.src,
                        fit=edited_elem.fit,
                    )
                )

    # Detect deletions
    for elem_id in known_ids:
        if elem_id not in retained_pristine_ids:
            slide_idx = pristine_slide_map.get(elem_id, "")
            result.changes.append(
                Change(
                    change_type=ChangeType.DELETE,
                    target_id=elem_id,
                    slide_index=slide_idx,
                )
            )

    return result


def _compare_elements(
    pristine: ParsedElement,
    edited: ParsedElement,
    slide_idx: str,
) -> list[Change]:
    """Compare two elements with the same ID and generate changes."""
    changes: list[Change] = []

    # Check position change (only for root elements)
    if (
        pristine.has_position
        and edited.has_position
        and (
            pristine.x != edited.x
            or pristine.y != edited.y
            or pristine.w != edited.w
            or pristine.h != edited.h
        )
    ):
        changes.append(
            Change(
                change_type=ChangeType.MOVE,
                target_id=pristine.clean_id,
                slide_index=slide_idx,
                new_position=_get_position(edited),
                old_position=_get_position(pristine),
            )
        )

    # Check text change (text content or per-run styling)
    if pristine.paragraphs != edited.paragraphs or pristine.runs != edited.runs:
        changes.append(
            Change(
                change_type=ChangeType.TEXT_UPDATE,
                target_id=pristine.clean_id,
                slide_index=slide_idx,
                new_text=edited.paragraphs,
                new_runs=edited.runs if edited.runs else None,
                old_text=pristine.paragraphs,
                old_runs=pristine.runs if pristine.runs else None,
            )
        )

    paragraph_updates = [
        ParagraphClassUpdate(index, old_style, new_style)
        for index, (old_style, new_style) in enumerate(
            zip(
                pristine.paragraph_styles,
                edited.paragraph_styles,
                strict=False,
            )
        )
        if old_style != new_style
    ]
    if len(edited.paragraph_styles) > len(pristine.paragraph_styles):
        paragraph_updates.extend(
            ParagraphClassUpdate(index, None, edited.paragraph_styles[index])
            for index in range(
                len(pristine.paragraph_styles), len(edited.paragraph_styles)
            )
            if edited.paragraph_styles[index] is not None
        )
    if paragraph_updates:
        changes.append(
            Change(
                change_type=ChangeType.PARAGRAPH_STYLE_UPDATE,
                target_id=pristine.clean_id,
                slide_index=slide_idx,
                new_text=edited.paragraphs,
                new_runs=edited.runs,
                paragraph_style_updates=paragraph_updates,
            )
        )

    # Check class-derived style change
    if edited.styles != pristine.styles:
        pristine_styles = pristine.styles or ElementStyles()
        edited_styles = edited.styles or ElementStyles()
        # Carry only changed style groups. Once pulled SML contains multiple
        # explicit groups, replaying every edited group would let a fill-only
        # edit overwrite a concurrent human paragraph/stroke change.
        style_delta = ElementStyles(
            fill=(
                edited_styles.fill
                if edited_styles.fill is not None
                else Fill(state=PropertyState.INHERIT)
            )
            if edited_styles.fill != pristine_styles.fill
            else None,
            stroke=(
                edited_styles.stroke
                if edited_styles.stroke is not None
                else Stroke(
                    state=PropertyState.INHERIT,
                    color=pristine_styles.stroke.color
                    if pristine_styles.stroke is not None
                    else None,
                    weight_pt=pristine_styles.stroke.weight_pt
                    if pristine_styles.stroke is not None
                    else None,
                    dash_style=pristine_styles.stroke.dash_style
                    if pristine_styles.stroke is not None
                    else None,
                )
            )
            if edited_styles.stroke != pristine_styles.stroke
            else None,
            text_style=edited_styles.text_style
            if edited_styles.text_style != pristine_styles.text_style
            else None,
            paragraph_style=edited_styles.paragraph_style
            if edited_styles.paragraph_style != pristine_styles.paragraph_style
            else None,
            content_alignment=edited_styles.content_alignment
            if edited_styles.content_alignment != pristine_styles.content_alignment
            else None,
        )
        changes.append(
            Change(
                change_type=ChangeType.STYLE_UPDATE,
                target_id=pristine.clean_id,
                slide_index=slide_idx,
                new_styles=style_delta,
                text_style_reset_fields=(
                    _removed_text_style_fields(
                        pristine_styles.text_style,
                        edited_styles.text_style,
                    )
                ),
                paragraph_style_reset_fields=(
                    _removed_paragraph_style_fields(
                        pristine_styles.paragraph_style,
                        edited_styles.paragraph_style,
                    )
                ),
                stroke_reset_fields=(
                    _removed_stroke_fields(
                        pristine_styles.stroke,
                        edited_styles.stroke,
                    )
                    if edited_styles.stroke is not None
                    else None
                ),
                reset_content_alignment=(
                    pristine_styles.content_alignment is not None
                    and edited_styles.content_alignment is None
                ),
                new_text=edited.paragraphs if edited.paragraphs else None,
                new_runs=edited.runs if edited.runs else None,
                new_paragraph_styles=edited.paragraph_styles
                if edited.paragraph_styles
                else None,
                tag=edited.tag,
            )
        )

    return changes


def _make_copy_change(
    source_id: str,
    copy_index: int,
    slide_index: str,
    edited: ParsedElement,
    pristine: ParsedElement,
    source_slide_index: str,
) -> Change:
    """Build the canonical COPY change for either copy-detection branch."""
    children = (
        _serialize_children(edited.children, pristine.children)
        if edited.children
        else None
    )
    return Change(
        change_type=ChangeType.COPY,
        target_id=f"{source_id}_copy{copy_index}",
        source_id=source_id,
        slide_index=slide_index,
        parent_id=edited.parent_id,
        new_position=_get_position_with_pristine_size(edited, pristine),
        old_position=_get_position(pristine),
        translation=_calculate_translation(pristine, edited),
        new_text=edited.paragraphs if edited.paragraphs else None,
        old_text=pristine.paragraphs if pristine.paragraphs else None,
        new_runs=edited.runs if edited.runs else None,
        old_runs=pristine.runs if pristine.runs else None,
        new_paragraph_styles=(
            edited.paragraph_styles if edited.paragraph_styles else None
        ),
        old_paragraph_styles=(
            pristine.paragraph_styles if pristine.paragraph_styles else None
        ),
        source_slide_index=source_slide_index,
        children=children,
        tag=edited.tag,
    )


def _split_original_and_copies(
    instances: list[tuple[str, ParsedElement]],
    original_slide: str,
    pristine: ParsedElement,
) -> tuple[
    tuple[str, ParsedElement] | None,
    list[tuple[str, ParsedElement]],
]:
    """Partition duplicate edited instances into the original and its copies."""
    original_instance: tuple[str, ParsedElement] | None = None
    copy_instances: list[tuple[str, ParsedElement]] = []
    same_slide_instances: list[tuple[str, ParsedElement]] = []

    for slide_index, edited in instances:
        if _is_copy_by_missing_dimensions(edited):
            copy_instances.append((slide_index, edited))
        elif slide_index == original_slide:
            same_slide_instances.append((slide_index, edited))
        else:
            copy_instances.append((slide_index, edited))

    if len(same_slide_instances) == 1:
        original_instance = same_slide_instances[0]
    elif len(same_slide_instances) > 1:
        for slide_index, edited in same_slide_instances:
            if edited.x == pristine.x and edited.y == pristine.y:
                original_instance = (slide_index, edited)
            else:
                copy_instances.append((slide_index, edited))
        if original_instance is None:
            original_instance = same_slide_instances[0]
            copy_instances.extend(same_slide_instances[1:])

    return original_instance, copy_instances


def _serialize_children(
    children: list[ParsedElement],
    pristine_children: list[ParsedElement] | None = None,
) -> list[dict[str, Any]]:
    """Serialize children elements for inclusion in a Change.

    This captures all the information needed to recreate the children
    when generating copy requests.
    """
    result: list[dict[str, Any]] = []

    pristine_by_id = {
        child.clean_id: child for child in (pristine_children or [])
    }

    for child in children:
        pristine_child = pristine_by_id.get(child.clean_id)
        child_data: dict[str, Any] = {
            "id": child.clean_id,
            "tag": child.tag,
        }

        # Include position if available
        if child.has_position:
            child_data["position"] = {
                "x": child.x,
                "y": child.y,
                "w": child.w,
                "h": child.h,
            }
        if pristine_child is not None and pristine_child.has_position:
            child_data["sourcePosition"] = {
                "x": pristine_child.x,
                "y": pristine_child.y,
                "w": pristine_child.w,
                "h": pristine_child.h,
            }

        # Include edited and pristine text state so the duplicateObject path
        # can replay only authored descendant deltas onto its mapped IDs.
        if child.paragraphs or (pristine_child and pristine_child.paragraphs):
            child_data["text"] = child.paragraphs
            child_data["runs"] = child.runs
            child_data["paragraphStyles"] = child.paragraph_styles
            if pristine_child is not None:
                child_data["sourceText"] = pristine_child.paragraphs
                child_data["sourceRuns"] = pristine_child.runs
                child_data["sourceParagraphStyles"] = pristine_child.paragraph_styles

        # Recursively include nested children
        if child.children:
            child_data["children"] = _serialize_children(
                child.children,
                pristine_child.children if pristine_child is not None else None,
            )

        result.append(child_data)

    return result


def _collect_all_elements(roots: list[ParsedElement]) -> list[ParsedElement]:
    """Collect all elements from a tree, including duplicates.

    Unlike flatten_elements which returns a dict (losing duplicates),
    this returns a list preserving all elements including duplicates.
    """
    result: list[ParsedElement] = []

    def _collect(elem: ParsedElement) -> None:
        result.append(elem)
        for child in elem.children:
            _collect(child)

    for root in roots:
        _collect(root)

    return result


def _collect_descendant_instances(elem: ParsedElement, result: set[int]) -> None:
    """Collect object identities for descendants belonging to one copy."""
    for child in elem.children:
        result.add(id(child))
        _collect_descendant_instances(child, result)


_TEXT_STYLE_FIELD_NAMES = {
    "bold": "bold",
    "italic": "italic",
    "underline": "underline",
    "strikethrough": "strikethrough",
    "small_caps": "smallCaps",
    "baseline_offset": "baselineOffset",
    "font_family": "fontFamily",
    "font_size_pt": "fontSize",
    "font_weight": "weightedFontFamily",
    "foreground_color": "foregroundColor",
    "background_color": "backgroundColor",
    "link": "link",
}
_TEXT_STYLE_FONT_FAMILY_FIELDS = frozenset({"fontFamily", "weightedFontFamily"})
_PARAGRAPH_STYLE_FIELD_NAMES = {
    "alignment": "alignment",
    "line_spacing": "lineSpacing",
    "space_above_pt": "spaceAbove",
    "space_below_pt": "spaceBelow",
    "indent_start_pt": "indentStart",
    "indent_end_pt": "indentEnd",
    "indent_first_line_pt": "indentFirstLine",
    "direction": "direction",
    "spacing_mode": "spacingMode",
}


def _changed_style_fields(
    old_style: Any,
    new_style: Any,
    field_names: dict[str, str],
) -> list[str]:
    """Return API fields whose typed style values changed."""
    fields: list[str] = []
    for attr_name, api_name in field_names.items():
        if getattr(old_style, attr_name, None) == getattr(
            new_style, attr_name, None
        ):
            continue
        if (
            field_names is _TEXT_STYLE_FIELD_NAMES
            and attr_name == "font_family"
            and getattr(new_style, "font_weight", None) is not None
        ):
            api_name = "weightedFontFamily"
        if api_name not in fields:
            fields.append(api_name)
    return fields


def _text_style_font_family_field(style: TextStyle) -> str | None:
    """Return the API field representing the authored font-family group."""
    if style.font_weight is not None:
        return "weightedFontFamily"
    if style.font_family is not None:
        return "fontFamily"
    return None


def _text_style_fields(style: TextStyle) -> list[str]:
    """Return the API field mask represented by one authored text class group."""
    fields: list[str] = []
    for attribute, api_name in (
        ("bold", "bold"),
        ("italic", "italic"),
        ("underline", "underline"),
        ("strikethrough", "strikethrough"),
        ("small_caps", "smallCaps"),
        ("baseline_offset", "baselineOffset"),
        ("font_size_pt", "fontSize"),
        ("foreground_color", "foregroundColor"),
        ("background_color", "backgroundColor"),
        ("link", "link"),
    ):
        if getattr(style, attribute) is not None:
            fields.append(api_name)
    family_field = _text_style_font_family_field(style)
    if family_field is not None:
        fields.append(family_field)
    return fields


def _paragraph_style_fields(style: ParagraphStyle) -> list[str]:
    """Return the API field mask represented by one paragraph class group."""
    return [
        api_name
        for attribute, api_name in (
            ("alignment", "alignment"),
            ("line_spacing", "lineSpacing"),
            ("space_above_pt", "spaceAbove"),
            ("space_below_pt", "spaceBelow"),
            ("indent_start_pt", "indentStart"),
            ("indent_end_pt", "indentEnd"),
            ("indent_first_line_pt", "indentFirstLine"),
            ("direction", "direction"),
            ("spacing_mode", "spacingMode"),
        )
        if getattr(style, attribute) is not None
    ]


def _removed_text_style_fields(
    pristine: TextStyle | None,
    edited: TextStyle | None,
) -> list[str] | None:
    """Return element text fields removed while sibling classes survive."""
    old = pristine or TextStyle()
    new = edited or TextStyle()
    changed = set(_changed_style_fields(old, new, _TEXT_STYLE_FIELD_NAMES))
    represented_after = set(_text_style_fields(new))
    if new.font_family is not None:
        represented_after.update(_TEXT_STYLE_FONT_FAMILY_FIELDS)
    removed = [
        field
        for field in _text_style_fields(old)
        if field in changed and field not in represented_after
    ]
    return removed or None


def _removed_paragraph_style_fields(
    pristine: ParagraphStyle | None,
    edited: ParagraphStyle | None,
) -> list[str] | None:
    """Return element paragraph fields removed while sibling classes survive."""
    old = pristine or ParagraphStyle()
    new = edited or ParagraphStyle()
    changed = set(_changed_style_fields(old, new, _PARAGRAPH_STYLE_FIELD_NAMES))
    represented_after = set(_paragraph_style_fields(new))
    removed = [
        field
        for field in _paragraph_style_fields(old)
        if field in changed and field not in represented_after
    ]
    return removed or None


def _removed_stroke_fields(
    pristine: Stroke | None,
    edited: Stroke | None,
) -> list[str] | None:
    """Return logical Slides stroke fields removed from a partial class group."""
    old = pristine or Stroke()
    new = edited or Stroke()
    fields = _changed_style_fields(
        old,
        new,
        {
            "color": "lineFill",
            "weight_pt": "weight",
            "dash_style": "dashStyle",
        },
    )
    represented_after = {
        field
        for attribute, field in (
            ("color", "lineFill"),
            ("weight_pt", "weight"),
            ("dash_style", "dashStyle"),
        )
        if getattr(new, attribute) is not None
    }
    removed = [field for field in fields if field not in represented_after]
    return removed or None


def _get_position(elem: ParsedElement) -> dict[str, float] | None:
    """Extract position dictionary from element."""
    if not elem.has_position:
        return None
    return {
        "x": elem.x or 0,
        "y": elem.y or 0,
        "w": elem.w or 0,
        "h": elem.h or 0,
    }


def _get_create_position(
    elem: ParsedElement,
    *,
    workspace_root: Path | None = None,
) -> dict[str, float] | None:
    """Resolve authored CREATE geometry, including Image contain fitting."""
    if elem.tag == "Image" and elem.src is not None:
        validate_authored_image_geometry(
            elem.clean_id,
            x=elem.x,
            y=elem.y,
            w=elem.w,
            h=elem.h,
        )
        if image_source_kind(elem.src) == "local":
            if workspace_root is None:
                raise ValueError(
                    f"Local image source {elem.src!r} on Image element "
                    f"'{elem.clean_id}' requires a presentation workspace"
                )
            local_path = resolve_local_image_path(workspace_root, elem.src)
            local_pixels = inspect_local_image(local_path, source=elem.src)[:2]
        else:
            local_pixels = None
    position = _get_position(elem)
    if elem.tag != "Image" or elem.fit != "contain":
        return position
    if position is None or not elem.src:
        return position

    width = position["w"]
    height = position["h"]
    if width <= 0 or height <= 0:
        raise ValueError(
            f"Image element '{elem.clean_id}' with fit='contain' requires "
            "positive w and h"
        )

    if local_pixels is not None:
        pixel_width, pixel_height = local_pixels
    else:
        pixel_width, pixel_height = _fetch_image_dimensions(elem.src)
    if pixel_width <= 0 or pixel_height <= 0:
        raise ValueError(
            f"Could not determine positive pixel dimensions for Image element "
            f"'{elem.clean_id}' from {elem.src!r}"
        )

    image_aspect = pixel_width / pixel_height
    frame_aspect = width / height
    contained = dict(position)
    if image_aspect > frame_aspect:
        contained["h"] = width / image_aspect
    elif image_aspect < frame_aspect:
        contained["w"] = height * image_aspect
    return contained


def _fetch_image_dimensions(url: str) -> tuple[int, int]:
    """Download an authored image through the shared constrained fetcher."""
    return fetch_image_dimensions(url)


def _is_copy_by_missing_dimensions(elem: ParsedElement) -> bool:
    """Check if element is a copy based on missing w/h.

    The copy convention: copies have x, y but omit w, h.
    """
    return elem.x is not None and elem.w is None


def _calculate_translation(
    pristine: ParsedElement, edited: ParsedElement
) -> dict[str, float]:
    """Calculate translation from pristine to edited position.

    Returns dx, dy that can be applied to child positions.
    """
    pristine_x = pristine.x or 0
    pristine_y = pristine.y or 0
    edited_x = edited.x or 0
    edited_y = edited.y or 0

    return {
        "dx": edited_x - pristine_x,
        "dy": edited_y - pristine_y,
    }


def _get_position_with_pristine_size(
    edited: ParsedElement, pristine: ParsedElement
) -> dict[str, float]:
    """Get position using edited x,y but pristine w,h.

    For copies, the edited element may omit w,h, so we use pristine values.
    """
    return {
        "x": edited.x or 0,
        "y": edited.y or 0,
        "w": edited.w if edited.w is not None else (pristine.w or 0),
        "h": edited.h if edited.h is not None else (pristine.h or 0),
    }


def diff_slide_content(
    pristine_content: str,
    edited_content: str,
    pristine_styles: dict[str, dict[str, Any]],
    slide_index: str,
) -> list[Change]:
    """Diff a single slide's content.

    Convenience function for diffing one slide at a time.

    Args:
        pristine_content: Original content.sml
        edited_content: Modified content.sml
        pristine_styles: Styles from original styles.json
        slide_index: The slide index (e.g., "01")

    Returns:
        List of changes for this slide
    """
    pristine_elements = parse_slide_content(pristine_content)
    edited_elements = parse_slide_content(edited_content)

    result = diff_presentation(
        {slide_index: pristine_elements},
        {slide_index: edited_elements},
        pristine_styles,
        {},
    )

    return result.changes
