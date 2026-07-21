"""Helpers for diffing authored style class groups."""

from __future__ import annotations

from typing import Any

from slidesmith.engine.classes import ParagraphStyle, Stroke, TextStyle


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
