"""Build Google Slides requests that create or move page elements."""

from __future__ import annotations

from typing import Any

from extraslide.classes import Color
from extraslide.class_style_requests import (
    _create_class_line_style_request,
    _create_class_paragraph_style_request,
    _create_class_shape_style_requests,
    _create_class_text_style_request,
)
from extraslide.content_diff import Change, ParagraphClassUpdate
from extraslide.shape_types import TAG_TO_TYPE, VALID_GOOGLE_TYPES
from extraslide.text_requests import (
    _create_paragraph_class_update_requests,
    _create_run_style_requests,
    _create_text_insert_requests,
)
from extraslide.units import hex_to_rgb, pt_to_emu

_MIN_EMU = 1


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
