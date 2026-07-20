"""Recreate copied elements and their pristine Google Slides styles."""

from __future__ import annotations

from collections.abc import Callable
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
    TextAlignment,
)
from extraslide.class_style_requests import (
    _class_paragraph_style_to_api,
    _class_text_style_to_api,
    _create_class_line_style_request,
    _create_class_shape_style_requests,
)
from extraslide.content_diff import Change, ParagraphClassUpdate
from extraslide.content_parser import ElementStyles, ParagraphStyles, ParsedRun
from extraslide.element_factories import (
    _create_image_request,
    _create_line_request,
    _create_move_request,
    _create_shape_request,
    _parse_color,
    _tag_to_type,
)
from extraslide.text_requests import (
    _create_paragraph_class_update_requests,
    _create_run_style_requests,
    _create_text_insert_requests,
    _create_text_update_requests,
    _utf16_len,
)


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
    warnings: list[str],
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

    if _contains_auto_text(change.new_runs, change.children):
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
        object_ids = {source_google_id: new_object_id}
        _map_duplicate_descendants(
            change.children or [],
            id_mapping or {},
            object_ids,
            new_object_id,
            reserved_ids,
            allocate_object_id,
        )
        requests.append(
            {
                "duplicateObject": {
                    "objectId": source_google_id,
                    "objectIds": object_ids,
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
        requests.extend(
            _duplicate_paragraph_style_requests(
                new_object_id,
                change.new_text or [],
                change.new_runs or [],
                change.old_paragraph_styles or [],
                change.new_paragraph_styles or [],
            )
        )
        _apply_duplicate_descendant_edits(
            change.children or [],
            id_mapping or {},
            object_ids,
            requests,
        )
        return requests

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


def _map_duplicate_descendants(
    children: list[dict[str, Any]],
    id_mapping: dict[str, str],
    object_ids: dict[str, str],
    id_prefix: str,
    reserved_ids: set[str],
    allocate_object_id: Callable[[str, set[str]], str],
    depth: int = 0,
) -> None:
    """Populate duplicateObject mappings for every serialized descendant."""
    for index, child in enumerate(children):
        clean_id = str(child.get("id", ""))
        source_google_id = id_mapping.get(clean_id)
        if source_google_id is None:
            raise ValueError(
                f"Cannot preserve edits on copied child '{clean_id}': "
                "source Google object ID is missing"
            )
        new_object_id = allocate_object_id(
            f"{id_prefix}_c{depth}_{index}", reserved_ids
        )
        reserved_ids.add(new_object_id)
        object_ids[source_google_id] = new_object_id
        _map_duplicate_descendants(
            child.get("children", []),
            id_mapping,
            object_ids,
            new_object_id,
            reserved_ids,
            allocate_object_id,
            depth + 1,
        )


def _duplicate_paragraph_style_requests(
    object_id: str,
    text: list[str],
    runs: list[list[ParsedRun]],
    old_styles: list[ParagraphStyles | None],
    new_styles: list[ParagraphStyles | None],
) -> list[dict[str, Any]]:
    """Apply paragraph defaults changed on a duplicated text element."""
    updates = [
        ParagraphClassUpdate(index, old_style, new_style)
        for index, (old_style, new_style) in enumerate(
            zip(old_styles, new_styles, strict=False)
        )
        if old_style != new_style
    ]
    if len(new_styles) > len(old_styles):
        updates.extend(
            ParagraphClassUpdate(index, None, new_styles[index])
            for index in range(len(old_styles), len(new_styles))
            if new_styles[index] is not None
        )
    if not updates:
        return []
    return _create_paragraph_class_update_requests(
        object_id,
        text,
        runs,
        updates,
    )


def _apply_duplicate_descendant_edits(
    children: list[dict[str, Any]],
    id_mapping: dict[str, str],
    object_ids: dict[str, str],
    requests: list[dict[str, Any]],
) -> None:
    """Replay authored text and paragraph deltas on mapped copied children."""
    for child in children:
        clean_id = str(child.get("id", ""))
        source_google_id = id_mapping.get(clean_id)
        new_object_id = object_ids.get(source_google_id or "")
        if new_object_id is None:
            raise ValueError(
                f"Cannot preserve edits on copied child '{clean_id}': "
                "duplicateObject descendant mapping is missing"
            )
        new_text = child.get("text", [])
        new_runs = child.get("runs", [])
        old_text = child.get("sourceText", [])
        old_runs = child.get("sourceRuns", [])
        if new_text != old_text or new_runs != old_runs:
            requests.extend(
                _create_text_update_requests(
                    new_object_id,
                    new_text,
                    new_runs,
                    old_text,
                    old_runs,
                )
            )
        requests.extend(
            _duplicate_paragraph_style_requests(
                new_object_id,
                new_text,
                new_runs,
                child.get("sourceParagraphStyles", []),
                child.get("paragraphStyles", []),
            )
        )
        _apply_duplicate_descendant_edits(
            child.get("children", []),
            id_mapping,
            object_ids,
            requests,
        )


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
    warnings: list[str],
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
        image_properties = style.get("imageProperties")
        image_properties_request = _create_image_properties_request(
            object_id, image_properties
        )
        if image_properties_request:
            requests.append(image_properties_request)
        dropped = _dropped_image_property_names(image_properties)
        if dropped:
            warnings.append(
                f"copy '{source_id}': image adjustments {', '.join(dropped)} "
                "cannot be preserved because the Google Slides API exposes them "
                "as read-only; the copy uses the source image without those "
                "adjustments"
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
            if paragraph_styles:
                paragraph_updates = [
                    ParagraphClassUpdate(index, None, styles)
                    for index, styles in enumerate(paragraph_styles)
                    if styles is not None
                ]
                requests.extend(
                    _create_paragraph_class_update_requests(
                        object_id,
                        text,
                        runs,
                        paragraph_updates,
                        reapply_runs=False,
                    )
                )
            if runs:
                requests.extend(_create_run_style_requests(object_id, runs))

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
    warnings: list[str],
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
                warnings.append(
                    f"copy '{copy_source_id}' child '{source_id}': "
                    f"authored position ({_format_number(child_orig_pos.get('x', 0))}, "
                    f"{_format_number(child_orig_pos.get('y', 0))}) matches neither "
                    f"the source position ({_format_number(source_position.get('x', 0))}, "
                    f"{_format_number(source_position.get('y', 0))}) nor the translated "
                    f"copy position ({_format_number(expected_final_x)}, "
                    f"{_format_number(expected_final_y)}); Slidesmith applied the parent "
                    "translation, so verify the copied child position"
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


def _contains_auto_text(
    runs: list[list[ParsedRun]] | None,
    children: list[dict[str, Any]] | None = None,
) -> bool:
    """Return whether copied root or descendant text contains dynamic autoText."""
    if any(run.auto_text_type for paragraph in runs or [] for run in paragraph):
        return True
    return any(
        _contains_auto_text(child.get("runs"), child.get("children"))
        for child in children or []
    )


def _format_number(value: Any) -> str:
    """Format warning coordinates without noisy integral decimal suffixes."""
    number = float(value)
    return f"{number:g}"


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
    """Apply pristine paragraph/run styles over clamped new-text ranges."""
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
        paragraph_length = _utf16_len(authored_text)
        run_offset = 0
        for run_index, run in enumerate(source_runs):
            run_text = str(run.get("content", ""))
            if run_index == len(source_runs) - 1:
                run_text = run_text.removesuffix("\n")
            run_length = _utf16_len(run_text)
            text_style, fields = _copied_text_style_to_api(run.get("style", {}))
            start_offset = min(run_offset, paragraph_length)
            end_offset = min(run_offset + run_length, paragraph_length)
            if fields and end_offset > start_offset:
                start = paragraph_start + start_offset
                requests.append(
                    {
                        "updateTextStyle": {
                            "objectId": object_id,
                            "textRange": {
                                "type": "FIXED_RANGE",
                                "startIndex": start,
                                "endIndex": paragraph_start + end_offset,
                            },
                            "style": text_style,
                            "fields": ",".join(fields),
                        }
                    }
                )
            run_offset += run_length

        paragraph_style, paragraph_fields = _copied_paragraph_style_to_api(
            paragraph.get("style", {})
        )
        if paragraph_fields and authored_text:
            requests.append(
                {
                    "updateParagraphStyle": {
                        "objectId": object_id,
                        "textRange": {
                            "type": "FIXED_RANGE",
                            "startIndex": paragraph_start,
                            "endIndex": paragraph_start + _utf16_len(authored_text),
                        },
                        "style": paragraph_style,
                        "fields": ",".join(paragraph_fields),
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
    style, fields = _class_text_style_to_api(typed)
    if "linkSlideIndex" in run_style:
        style["link"] = {"slideIndex": run_style["linkSlideIndex"]}
        if "link" not in fields:
            fields.append("link")
    elif "linkPageObjectId" in run_style:
        style["link"] = {"pageObjectId": run_style["linkPageObjectId"]}
        if "link" not in fields:
            fields.append("link")
    elif "linkRelative" in run_style:
        style["link"] = {"relativeLink": run_style["linkRelative"]}
        if "link" not in fields:
            fields.append("link")
    return style, fields


def _copied_paragraph_style_to_api(
    style: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Convert persisted styles.json paragraph data through typed requests."""
    alignment = style.get("alignment")
    typed = ParagraphStyle(
        alignment=TextAlignment(str(alignment)) if alignment else None,
        line_spacing=style.get("lineSpacing"),
        space_above_pt=style.get("spaceAbove"),
        space_below_pt=style.get("spaceBelow"),
        indent_start_pt=style.get("indentStart"),
        indent_end_pt=style.get("indentEnd"),
        indent_first_line_pt=style.get("indentFirstLine"),
        direction=style.get("direction"),
        spacing_mode=style.get("spacingMode"),
    )
    return _class_paragraph_style_to_api(typed)
