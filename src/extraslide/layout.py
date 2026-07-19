"""Compile SML authoring layout into absolute, parser-ready SML.

The built-in text measurement is deliberately approximate and deterministic.
Google Slides exposes no writable text-autofit or text-measurement API, so
``ApproximateTextMeasurer`` estimates wrapping from per-font average character
widths for Arial, Roboto, Open Sans, Lato, and Montserrat (falling back to an
Arial-like default). It uses a 1.2 line-height and an 8% vertical safety margin
so ``h=\"auto\"`` slightly over-allocates instead of clipping. It does not model
glyph kerning or exact font files. ``TextMeasurer`` is intentionally small so a
font-metrics implementation can be injected later without changing SML.

Layout calculations use SML points. Derived values are formatted through
``units.format_pt`` (at most two decimals), matching the existing SML generator
and its subsequent point-to-EMU request conversion.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Protocol

from extraslide.classes import parse_class_string, parse_text_style_classes
from extraslide.units import format_pt


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


@dataclass(frozen=True)
class _Frame:
    x: float
    y: float
    w: float
    h: float


_AUTHORING_MARKER = re.compile(
    r"<(?:Stack|Grid)\b|<TextBox\b[^>]*\bh\s*=\s*(['\"])auto\1",
    re.DOTALL,
)
_DEFAULT_MEASURER = ApproximateTextMeasurer()


def compile_layout(content: str, text_measurer: TextMeasurer | None = None) -> str:
    """Compile Stack, Grid, and TextBox ``h=\"auto\"`` authoring syntax.

    The transform is pure: it parses a new XML tree and returns a new string.
    Content with no authoring constructs is returned directly, byte-for-byte,
    without XML parsing or serialization.
    """
    if not _AUTHORING_MARKER.search(content):
        return content

    measurer = text_measurer or _DEFAULT_MEASURER
    wrapped = False
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        try:
            root = ET.fromstring(f"<Root>{content}</Root>")
            wrapped = True
        except ET.ParseError as exc:
            raise ValueError(f"Invalid content.sml XML: {exc}") from exc

    # The fast marker scan may match a comment or text fragment. Confirm that
    # an actual authoring element exists before serializing, preserving the
    # byte-identical passthrough guarantee for ordinary SML.
    if not _tree_needs_layout(root):
        return content

    if root.tag in {"Stack", "Grid"}:
        compiled_roots = _compile_container(root, None, measurer)
        return "".join(ET.tostring(node, encoding="unicode") for node in compiled_roots)

    _compile_children(root, measurer)
    if wrapped:
        return "".join(ET.tostring(node, encoding="unicode") for node in root)
    return ET.tostring(root, encoding="unicode")


def _compile_children(parent: ET.Element, measurer: TextMeasurer) -> None:
    if parent.tag == "TextBox" and parent.get("h") == "auto":
        _resolve_auto_height(parent, measurer)

    children = list(parent)
    if not children:
        return

    compiled: list[ET.Element] = []
    for child in children:
        if child.tag in {"Stack", "Grid"}:
            replacements = _compile_container(child, None, measurer)
            _carry_tail(child, replacements)
            compiled.extend(replacements)
        else:
            if child.tag == "TextBox" and child.get("h") == "auto":
                _resolve_auto_height(child, measurer)
            # Paragraph and text-run nodes cannot contain layout elements.
            if child.tag not in {"P", "T"}:
                _compile_children(child, measurer)
            compiled.append(child)
    parent[:] = compiled


def _compile_container(
    container: ET.Element,
    assigned_frame: _Frame | None,
    measurer: TextMeasurer,
) -> list[ET.Element]:
    frame = assigned_frame or _read_frame(container)
    if container.tag == "Stack":
        return _compile_stack(container, frame, measurer)
    return _compile_grid(container, frame, measurer)


def _compile_stack(
    stack: ET.Element,
    frame: _Frame,
    measurer: TextMeasurer,
) -> list[ET.Element]:
    label = _element_label(stack)
    direction = stack.get("direction", "row")
    align = stack.get("align", "start")
    distribute = stack.get("distribute", "none")
    if direction not in {"row", "column"}:
        raise ValueError(f"{label}: direction must be 'row' or 'column'")
    if align not in {"start", "center", "end", "stretch"}:
        raise ValueError(
            f"{label}: align must be start, center, end, or stretch"
        )
    if distribute not in {"none", "space-between"}:
        raise ValueError(f"{label}: distribute must be 'none' or 'space-between'")

    gap = _number_attr(stack, "gap", default=0)
    padding = _number_attr(stack, "padding", default=0)
    if gap < 0 or padding < 0:
        raise ValueError(f"{label}: gap and padding cannot be negative")

    children = list(stack)
    if not children:
        return []
    for child in children:
        _reject_authored_position(child, stack.tag)

    inner_x = frame.x + padding
    inner_y = frame.y + padding
    inner_w = frame.w - 2 * padding
    inner_h = frame.h - 2 * padding
    if inner_w < 0 or inner_h < 0:
        raise ValueError(f"{label}: padding is larger than the container frame")

    is_row = direction == "row"
    inner_main = inner_w if is_row else inner_h
    inner_cross = inner_h if is_row else inner_w

    cross_sizes: list[float | None] = []
    for child in children:
        cross_attr = "h" if is_row else "w"
        if align == "stretch":
            cross_sizes.append(inner_cross)
        elif is_row and child.tag == "TextBox" and child.get("h") == "auto":
            cross_sizes.append(None)
        else:
            cross_sizes.append(_required_number_attr(child, cross_attr, stack.tag))

    flexes = [_flex(child, stack.tag) for child in children]
    main_sizes: list[float | None] = []
    for index, child in enumerate(children):
        if flexes[index] is not None:
            main_sizes.append(None)
            continue
        main_attr = "w" if is_row else "h"
        if not is_row and child.tag == "TextBox" and child.get("h") == "auto":
            width = cross_sizes[index]
            assert width is not None
            main_sizes.append(_measure_textbox(child, width, measurer))
        else:
            main_sizes.append(_required_number_attr(child, main_attr, stack.tag))

    minimum_gaps = gap * (len(children) - 1)
    fixed_total = sum(size for size in main_sizes if size is not None)
    remaining = inner_main - minimum_gaps - fixed_total
    total_flex = sum(value for value in flexes if value is not None)
    if total_flex:
        if remaining < 0:
            raise ValueError(f"{label}: children, gap, and padding exceed its frame")
        for index, flex in enumerate(flexes):
            if flex is not None:
                main_sizes[index] = remaining * flex / total_flex
        remaining_after_layout = 0.0
    else:
        remaining_after_layout = remaining
        if remaining_after_layout < 0:
            raise ValueError(f"{label}: children, gap, and padding exceed its frame")

    if is_row:
        for index, child in enumerate(children):
            if cross_sizes[index] is None:
                width = main_sizes[index]
                assert width is not None
                cross_sizes[index] = _measure_textbox(child, width, measurer)

    spacing = gap
    if distribute == "space-between" and len(children) > 1:
        spacing += remaining_after_layout / (len(children) - 1)

    cursor = inner_x if is_row else inner_y
    compiled: list[ET.Element] = []
    for index, child in enumerate(children):
        main_size = main_sizes[index]
        cross_size = cross_sizes[index]
        assert main_size is not None and cross_size is not None
        cross_origin = inner_y if is_row else inner_x
        if align == "center":
            cross_origin += (inner_cross - cross_size) / 2
        elif align == "end":
            cross_origin += inner_cross - cross_size

        child_frame = (
            _Frame(cursor, cross_origin, main_size, cross_size)
            if is_row
            else _Frame(cross_origin, cursor, cross_size, main_size)
        )
        compiled.extend(_compile_container_child(child, child_frame, measurer))
        cursor += main_size + spacing

    return compiled


def _compile_grid(
    grid: ET.Element,
    frame: _Frame,
    measurer: TextMeasurer,
) -> list[ET.Element]:
    label = _element_label(grid)
    columns_float = _required_number_attr(grid, "columns", "Grid")
    columns = int(columns_float)
    if columns <= 0 or columns_float != columns:
        raise ValueError(f"{label}: columns must be a positive integer")
    gap = _number_attr(grid, "gap", default=0)
    if gap < 0:
        raise ValueError(f"{label}: gap cannot be negative")

    children = list(grid)
    if not children:
        return []
    for child in children:
        _reject_authored_position(child, grid.tag)

    cell_width = (frame.w - gap * (columns - 1)) / columns
    if cell_width < 0:
        raise ValueError(f"{label}: columns and gap exceed its width")

    row_count = math.ceil(len(children) / columns)
    row_h_attr = grid.get("row-h")
    if row_h_attr is not None:
        row_height = _parse_number(row_h_attr, grid, "row-h")
        row_heights = [row_height] * row_count
    else:
        row_heights = []
        for row_index in range(row_count):
            row_children = children[row_index * columns : (row_index + 1) * columns]
            heights = [
                _authored_grid_height(child, cell_width, measurer)
                for child in row_children
            ]
            row_heights.append(max(heights))

    used_height = sum(row_heights) + gap * (row_count - 1)
    if used_height > frame.h + 1e-9:
        raise ValueError(f"{label}: rows and gap exceed its height")

    row_origins: list[float] = []
    cursor_y = frame.y
    for row_height in row_heights:
        row_origins.append(cursor_y)
        cursor_y += row_height + gap

    compiled: list[ET.Element] = []
    for index, child in enumerate(children):
        row = index // columns
        column = index % columns
        child_frame = _Frame(
            frame.x + column * (cell_width + gap),
            row_origins[row],
            cell_width,
            row_heights[row],
        )
        compiled.extend(_compile_container_child(child, child_frame, measurer))
    return compiled


def _compile_container_child(
    child: ET.Element,
    frame: _Frame,
    measurer: TextMeasurer,
) -> list[ET.Element]:
    if child.tag in {"Stack", "Grid"}:
        return _compile_container(child, frame, measurer)

    _write_frame(child, frame)
    child.attrib.pop("flex", None)
    _compile_children(child, measurer)
    return [child]


def _authored_grid_height(
    child: ET.Element,
    cell_width: float,
    measurer: TextMeasurer,
) -> float:
    if child.tag == "TextBox" and child.get("h") == "auto":
        return _measure_textbox(child, cell_width, measurer)
    return _required_number_attr(child, "h", "Grid")


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


def _read_frame(element: ET.Element) -> _Frame:
    return _Frame(
        _required_number_attr(element, "x", "top-level container"),
        _required_number_attr(element, "y", "top-level container"),
        _required_number_attr(element, "w", "top-level container"),
        _required_number_attr(element, "h", "top-level container"),
    )


def _write_frame(element: ET.Element, frame: _Frame) -> None:
    element.set("x", format_pt(frame.x))
    element.set("y", format_pt(frame.y))
    element.set("w", format_pt(frame.w))
    element.set("h", format_pt(frame.h))


def _reject_authored_position(child: ET.Element, container_tag: str) -> None:
    if child.get("x") is not None or child.get("y") is not None:
        raise ValueError(
            f"Element '{child.get('id', '')}' inside {container_tag} cannot declare "
            "x or y; its container assigns the position"
        )


def _flex(child: ET.Element, container_tag: str) -> float | None:
    value = child.get("flex")
    if value is None:
        return None
    flex = _parse_number(value, child, "flex")
    if flex <= 0:
        raise ValueError(
            f"{_element_label(child)} inside {container_tag}: flex must be positive"
        )
    return flex


def _number_attr(element: ET.Element, name: str, default: float) -> float:
    value = element.get(name)
    return default if value is None else _parse_number(value, element, name)


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


def _carry_tail(source: ET.Element, replacements: list[ET.Element]) -> None:
    if source.tail and replacements:
        replacements[-1].tail = source.tail


def _tree_needs_layout(root: ET.Element) -> bool:
    return any(
        element.tag in {"Stack", "Grid"}
        or (element.tag == "TextBox" and element.get("h") == "auto")
        for element in root.iter()
    )
