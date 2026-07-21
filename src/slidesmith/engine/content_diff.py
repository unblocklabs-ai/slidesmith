"""Diff algorithm for the copy-based workflow.

Compares pristine vs edited content and generates change operations.

Copy detection:
1. Element with same ID but missing w/h = COPY (new convention)
2. Duplicate IDs at different positions = COPY (legacy detection)

For copies, calculates translation from original position to apply to children.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from slidesmith.engine.assets import (  # noqa: F401
    image_source_kind,
    inspect_local_image,
    resolve_local_image_path,
)
from slidesmith.engine.classes import (  # noqa: F401
    Fill,
    ParagraphStyle,
    PropertyState,
    Stroke,
    TextStyle,
)
from slidesmith.engine.content_parser import (  # noqa: F401
    ElementStyles,
    ParsedElement,
    flatten_elements,
    parse_slide_content,
    validate_authored_image_geometry,
)
from slidesmith.engine.diff_model import (
    Change,
    ChangeType,
    DiffResult,
    ParagraphClassUpdate,
    PushWarning,
)
from slidesmith.engine.diff_summary import format_diff_summary  # noqa: F401
from slidesmith.engine.image_geometry import (  # noqa: F401
    _fetch_image_dimensions,
    get_image_source_dimensions,
    get_effective_position,
)
from slidesmith.engine.image_fetch import fetch_image_dimensions  # noqa: F401
from slidesmith.engine.style_delta import (  # noqa: F401
    _PARAGRAPH_STYLE_FIELD_NAMES,
    _TEXT_STYLE_FIELD_NAMES,
    _TEXT_STYLE_FONT_FAMILY_FIELDS,
    _changed_style_fields,
    _paragraph_style_fields,
    _removed_paragraph_style_fields,
    _removed_stroke_fields,
    _removed_text_style_fields,
    _text_style_fields,
    _text_style_font_family_field,
)


def _utf16_len(text: str) -> int:
    """Length of text in UTF-16 code units (the Slides API index space)."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def diff_presentation(
    pristine_slides: dict[str, list[ParsedElement]],
    edited_slides: dict[str, list[ParsedElement]],
    pristine_styles: dict[str, dict[str, Any]],
    *,
    workspace_root: Path | None = None,
    allow_remote_image_fetch: bool = False,
    fetch_remote_stretch_dimensions: bool = False,
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
                    changes = _compare_elements(
                        pristine_elem,
                        edited_elem,
                        slide_idx,
                        workspace_root=workspace_root,
                        allow_remote_image_fetch=allow_remote_image_fetch,
                        fetch_remote_stretch_dimensions=fetch_remote_stretch_dimensions,
                        warnings=result.warnings,
                    )
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
                    changes = _compare_elements(
                        pristine_elem,
                        edited_elem,
                        slide_idx,
                        workspace_root=workspace_root,
                        allow_remote_image_fetch=allow_remote_image_fetch,
                        fetch_remote_stretch_dimensions=fetch_remote_stretch_dimensions,
                        warnings=result.warnings,
                    )
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
                image_fetch_failure: list[bool] = []
                image_dimensions = get_image_source_dimensions(
                    edited_elem,
                    workspace_root=workspace_root,
                    allow_remote_image_fetch=allow_remote_image_fetch,
                    fetch_remote_stretch=fetch_remote_stretch_dimensions,
                    warnings=result.warnings,
                    fetch_failure=image_fetch_failure,
                )
                result.changes.append(
                    Change(
                        change_type=ChangeType.CREATE,
                        target_id=elem_id,
                        slide_index=slide_idx,
                        parent_id=edited_elem.parent_id,
                        new_position=get_effective_position(
                            edited_elem,
                            workspace_root=workspace_root,
                            allow_remote_image_fetch=allow_remote_image_fetch,
                            source_dimensions=image_dimensions,
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
                        image_pixel_width=(
                            image_dimensions[0] if image_dimensions else None
                        ),
                        image_pixel_height=(
                            image_dimensions[1] if image_dimensions else None
                        ),
                        image_dimensions_fetch_failed=bool(image_fetch_failure),
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
    *,
    workspace_root: Path | None = None,
    allow_remote_image_fetch: bool = False,
    fetch_remote_stretch_dimensions: bool = False,
    warnings: list[PushWarning] | None = None,
) -> list[Change]:
    """Compare two elements with the same ID and generate changes."""
    changes: list[Change] = []
    pristine_position = _get_position(pristine)
    image_update = (
        edited.tag == "Image"
        and edited.src is not None
        and (pristine.src != edited.src or pristine.fit != edited.fit)
    )
    image_fetch_failure: list[bool] = []
    image_dimensions = get_image_source_dimensions(
        edited,
        workspace_root=workspace_root,
        allow_remote_image_fetch=allow_remote_image_fetch,
        fetch_remote_stretch=fetch_remote_stretch_dimensions and image_update,
        warnings=warnings,
        fetch_failure=image_fetch_failure,
    )
    edited_position = get_effective_position(
        edited,
        workspace_root=workspace_root,
        allow_remote_image_fetch=allow_remote_image_fetch,
        source_dimensions=image_dimensions,
    )

    # Check position change (only for root elements)
    if (
        not image_update
        and pristine.has_position
        and edited.has_position
        and pristine_position != edited_position
    ):
        changes.append(
            Change(
                change_type=ChangeType.MOVE,
                target_id=pristine.clean_id,
                slide_index=slide_idx,
                new_position=edited_position,
                old_position=pristine_position,
            )
        )

    if image_update:
        changes.append(
            Change(
                change_type=ChangeType.IMAGE_UPDATE,
                target_id=pristine.clean_id,
                slide_index=slide_idx,
                new_position=edited_position,
                old_position=pristine_position,
                src=edited.src,
                fit=edited.fit,
                image_pixel_width=(
                    image_dimensions[0] if image_dimensions else None
                ),
                image_pixel_height=(
                    image_dimensions[1] if image_dimensions else None
                ),
                image_dimensions_fetch_failed=bool(image_fetch_failure),
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
                author_removed_classes=_removed_run_classes(
                    pristine.runs, edited.runs
                ),
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
                author_removed_classes=frozenset(
                    removed
                    for update in paragraph_updates
                    for removed in _removed_paragraph_classes(
                        update.old_styles, update.new_styles
                    )
                ),
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
                author_removed_classes=_removed_element_classes(
                    pristine_styles, edited_styles
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


def _style_classes(style: Any | None) -> set[str]:
    return set(style.to_classes()) if style is not None else set()


def _paragraph_classes(styles: Any | None) -> set[str]:
    if styles is None:
        return set()
    return _style_classes(styles.text_style) | _style_classes(styles.paragraph_style)


def _removed_paragraph_classes(old: Any | None, new: Any | None) -> frozenset[str]:
    return frozenset(_paragraph_classes(old) - _paragraph_classes(new))


def _removed_element_classes(old: ElementStyles, new: ElementStyles) -> frozenset[str]:
    old_classes: set[str] = set()
    new_classes: set[str] = set()
    for styles, target in ((old, old_classes), (new, new_classes)):
        if styles.fill is not None:
            target.add(styles.fill.to_class())
        for style in (
            styles.stroke,
            styles.text_style,
            styles.paragraph_style,
        ):
            target.update(_style_classes(style))
        if styles.content_alignment is not None:
            target.add(styles.content_alignment.to_class())
    return frozenset(old_classes - new_classes)


def _removed_run_classes(
    old_runs: list[list[Any]], new_runs: list[list[Any]]
) -> frozenset[str]:
    """Find classes removed from any covered character range.

    Run boundaries are not stable: an author can split one run without
    changing its text. Compare the UTF-16 ranges covered by each run instead
    of assuming that matching list positions represent matching text.
    """
    removed: set[str] = set()
    for paragraph_index, old_paragraph in enumerate(old_runs):
        new_paragraph = (
            new_runs[paragraph_index] if paragraph_index < len(new_runs) else []
        )
        old_spans: list[tuple[int, int, set[str]]] = []
        offset = 0
        for old_run in old_paragraph:
            end = offset + _utf16_len(old_run.text)
            if end > offset:
                old_spans.append((offset, end, _style_classes(old_run.text_style)))
            offset = end

        new_spans: list[tuple[int, int, set[str]]] = []
        offset = 0
        for new_run in new_paragraph:
            end = offset + _utf16_len(new_run.text)
            if end > offset:
                new_spans.append((offset, end, _style_classes(new_run.text_style)))
            offset = end

        for old_start, old_end, old_classes in old_spans:
            for class_name in old_classes:
                covered_until = old_start
                for new_start, new_end, new_classes in new_spans:
                    if class_name not in new_classes or new_end <= old_start:
                        continue
                    if new_start > covered_until:
                        break
                    covered_until = max(covered_until, new_end)
                    if covered_until >= old_end:
                        break
                if covered_until < old_end:
                    removed.add(class_name)
    return frozenset(removed)


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
    )

    return result.changes
