"""Tailwind-style class system for SML.

This module provides bidirectional conversion between:
- Google Slides API properties (JSON)
- SML Tailwind-style classes (strings)

Class naming follows the pattern: {property}-{subproperty}-{value}[/{modifier}]
"""

from __future__ import annotations

import contextlib
import math
import re
import urllib.parse
from dataclasses import dataclass
from enum import Enum
from typing import Any

from slidesmith.engine.units import (
    emu_to_pt,
    format_lossless_float,
    format_pt,
    hex_to_rgb,
    rgb_to_hex,
)


class PropertyState(Enum):
    """Property state values from Google Slides API."""

    NOT_RENDERED = "NOT_RENDERED"
    INHERIT = "INHERIT"


class DashStyle(Enum):
    """Dash styles for outlines/strokes."""

    SOLID = "SOLID"
    DOT = "DOT"
    DASH = "DASH"
    DASH_DOT = "DASH_DOT"
    LONG_DASH = "LONG_DASH"
    LONG_DASH_DOT = "LONG_DASH_DOT"


class ContentAlignment(Enum):
    """Vertical content alignment."""

    TOP = "TOP"
    MIDDLE = "MIDDLE"
    BOTTOM = "BOTTOM"

    def to_class(self) -> str:
        """Convert a Slides vertical alignment to its SML class."""
        return _CONTENT_ALIGNMENT_CLASSES[self]


_CONTENT_ALIGNMENT_CLASSES = {
    ContentAlignment.TOP: "content-align-top",
    ContentAlignment.MIDDLE: "content-align-middle",
    ContentAlignment.BOTTOM: "content-align-bottom",
}
_CLASS_CONTENT_ALIGNMENTS = {
    class_name: alignment
    for alignment, class_name in _CONTENT_ALIGNMENT_CLASSES.items()
}


class TextAlignment(Enum):
    """Horizontal text alignment."""

    START = "START"
    CENTER = "CENTER"
    END = "END"
    JUSTIFIED = "JUSTIFIED"


def _api_dimension_to_pt(dimension: dict[str, Any]) -> float:
    """Convert an explicitly-present Slides dimension to points.

    Protobuf JSON omits a zero magnitude, so ``{"unit": "PT"}`` is an
    explicitly-set zero rather than a missing property.
    """
    magnitude = dimension.get("magnitude", 0)
    if dimension.get("unit") == "EMU":
        return emu_to_pt(magnitude)
    return float(magnitude)


@dataclass
class Color:
    """Represents a color with optional alpha/opacity."""

    hex: str | None = None  # "#rrggbb"
    theme: str | None = None  # Theme color name
    alpha: float = 1.0  # 0.0 to 1.0

    @classmethod
    def from_api(cls, color_obj: dict[str, Any] | None) -> Color | None:
        """Create Color from API color object.

        Handles both rgbColor and themeColor.
        """
        if not color_obj:
            return None

        # Check for themeColor first
        if "themeColor" in color_obj:
            return cls(theme=color_obj["themeColor"].lower().replace("_", "-"))

        # Handle rgbColor
        rgb = color_obj.get("rgbColor", {})
        if not rgb:
            # Empty RGB means black
            return cls(hex="#000000")

        hex_color = rgb_to_hex(
            rgb.get("red", 0), rgb.get("green", 0), rgb.get("blue", 0)
        )
        return cls(hex=hex_color)

    def to_api(self) -> dict[str, Any]:
        """Convert to API color format."""
        if self.theme:
            return {"themeColor": self.theme.upper().replace("-", "_")}

        if self.hex:
            r, g, b = hex_to_rgb(self.hex)
            return {"rgbColor": {"red": r, "green": g, "blue": b}}

        return {"rgbColor": {}}


@dataclass
class Fill:
    """Fill styling (solid color, gradient, or state)."""

    color: Color | None = None
    state: PropertyState | None = None

    @classmethod
    def from_api(cls, fill_obj: dict[str, Any] | None) -> Fill | None:
        """Create Fill from API fill object."""
        if not fill_obj:
            return None

        # Check property state
        if "propertyState" in fill_obj:
            state_str = fill_obj["propertyState"]
            if state_str == "NOT_RENDERED":
                return cls(state=PropertyState.NOT_RENDERED)
            elif state_str == "INHERIT":
                return cls(state=PropertyState.INHERIT)

        # Handle solidFill
        solid = fill_obj.get("solidFill")
        if solid:
            color_obj = solid.get("color")
            color = Color.from_api(color_obj)
            alpha = solid.get("alpha", 1.0)
            if color:
                color.alpha = alpha
            return cls(color=color)

        return None

    def to_class(self) -> str:
        """Convert to SML class string."""
        if self.state == PropertyState.NOT_RENDERED:
            return "fill-none"
        if self.state == PropertyState.INHERIT:
            return ""

        if self.color:
            if self.color.theme:
                base = f"fill-theme-{self.color.theme}"
            elif self.color.hex:
                base = f"fill-{self.color.hex}"
            else:
                return ""

            # Add alpha/opacity if not 100%
            if self.color.alpha < 1.0:
                opacity = round(self.color.alpha * 100)
                return f"{base}/{opacity}"
            return base

        return ""


@dataclass
class Stroke:
    """Stroke/outline styling."""

    color: Color | None = None
    weight_pt: float | None = None
    dash_style: DashStyle | None = None
    state: PropertyState | None = None

    @classmethod
    def from_api(cls, outline_obj: dict[str, Any] | None) -> Stroke | None:
        """Create Stroke from API outline object."""
        if not outline_obj:
            return None

        # Check property state
        if "propertyState" in outline_obj:
            state_str = outline_obj["propertyState"]
            if state_str == "NOT_RENDERED":
                return cls(state=PropertyState.NOT_RENDERED)
            elif state_str == "INHERIT":
                return cls(state=PropertyState.INHERIT)

        stroke = cls()

        # Get fill color
        fill = outline_obj.get("outlineFill", {}).get("solidFill")
        if fill:
            color_obj = fill.get("color")
            stroke.color = Color.from_api(color_obj)
            if stroke.color:
                stroke.color.alpha = fill.get("alpha", 1.0)

        # Get weight
        weight = outline_obj.get("weight")
        if weight is not None:
            stroke.weight_pt = _api_dimension_to_pt(weight)

        # Get dash style
        dash = outline_obj.get("dashStyle")
        if dash:
            with contextlib.suppress(ValueError):
                stroke.dash_style = DashStyle(dash)

        return stroke

    def to_classes(self) -> list[str]:
        """Convert to list of SML class strings."""
        classes = []

        if self.state == PropertyState.NOT_RENDERED:
            return ["stroke-none"]
        if self.state == PropertyState.INHERIT:
            return []

        if self.color:
            if self.color.theme:
                base = f"stroke-theme-{self.color.theme}"
            elif self.color.hex:
                base = f"stroke-{self.color.hex}"
            else:
                base = ""

            if base:
                if self.color.alpha < 1.0:
                    opacity = round(self.color.alpha * 100)
                    classes.append(f"{base}/{opacity}")
                else:
                    classes.append(base)

        if self.weight_pt is not None:
            classes.append(f"stroke-w-{format_pt(self.weight_pt)}")

        if self.dash_style:
            dash_map = {
                DashStyle.SOLID: "stroke-solid",
                DashStyle.DOT: "stroke-dot",
                DashStyle.DASH: "stroke-dash",
                DashStyle.DASH_DOT: "stroke-dash-dot",
                DashStyle.LONG_DASH: "stroke-long-dash",
                DashStyle.LONG_DASH_DOT: "stroke-long-dash-dot",
            }
            if self.dash_style in dash_map:
                classes.append(dash_map[self.dash_style])

        return classes


# Compatibility models retained because the untouched donor vendor suite directly
# covers Shadow, Transform, and parse_position_classes as public conversion helpers.
@dataclass
class Shadow:
    """Shadow styling."""

    color: Color | None = None
    blur_pt: float | None = None
    alignment: str | None = None
    alpha: float = 1.0
    state: PropertyState | None = None

    @classmethod
    def from_api(cls, shadow_obj: dict[str, Any] | None) -> Shadow | None:
        """Create Shadow from API shadow object."""
        if not shadow_obj:
            return None

        # Check property state
        if "propertyState" in shadow_obj:
            state_str = shadow_obj["propertyState"]
            if state_str == "NOT_RENDERED":
                return cls(state=PropertyState.NOT_RENDERED)
            elif state_str == "INHERIT":
                return cls(state=PropertyState.INHERIT)

        shadow = cls()
        shadow.alpha = shadow_obj.get("alpha", 1.0)

        # Get color
        color_obj = shadow_obj.get("color")
        if color_obj:
            shadow.color = Color.from_api({"rgbColor": color_obj.get("rgbColor", {})})

        # Get blur
        blur = shadow_obj.get("blurRadius")
        if blur and blur.get("magnitude"):
            shadow.blur_pt = emu_to_pt(blur["magnitude"])

        # Get alignment
        shadow.alignment = shadow_obj.get("alignment")

        return shadow

    def to_classes(self) -> list[str]:
        """Convert to list of SML class strings."""
        if self.state == PropertyState.NOT_RENDERED:
            return ["shadow-none"]
        if self.state == PropertyState.INHERIT:
            return []

        classes = []

        # If we have actual shadow properties, output them
        if self.blur_pt is not None:
            if self.blur_pt <= 2:
                classes.append("shadow-sm")
            elif self.blur_pt <= 4:
                classes.append("shadow")
            elif self.blur_pt <= 8:
                classes.append("shadow-md")
            elif self.blur_pt <= 16:
                classes.append("shadow-lg")
            else:
                classes.append("shadow-xl")

            # Add specific blur if non-standard
            if self.blur_pt not in [2, 4, 8, 16, 24]:
                classes.append(f"shadow-blur-{format_pt(self.blur_pt)}")

        # Add alignment
        alignment_map = {
            "TOP_LEFT": "shadow-tl",
            "TOP_CENTER": "shadow-tc",
            "TOP_RIGHT": "shadow-tr",
            "CENTER_LEFT": "shadow-cl",
            "CENTER": "shadow-c",
            "CENTER_RIGHT": "shadow-cr",
            "BOTTOM_LEFT": "shadow-bl",
            "BOTTOM_CENTER": "shadow-bc",
            "BOTTOM_RIGHT": "shadow-br",
        }
        if self.alignment and self.alignment in alignment_map:
            classes.append(alignment_map[self.alignment])

        return classes


@dataclass
class Transform:
    """Element transform (position, rotation, scale, shear)."""

    translate_x_pt: float = 0
    translate_y_pt: float = 0
    scale_x: float = 1.0
    scale_y: float = 1.0
    shear_x: float = 0
    shear_y: float = 0
    # Derived values
    width_pt: float | None = None
    height_pt: float | None = None

    @classmethod
    def from_api(
        cls,
        transform_obj: dict[str, Any] | None,
        size_obj: dict[str, Any] | None = None,
    ) -> Transform:
        """Create Transform from API transform and size objects."""
        t = cls()

        if transform_obj:
            # Get translation (position)
            t.translate_x_pt = emu_to_pt(transform_obj.get("translateX", 0))
            t.translate_y_pt = emu_to_pt(transform_obj.get("translateY", 0))

            # Get scale
            t.scale_x = transform_obj.get("scaleX", 1.0)
            t.scale_y = transform_obj.get("scaleY", 1.0)

            # Get shear
            t.shear_x = transform_obj.get("shearX", 0)
            t.shear_y = transform_obj.get("shearY", 0)

        # Calculate actual dimensions from size and scale
        if size_obj:
            base_width = emu_to_pt(size_obj.get("width", {}).get("magnitude", 0))
            base_height = emu_to_pt(size_obj.get("height", {}).get("magnitude", 0))
            t.width_pt = abs(base_width * t.scale_x)
            t.height_pt = abs(base_height * t.scale_y)

        return t

    def to_classes(self) -> list[str]:
        """Convert to list of SML class strings."""
        classes = []

        # Position
        classes.append(f"x-{format_pt(self.translate_x_pt)}")
        classes.append(f"y-{format_pt(self.translate_y_pt)}")

        # Size (if known)
        if self.width_pt is not None:
            classes.append(f"w-{format_pt(self.width_pt)}")
        if self.height_pt is not None:
            classes.append(f"h-{format_pt(self.height_pt)}")

        # Rotation - derived from scale/shear if rotated
        # For now, handle simple case of negative scale (flip)
        if self.scale_x < 0:
            classes.append("-scale-x-100")
        if self.scale_y < 0:
            classes.append("-scale-y-100")

        # TODO: Extract rotation angle from transform matrix if needed

        return classes


@dataclass
class TextStyle:
    """Text run styling."""

    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strikethrough: bool | None = None
    small_caps: bool | None = None
    font_family: str | None = None
    font_size_pt: float | None = None
    font_weight: int | None = None
    foreground_color: Color | None = None
    background_color: Color | None = None
    link: str | None = None
    baseline_offset: str | None = None  # SUPERSCRIPT, SUBSCRIPT, NONE

    @classmethod
    def from_api(cls, style_obj: dict[str, Any] | None) -> TextStyle | None:
        """Create TextStyle from API style object."""
        if not style_obj:
            return None

        ts = cls()

        # Boolean styles
        ts.bold = style_obj.get("bold")
        ts.italic = style_obj.get("italic")
        ts.underline = style_obj.get("underline")
        ts.strikethrough = style_obj.get("strikethrough")
        ts.small_caps = style_obj.get("smallCaps")

        # Font
        ts.font_family = style_obj.get("fontFamily")

        # Font size
        font_size = style_obj.get("fontSize")
        if font_size is not None:
            ts.font_size_pt = _api_dimension_to_pt(font_size)

        # Font weight
        weighted = style_obj.get("weightedFontFamily")
        if weighted:
            if not ts.font_family:
                ts.font_family = weighted.get("fontFamily")
            ts.font_weight = weighted.get("weight")

        # Foreground color
        fg = style_obj.get("foregroundColor", {}).get("opaqueColor")
        if fg:
            ts.foreground_color = Color.from_api(fg)

        # Background color
        bg = style_obj.get("backgroundColor", {}).get("opaqueColor")
        if bg:
            ts.background_color = Color.from_api(bg)

        # Link
        link = style_obj.get("link")
        if link:
            ts.link = (
                link.get("url") or link.get("slideIndex") or link.get("pageObjectId")
            )

        # Baseline offset
        ts.baseline_offset = style_obj.get("baselineOffset")

        return ts

    def to_classes(self) -> list[str]:
        """Convert to list of SML class strings."""
        classes = []

        # Boolean decorations
        if self.bold:
            classes.append("bold")
        if self.italic:
            classes.append("italic")
        if self.underline:
            classes.append("underline")
        if self.strikethrough:
            classes.append("line-through")
        if self.small_caps:
            classes.append("small-caps")

        # Baseline offset
        if self.baseline_offset == "SUPERSCRIPT":
            classes.append("superscript")
        elif self.baseline_offset == "SUBSCRIPT":
            classes.append("subscript")

        # Font family
        if self.font_family:
            # Keep the familiar legacy spelling when it is exactly reversible;
            # otherwise percent-encode the original name so capitalization and
            # punctuation survive a pull/push round trip.
            font_name = self.font_family.lower().replace(" ", "-")
            if font_name.replace("-", " ").title() == self.font_family:
                classes.append(f"font-family-{font_name}")
            else:
                classes.append(
                    f"font-family-{urllib.parse.quote(self.font_family, safe='')}"
                )

        # Font size
        if self.font_size_pt:
            classes.append(f"text-size-{format_pt(self.font_size_pt)}")

        # Font weight is independent from the bold flag in the Slides API.
        if self.font_weight is not None:
            classes.append(f"font-weight-{self.font_weight}")

        # Text color
        if self.foreground_color:
            if self.foreground_color.theme:
                classes.append(f"text-color-theme-{self.foreground_color.theme}")
            elif self.foreground_color.hex:
                classes.append(f"text-color-{self.foreground_color.hex}")

        # Background/highlight color
        if self.background_color and self.background_color.hex:
            classes.append(f"bg-{self.background_color.hex}")

        return classes


@dataclass
class ParagraphStyle:
    """Paragraph styling."""

    alignment: TextAlignment | None = None
    line_spacing: float | None = None  # Percentage (100 = single)
    space_above_pt: float | None = None
    space_below_pt: float | None = None
    indent_start_pt: float | None = None
    indent_end_pt: float | None = None
    indent_first_line_pt: float | None = None
    direction: str | None = None
    spacing_mode: str | None = None
    # Bullet properties
    has_bullet: bool = False
    bullet_glyph: str | None = None
    nesting_level: int = 0

    @classmethod
    def from_api(cls, style_obj: dict[str, Any] | None) -> ParagraphStyle | None:
        """Create ParagraphStyle from API style object."""
        if not style_obj:
            return None

        ps = cls()

        # Alignment
        align = style_obj.get("alignment")
        if align:
            with contextlib.suppress(ValueError):
                ps.alignment = TextAlignment(align)

        # Line spacing
        line_spacing = style_obj.get("lineSpacing")
        if line_spacing is not None:
            ps.line_spacing = float(line_spacing)

        # Space above/below
        space_above = style_obj.get("spaceAbove")
        if space_above is not None:
            ps.space_above_pt = _api_dimension_to_pt(space_above)

        space_below = style_obj.get("spaceBelow")
        if space_below is not None:
            ps.space_below_pt = _api_dimension_to_pt(space_below)

        # Indentation
        indent_start = style_obj.get("indentStart")
        if indent_start is not None:
            ps.indent_start_pt = _api_dimension_to_pt(indent_start)

        indent_end = style_obj.get("indentEnd")
        if indent_end is not None:
            ps.indent_end_pt = _api_dimension_to_pt(indent_end)

        indent_first = style_obj.get("indentFirstLine")
        if indent_first is not None:
            ps.indent_first_line_pt = _api_dimension_to_pt(indent_first)

        # Direction
        ps.direction = style_obj.get("direction")

        # Spacing mode
        ps.spacing_mode = style_obj.get("spacingMode")

        return ps

    def to_classes(self) -> list[str]:
        """Convert to list of SML class strings."""
        classes = []

        # Alignment
        if self.alignment:
            align_map = {
                TextAlignment.START: "text-align-left",
                TextAlignment.CENTER: "text-align-center",
                TextAlignment.END: "text-align-right",
                TextAlignment.JUSTIFIED: "text-align-justify",
            }
            if self.alignment in align_map:
                classes.append(align_map[self.alignment])

        # Line spacing
        if (
            self.line_spacing is not None
            and math.isfinite(self.line_spacing)
            and self.line_spacing > 0
        ):
            classes.append(
                f"leading-{format_lossless_float(self.line_spacing, positional=True)}"
            )

        # Space above/below
        if self.space_above_pt is not None:
            classes.append(f"space-above-{format_pt(self.space_above_pt)}")
        if self.space_below_pt is not None:
            classes.append(f"space-below-{format_pt(self.space_below_pt)}")

        # Indentation
        if self.indent_start_pt is not None:
            classes.append(f"indent-start-{format_pt(self.indent_start_pt)}")
        if self.indent_first_line_pt is not None:
            classes.append(f"indent-first-{format_pt(self.indent_first_line_pt)}")

        # Direction
        if self.direction == "RIGHT_TO_LEFT":
            classes.append("dir-rtl")

        # Spacing mode
        if self.spacing_mode == "NEVER_COLLAPSE":
            classes.append("spacing-never-collapse")
        elif self.spacing_mode == "COLLAPSE_LISTS":
            classes.append("spacing-collapse-lists")

        # Bullet styling
        if self.has_bullet:
            classes.append("bullet")
            if self.bullet_glyph:
                # Map common glyphs to classes
                glyph_map = {
                    "●": "bullet-disc",
                    "○": "bullet-circle",
                    "■": "bullet-square",
                    "◆": "bullet-diamond",
                    "➔": "bullet-arrow",
                    "★": "bullet-star",
                }
                if self.bullet_glyph in glyph_map:
                    classes.append(glyph_map[self.bullet_glyph])

            # Nesting level
            if self.nesting_level > 0:
                classes.append(f"indent-level-{self.nesting_level}")

        return classes


# ============================================================================
# Class Parsing (SML → Data Structures)
# ============================================================================


def parse_class_string(class_str: str) -> list[str]:
    """Parse class string into individual classes."""
    return class_str.split() if class_str else []


def common_classes(class_sets: list[list[str]]) -> list[str]:
    """Return classes present in every set, preserving first-set order."""
    if not class_sets:
        return []
    return [
        cls
        for cls in class_sets[0]
        if all(cls in candidate for candidate in class_sets[1:])
    ]


class ClassKind(Enum):
    """The single parser family that owns an SML class."""

    CONTENT_ALIGNMENT = "content_alignment"
    FILL = "fill"
    STROKE = "stroke"
    TEXT = "text"
    PARAGRAPH = "paragraph"


def classify_class(cls: str) -> tuple[ClassKind, Any] | None:
    """Classify one class using the same parsers that construct typed styles."""
    if alignment := parse_content_alignment_class(cls):
        return ClassKind.CONTENT_ALIGNMENT, alignment
    if fill := parse_fill_class(cls):
        return ClassKind.FILL, fill
    if stroke := parse_stroke_classes([cls]):
        return ClassKind.STROKE, stroke
    text = parse_text_style_classes([cls])
    if text != TextStyle():
        return ClassKind.TEXT, text
    if paragraph := parse_paragraph_style_classes([cls]):
        return ClassKind.PARAGRAPH, paragraph
    return None


def mutually_exclusive_class_family(cls: str) -> str | None:
    """Return a single-value family using the canonical typed class parser."""
    classified = classify_class(cls)
    if classified is None:
        return None
    kind, value = classified
    if kind == ClassKind.CONTENT_ALIGNMENT:
        return "content alignment"
    if kind == ClassKind.FILL:
        return "fill"
    if kind == ClassKind.STROKE:
        if value.state is not None or value.color is not None:
            return "stroke color or state"
        if value.weight_pt is not None:
            return "stroke weight"
        if value.dash_style is not None:
            return "stroke dash style"
    if kind == ClassKind.PARAGRAPH:
        for attribute, family in (
            ("alignment", "text alignment"),
            ("line_spacing", "line spacing"),
            ("space_above_pt", "space above"),
            ("space_below_pt", "space below"),
            ("indent_start_pt", "start indent"),
            ("indent_first_line_pt", "first-line indent"),
            ("spacing_mode", "paragraph spacing mode"),
        ):
            if getattr(value, attribute) is not None:
                return family
    if kind == ClassKind.TEXT:
        for attribute, family in (
            ("baseline_offset", "baseline offset"),
            ("font_family", "font family"),
            ("font_size_pt", "font size"),
            ("font_weight", "font weight"),
            ("foreground_color", "text color"),
            ("background_color", "text background color"),
        ):
            if getattr(value, attribute) is not None:
                return family
    return None


def validate_mutually_exclusive_classes(
    classes: list[str],
    element_id: str,
    *,
    scope: str = "element",
) -> None:
    """Reject distinct classes that set the same single-value property.

    Identical repeats are harmless and remain accepted. Unknown classes are
    deliberately ignored here so the existing scope-specific loud errors can
    report them.
    """
    selected: dict[str, str] = {}
    for cls in classes:
        family = mutually_exclusive_class_family(cls)
        if family is None:
            continue
        previous = selected.get(family)
        if previous is None:
            selected[family] = cls
        elif previous != cls:
            location = (
                f"element '{element_id}'"
                if scope == "element"
                else f"{scope} in element '{element_id}'"
            )
            raise ValueError(
                f"Conflicting classes '{previous}' and '{cls}' on {location}: "
                f"both set the mutually-exclusive {family} family; remove one."
            )


def parse_content_alignment_class(cls: str) -> ContentAlignment | None:
    """Parse one ``content-align-*`` class, or return None."""
    return _CLASS_CONTENT_ALIGNMENTS.get(cls)


def _parse_opacity(value: str | None, cls: str) -> float:
    """Parse a color opacity suffix and reject values outside 0–100."""
    if value is None:
        return 1.0
    opacity = int(value)
    if opacity > 100:
        raise ValueError(f"Invalid class '{cls}': opacity must be between 0 and 100")
    return opacity / 100


def parse_position_classes(classes: list[str]) -> dict[str, float]:
    """Extract position values from classes.

    Returns dict with keys: x, y, w, h
    """
    result: dict[str, float] = {}

    for cls in classes:
        # x-{value}
        if match := re.match(r"^x-(-?\d+(?:\.\d+)?)$", cls):
            result["x"] = float(match.group(1))
        # y-{value}
        elif match := re.match(r"^y-(-?\d+(?:\.\d+)?)$", cls):
            result["y"] = float(match.group(1))
        # w-{value}
        elif match := re.match(r"^w-(\d+(?:\.\d+)?)$", cls):
            result["w"] = float(match.group(1))
        # h-{value}
        elif match := re.match(r"^h-(\d+(?:\.\d+)?)$", cls):
            result["h"] = float(match.group(1))

    return result


def parse_fill_class(cls: str) -> Fill | None:
    """Parse a fill class into a Fill object."""
    if cls == "fill-none":
        return Fill(state=PropertyState.NOT_RENDERED)
    if cls == "fill-inherit":
        return Fill(state=PropertyState.INHERIT)

    # fill-theme-{name}[/opacity]
    if match := re.match(r"^fill-theme-([a-z0-9-]+)(?:/(\d+))?$", cls):
        theme_name = match.group(1)
        opacity = _parse_opacity(match.group(2), cls)
        return Fill(color=Color(theme=theme_name, alpha=opacity))

    # fill-#{hex}[/opacity]
    if match := re.match(r"^fill-(#[0-9a-fA-F]{6})(?:/(\d+))?$", cls):
        hex_color = match.group(1).lower()
        opacity = _parse_opacity(match.group(2), cls)
        return Fill(color=Color(hex=hex_color, alpha=opacity))

    return None


def parse_stroke_classes(classes: list[str]) -> Stroke | None:
    """Parse stroke-related classes into a Stroke object."""
    stroke = Stroke()
    found_stroke = False

    for cls in classes:
        if cls == "stroke-none":
            return Stroke(state=PropertyState.NOT_RENDERED)
        if cls == "stroke-inherit":
            return Stroke(state=PropertyState.INHERIT)

        # stroke-theme-{name}[/opacity]
        if match := re.match(r"^stroke-theme-([a-z0-9-]+)(?:/(\d+))?$", cls):
            theme_name = match.group(1)
            opacity = _parse_opacity(match.group(2), cls)
            stroke.color = Color(theme=theme_name, alpha=opacity)
            found_stroke = True

        # stroke-#{hex}[/opacity]
        elif match := re.match(r"^stroke-(#[0-9a-fA-F]{6})(?:/(\d+))?$", cls):
            hex_color = match.group(1).lower()
            opacity = _parse_opacity(match.group(2), cls)
            stroke.color = Color(hex=hex_color, alpha=opacity)
            found_stroke = True

        # stroke-w-{value}
        elif match := re.match(r"^stroke-w-(\d+(?:\.\d+)?)$", cls):
            stroke.weight_pt = float(match.group(1))
            found_stroke = True

        # stroke dash styles
        elif cls == "stroke-solid":
            stroke.dash_style = DashStyle.SOLID
            found_stroke = True
        elif cls == "stroke-dot":
            stroke.dash_style = DashStyle.DOT
            found_stroke = True
        elif cls == "stroke-dash":
            stroke.dash_style = DashStyle.DASH
            found_stroke = True
        elif cls == "stroke-dash-dot":
            stroke.dash_style = DashStyle.DASH_DOT
            found_stroke = True
        elif cls == "stroke-long-dash":
            stroke.dash_style = DashStyle.LONG_DASH
            found_stroke = True
        elif cls == "stroke-long-dash-dot":
            stroke.dash_style = DashStyle.LONG_DASH_DOT
            found_stroke = True

    return stroke if found_stroke else None


def parse_paragraph_style_classes(classes: list[str]) -> ParagraphStyle | None:
    """Parse paragraph styling classes into a ParagraphStyle object.

    Reverse of ParagraphStyle.to_classes(). Returns None if no
    paragraph-level classes were found.
    """
    ps = ParagraphStyle()
    found = False

    align_map = {
        "text-align-left": TextAlignment.START,
        "text-align-center": TextAlignment.CENTER,
        "text-align-right": TextAlignment.END,
        "text-align-justify": TextAlignment.JUSTIFIED,
    }

    for cls in classes:
        # Alignment
        if cls in align_map:
            ps.alignment = align_map[cls]
            found = True

        # Line spacing
        elif match := re.match(r"^leading-(\d+(?:\.\d+)?)$", cls):
            ps.line_spacing = float(match.group(1))
            found = True

        # Space above/below
        elif match := re.match(r"^space-above-(\d+(?:\.\d+)?)$", cls):
            ps.space_above_pt = float(match.group(1))
            found = True
        elif match := re.match(r"^space-below-(\d+(?:\.\d+)?)$", cls):
            ps.space_below_pt = float(match.group(1))
            found = True

        # Indentation
        elif match := re.match(r"^indent-start-(\d+(?:\.\d+)?)$", cls):
            ps.indent_start_pt = float(match.group(1))
            found = True
        elif match := re.match(r"^indent-first-(\d+(?:\.\d+)?)$", cls):
            ps.indent_first_line_pt = float(match.group(1))
            found = True

        # Direction
        elif cls == "dir-rtl":
            ps.direction = "RIGHT_TO_LEFT"
            found = True

        # Spacing mode
        elif cls == "spacing-never-collapse":
            ps.spacing_mode = "NEVER_COLLAPSE"
            found = True
        elif cls == "spacing-collapse-lists":
            ps.spacing_mode = "COLLAPSE_LISTS"
            found = True

    return ps if found else None


def parse_text_style_classes(classes: list[str]) -> TextStyle:
    """Parse text styling classes into a TextStyle object."""
    ts = TextStyle()

    for cls in classes:
        # Boolean styles
        if cls == "bold":
            ts.bold = True
        elif cls == "italic":
            ts.italic = True
        elif cls == "underline":
            ts.underline = True
        elif cls == "line-through":
            ts.strikethrough = True
        elif cls == "small-caps":
            ts.small_caps = True
        elif cls == "superscript":
            ts.baseline_offset = "SUPERSCRIPT"
        elif cls == "subscript":
            ts.baseline_offset = "SUBSCRIPT"

        # Font family
        elif match := re.match(r"^font-family-(.+)$", cls):
            encoded_name = match.group(1)
            font_name = (
                urllib.parse.unquote(encoded_name)
                if "%" in encoded_name
                else encoded_name.replace("-", " ").title()
            )
            ts.font_family = font_name

        # Font size
        elif match := re.match(r"^text-size-(\d+(?:\.\d+)?)$", cls):
            ts.font_size_pt = float(match.group(1))

        # Font weight
        elif match := re.match(r"^font-weight-(\d+)$", cls):
            ts.font_weight = int(match.group(1))

        # Text color
        elif match := re.match(r"^text-color-theme-([a-z0-9-]+)$", cls):
            ts.foreground_color = Color(theme=match.group(1))
        elif match := re.match(r"^text-color-(#[0-9a-fA-F]{6})(?:/(\d+))?$", cls):
            opacity = _parse_opacity(match.group(2), cls)
            ts.foreground_color = Color(hex=match.group(1).lower(), alpha=opacity)

        # Background color
        elif match := re.match(r"^bg-(#[0-9a-fA-F]{6})$", cls):
            ts.background_color = Color(hex=match.group(1).lower())

    return ts
