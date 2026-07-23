"""Text measurement helpers for authoring layout compilation."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
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


@dataclass(frozen=True)
class TextRunMetrics:
    """The metrics needed to estimate one character run."""

    text: str
    font_family: str = "Arial"
    font_size_pt: float = 12.0
    font_weight: int = 400


@dataclass(frozen=True)
class ParagraphMetrics:
    """Resolved metrics for one paragraph in a text element."""

    runs: tuple[TextRunMetrics, ...]
    # Google exposes line spacing as a percentage. ``None`` means use the
    # estimator's historical 1.2 default rather than silently treating an
    # omitted property as authored single spacing.
    line_spacing_percent: float | None = None
    space_above_pt: float = 0.0
    space_below_pt: float = 0.0
    indent_start_pt: float = 0.0
    indent_end_pt: float = 0.0
    indent_first_line_pt: float = 0.0


@dataclass(frozen=True)
class TextLayoutMeasurement:
    """Approximate content-box geometry for a paragraph-aware text layout."""

    height_pt: float
    max_line_width_pt: float
    first_line_height_pt: float
    line_count: int
    max_font_size_pt: float
    paragraphs: tuple["ParagraphLayoutMeasurement", ...] = ()


@dataclass(frozen=True)
class ParagraphLayoutMeasurement:
    """Measured line widths/heights for one paragraph's ink block."""

    line_widths_pt: tuple[float, ...]
    line_heights_pt: tuple[float, ...]
    space_above_pt: float = 0.0
    space_below_pt: float = 0.0


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
    # QA now resolves per-paragraph/run metrics, authored leading, insets, and
    # captured autofit before measuring. Keep a small 2% residual allowance for
    # kerning, word-boundary wrapping, and font-file differences; retaining the
    # old 8% here would double-count the uncertainty that the richer model
    # removes. The legacy ``measure_wrapped_height`` path below keeps its old
    # constant for authoring-layout compatibility.
    QA_SAFETY_MARGIN_FACTOR = 1.02
    SAFETY_MARGIN_FACTOR = 1.08

    def measure_paragraphs(
        self,
        paragraphs: tuple[ParagraphMetrics, ...] | list[ParagraphMetrics],
        available_width: float,
        *,
        font_scale: float = 1.0,
        line_spacing_reduction: float | None = None,
    ) -> TextLayoutMeasurement:
        """Measure paragraphs using their effective run and paragraph styles.

        Width is accumulated per run, so a large heading run no longer makes
        every paragraph wrap as if it used the largest font in the element.
        ``line_spacing_reduction`` follows the Slides autofit meaning: it is a
        fraction removed from line spacing (0 means unchanged, 0.2 means 20%
        less). It is only supplied by QA for a text-autofit shape.
        """
        if available_width <= 0:
            raise ValueError("Text measurement requires a positive available width")

        safe_font_scale = (
            font_scale if math.isfinite(font_scale) and font_scale > 0 else 1.0
        )
        if line_spacing_reduction is None or not math.isfinite(line_spacing_reduction):
            line_spacing_factor = 1.0
        else:
            line_spacing_factor = max(0.0, 1.0 - line_spacing_reduction)

        total_height = 0.0
        max_line_width = 0.0
        first_line_height = 0.0
        line_count = 0
        max_font_size = 0.0
        paragraph_measurements: list[ParagraphLayoutMeasurement] = []
        for paragraph in paragraphs:
            paragraph_lines = self._measure_paragraph_lines(
                paragraph,
                available_width,
                safe_font_scale,
            )
            authored_line_spacing = paragraph.line_spacing_percent
            line_height_factor = (
                authored_line_spacing / 100.0
                if (
                    authored_line_spacing is not None
                    and math.isfinite(authored_line_spacing)
                    and authored_line_spacing > 0
                )
                else self.LINE_HEIGHT_FACTOR
            )
            line_height_factor *= line_spacing_factor
            line_widths: list[float] = []
            line_heights: list[float] = []
            for line_width, line_font_size in paragraph_lines:
                scaled_size = line_font_size * safe_font_scale
                line_height = (
                    scaled_size
                    * line_height_factor
                    * self.QA_SAFETY_MARGIN_FACTOR
                )
                total_height += line_height
                max_line_width = max(max_line_width, line_width)
                max_font_size = max(max_font_size, scaled_size)
                line_count += 1
                line_widths.append(line_width)
                line_heights.append(line_height)
                if line_count == 1:
                    first_line_height = line_height

            space_above = max(0.0, paragraph.space_above_pt)
            space_below = max(0.0, paragraph.space_below_pt)
            total_height += space_above
            total_height += space_below
            paragraph_measurements.append(
                ParagraphLayoutMeasurement(
                    tuple(line_widths),
                    tuple(line_heights),
                    space_above,
                    space_below,
                )
            )

        if line_count == 0:
            # Empty text is not normally sent to QA, but the stable result is
            # useful to callers that use this helper directly.
            return TextLayoutMeasurement(0.0, 0.0, 0.0, 0, 0.0)
        return TextLayoutMeasurement(
            total_height,
            max_line_width,
            first_line_height,
            line_count,
            max_font_size,
            tuple(paragraph_measurements),
        )

    def _measure_paragraph_lines(
        self,
        paragraph: ParagraphMetrics,
        available_width: float,
        font_scale: float,
    ) -> list[tuple[float, float]]:
        """Return ``(line width, largest unscaled font)`` pairs."""
        runs = paragraph.runs or (TextRunMetrics(""),)
        lines: list[tuple[float, float]] = []
        line_width = 0.0
        line_font_size = 0.0
        has_content = False
        empty_line_font_size = max(
            (run.font_size_pt for run in runs if run.font_size_pt > 0),
            default=12.0,
        )
        first_line = True
        line_width_limit = max(
            0.01,
            available_width
            - max(0.0, paragraph.indent_start_pt)
            - max(0.0, paragraph.indent_end_pt)
                - (max(0.0, paragraph.indent_first_line_pt) if first_line else 0.0),
        )

        def finish_line() -> None:
            nonlocal line_width, line_font_size, has_content, first_line, line_width_limit
            lines.append((line_width, line_font_size or empty_line_font_size))
            line_width = 0.0
            line_font_size = 0.0
            has_content = False
            first_line = False
            line_width_limit = max(
                0.01,
                available_width
                - max(0.0, paragraph.indent_start_pt)
                - max(0.0, paragraph.indent_end_pt),
            )

        def add_glyph(glyph_width: float, glyph_font_size: float) -> None:
            nonlocal line_width, line_font_size, has_content
            if has_content and line_width + glyph_width > line_width_limit:
                finish_line()
            line_width += glyph_width
            line_font_size = max(line_font_size, glyph_font_size)
            has_content = True

        def add_word(glyphs: list[tuple[float, float]], pending_space: list[tuple[float, float]]) -> None:
            nonlocal line_width, line_font_size, has_content
            if not glyphs:
                return
            word_width = sum(width for width, _ in glyphs)
            space_width = sum(width for width, _ in pending_space)
            if has_content and word_width <= line_width_limit and (
                line_width + space_width + word_width > line_width_limit
            ):
                # A whitespace break is preferred to splitting a word.
                finish_line()
                space_width = 0.0
            elif not has_content:
                space_width = 0.0

            if word_width <= line_width_limit:
                if space_width:
                    for glyph_width, glyph_font_size in pending_space:
                        add_glyph(glyph_width, glyph_font_size)
                for glyph_width, glyph_font_size in glyphs:
                    add_glyph(glyph_width, glyph_font_size)
                return

            # Slides keeps a word together when it fits on a fresh line. A
            # single word wider than the frame is the exception: match the
            # renderer's emergency character breaking for that token.
            if has_content:
                finish_line()
            for glyph_width, glyph_font_size in glyphs:
                add_glyph(glyph_width, glyph_font_size)

        word: list[tuple[float, float]] = []
        whitespace: list[tuple[float, float]] = []

        def flush_word() -> None:
            nonlocal word, whitespace
            add_word(word, whitespace)
            word = []
            whitespace = []

        for run in runs:
            family = " ".join(run.font_family.lower().replace("-", " ").split())
            width_factor = self.AVERAGE_CHAR_WIDTH_FACTORS.get(
                family, self.DEFAULT_CHAR_WIDTH_FACTOR
            )
            weight_factor = 1.0 + max(run.font_weight - 400, 0) / 4000
            char_width = run.font_size_pt * width_factor * weight_factor * font_scale
            for character in run.text:
                if character == "\n":
                    flush_word()
                    finish_line()
                    continue
                character_width = char_width * (4 if character == "\t" else 1)
                glyph = (character_width, run.font_size_pt)
                if character.isspace():
                    if word:
                        flush_word()
                    whitespace.append(glyph)
                else:
                    word.append(glyph)

        flush_word()

        if has_content or not lines:
            finish_line()
        return lines

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
