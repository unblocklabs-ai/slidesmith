"""Recreate copied elements and their pristine Google Slides styles."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from extraslide.classes import (
    Color,
    ContentAlignment,
    DashStyle,
    Fill,
    PropertyState,
    Stroke,
    TextStyle,
)
from extraslide.class_style_requests import (
    _class_text_style_to_api,
    _create_class_line_style_request,
    _create_class_shape_style_requests,
)
from extraslide.content_diff import Change
from extraslide.content_parser import ElementStyles, ParsedRun
from extraslide.element_factories import (
    _create_image_request,
    _create_line_request,
    _create_move_request,
    _create_shape_request,
    _parse_color,
    _tag_to_type,
)
from extraslide.text_requests import (
    _create_run_style_requests,
    _create_text_insert_requests,
    _create_text_update_requests,
    _utf16_len,
)
from extraslide.units import pt_to_emu


def _create_copy_requests(
    change: Change,
    source_style: dict[str, Any],
    slide_google_id: str,
    all_styles: dict[str, dict[str, Any]],
    reserved_ids: set[str],
    *,
    source_google_id: str | None = None,
    allocate_object_id: Callable[[str, set[str]], str],
    unique_suffix: Callable[[], str],
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

    if _contains_auto_text(change.new_runs):
        if source_google_id is None:
            raise ValueError(
                f"Cannot preserve autoText on copy '{change.source_id}': "
                "source Google object ID is missing"
            )
        if (
            change.source_slide_index is not None
            and change.source_slide_index != change.slide_index
        ):
            raise ValueError(
                f"Cannot preserve autoText on cross-slide copy '{change.source_id}': "
                "the Slides API can only duplicate a page element on its source slide"
            )
        requests.append(
            {
                "duplicateObject": {
                    "objectId": source_google_id,
                    "objectIds": {source_google_id: new_object_id},
                }
            }
        )
        requests.append(
            _create_move_request(
                new_object_id,
                position,
                source_style,
                change.old_position,
            )
        )
        if change.new_text != change.old_text or change.new_runs != change.old_runs:
            requests.extend(
                _create_text_update_requests(
                    new_object_id,
                    change.new_text or [],
                    change.new_runs,
                    change.old_text,
                    change.old_runs,
                )
            )
        return requests

    _create_one_copied_element(
        object_id=new_object_id,
        elem_type=elem_type,
        source_id=change.source_id or change.target_id,
        position=position,
        text=change.new_text or [],
        runs=change.new_runs or [],
        children=change.children or [],
        translation=translation,
        slide_google_id=slide_google_id,
        all_styles=all_styles,
        style=source_style,
        requests=requests,
        child_depth=0,
        reserved_ids=reserved_ids,
        allocate_object_id=allocate_object_id,
    )

    return requests


def _create_one_copied_element(
    *,
    object_id: str,
    elem_type: str,
    source_id: str,
    position: dict[str, float],
    text: list[str],
    runs: list[list[ParsedRun]],
    children: list[dict[str, Any]],
    translation: dict[str, float],
    slide_google_id: str,
    all_styles: dict[str, dict[str, Any]],
    style: dict[str, Any],
    requests: list[dict[str, Any]],
    child_depth: int,
    reserved_ids: set[str],
    allocate_object_id: Callable[[str, set[str]], str],
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
            allocate_object_id=allocate_object_id,
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
        image_properties_request = _create_image_properties_request(
            object_id, style.get("imageProperties")
        )
        if image_properties_request:
            requests.append(image_properties_request)
    else:
        requests.append(
            _create_shape_request(object_id, slide_google_id, elem_type, position)
        )
        requests.extend(_apply_style_requests(object_id, style))
        if text:
            requests.extend(_create_text_insert_requests(object_id, text))
            if runs:
                requests.extend(_create_run_style_requests(object_id, runs))
            else:
                text_style_info = style.get("text", {})
                requests.extend(
                    _apply_text_style_requests(object_id, text, text_style_info)
                    if text_style_info
                    else []
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
            allocate_object_id=allocate_object_id,
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
    allocate_object_id: Callable[[str, set[str]], str],
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
            runs=child_runs,
            children=nested_children,
            translation=translation,
            slide_google_id=slide_google_id,
            all_styles=all_styles,
            style=child_style,
            requests=requests,
            child_depth=depth + 1,
            reserved_ids=reserved_ids,
            allocate_object_id=allocate_object_id,
        )
        child_ids.append(child_obj_id)

    return child_ids


def _contains_auto_text(runs: list[list[ParsedRun]] | None) -> bool:
    """Return whether copied text contains a dynamic autoText run."""
    return any(run.auto_text_type for paragraph in runs or [] for run in paragraph)


def _create_image_properties_request(
    object_id: str,
    properties: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Replay persisted image properties after createImage."""
    if not properties:
        return None

    image_properties: dict[str, Any] = {}
    fields: list[str] = []
    for name in ("transparency", "brightness", "contrast"):
        if name in properties:
            image_properties[name] = properties[name]
            fields.append(name)

    crop = properties.get("crop")
    if crop is not None:
        image_properties["cropProperties"] = {
            "leftOffset": crop.get("left", 0),
            "rightOffset": crop.get("right", 0),
            "topOffset": crop.get("top", 0),
            "bottomOffset": crop.get("bottom", 0),
        }
        fields.append("cropProperties")

    recolor = properties.get("recolor")
    if recolor:
        image_properties["recolor"] = {"name": recolor}
        fields.append("recolor")

    shadow = properties.get("shadow")
    if shadow:
        if shadow.get("type") == "none":
            image_properties["shadow"] = {"propertyState": "NOT_RENDERED"}
            fields.append("shadow.propertyState")
        else:
            api_shadow: dict[str, Any] = {}
            for name in ("type", "alignment", "alpha"):
                if name in shadow:
                    api_shadow[name] = shadow[name]
            if shadow.get("color"):
                api_shadow["color"] = _color_from_styles_json(
                    str(shadow["color"])
                ).to_api()
            if "blurRadius" in shadow:
                api_shadow["blurRadius"] = {
                    "magnitude": pt_to_emu(float(shadow["blurRadius"])),
                    "unit": "EMU",
                }
            if api_shadow:
                image_properties["shadow"] = api_shadow
                fields.append("shadow")

    if not fields:
        return None
    return {
        "updateImageProperties": {
            "objectId": object_id,
            "imageProperties": image_properties,
            "fields": ",".join(fields),
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
