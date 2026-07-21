"""Adapt persisted styles.json values into Google Slides API requests."""

from __future__ import annotations

from typing import Any

from slidesmith.engine.classes import (
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
from slidesmith.engine.class_style_requests import (
    _class_paragraph_style_to_api,
    _class_text_style_to_api,
    _create_class_line_style_request,
    _create_class_shape_style_requests,
)
from slidesmith.engine.content_parser import ElementStyles
from slidesmith.engine.element_factories import _parse_color
from slidesmith.engine.text_requests import _utf16_len


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
