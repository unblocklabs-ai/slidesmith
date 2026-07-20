"""Build class-derived style requests for Google Slides elements."""

from __future__ import annotations

from typing import Any

from extraslide.classes import ParagraphStyle, PropertyState, Stroke, TextStyle
from extraslide.content_parser import ElementStyles
from extraslide.units import pt_to_emu


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
