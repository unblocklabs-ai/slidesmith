"""Generate Google Slides API requests from diff changes.

Converts Change operations to batchUpdate request objects.
Key feature: Handles copy operations by recreating elements with source styles.
"""

from __future__ import annotations

import re
import time
from typing import Any

from extraslide.classes import (
    Color,
    ContentAlignment,
    DashStyle,
    Fill,
    ParagraphStyle,
    PropertyState,
    Stroke,
    TextStyle,
)
from extraslide.content_diff import Change, ChangeType, DiffResult, ParagraphClassUpdate
from extraslide.content_parser import ElementStyles, ParagraphStyles, ParsedRun
from extraslide.id_manager import is_valid_google_object_id
from extraslide.shape_types import TAG_TO_TYPE, VALID_GOOGLE_TYPES
from extraslide.units import hex_to_rgb, pt_to_emu

# Global counter for unique ID generation within a session
_id_counter = 0

_MIN_EMU = 1


def _get_unique_suffix() -> str:
    """Generate a unique suffix for object IDs."""
    global _id_counter
    _id_counter += 1
    # Use timestamp (last 6 digits) + counter for uniqueness
    ts = int(time.time() * 1000) % 1000000
    return f"{ts}_{_id_counter}"


def _allocate_create_object_id(authored_id: str, reserved_ids: set[str]) -> str:
    """Choose a valid, unoccupied Google object ID for an authored element."""
    if is_valid_google_object_id(authored_id) and authored_id not in reserved_ids:
        return authored_id

    stem = re.sub(r"[^a-zA-Z0-9_-]", "_", authored_id)
    if not stem or not re.match(r"^[a-zA-Z_]", stem):
        stem = f"new_{stem}"
    if len(stem) < 5:
        stem = f"new_{stem}"

    suffix_number = 2
    while True:
        suffix = f"_{suffix_number}"
        candidate = f"{stem[: 50 - len(suffix)]}{suffix}"
        if is_valid_google_object_id(candidate) and candidate not in reserved_ids:
            return candidate
        suffix_number += 1


def generate_batch_requests(
    diff_result: DiffResult,
    id_mapping: dict[str, str],
    slide_id_mapping: dict[str, str],
    _pristine_element_types: dict[str, str] | None = None,
    _pristine_element_parents: dict[str, str | None] | None = None,
) -> list[dict[str, Any]]:
    """Generate batchUpdate requests from diff result.

    Args:
        diff_result: Result from diff_presentation()
        id_mapping: clean_id -> google_object_id mapping
        slide_id_mapping: slide_index (e.g., "01") -> google_slide_id mapping
        _pristine_element_types: Optional mapping of google_id -> element_type.
        _pristine_element_parents: Optional mapping of google_id -> parent group id.
            Together these identify deleted ancestor groups without relying on
            object-id spelling conventions.

    Returns:
        List of Google Slides API batchUpdate request objects
    """
    requests: list[dict[str, Any]] = []

    # Make a mutable copy of slide_id_mapping for adding new slides
    slide_ids = dict(slide_id_mapping)
    reserved_object_ids = set(id_mapping.values()) | set(slide_ids.values())

    # Process changes in order: deletes first, then modifications, then creates/copies
    # This ensures space is freed before new elements are created

    # Group changes by type
    deletes = [c for c in diff_result.changes if c.change_type == ChangeType.DELETE]
    moves = [c for c in diff_result.changes if c.change_type == ChangeType.MOVE]
    text_updates = [
        c for c in diff_result.changes if c.change_type == ChangeType.TEXT_UPDATE
    ]
    style_updates = [
        c for c in diff_result.changes if c.change_type == ChangeType.STYLE_UPDATE
    ]
    paragraph_style_updates = [
        c
        for c in diff_result.changes
        if c.change_type == ChangeType.PARAGRAPH_STYLE_UPDATE
    ]
    copies = [c for c in diff_result.changes if c.change_type == ChangeType.COPY]
    creates = [c for c in diff_result.changes if c.change_type == ChangeType.CREATE]

    # Detect new slides needed
    # Check all copies and creates for slides not in slide_ids
    new_slide_indices: set[str] = set()
    for change in copies + creates:
        if change.slide_index and change.slide_index not in slide_ids:
            new_slide_indices.add(change.slide_index)

    # Generate createSlide requests for new slides (in order)
    for slide_index in sorted(new_slide_indices):
        suffix = _get_unique_suffix()
        new_slide_id = f"new_slide_{slide_index}_{suffix}"
        requests.append(_create_slide_request(new_slide_id))
        slide_ids[slide_index] = new_slide_id
        reserved_object_ids.add(new_slide_id)

    # Generate delete requests
    # Order: deepest leaves first, then root shapes (groups auto-delete when empty)
    delete_ids = {
        id_mapping.get(c.target_id) for c in deletes if id_mapping.get(c.target_id)
    }
    ordered_delete_ids = _order_deletes_for_safe_removal(
        delete_ids,
        _pristine_element_types,
        _pristine_element_parents,
    )

    for google_id in ordered_delete_ids:
        requests.append(
            {
                "deleteObject": {
                    "objectId": google_id,
                }
            }
        )

    # Generate move requests (updatePageElementTransform)
    for change in moves:
        move_google_id = id_mapping.get(change.target_id)
        if move_google_id and change.new_position:
            requests.append(
                _create_move_request(
                    move_google_id,
                    change.new_position,
                    diff_result.pristine_styles.get(change.target_id),
                )
            )

    # Generate text update requests
    for change in text_updates:
        text_google_id = id_mapping.get(change.target_id)
        if text_google_id and change.new_text is not None:
            text_requests = _create_text_update_requests(
                text_google_id,
                change.new_text,
                change.new_runs,
                change.old_text,
                change.old_runs,
            )
            requests.extend(text_requests)

    # Generate style update requests (class-derived styling on existing elements)
    for change in style_updates:
        style_google_id = id_mapping.get(change.target_id)
        if style_google_id and change.new_styles:
            edited_element = diff_result.edited_elements.get(change.target_id)
            element_tag = change.tag
            if element_tag is None and edited_element is not None:
                element_tag = edited_element.tag
            if element_tag is None and _pristine_element_types is not None:
                element_type = _pristine_element_types.get(style_google_id)
                element_tag = "Line" if element_type == "LINE" else None
            style_requests = _create_class_style_requests(
                style_google_id,
                change.new_styles,
                has_text=bool(change.new_text),
                element_tag=element_tag,
            )
            requests.extend(style_requests)
            if (
                change.new_text
                and (
                    change.new_styles.text_style is not None
                    or change.new_styles.paragraph_style is not None
                )
            ):
                if change.new_paragraph_styles:
                    paragraph_updates = [
                        ParagraphClassUpdate(index, None, styles)
                        for index, styles in enumerate(change.new_paragraph_styles)
                        if styles is not None
                    ]
                    requests.extend(
                        _create_paragraph_class_update_requests(
                            style_google_id,
                            change.new_text,
                            change.new_runs or [],
                            paragraph_updates,
                            reapply_runs=False,
                        )
                    )
                if change.new_styles.text_style is not None and change.new_runs:
                    requests.extend(
                        _create_run_style_requests(style_google_id, change.new_runs)
                    )

    # Paragraph defaults follow element defaults and precede/reapply explicit
    # <T> overrides, so scope and inheritance match the SML nesting.
    for change in paragraph_style_updates:
        style_google_id = id_mapping.get(change.target_id)
        if (
            style_google_id
            and change.new_text is not None
            and change.paragraph_style_updates
        ):
            requests.extend(
                _create_paragraph_class_update_requests(
                    style_google_id,
                    change.new_text,
                    change.new_runs or [],
                    change.paragraph_style_updates,
                )
            )

    # Generate copy requests (recreate elements with source styles)
    for change in copies:
        if change.source_id and change.slide_index:
            slide_google_id = slide_ids.get(change.slide_index)
            source_google_id = id_mapping.get(change.source_id)
            source_style = diff_result.pristine_styles.get(change.source_id, {})

            if slide_google_id and source_google_id:
                copy_requests = _create_copy_requests(
                    change,
                    source_style,
                    slide_google_id,
                    diff_result.pristine_styles,
                    reserved_object_ids,
                )
                requests.extend(copy_requests)

    # Generate create requests (new elements)
    for change in creates:
        if change.slide_index:
            slide_google_id = slide_ids.get(change.slide_index)
            if slide_google_id:
                new_object_id = _allocate_create_object_id(
                    change.target_id, reserved_object_ids
                )
                reserved_object_ids.add(new_object_id)
                create_requests = _create_element_requests(
                    change, slide_google_id, new_object_id
                )
                requests.extend(create_requests)

    return requests


def _order_deletes_for_safe_removal(
    delete_ids: set[str | None],
    pristine_element_types: dict[str, str] | None = None,
    pristine_element_parents: dict[str, str | None] | None = None,
) -> list[str]:
    """Return deterministic deletes with descendants of deleted groups omitted.

    Deleting a group removes its subtree. Emitting child deletes afterward can
    make the atomic Google batch fail because those object IDs no longer exist.
    Hierarchy comes exclusively from the pristine API tree; IDs are opaque.

    Args:
        delete_ids: Set of Google object IDs to delete

    Returns:
        Ordered list: deepest leaves first, then root shapes
    """
    valid_ids = {object_id for object_id in delete_ids if object_id is not None}
    types = pristine_element_types or {}
    parents = pristine_element_parents or {}

    def has_deleted_group_ancestor(object_id: str) -> bool:
        seen: set[str] = set()
        parent_id = parents.get(object_id)
        while parent_id and parent_id not in seen:
            if parent_id in valid_ids and types.get(parent_id) == "GROUP":
                return True
            seen.add(parent_id)
            parent_id = parents.get(parent_id)
        return False

    return sorted(
        object_id
        for object_id in valid_ids
        if not has_deleted_group_ancestor(object_id)
    )


def _create_slide_request(slide_id: str) -> dict[str, Any]:
    """Create a createSlide request."""
    return {
        "createSlide": {
            "objectId": slide_id,
            # Insert at the end (no insertionIndex means end)
        }
    }


def _create_move_request(
    google_id: str,
    position: dict[str, float],
    pristine_style: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a transform update that preserves native scale, shear, and flips."""
    pristine_style = pristine_style or {}
    old_position = pristine_style.get("position", {})
    native_size = pristine_style.get("nativeSize", {})
    native_transform = pristine_style.get("nativeTransform", {})

    old_w = float(old_position.get("w", position["w"]))
    old_h = float(old_position.get("h", position["h"]))
    target_w = float(position["w"])
    target_h = float(position["h"])
    has_native_geometry = bool(native_size and native_transform)
    size_unchanged = abs(target_w - old_w) <= 0.005 and abs(target_h - old_h) <= 0.005

    if has_native_geometry and size_unchanged:
        return {
            "updatePageElementTransform": {
                "objectId": google_id,
                "transform": {
                    "scaleX": 1,
                    "scaleY": 1,
                    "translateX": pt_to_emu(position["x"] - old_position.get("x", 0)),
                    "translateY": pt_to_emu(position["y"] - old_position.get("y", 0)),
                    "unit": "EMU",
                },
                "applyMode": "RELATIVE",
            }
        }

    if has_native_geometry:
        width_emu = max(abs(float(native_size.get("w", 0))), _MIN_EMU)
        height_emu = max(abs(float(native_size.get("h", 0))), _MIN_EMU)
        sx = float(native_transform.get("scaleX", 1))
        sy = float(native_transform.get("scaleY", 1))
        shx = float(native_transform.get("shearX", 0))
        shy = float(native_transform.get("shearY", 0))
        old_visual_w = max(abs(sx) * width_emu + abs(shx) * height_emu, _MIN_EMU)
        old_visual_h = max(abs(shy) * width_emu + abs(sy) * height_emu, _MIN_EMU)
        # Recompute only authored dimensions. SML is rounded to two decimals,
        # so replaying an unchanged axis through that rounded value would
        # introduce a tiny scale drift on every one-axis resize.
        ratio_x = (
            1.0
            if abs(target_w - old_w) <= 0.005
            else max(abs(pt_to_emu(target_w)), _MIN_EMU) / old_visual_w
        )
        ratio_y = (
            1.0
            if abs(target_h - old_h) <= 0.005
            else max(abs(pt_to_emu(target_h)), _MIN_EMU) / old_visual_h
        )
        sx *= ratio_x
        shx *= ratio_x
        shy *= ratio_y
        sy *= ratio_y
        x_offsets = (
            0.0,
            sx * width_emu,
            shx * height_emu,
            sx * width_emu + shx * height_emu,
        )
        y_offsets = (
            0.0,
            shy * width_emu,
            sy * height_emu,
            shy * width_emu + sy * height_emu,
        )
        transform = {
            "scaleX": sx,
            "scaleY": sy,
            "shearX": shx,
            "shearY": shy,
            "translateX": pt_to_emu(position["x"]) - min(x_offsets),
            "translateY": pt_to_emu(position["y"]) - min(y_offsets),
            "unit": "EMU",
        }
    else:
        base_size_emu = 3000024
        transform = {
            "scaleX": _nonzero_scale(pt_to_emu(target_w) / base_size_emu),
            "scaleY": _nonzero_scale(pt_to_emu(target_h) / base_size_emu),
            "translateX": pt_to_emu(position["x"]),
            "translateY": pt_to_emu(position["y"]),
            "unit": "EMU",
        }

    return {
        "updatePageElementTransform": {
            "objectId": google_id,
            "transform": transform,
            "applyMode": "ABSOLUTE",
        }
    }


def _nonzero_scale(value: float) -> float:
    """Floor a scale away from the singular zero transform."""
    minimum = _MIN_EMU / 3000024
    if abs(value) >= minimum:
        return value
    return -minimum if value < 0 else minimum


def _utf16_len(text: str) -> int:
    """Length of text in UTF-16 code units (the Slides API index space)."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _common_prefix_chars(a: str, b: str) -> int:
    """Number of leading characters (code points) shared by a and b.

    Trimming whole code points never splits a UTF-16 surrogate pair.
    """
    limit = min(len(a), len(b))
    count = 0
    while count < limit and a[count] == b[count]:
        count += 1
    return count


def _common_suffix_chars(a: str, b: str, limit: int) -> int:
    """Number of trailing characters shared by a and b, capped at limit.

    The cap keeps the suffix from overlapping an already-matched prefix.
    """
    count = 0
    while count < limit and a[len(a) - 1 - count] == b[len(b) - 1 - count]:
        count += 1
    return count


def _normalize_runs(
    paragraphs: list[str],
    runs: list[list[ParsedRun]] | None,
) -> list[list[ParsedRun]]:
    """Return runs parallel to paragraphs; plain paragraphs get one unstyled run."""
    if runs and len(runs) == len(paragraphs):
        return runs
    return [[ParsedRun(text=text)] for text in paragraphs]


def _delete_text_request(object_id: str, start: int, end: int) -> dict[str, Any]:
    """Create a deleteText request over a FIXED_RANGE of UTF-16 indices."""
    return {
        "deleteText": {
            "objectId": object_id,
            "textRange": {
                "type": "FIXED_RANGE",
                "startIndex": start,
                "endIndex": end,
            },
        }
    }


def _insert_text_request(object_id: str, index: int, text: str) -> dict[str, Any]:
    """Create an insertText request at a UTF-16 insertion index."""
    return {
        "insertText": {
            "objectId": object_id,
            "insertionIndex": index,
            "text": text,
        }
    }


def _create_text_update_requests(
    google_id: str,
    new_text: list[str],
    new_runs: list[list[ParsedRun]] | None = None,
    old_text: list[str] | None = None,
    old_runs: list[list[ParsedRun]] | None = None,
) -> list[dict[str, Any]]:
    """Create requests to update element text with minimal range edits.

    Strategy (contract C3): trim unchanged paragraphs from both ends,
    then trim the common prefix/suffix of the changed span, and emit
    deleteText/insertText scoped to just the changed range. Untouched
    text is never rewritten, so human-applied character styling survives.

    Paragraphs whose <T> runs changed count as changed: if their text
    also changed they are replaced wholesale and their run styles
    reapplied; other paragraphs stay untouched either way.

    All indices count UTF-16 code units (the Slides API index space).
    Falls back to delete-all + reinsert when pristine text is unknown.
    """
    if old_text is None:
        return _create_full_text_replace_requests(google_id, new_text, new_runs)

    old_paras = old_text
    new_paras = new_text
    old_para_runs = _normalize_runs(old_paras, old_runs)
    new_para_runs = _normalize_runs(new_paras, new_runs)
    m, n = len(old_paras), len(new_paras)

    def _paragraph_unchanged(i: int, j: int) -> bool:
        return old_paras[i] == new_paras[j] and old_para_runs[i] == new_para_runs[j]

    limit = min(m, n)
    prefix = 0
    while prefix < limit and _paragraph_unchanged(prefix, prefix):
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and _paragraph_unchanged(
        m - 1 - suffix, n - 1 - suffix
    ):
        suffix += 1

    old_mid = "\n".join(old_paras[prefix : m - suffix])
    new_mid = "\n".join(new_paras[prefix : n - suffix])

    # UTF-16 offset of the changed span within the old combined text.
    # When every old paragraph matched the prefix, edits append at the end.
    if prefix == m and m > 0:
        start = _utf16_len("\n".join(old_paras))
    else:
        start = sum(_utf16_len(text) + 1 for text in old_paras[:prefix])
    end = start + _utf16_len(old_mid)

    styled = any(
        run.text_style is not None
        for para_runs in new_para_runs[prefix : n - suffix]
        for run in para_runs
    )

    requests: list[dict[str, Any]] = []

    if old_mid == new_mid:
        # Only run styling changed. Emit field-level deltas so removing a
        # <T> class resets that property instead of leaving stale formatting.
        return _create_run_style_delta_requests(
            google_id,
            old_paras,
            old_para_runs,
            new_para_runs,
            paragraph_range=(prefix, n - suffix),
        )
    elif not old_mid:
        # Pure paragraph insertion: add a separator toward the changed side.
        if prefix < m:
            requests.append(_insert_text_request(google_id, start, new_mid + "\n"))
        elif m > 0:
            requests.append(_insert_text_request(google_id, start, "\n" + new_mid))
        else:
            requests.append(_insert_text_request(google_id, 0, new_mid))
    elif not new_mid:
        # Pure paragraph deletion: remove a separator with the paragraphs.
        if prefix > 0:
            requests.append(_delete_text_request(google_id, start - 1, end))
        elif suffix > 0:
            requests.append(_delete_text_request(google_id, start, end + 1))
        else:
            requests.append(_delete_text_request(google_id, start, end))
    elif styled:
        # Explicit <T> runs: replace the changed paragraphs wholesale and
        # reapply their run styles below. Other paragraphs stay untouched.
        requests.append(_delete_text_request(google_id, start, end))
        requests.append(_insert_text_request(google_id, start, new_mid))
    else:
        # Within the changed paragraphs, trim the common prefix/suffix and
        # touch only the span that actually differs.
        a = _common_prefix_chars(old_mid, new_mid)
        b = _common_suffix_chars(
            old_mid, new_mid, min(len(old_mid), len(new_mid)) - a
        )
        del_start = start + _utf16_len(old_mid[:a])
        del_end = end - _utf16_len(old_mid[len(old_mid) - b :])
        inserted = new_mid[a : len(new_mid) - b]
        if del_end > del_start:
            requests.append(_delete_text_request(google_id, del_start, del_end))
        if inserted:
            requests.append(_insert_text_request(google_id, del_start, inserted))

    if new_runs:
        requests.extend(
            _create_run_style_requests(
                google_id, new_runs, paragraph_range=(prefix, n - suffix)
            )
        )

    return requests


def _create_full_text_replace_requests(
    google_id: str,
    new_text: list[str],
    new_runs: list[list[ParsedRun]] | None = None,
) -> list[dict[str, Any]]:
    """Replace an element's entire text (used when pristine text is unknown).

    Deletes all existing text, then inserts the new text. If styled runs
    are provided, per-run text styles are applied after insert.
    """
    requests: list[dict[str, Any]] = []

    requests.append(
        {
            "deleteText": {
                "objectId": google_id,
                "textRange": {
                    "type": "ALL",
                },
            }
        }
    )

    if new_text:
        combined_text = "\n".join(new_text)
        requests.append(
            {
                "insertText": {
                    "objectId": google_id,
                    "insertionIndex": 0,
                    "text": combined_text,
                }
            }
        )

        # Apply per-run text styles from <T> runs
        if new_runs:
            requests.extend(_create_run_style_requests(google_id, new_runs))

    return requests


def _create_copy_requests(
    change: Change,
    source_style: dict[str, Any],
    slide_google_id: str,
    all_styles: dict[str, dict[str, Any]],
    reserved_ids: set[str],
) -> list[dict[str, Any]]:
    """Create requests to copy an element.

    Since duplicateObject only works on same slide, we recreate
    the element with properties from the source.

    For groups, recursively creates all children and then groups them.
    Uses translation (dx, dy) from change to calculate child positions.
    """
    requests: list[dict[str, Any]] = []

    # Determine element type from source style
    if not source_style:
        raise ValueError(
            f"Cannot copy '{change.source_id}': pristine style data is missing"
        )
    elem_type = source_style.get("type")
    if not isinstance(elem_type, str):
        raise ValueError(
            f"Cannot copy '{change.source_id}': pristine element type is missing"
        )

    # Use the new position if provided, otherwise use source position
    position = change.new_position
    if not position:
        source_pos = source_style.get("position", {})
        position = {
            "x": source_pos.get("x", 0),
            "y": source_pos.get("y", 0),
            "w": source_pos.get("w", 100),
            "h": source_pos.get("h", 100),
        }

    # Get translation for child positioning
    translation = change.translation or {"dx": 0, "dy": 0}

    # Generate a unique object ID for the new element
    # Include slide index and unique suffix to avoid collisions with existing IDs
    suffix = _get_unique_suffix()
    new_object_id = _allocate_create_object_id(
        f"copy_{change.slide_index}_{suffix}", reserved_ids
    )
    reserved_ids.add(new_object_id)

    _create_one_copied_element(
        object_id=new_object_id,
        elem_type=elem_type,
        source_id=change.source_id or change.target_id,
        position=position,
        text=change.new_text or [],
        children=change.children or [],
        translation=translation,
        slide_google_id=slide_google_id,
        all_styles=all_styles,
        style=source_style,
        requests=requests,
        child_depth=0,
        reserved_ids=reserved_ids,
    )

    return requests


def _create_one_copied_element(
    *,
    object_id: str,
    elem_type: str,
    source_id: str,
    position: dict[str, float],
    text: list[str],
    children: list[dict[str, Any]],
    translation: dict[str, float],
    slide_google_id: str,
    all_styles: dict[str, dict[str, Any]],
    style: dict[str, Any],
    requests: list[dict[str, Any]],
    child_depth: int,
    reserved_ids: set[str],
) -> None:
    """Recreate one copied element using the shared root/descendant pipeline."""
    if elem_type == "GROUP":
        if not children:
            raise ValueError(f"Cannot copy group '{source_id}': child data is missing")
        child_ids = _create_children_from_data(
            children,
            translation,
            slide_google_id,
            all_styles,
            requests,
            object_id,
            child_depth,
            reserved_ids=reserved_ids,
        )
        if not child_ids:
            raise ValueError(
                f"Cannot copy group '{source_id}': no children were created"
            )
        requests.append(
            {
                "groupObjects": {
                    "groupObjectId": object_id,
                    "childrenObjectIds": child_ids,
                }
            }
        )
        return

    if elem_type == "LINE":
        requests.append(_create_line_request(object_id, slide_google_id, position))
        requests.extend(_apply_line_style_requests(object_id, style))
    elif elem_type == "IMAGE":
        content_url = style.get("contentUrl", "")
        if not content_url:
            raise ValueError(f"Cannot copy image '{source_id}': contentUrl is missing")
        requests.append(
            _create_image_request(
                object_id,
                slide_google_id,
                position,
                content_url,
                native_size=style.get("nativeSize"),
                native_scale=style.get("nativeScale"),
            )
        )
    else:
        requests.append(
            _create_shape_request(object_id, slide_google_id, elem_type, position)
        )
        requests.extend(_apply_style_requests(object_id, style))
        if text:
            requests.extend(_create_text_insert_requests(object_id, text))
            text_style_info = style.get("text", {})
            if text_style_info:
                requests.extend(
                    _apply_text_style_requests(object_id, text, text_style_info)
                )

    if children:
        _create_children_from_data(
            children,
            translation,
            slide_google_id,
            all_styles,
            requests,
            object_id,
            child_depth,
            reserved_ids=reserved_ids,
        )


def _create_children_from_data(
    children: list[dict[str, Any]],
    translation: dict[str, float],
    slide_google_id: str,
    all_styles: dict[str, dict[str, Any]],
    requests: list[dict[str, Any]],
    id_prefix: str,
    depth: int = 0,
    *,
    reserved_ids: set[str],
) -> list[str]:
    """Create child elements from serialized children data.

    Uses translation-based positioning:
    - Children have their original absolute positions in child_data["position"]
    - New position = original position + translation (dx, dy)

    Args:
        children: List of child data dicts from Change.children
        translation: Translation offset {"dx": float, "dy": float}
        slide_google_id: Target slide ID
        all_styles: All element styles for styling lookup
        requests: List to append requests to
        id_prefix: Prefix for generated object IDs
        depth: Recursion depth

    Returns:
        List of created child object IDs
    """
    child_ids: list[str] = []
    dx = translation.get("dx", 0)
    dy = translation.get("dy", 0)

    for i, child_data in enumerate(children):
        child_obj_id = _allocate_create_object_id(
            f"{id_prefix}_c{depth}_{i}", reserved_ids
        )
        reserved_ids.add(child_obj_id)
        child_tag = child_data.get("tag", "Rect")
        child_text = child_data.get("text", [])
        nested_children = child_data.get("children", [])

        # Get style for this child from all_styles
        source_id = child_data.get("id", "")
        child_style = all_styles.get(source_id, {})
        if not child_style:
            raise ValueError(
                f"Cannot copy child '{source_id}': pristine style data is missing"
            )

        # Calculate new position using translation
        # Children have absolute positions in child_data["position"]
        # New position = original position + translation
        child_orig_pos = child_data.get("position", {})
        source_position = child_data.get("sourcePosition", {})
        child_dx = dx
        child_dy = dy
        if child_orig_pos and source_position:
            expected_final_x = source_position.get("x", 0) + dx
            expected_final_y = source_position.get("y", 0) + dy
            if (
                abs(child_orig_pos.get("x", 0) - expected_final_x) <= 0.01
                and abs(child_orig_pos.get("y", 0) - expected_final_y) <= 0.01
            ):
                child_dx = 0
                child_dy = 0
        if child_orig_pos:
            abs_position = {
                "x": child_orig_pos.get("x", 0) + child_dx,
                "y": child_orig_pos.get("y", 0) + child_dy,
                "w": child_orig_pos.get("w", 50),
                "h": child_orig_pos.get("h", 50),
            }
        else:
            # Fallback: use style's position if child_data doesn't have position
            style_pos = child_style.get("position", {})
            abs_position = {
                "x": style_pos.get("x", 0) + dx,
                "y": style_pos.get("y", 0) + dy,
                "w": style_pos.get("w", 50),
                "h": style_pos.get("h", 50),
            }

        # Map tag to element type
        elem_type = _tag_to_type(child_tag)

        _create_one_copied_element(
            object_id=child_obj_id,
            elem_type=elem_type,
            source_id=source_id,
            position=abs_position,
            text=child_text,
            children=nested_children,
            translation=translation,
            slide_google_id=slide_google_id,
            all_styles=all_styles,
            style=child_style,
            requests=requests,
            child_depth=depth + 1,
            reserved_ids=reserved_ids,
        )
        child_ids.append(child_obj_id)

    return child_ids


def _tag_to_type(tag: str) -> str:
    """Convert an SML tag to its canonical Google Slides element type."""
    try:
        return TAG_TO_TYPE[tag]
    except KeyError as exc:
        raise ValueError(f"Unsupported SML element tag '{tag}'") from exc

def _create_shape_request(
    object_id: str,
    slide_id: str,
    shape_type: str,
    position: dict[str, float],
) -> dict[str, Any]:
    """Create a createShape request.

    Google Slides internally uses a base size of 3000024 EMU (236.2 pt) for shapes
    and applies scale factors to achieve the desired visual size. We calculate
    the scale factors to match the requested size.
    """
    # If shape_type is already a valid Google shape type (uppercase with underscores),
    # use it directly. Otherwise, fall back to RECTANGLE.
    # Valid Google shape types are uppercase like RECTANGLE, ROUND_RECTANGLE, etc.
    if shape_type not in VALID_GOOGLE_TYPES:
        raise ValueError(f"Unsupported Google shape type '{shape_type}'")
    google_shape_type = shape_type

    # Google Slides uses a base size of 3000024 EMU (236.2 pt) and applies
    # scale factors to get the visual size
    base_size_emu = 3000024
    target_w_emu = pt_to_emu(position["w"])
    target_h_emu = pt_to_emu(position["h"])

    # Calculate scale factors
    scale_x = _nonzero_scale(target_w_emu / base_size_emu)
    scale_y = _nonzero_scale(target_h_emu / base_size_emu)

    return {
        "createShape": {
            "objectId": object_id,
            "shapeType": google_shape_type,
            "elementProperties": {
                "pageObjectId": slide_id,
                "size": {
                    "width": {"magnitude": base_size_emu, "unit": "EMU"},
                    "height": {"magnitude": base_size_emu, "unit": "EMU"},
                },
                "transform": {
                    "scaleX": scale_x,
                    "scaleY": scale_y,
                    "translateX": pt_to_emu(position["x"]),
                    "translateY": pt_to_emu(position["y"]),
                    "unit": "EMU",
                },
            },
        }
    }


def _create_line_request(
    object_id: str,
    slide_id: str,
    position: dict[str, float],
) -> dict[str, Any]:
    """Create a createLine request."""
    return {
        "createLine": {
            "objectId": object_id,
            "lineCategory": "STRAIGHT",
            "elementProperties": {
                "pageObjectId": slide_id,
                "size": {
                    "width": {
                        "magnitude": max(abs(pt_to_emu(position["w"])), _MIN_EMU),
                        "unit": "EMU",
                    },
                    "height": {
                        "magnitude": max(abs(pt_to_emu(position["h"])), _MIN_EMU),
                        "unit": "EMU",
                    },
                },
                "transform": {
                    "scaleX": 1,
                    "scaleY": 1,
                    "translateX": pt_to_emu(position["x"]),
                    "translateY": pt_to_emu(position["y"]),
                    "unit": "EMU",
                },
            },
        }
    }


def _create_image_request(
    object_id: str,
    slide_id: str,
    position: dict[str, float],
    url: str,
    native_size: dict[str, float] | None = None,
    native_scale: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Create a createImage request.

    For accurate image copying, we need to use the native image dimensions and
    calculate appropriate scale factors. Google Slides uses native image size
    as the base and applies scale factors from there.

    Args:
        object_id: The new object ID
        slide_id: Target slide ID
        position: Target position {x, y, w, h} in points
        url: Image URL
        native_size: Native image dimensions {w, h} in EMU (from source style)
        native_scale: Original scale factors {x, y} (from source style)
    """
    target_w_emu = pt_to_emu(position["w"])
    target_h_emu = pt_to_emu(position["h"])

    if native_size and native_scale:
        # Use native dimensions as base and calculate scale factors
        # to achieve target visual size
        native_w = native_size.get("w", 0)
        native_h = native_size.get("h", 0)

        if native_w > 0 and native_h > 0:
            scale_x = target_w_emu / native_w
            scale_y = target_h_emu / native_h

            return {
                "createImage": {
                    "objectId": object_id,
                    "url": url,
                    "elementProperties": {
                        "pageObjectId": slide_id,
                        "size": {
                            "width": {"magnitude": native_w, "unit": "EMU"},
                            "height": {"magnitude": native_h, "unit": "EMU"},
                        },
                        "transform": {
                            "scaleX": scale_x,
                            "scaleY": scale_y,
                            "translateX": pt_to_emu(position["x"]),
                            "translateY": pt_to_emu(position["y"]),
                            "unit": "EMU",
                        },
                    },
                }
            }

    # Fallback: use standard base size approach (less accurate)
    base_size_emu = 3000024
    scale_x = target_w_emu / base_size_emu
    scale_y = target_h_emu / base_size_emu

    return {
        "createImage": {
            "objectId": object_id,
            "url": url,
            "elementProperties": {
                "pageObjectId": slide_id,
                "size": {
                    "width": {"magnitude": base_size_emu, "unit": "EMU"},
                    "height": {"magnitude": base_size_emu, "unit": "EMU"},
                },
                "transform": {
                    "scaleX": scale_x,
                    "scaleY": scale_y,
                    "translateX": pt_to_emu(position["x"]),
                    "translateY": pt_to_emu(position["y"]),
                    "unit": "EMU",
                },
            },
        }
    }


def _color_from_styles_json(value: str, *, alpha: float = 1.0) -> Color:
    """Convert a persisted styles.json color without changing its disk format."""
    _parse_color(value)  # Validate through the shared unit conversion path.
    if value.startswith("@"):
        return Color(theme=value[1:].lower().replace("_", "-"), alpha=alpha)
    return Color(hex=value.lower(), alpha=alpha)


def _fill_from_styles_json(data: dict[str, Any] | None) -> Fill | None:
    if not data:
        return None
    fill_type = data.get("type")
    if fill_type == "none":
        return Fill(state=PropertyState.NOT_RENDERED)
    if fill_type == "solid":
        return Fill(
            color=_color_from_styles_json(
                str(data.get("color", "#000000")),
                alpha=float(data.get("alpha", 1.0)),
            )
        )
    raise ValueError(f"Unsupported styles.json fill type {fill_type!r}")


def _stroke_from_styles_json(data: dict[str, Any] | None) -> Stroke | None:
    if not data:
        return None
    if data.get("type") == "none":
        return Stroke(state=PropertyState.NOT_RENDERED)
    color_value = data.get("color")
    dash_value = data.get("dashStyle")
    return Stroke(
        color=(
            _color_from_styles_json(
                str(color_value), alpha=float(data.get("alpha", 1.0))
            )
            if color_value
            else None
        ),
        weight_pt=float(data["weight"]) if "weight" in data else None,
        dash_style=DashStyle(str(dash_value)) if dash_value else None,
    )


def _apply_style_requests(
    object_id: str,
    style: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply persisted shape styles through the typed classes.py pipeline."""
    content_alignment = style.get("contentAlignment")
    typed_styles = ElementStyles(
        fill=_fill_from_styles_json(style.get("fill")),
        stroke=_stroke_from_styles_json(style.get("stroke")),
        content_alignment=(
            ContentAlignment(str(content_alignment)) if content_alignment else None
        ),
    )
    return _create_class_shape_style_requests(object_id, typed_styles)


def _apply_line_style_requests(
    object_id: str,
    style: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply persisted line styles through the typed classes.py pipeline."""
    request = _create_class_line_style_request(
        object_id, _stroke_from_styles_json(style.get("stroke"))
    )
    return [request] if request is not None else []


def _apply_text_style_requests(
    object_id: str,
    text_lines: list[str],
    text_style_info: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply each copied paragraph/run style to its original text range."""
    requests: list[dict[str, Any]] = []

    paragraphs = text_style_info.get("paragraphs", [])
    if not paragraphs:
        return requests

    paragraph_start = 0
    for paragraph_index, paragraph in enumerate(paragraphs):
        authored_text = (
            text_lines[paragraph_index]
            if paragraph_index < len(text_lines)
            else ""
        )
        source_runs = paragraph.get("runs", [])
        run_offset = 0
        for run_index, run in enumerate(source_runs):
            run_text = str(run.get("content", ""))
            if run_index == len(source_runs) - 1:
                run_text = run_text.removesuffix("\n")
            run_length = _utf16_len(run_text)
            text_style, fields = _copied_text_style_to_api(run.get("style", {}))
            if fields and run_length:
                start = paragraph_start + run_offset
                requests.append(
                    {
                        "updateTextStyle": {
                            "objectId": object_id,
                            "textRange": {
                                "type": "FIXED_RANGE",
                                "startIndex": start,
                                "endIndex": start + run_length,
                            },
                            "style": text_style,
                            "fields": ",".join(fields),
                        }
                    }
                )
            run_offset += run_length

        alignment = paragraph.get("style", {}).get("alignment")
        if alignment and authored_text:
            requests.append(
                {
                    "updateParagraphStyle": {
                        "objectId": object_id,
                        "textRange": {
                            "type": "FIXED_RANGE",
                            "startIndex": paragraph_start,
                            "endIndex": paragraph_start + _utf16_len(authored_text),
                        },
                        "style": {"alignment": alignment},
                        "fields": "alignment",
                    }
                }
            )
        paragraph_start += _utf16_len(authored_text)
        if paragraph_index < len(text_lines) - 1:
            paragraph_start += 1

    return requests


def _copied_text_style_to_api(
    run_style: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Convert one styles.json run through the typed TextStyle pipeline."""
    color = run_style.get("color")
    background = run_style.get("backgroundColor")
    typed = TextStyle(
        bold=run_style.get("bold"),
        italic=run_style.get("italic"),
        underline=run_style.get("underline"),
        strikethrough=run_style.get("strikethrough"),
        small_caps=run_style.get("smallCaps"),
        font_family=run_style.get("fontFamily"),
        font_size_pt=run_style.get("fontSize"),
        font_weight=run_style.get("fontWeight"),
        foreground_color=(
            _color_from_styles_json(str(color)) if color else None
        ),
        background_color=(
            _color_from_styles_json(str(background)) if background else None
        ),
        link=run_style.get("link"),
        baseline_offset=run_style.get("baselineOffset"),
    )
    return _class_text_style_to_api(typed)


def _create_class_style_requests(
    object_id: str,
    styles: ElementStyles,
    *,
    has_text: bool,
    element_tag: str | None = None,
) -> list[dict[str, Any]]:
    """Generate requests applying class-derived styles to an existing element.

    Emits field-masked updates covering only the properties derived from
    the element's classes. Text/paragraph styles are skipped when the
    element has no text (updateTextStyle on empty text is an API error).
    """
    requests: list[dict[str, Any]] = []

    if element_tag == "Line":
        line_request = _create_class_line_style_request(object_id, styles.stroke)
        if line_request:
            requests.append(line_request)
        return requests

    requests.extend(_create_class_shape_style_requests(object_id, styles))

    if has_text:
        text_request = _create_class_text_style_request(object_id, styles.text_style)
        if text_request:
            requests.append(text_request)
        para_request = _create_class_paragraph_style_request(
            object_id, styles.paragraph_style
        )
        if para_request:
            requests.append(para_request)

    return requests


def _create_class_shape_style_requests(
    object_id: str,
    styles: ElementStyles,
) -> list[dict[str, Any]]:
    """Generate updateShapeProperties requests from class-derived styles.

    Field masks name only the properties the classes set.
    """
    requests: list[dict[str, Any]] = []

    if styles.content_alignment is not None:
        requests.append(
            {
                "updateShapeProperties": {
                    "objectId": object_id,
                    "shapeProperties": {
                        "contentAlignment": styles.content_alignment.value,
                    },
                    "fields": "contentAlignment",
                }
            }
        )

    fill = styles.fill
    if fill:
        if fill.state == PropertyState.NOT_RENDERED:
            requests.append(
                {
                    "updateShapeProperties": {
                        "objectId": object_id,
                        "shapeProperties": {
                            "shapeBackgroundFill": {
                                "propertyState": "NOT_RENDERED",
                            },
                        },
                        "fields": "shapeBackgroundFill.propertyState",
                    }
                }
            )
        elif fill.color:
            requests.append(
                {
                    "updateShapeProperties": {
                        "objectId": object_id,
                        "shapeProperties": {
                            "shapeBackgroundFill": {
                                "solidFill": {
                                    "color": fill.color.to_api(),
                                    "alpha": fill.color.alpha,
                                },
                            },
                        },
                        "fields": "shapeBackgroundFill.solidFill",
                    }
                }
            )

    stroke = styles.stroke
    if stroke:
        outline_request = _create_class_outline_request(object_id, stroke)
        if outline_request:
            requests.append(outline_request)

    return requests


def _create_class_outline_request(
    object_id: str,
    stroke: Stroke,
) -> dict[str, Any] | None:
    """Create updateShapeProperties request for class-derived stroke classes."""
    if stroke.state == PropertyState.NOT_RENDERED:
        return {
            "updateShapeProperties": {
                "objectId": object_id,
                "shapeProperties": {
                    "outline": {
                        "propertyState": "NOT_RENDERED",
                    },
                },
                "fields": "outline.propertyState",
            }
        }
    if stroke.state == PropertyState.INHERIT:
        return None

    outline: dict[str, Any] = {}
    fields: list[str] = []

    if stroke.color:
        outline["outlineFill"] = {
            "solidFill": {
                "color": stroke.color.to_api(),
                "alpha": stroke.color.alpha,
            },
        }
        fields.append("outline.outlineFill.solidFill")

    if stroke.weight_pt is not None:
        outline["weight"] = {"magnitude": pt_to_emu(stroke.weight_pt), "unit": "EMU"}
        fields.append("outline.weight")

    if stroke.dash_style is not None:
        outline["dashStyle"] = stroke.dash_style.value
        fields.append("outline.dashStyle")

    if not fields:
        return None

    return {
        "updateShapeProperties": {
            "objectId": object_id,
            "shapeProperties": {
                "outline": outline,
            },
            "fields": ",".join(fields),
        }
    }


def _create_class_line_style_request(
    object_id: str,
    stroke: Stroke | None,
) -> dict[str, Any] | None:
    """Create one field-masked line update from authored stroke classes."""
    if stroke is None or stroke.state == PropertyState.INHERIT:
        return None

    line_properties: dict[str, Any] = {}
    fields: list[str] = []

    if stroke.state == PropertyState.NOT_RENDERED:
        line_properties["lineFill"] = {
            "solidFill": {"color": {"rgbColor": {}}, "alpha": 0.0}
        }
        fields.append("lineFill.solidFill")
    elif stroke.color:
        line_properties["lineFill"] = {
            "solidFill": {
                "color": stroke.color.to_api(),
                "alpha": stroke.color.alpha,
            }
        }
        fields.append("lineFill.solidFill")

    if stroke.weight_pt is not None:
        line_properties["weight"] = {
            "magnitude": pt_to_emu(stroke.weight_pt),
            "unit": "EMU",
        }
        fields.append("weight")

    if stroke.dash_style is not None:
        line_properties["dashStyle"] = stroke.dash_style.value
        fields.append("dashStyle")

    if not fields:
        return None

    return {
        "updateLineProperties": {
            "objectId": object_id,
            "lineProperties": line_properties,
            "fields": ",".join(fields),
        }
    }


def _class_text_style_to_api(
    text_style: TextStyle,
) -> tuple[dict[str, Any], list[str]]:
    """Convert a class-derived TextStyle to API style dict + field mask parts."""
    style: dict[str, Any] = {}
    fields: list[str] = []

    if text_style.bold is not None:
        style["bold"] = text_style.bold
        fields.append("bold")
    if text_style.italic is not None:
        style["italic"] = text_style.italic
        fields.append("italic")
    if text_style.underline is not None:
        style["underline"] = text_style.underline
        fields.append("underline")
    if text_style.strikethrough is not None:
        style["strikethrough"] = text_style.strikethrough
        fields.append("strikethrough")
    if text_style.small_caps is not None:
        style["smallCaps"] = text_style.small_caps
        fields.append("smallCaps")

    if text_style.baseline_offset:
        style["baselineOffset"] = text_style.baseline_offset
        fields.append("baselineOffset")

    if text_style.font_size_pt is not None:
        style["fontSize"] = {"magnitude": text_style.font_size_pt, "unit": "PT"}
        fields.append("fontSize")

    if text_style.font_weight is not None:
        weighted: dict[str, Any] = {"weight": text_style.font_weight}
        if text_style.font_family:
            weighted["fontFamily"] = text_style.font_family
        style["weightedFontFamily"] = weighted
        fields.append("weightedFontFamily")
    elif text_style.font_family:
        style["fontFamily"] = text_style.font_family
        fields.append("fontFamily")

    if text_style.foreground_color:
        style["foregroundColor"] = {
            "opaqueColor": text_style.foreground_color.to_api()
        }
        fields.append("foregroundColor")

    if text_style.background_color:
        style["backgroundColor"] = {
            "opaqueColor": text_style.background_color.to_api()
        }
        fields.append("backgroundColor")

    if text_style.link:
        style["link"] = {"url": text_style.link}
        fields.append("link")

    return style, fields


def _create_class_text_style_request(
    object_id: str,
    text_style: TextStyle | None,
    text_range: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Create updateTextStyle request from class-derived text styles.

    Defaults to the ALL range; pass text_range for a specific run.
    """
    if text_style is None:
        return None

    style, fields = _class_text_style_to_api(text_style)
    if not fields:
        return None

    return {
        "updateTextStyle": {
            "objectId": object_id,
            "textRange": text_range or {"type": "ALL"},
            "style": style,
            "fields": ",".join(fields),
        }
    }


def _create_class_paragraph_style_request(
    object_id: str,
    paragraph_style: ParagraphStyle | None,
    text_range: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Create updateParagraphStyle request from class-derived paragraph styles."""
    if paragraph_style is None:
        return None

    style, fields = _class_paragraph_style_to_api(paragraph_style)
    if not fields:
        return None

    return {
        "updateParagraphStyle": {
            "objectId": object_id,
            "textRange": text_range or {"type": "ALL"},
            "style": style,
            "fields": ",".join(fields),
        }
    }


def _class_paragraph_style_to_api(
    paragraph_style: ParagraphStyle,
) -> tuple[dict[str, Any], list[str]]:
    """Convert a class-derived ParagraphStyle to API style + field names."""

    style: dict[str, Any] = {}
    fields: list[str] = []

    if paragraph_style.alignment:
        style["alignment"] = paragraph_style.alignment.value
        fields.append("alignment")

    if paragraph_style.line_spacing:
        style["lineSpacing"] = paragraph_style.line_spacing
        fields.append("lineSpacing")

    if paragraph_style.space_above_pt is not None:
        style["spaceAbove"] = {
            "magnitude": paragraph_style.space_above_pt,
            "unit": "PT",
        }
        fields.append("spaceAbove")

    if paragraph_style.space_below_pt is not None:
        style["spaceBelow"] = {
            "magnitude": paragraph_style.space_below_pt,
            "unit": "PT",
        }
        fields.append("spaceBelow")

    if paragraph_style.indent_start_pt is not None:
        style["indentStart"] = {
            "magnitude": paragraph_style.indent_start_pt,
            "unit": "PT",
        }
        fields.append("indentStart")

    if paragraph_style.indent_end_pt is not None:
        style["indentEnd"] = {
            "magnitude": paragraph_style.indent_end_pt,
            "unit": "PT",
        }
        fields.append("indentEnd")

    if paragraph_style.indent_first_line_pt is not None:
        style["indentFirstLine"] = {
            "magnitude": paragraph_style.indent_first_line_pt,
            "unit": "PT",
        }
        fields.append("indentFirstLine")

    if paragraph_style.direction:
        style["direction"] = paragraph_style.direction
        fields.append("direction")

    if paragraph_style.spacing_mode:
        style["spacingMode"] = paragraph_style.spacing_mode
        fields.append("spacingMode")

    return style, fields


def _create_run_style_requests(
    object_id: str,
    runs: list[list[ParsedRun]],
    paragraph_range: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Create updateTextStyle requests for styled <T> runs.

    Assumes the element's text is the paragraphs joined with newlines
    (matching _create_text_insert_requests), and computes FIXED_RANGE
    indices over that combined text in UTF-16 code units.

    When paragraph_range is given, only runs in paragraphs within
    [start, end) produce requests; other paragraphs stay untouched.
    """
    requests: list[dict[str, Any]] = []
    index = 0

    for para_num, para_runs in enumerate(runs):
        if para_num > 0:
            index += 1  # Newline separator between paragraphs

        in_range = paragraph_range is None or (
            paragraph_range[0] <= para_num < paragraph_range[1]
        )
        for run in para_runs:
            end_index = index + _utf16_len(run.text)
            if run.text_style is not None and in_range:
                request = _create_class_text_style_request(
                    object_id,
                    run.text_style,
                    text_range={
                        "type": "FIXED_RANGE",
                        "startIndex": index,
                        "endIndex": end_index,
                    },
                )
                if request:
                    requests.append(request)
            index = end_index

    return requests


def _create_run_style_delta_requests(
    object_id: str,
    paragraphs: list[str],
    old_runs: list[list[ParsedRun]],
    new_runs: list[list[ParsedRun]],
    paragraph_range: tuple[int, int],
) -> list[dict[str, Any]]:
    """Emit precise TextStyle changes, including resets to inherited values."""
    requests: list[dict[str, Any]] = []

    def spans(runs: list[ParsedRun]) -> list[tuple[int, int, TextStyle | None]]:
        result: list[tuple[int, int, TextStyle | None]] = []
        offset = 0
        for run in runs:
            end = offset + len(run.text)
            if end > offset:
                result.append((offset, end, run.text_style))
            offset = end
        return result

    def style_at(
        style_spans: list[tuple[int, int, TextStyle | None]], offset: int
    ) -> TextStyle | None:
        for start, end, style in style_spans:
            if start <= offset < end:
                return style
        return None

    for paragraph_index in range(*paragraph_range):
        text = paragraphs[paragraph_index]
        old_spans = spans(old_runs[paragraph_index])
        new_spans = spans(new_runs[paragraph_index])
        boundaries = sorted(
            {0, len(text)}
            | {value for start, end, _ in old_spans + new_spans for value in (start, end)}
        )
        paragraph_start = sum(
            _utf16_len(value) + 1 for value in paragraphs[:paragraph_index]
        )
        for start, end in zip(boundaries, boundaries[1:], strict=False):
            if end <= start:
                continue
            old_style = style_at(old_spans, start) or TextStyle()
            new_style = style_at(new_spans, start) or TextStyle()
            fields = _changed_style_fields(
                old_style, new_style, _TEXT_STYLE_FIELD_NAMES
            )
            if not fields:
                continue
            api_style, _ = _class_text_style_to_api(new_style)
            requests.append(
                {
                    "updateTextStyle": {
                        "objectId": object_id,
                        "textRange": {
                            "type": "FIXED_RANGE",
                            "startIndex": paragraph_start
                            + _utf16_len(text[:start]),
                            "endIndex": paragraph_start + _utf16_len(text[:end]),
                        },
                        "style": {
                            key: value
                            for key, value in api_style.items()
                            if key in fields
                        },
                        "fields": ",".join(fields),
                    }
                }
            )
    return requests


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
    return [
        api_name
        for attr_name, api_name in field_names.items()
        if getattr(old_style, attr_name, None) != getattr(new_style, attr_name, None)
    ]


def _paragraph_text_range(
    paragraphs: list[str], paragraph_index: int
) -> dict[str, Any]:
    """Return the fixed UTF-16 range containing one paragraph's text."""
    start = sum(_utf16_len(text) + 1 for text in paragraphs[:paragraph_index])
    return {
        "type": "FIXED_RANGE",
        "startIndex": start,
        "endIndex": start + _utf16_len(paragraphs[paragraph_index]),
    }


def _create_paragraph_class_update_requests(
    object_id: str,
    paragraphs: list[str],
    runs: list[list[ParsedRun]],
    updates: list[ParagraphClassUpdate],
    *,
    reapply_runs: bool = True,
) -> list[dict[str, Any]]:
    """Create precise-range updates for changed ``<P class>`` defaults."""
    requests: list[dict[str, Any]] = []
    for update in updates:
        if update.paragraph_index >= len(paragraphs):
            continue
        old = update.old_styles or ParagraphStyles()
        new = update.new_styles or ParagraphStyles()
        text_range = _paragraph_text_range(paragraphs, update.paragraph_index)

        paragraph_fields = _changed_style_fields(
            old.paragraph_style,
            new.paragraph_style,
            _PARAGRAPH_STYLE_FIELD_NAMES,
        )
        if paragraph_fields:
            style, _ = _class_paragraph_style_to_api(
                new.paragraph_style or ParagraphStyle()
            )
            requests.append(
                {
                    "updateParagraphStyle": {
                        "objectId": object_id,
                        "textRange": text_range,
                        "style": {
                            key: value
                            for key, value in style.items()
                            if key in paragraph_fields
                        },
                        "fields": ",".join(paragraph_fields),
                    }
                }
            )

        text_fields = _changed_style_fields(
            old.text_style,
            new.text_style,
            _TEXT_STYLE_FIELD_NAMES,
        )
        if text_fields:
            style, _ = _class_text_style_to_api(new.text_style or TextStyle())
            requests.append(
                {
                    "updateTextStyle": {
                        "objectId": object_id,
                        "textRange": text_range,
                        "style": {
                            key: value
                            for key, value in style.items()
                            if key in text_fields
                        },
                        "fields": ",".join(text_fields),
                    }
                }
            )

        if reapply_runs and text_fields and update.paragraph_index < len(runs):
            requests.extend(
                _create_run_style_requests(
                    object_id,
                    runs,
                    paragraph_range=(
                        update.paragraph_index,
                        update.paragraph_index + 1,
                    ),
                )
            )

    return requests


def _create_text_insert_requests(
    object_id: str,
    text_lines: list[str],
) -> list[dict[str, Any]]:
    """Create requests to insert text into an element."""
    if not text_lines:
        return []

    combined_text = "\n".join(text_lines)
    return [
        {
            "insertText": {
                "objectId": object_id,
                "insertionIndex": 0,
                "text": combined_text,
            }
        }
    ]


def _create_element_requests(
    change: Change,
    slide_google_id: str,
    new_object_id: str,
) -> list[dict[str, Any]]:
    """Create requests for a new element."""
    requests: list[dict[str, Any]] = []

    tag = change.tag or "Rect"
    position = change.new_position or {"x": 0, "y": 0, "w": 100, "h": 100}

    shape_type = _tag_to_type(tag)

    if shape_type == "LINE":
        requests.append(_create_line_request(new_object_id, slide_google_id, position))
    elif shape_type in {"IMAGE", "GROUP", "TABLE", "VIDEO", "SHEETS_CHART"}:
        raise ValueError(
            f"Creating <{tag}> requires source-specific data and is not supported"
        )
    else:
        requests.append(
            _create_shape_request(
                new_object_id,
                slide_google_id,
                shape_type,
                position,
            )
        )

    # Apply class-derived element styling (shape fill/outline or line stroke)
    if change.new_styles:
        if shape_type == "LINE":
            line_request = _create_class_line_style_request(
                new_object_id, change.new_styles.stroke
            )
            if line_request:
                requests.append(line_request)
        else:
            requests.extend(
                _create_class_shape_style_requests(new_object_id, change.new_styles)
            )

    # Add text if provided
    if change.new_text:
        requests.extend(_create_text_insert_requests(new_object_id, change.new_text))

        # Apply class-derived text/paragraph styling to the inserted text
        if change.new_styles:
            text_request = _create_class_text_style_request(
                new_object_id, change.new_styles.text_style
            )
            if text_request:
                requests.append(text_request)
            para_request = _create_class_paragraph_style_request(
                new_object_id, change.new_styles.paragraph_style
            )
            if para_request:
                requests.append(para_request)

        if change.new_paragraph_styles:
            paragraph_updates = [
                ParagraphClassUpdate(index, None, styles)
                for index, styles in enumerate(change.new_paragraph_styles)
                if styles is not None
            ]
            requests.extend(
                _create_paragraph_class_update_requests(
                    new_object_id,
                    change.new_text,
                    change.new_runs or [],
                    paragraph_updates,
                    reapply_runs=False,
                )
            )

        # Apply per-run text styles from <T> runs (override element-level styles)
        if change.new_runs:
            requests.extend(_create_run_style_requests(new_object_id, change.new_runs))

    return requests


def _parse_color(color: str) -> dict[str, Any]:
    """Parse a styles.json color through Color and units.hex_to_rgb."""
    if color.startswith("@"):
        return Color(theme=color[1:].lower().replace("_", "-")).to_api()

    # Call the unit helper here so malformed input raises ValueError instead of
    # being silently converted to black. Color.to_api uses the same helper.
    hex_to_rgb(color)
    return Color(hex=color.lower()).to_api()
