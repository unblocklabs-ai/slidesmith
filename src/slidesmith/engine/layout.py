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

import re
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

from slidesmith.engine.layout_algorithms import (
    _Frame,
    _required_number_attr,
    compute_grid_frames,
    compute_stack_frames,
)
from slidesmith.engine.layout_components import UseState, expand_use
from slidesmith.engine.layout_measure import (
    ApproximateTextMeasurer,
    TextMeasurer,
    _DEFAULT_MEASURER,
    _resolve_auto_height,
)
from slidesmith.engine.units import format_pt

if TYPE_CHECKING:
    from slidesmith.engine.components import ComponentLibrary


__all__ = ["ApproximateTextMeasurer", "TextMeasurer", "compile_layout"]


_UseState = UseState


_AUTHORING_MARKER = re.compile(
    r"<(?:Stack|Grid|Use)\b|<TextBox\b[^>]*\bh\s*=\s*(['\"])auto\1",
    re.DOTALL,
)


def compile_layout(
    content: str,
    text_measurer: TextMeasurer | None = None,
    *,
    components: ComponentLibrary | None = None,
) -> str:
    """Compile components, Stack, Grid, and ``h=\"auto\"`` authoring syntax.

    The transform is pure: it parses a new XML tree and returns a new string.
    Content with no authoring constructs is returned directly, byte-for-byte,
    without XML parsing or serialization.
    """
    if not _AUTHORING_MARKER.search(content):
        return content

    measurer = text_measurer or _DEFAULT_MEASURER
    use_state = _UseState()
    wrapped = False
    try:
        root = DefusedET.fromstring(content)
    except DefusedXmlException as exc:
        raise ValueError(f"Invalid content.sml XML: {exc}") from exc
    except ET.ParseError:
        try:
            root = DefusedET.fromstring(f"<Root>{content}</Root>")
            wrapped = True
        except (ET.ParseError, DefusedXmlException) as exc:
            raise ValueError(f"Invalid content.sml XML: {exc}") from exc

    # The fast marker scan may match a comment or text fragment. Confirm that
    # an actual authoring element exists before serializing, preserving the
    # byte-identical passthrough guarantee for ordinary SML.
    if not _tree_needs_layout(root):
        return content

    if root.tag in {"Stack", "Grid"}:
        compiled_roots = _compile_container(
            root, None, measurer, components, use_state
        )
        return "".join(ET.tostring(node, encoding="unicode") for node in compiled_roots)
    if root.tag == "Use":
        compiled_roots = _expand_use(root, _read_frame(root), measurer, components, use_state)
        return "".join(ET.tostring(node, encoding="unicode") for node in compiled_roots)

    _compile_children(root, measurer, components, use_state)
    if wrapped:
        return "".join(ET.tostring(node, encoding="unicode") for node in root)
    return ET.tostring(root, encoding="unicode")


def _compile_children(
    parent: ET.Element,
    measurer: TextMeasurer,
    components: ComponentLibrary | None,
    use_state: _UseState,
) -> None:
    if parent.tag == "TextBox" and parent.get("h") == "auto":
        _resolve_auto_height(parent, measurer)

    children = list(parent)
    if not children:
        return

    compiled: list[ET.Element] = []
    for child in children:
        if child.tag in {"Stack", "Grid"}:
            replacements = _compile_container(
                child, None, measurer, components, use_state
            )
            _carry_tail(child, replacements)
            compiled.extend(replacements)
        elif child.tag == "Use":
            replacements = _expand_use(
                child, _read_frame(child), measurer, components, use_state
            )
            _carry_tail(child, replacements)
            compiled.extend(replacements)
        else:
            if child.tag == "TextBox" and child.get("h") == "auto":
                _resolve_auto_height(child, measurer)
            # Paragraph and text-run nodes cannot contain layout elements.
            if child.tag not in {"P", "T"}:
                _compile_children(child, measurer, components, use_state)
            compiled.append(child)
    parent[:] = compiled


def _compile_container(
    container: ET.Element,
    assigned_frame: _Frame | None,
    measurer: TextMeasurer,
    components: ComponentLibrary | None,
    use_state: _UseState,
) -> list[ET.Element]:
    frame = assigned_frame or _read_frame(container)
    if container.tag == "Stack":
        return _compile_stack(container, frame, measurer, components, use_state)
    return _compile_grid(container, frame, measurer, components, use_state)


def _compile_stack(
    stack: ET.Element,
    frame: _Frame,
    measurer: TextMeasurer,
    components: ComponentLibrary | None,
    use_state: _UseState,
) -> list[ET.Element]:
    compiled: list[ET.Element] = []
    for child, child_frame in compute_stack_frames(stack, frame, measurer):
        compiled.extend(
            _compile_container_child(
                child, child_frame, measurer, components, use_state
            )
        )
    return compiled


def _compile_grid(
    grid: ET.Element,
    frame: _Frame,
    measurer: TextMeasurer,
    components: ComponentLibrary | None,
    use_state: _UseState,
) -> list[ET.Element]:
    compiled: list[ET.Element] = []
    for child, child_frame in compute_grid_frames(grid, frame, measurer):
        compiled.extend(
            _compile_container_child(
                child, child_frame, measurer, components, use_state
            )
        )
    return compiled


def _compile_container_child(
    child: ET.Element,
    frame: _Frame,
    measurer: TextMeasurer,
    components: ComponentLibrary | None,
    use_state: _UseState,
) -> list[ET.Element]:
    if child.tag in {"Stack", "Grid"}:
        return _compile_container(child, frame, measurer, components, use_state)
    if child.tag == "Use":
        return _expand_use(child, frame, measurer, components, use_state)

    _write_frame(child, frame)
    child.attrib.pop("flex", None)
    _compile_children(child, measurer, components, use_state)
    return [child]


def _expand_use(
    use: ET.Element,
    frame: _Frame,
    measurer: TextMeasurer,
    components: ComponentLibrary | None,
    use_state: _UseState,
) -> list[ET.Element]:
    return expand_use(
        use,
        frame,
        measurer,
        components,
        use_state,
        compile_container=_compile_container,
        compile_children=_compile_children,
    )


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


def _carry_tail(source: ET.Element, replacements: list[ET.Element]) -> None:
    if source.tail and replacements:
        replacements[-1].tail = source.tail


def _tree_needs_layout(root: ET.Element) -> bool:
    return any(
        element.tag in {"Stack", "Grid", "Use"}
        or (element.tag == "TextBox" and element.get("h") == "auto")
        for element in root.iter()
    )
