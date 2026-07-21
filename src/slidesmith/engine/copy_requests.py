"""Recreate copied elements and their pristine Google Slides styles."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from slidesmith.engine.content_diff import Change
from slidesmith.engine.content_parser import ParagraphStyles, ParsedRun
from slidesmith.engine.diff_model import PushWarning, WarningSeverity
from slidesmith.engine.duplicate_copy_requests import (
    _apply_duplicate_descendant_edits,
    _contains_auto_text,
    _create_duplicate_copy_requests,
    _duplicate_paragraph_style_requests,
    _format_number,
    _map_duplicate_descendants,
    _uses_duplicate_object,
    _warn_for_ambiguous_child_position,
)
from slidesmith.engine.element_factories import (
    _tag_to_type,
    emit_recreated_element,
)
from slidesmith.engine.styles_json_adapter import (
    _apply_line_style_requests,
    _apply_style_requests,
    _apply_text_style_requests,
    _color_from_styles_json,
    _copied_paragraph_style_to_api,
    _copied_text_style_to_api,
    _fill_from_styles_json,
    _stroke_from_styles_json,
)
__all__ = [
    "_apply_duplicate_descendant_edits",
    "_apply_line_style_requests",
    "_apply_style_requests",
    "_apply_text_style_requests",
    "_color_from_styles_json",
    "_contains_auto_text",
    "_copied_paragraph_style_to_api",
    "_copied_text_style_to_api",
    "_create_children_from_data",
    "_create_copy_requests",
    "_create_one_copied_element",
    "_duplicate_paragraph_style_requests",
    "_fill_from_styles_json",
    "_format_number",
    "_map_duplicate_descendants",
    "_stroke_from_styles_json",
    "_uses_duplicate_object",
    "_warn_for_ambiguous_child_position",
]


def _create_copy_requests(
    change: Change,
    source_style: dict[str, Any],
    slide_google_id: str,
    all_styles: dict[str, dict[str, Any]],
    reserved_ids: set[str],
    *,
    source_google_id: str | None = None,
    id_mapping: dict[str, str] | None = None,
    allocate_object_id: Callable[[str, set[str]], str],
    unique_suffix: Callable[[], str],
    warnings: list[PushWarning],
    pristine_element_types: dict[str, str] | None = None,
    pristine_element_parents: dict[str, str | None] | None = None,
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
    suffix = unique_suffix()
    new_object_id = allocate_object_id(
        f"copy_{change.slide_index}_{suffix}", reserved_ids
    )
    reserved_ids.add(new_object_id)

    if _uses_duplicate_object(change):
        return _create_duplicate_copy_requests(
            change=change,
            source_style=source_style,
            source_google_id=source_google_id,
            new_object_id=new_object_id,
            position=position,
            reserved_ids=reserved_ids,
            id_mapping=id_mapping,
            allocate_object_id=allocate_object_id,
            pristine_element_types=pristine_element_types,
            pristine_element_parents=pristine_element_parents,
            warnings=warnings,
        )

    _create_one_copied_element(
        object_id=new_object_id,
        elem_type=elem_type,
        source_id=change.source_id or change.target_id,
        copy_source_id=change.source_id or change.target_id,
        position=position,
        text=change.new_text or [],
        runs=change.new_runs or [],
        paragraph_styles=change.new_paragraph_styles or [],
        children=change.children or [],
        translation=translation,
        slide_google_id=slide_google_id,
        all_styles=all_styles,
        style=source_style,
        requests=requests,
        child_depth=0,
        reserved_ids=reserved_ids,
        allocate_object_id=allocate_object_id,
        warnings=warnings,
    )

    return requests


def _create_one_copied_element(
    *,
    object_id: str,
    elem_type: str,
    source_id: str,
    copy_source_id: str,
    position: dict[str, float],
    text: list[str],
    runs: list[list[ParsedRun]],
    paragraph_styles: list[ParagraphStyles | None],
    children: list[dict[str, Any]],
    translation: dict[str, float],
    slide_google_id: str,
    all_styles: dict[str, dict[str, Any]],
    style: dict[str, Any],
    requests: list[dict[str, Any]],
    child_depth: int,
    reserved_ids: set[str],
    allocate_object_id: Callable[[str, set[str]], str],
    warnings: list[PushWarning],
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
            copy_source_id,
            reserved_ids=reserved_ids,
            allocate_object_id=allocate_object_id,
            warnings=warnings,
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
        element_style_requests = _apply_line_style_requests(object_id, style)
        content_url = None
    elif elem_type == "IMAGE":
        content_url = style.get("contentUrl", "")
        if not content_url:
            raise ValueError(f"Cannot copy image '{source_id}': contentUrl is missing")
        element_style_requests = []
    else:
        content_url = None
        element_style_requests = _apply_style_requests(object_id, style)

    replayed_text = text if elem_type not in {"LINE", "IMAGE"} else []
    text_style_info = style.get("text", {})
    text_style_requests = (
        _apply_text_style_requests(object_id, replayed_text, text_style_info)
        if replayed_text and text_style_info
        else []
    )
    requests.extend(
        emit_recreated_element(
            object_id=object_id,
            element_type=elem_type,
            slide_google_id=slide_google_id,
            position=position,
            image_url=content_url,
            native_size=style.get("nativeSize"),
            native_scale=style.get("nativeScale"),
            element_style_requests=element_style_requests,
            text=replayed_text,
            text_style_requests=text_style_requests,
            paragraph_styles=paragraph_styles,
            runs=runs,
        )
    )

    if elem_type == "IMAGE":
        image_properties = style.get("imageProperties")
        image_properties_request = _create_image_properties_request(
            object_id, image_properties
        )
        if image_properties_request:
            requests.append(image_properties_request)
        dropped = _dropped_image_property_names(image_properties)
        if dropped:
            warnings.append(
                PushWarning(
                    WarningSeverity.WARNING,
                    f"copy '{source_id}': image adjustments {', '.join(dropped)} "
                    "cannot be preserved because the Google Slides API exposes "
                    "them as read-only; the copy uses the source image without "
                    "those adjustments",
                )
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
            copy_source_id,
            reserved_ids=reserved_ids,
            allocate_object_id=allocate_object_id,
            warnings=warnings,
        )


def _create_children_from_data(
    children: list[dict[str, Any]],
    translation: dict[str, float],
    slide_google_id: str,
    all_styles: dict[str, dict[str, Any]],
    requests: list[dict[str, Any]],
    id_prefix: str,
    depth: int = 0,
    copy_source_id: str = "",
    *,
    reserved_ids: set[str],
    allocate_object_id: Callable[[str, set[str]], str],
    warnings: list[PushWarning],
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
        child_obj_id = allocate_object_id(
            f"{id_prefix}_c{depth}_{i}", reserved_ids
        )
        reserved_ids.add(child_obj_id)
        child_tag = child_data.get("tag", "Rect")
        child_text = child_data.get("text", [])
        child_runs = child_data.get("runs", [])
        child_paragraph_styles = child_data.get("paragraphStyles", [])
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
            elif not (
                abs(child_orig_pos.get("x", 0) - source_position.get("x", 0))
                <= 0.01
                and abs(child_orig_pos.get("y", 0) - source_position.get("y", 0))
                <= 0.01
            ):
                _warn_for_ambiguous_child_position(
                    child_data,
                    translation,
                    copy_source_id,
                    warnings,
                )
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
            copy_source_id=copy_source_id,
            position=abs_position,
            text=child_text,
            runs=child_runs,
            paragraph_styles=child_paragraph_styles,
            children=nested_children,
            translation=translation,
            slide_google_id=slide_google_id,
            all_styles=all_styles,
            style=child_style,
            requests=requests,
            child_depth=depth + 1,
            reserved_ids=reserved_ids,
            allocate_object_id=allocate_object_id,
            warnings=warnings,
        )
        child_ids.append(child_obj_id)

    return child_ids


def _create_image_properties_request(
    object_id: str,
    properties: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Replay only writable persisted ImageProperties after createImage."""
    if not properties:
        return None

    image_properties: dict[str, Any] = {}
    fields: list[str] = []
    for name in ("outline", "link"):
        if name in properties:
            image_properties[name] = properties[name]
            fields.append(name)

    if not fields:
        return None
    return {
        "updateImageProperties": {
            "objectId": object_id,
            "imageProperties": image_properties,
            "fields": ",".join(fields),
        }
    }


def _dropped_image_property_names(
    properties: dict[str, Any] | None,
) -> list[str]:
    """Return persisted image adjustments that the Slides API cannot write."""
    return [
        name
        for name in (
            "transparency",
            "brightness",
            "contrast",
            "crop",
            "recolor",
            "shadow",
        )
        if properties and name in properties
    ]
