"""Pure frame calculations for Stack and Grid authoring containers."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import math
from dataclasses import dataclass

from slidesmith.engine.layout_measure import (
    TextMeasurer,
    _authored_grid_height,
    _element_label,
    _parse_number,
    _required_number_attr,
    _measure_textbox,
)


@dataclass(frozen=True)
class _Frame:
    x: float
    y: float
    w: float
    h: float


def compute_stack_frames(
    stack: ET.Element,
    frame: _Frame,
    measurer: TextMeasurer,
) -> list[tuple[ET.Element, _Frame]]:
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

    gap = _number_attr(stack, "gap")
    padding = _number_attr(stack, "padding")
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
    frames: list[tuple[ET.Element, _Frame]] = []
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
        frames.append((child, child_frame))
        cursor += main_size + spacing

    return frames


def compute_grid_frames(
    grid: ET.Element,
    frame: _Frame,
    measurer: TextMeasurer,
) -> list[tuple[ET.Element, _Frame]]:
    label = _element_label(grid)
    columns_float = _required_number_attr(grid, "columns", "Grid")
    columns = int(columns_float)
    if columns <= 0 or columns_float != columns:
        raise ValueError(f"{label}: columns must be a positive integer")
    gap = _number_attr(grid, "gap")
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

    frames: list[tuple[ET.Element, _Frame]] = []
    for index, child in enumerate(children):
        row = index // columns
        column = index % columns
        child_frame = _Frame(
            frame.x + column * (cell_width + gap),
            row_origins[row],
            cell_width,
            row_heights[row],
        )
        frames.append((child, child_frame))
    return frames


def _number_attr(element: ET.Element, name: str) -> float:
    value = element.get(name)
    return 0 if value is None else _parse_number(value, element, name)


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
