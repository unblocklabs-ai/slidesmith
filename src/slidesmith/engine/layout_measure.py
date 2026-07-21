"""Text measurement helpers for authoring layout compilation."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from typing import Protocol

from slidesmith.engine.classes import parse_class_string, parse_text_style_classes
from slidesmith.engine.units import format_pt


class TextMeasurer(Protocol):
    """Backend interface used to measure wrapped text height in points."""

    def measure_wrapped_height(
        self,
        text: str,
        font_family: str,
        font_size_pt: float,
        font_weight: int,
        available_width: float,
    ) -> float:
        """Return the height needed for ``text`` within ``available_width``."""


class ApproximateTextMeasurer:
    """Deterministic average-character-width text measurement backend."""

    AVERAGE_CHAR_WIDTH_FACTORS = {
        "arial": 0.52,
        "roboto": 0.51,
        "open sans": 0.53,
        "lato": 0.50,
        "montserrat": 0.55,
    }
    DEFAULT_CHAR_WIDTH_FACTOR = 0.52
    LINE_HEIGHT_FACTOR = 1.2
    SAFETY_MARGIN_FACTOR = 1.08

    def measure_wrapped_height(
        self,
        text: str,
        font_family: str,
        font_size_pt: float,
        font_weight: int,
        available_width: float,
    ) -> float:
        if available_width <= 0:
            raise ValueError("Text measurement requires a positive available width")
        if font_size_pt <= 0:
            raise ValueError("Text measurement requires a positive font size")

        family = " ".join(font_family.lower().replace("-", " ").split())
        width_factor = self.AVERAGE_CHAR_WIDTH_FACTORS.get(
            family, self.DEFAULT_CHAR_WIDTH_FACTOR
        )
        # Heavier faces are usually a little wider. Keep the adjustment small
        # because the table is intentionally an average, not font-file metrics.
        weight_factor = 1.0 + max(font_weight - 400, 0) / 4000
        average_char_width = font_size_pt * width_factor * weight_factor

        lines = 0
        explicit_lines = text.split("\n") if text else [""]
        for line in explicit_lines:
            estimated_width = len(line.expandtabs(4)) * average_char_width
            lines += max(1, math.ceil(estimated_width / available_width))

        return (
            lines
            * font_size_pt
            * self.LINE_HEIGHT_FACTOR
            * self.SAFETY_MARGIN_FACTOR
        )


def _measure_textbox(
    textbox: ET.Element,
    available_width: float,
    measurer: TextMeasurer,
) -> float:
    classes = parse_class_string(textbox.get("class", ""))
    text_style = parse_text_style_classes(classes)
    font_family = text_style.font_family or "Arial"
    font_size = text_style.font_size_pt or 12.0
    font_weight = text_style.font_weight or (700 if text_style.bold else 400)
    paragraphs = ["".join(paragraph.itertext()) for paragraph in textbox.findall("P")]
    text = "\n".join(paragraphs) if paragraphs else ""
    return measurer.measure_wrapped_height(
        text,
        font_family,
        font_size,
        font_weight,
        available_width,
    )


def _resolve_auto_height(textbox: ET.Element, measurer: TextMeasurer) -> None:
    label = _element_label(textbox)
    width = _required_number_attr(textbox, "w", "TextBox h=auto")
    if width <= 0:
        raise ValueError(f"{label}: h='auto' requires a positive width")
    textbox.set("h", format_pt(_measure_textbox(textbox, width, measurer)))


def _authored_grid_height(
    child: ET.Element,
    cell_width: float,
    measurer: TextMeasurer,
) -> float:
    if child.tag == "TextBox" and child.get("h") == "auto":
        return _measure_textbox(child, cell_width, measurer)
    return _required_number_attr(child, "h", "Grid")


def _required_number_attr(
    element: ET.Element,
    name: str,
    context: str,
) -> float:
    value = element.get(name)
    if value is None:
        raise ValueError(
            f"{_element_label(element)} inside {context}: missing required '{name}'"
        )
    return _parse_number(value, element, name)


def _parse_number(value: str, element: ET.Element, name: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(
            f"{_element_label(element)}: '{name}' must be a number, got '{value}'"
        ) from exc
    if not math.isfinite(number):
        raise ValueError(f"{_element_label(element)}: '{name}' must be finite")
    return number


def _element_label(element: ET.Element) -> str:
    element_id = element.get("id")
    return f"Element '{element_id}'" if element_id else f"<{element.tag}>"


_DEFAULT_MEASURER = ApproximateTextMeasurer()
