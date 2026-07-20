"""Generate editable SML content from render trees.

Produces clean, minimal XML with:
- <Slide> root tag for valid XML
- Clean IDs on all elements
- Absolute position (x, y, w, h) on ALL elements
- Text content preserved, including explicitly-set per-run styling
- Explicitly-set, class-expressible styling as Tailwind-style classes

Inherited or otherwise absent API properties are never resolved or emitted.
Non-class styling remains in styles.json.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from xml.etree.ElementTree import Element
from xml.sax.saxutils import escape

from slidesmith.engine.classes import ParagraphStyle, common_classes
from slidesmith.engine.shape_types import TYPE_TO_TAG
from slidesmith.engine.style_extractor import (
    extract_sml_element_classes,
    extract_sml_text_classes,
)
from slidesmith.engine.units import format_pt

# Pattern to match XML-invalid control characters (except tab, newline, carriage return)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_text(text: str) -> str:
    """Remove XML-invalid control characters from text."""
    return _CONTROL_CHARS.sub("", text)


if TYPE_CHECKING:
    from slidesmith.engine.render_tree import RenderNode


def generate_slide_content(
    roots: list[RenderNode],
    slide_id: str = "s1",
) -> str:
    """Generate minimal SML content for a slide.

    Args:
        roots: Root nodes for this slide
        slide_id: The slide's clean ID (e.g., "s1")

    Returns:
        Minimal SML XML string with <Slide> root tag
    """
    lines: list[str] = []

    # Add Slide root tag
    lines.append(f'<Slide id="{slide_id}">')

    for root in roots:
        _generate_node(root, lines, indent=1)

    lines.append("</Slide>")

    return "\n".join(lines)


def generate_canonical_slide_content(root: Element) -> str:
    """Serialize an SML XML tree using generator-compatible formatting."""
    if root.tag != "Slide":
        raise ValueError("Invalid content.sml XML: expected a <Slide> root")
    lines: list[str] = []
    _generate_xml_element(root, lines, indent=0)
    return "\n".join(lines)


def _generate_xml_element(element: Element, lines: list[str], indent: int) -> None:
    prefix = "  " * indent
    opening = _xml_opening(element)

    if element.tag == "P":
        lines.append(f"{prefix}{_serialize_xml_paragraph(element)}")
        return

    children = list(element)
    if not children and element.tag != "Slide":
        lines.append(f"{prefix}{opening} />")
        return

    lines.append(f"{prefix}{opening}>")
    for child in children:
        _generate_xml_element(child, lines, indent + 1)
    lines.append(f"{prefix}</{element.tag}>")


def _serialize_xml_paragraph(paragraph: Element) -> str:
    opening = _xml_opening(paragraph)
    children = list(paragraph)
    if not children and not paragraph.text:
        return f"{opening} />"
    if not children:
        return f"{opening}>{escape(paragraph.text or '')}</P>"

    content: list[str] = []
    if paragraph.text and not _is_xml_formatting(paragraph.text):
        content.append(escape(paragraph.text))
    for child in children:
        attrs = _xml_attributes(child)
        attr_text = f" {attrs}" if attrs else ""
        content.append(
            f"<{child.tag}{attr_text}>{escape(child.text or '')}</{child.tag}>"
        )
        if child.tail and not _is_xml_formatting(child.tail):
            content.append(escape(child.tail))
    return f"{opening}>{''.join(content)}</P>"


def _xml_opening(element: Element) -> str:
    attrs = _xml_attributes(element)
    attr_text = f" {attrs}" if attrs else ""
    return f"<{element.tag}{attr_text}"


def _xml_attributes(element: Element) -> str:
    preferred = {
        "Slide": ("id",),
        "P": ("class",),
        "T": ("class", "auto-text"),
    }.get(element.tag, ("id", "x", "y", "w", "h", "class"))
    names = [name for name in preferred if name in element.attrib]
    names.extend(name for name in element.attrib if name not in names)
    return " ".join(
        f'{name}="{_escape_xml_attribute(element.attrib[name])}"' for name in names
    )


def _escape_xml_attribute(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def _is_xml_formatting(text: str) -> bool:
    return not text.strip() and ("\n" in text or "\r" in text)


def _generate_node(
    node: RenderNode,
    lines: list[str],
    indent: int,
) -> None:
    """Generate XML for a single node and its children."""
    if not node.clean_id:
        return

    prefix = "  " * indent
    tag = _get_tag_name(node)
    attrs = _build_attributes(node)

    # Check if this is a self-closing element (no text, no children)
    has_text = node.has_text
    has_children = bool(node.children)

    if not has_text and not has_children:
        # Self-closing tag
        lines.append(f"{prefix}<{tag}{attrs} />")
    else:
        # Opening tag
        lines.append(f"{prefix}<{tag}{attrs}>")

        # Add text content if present
        if has_text:
            _generate_text_content(node, lines, indent + 1)

        # Add children
        for child in node.children:
            _generate_node(child, lines, indent + 1)

        # Closing tag
        lines.append(f"{prefix}</{tag}>")


def _get_tag_name(node: RenderNode) -> str:
    """Get the XML tag name for an element type."""
    return TYPE_TO_TAG.get(node.element_type, node.element_type)


def _build_attributes(node: RenderNode) -> str:
    """Build attribute string for an element.

    All elements get absolute x, y, w, h positions.
    """
    attrs: list[str] = []

    # Always include clean ID
    attrs.append(f'id="{node.clean_id}"')

    # Absolute position for ALL elements
    bounds = node.bounds
    attrs.append(f'x="{format_pt(bounds.x)}"')
    attrs.append(f'y="{format_pt(bounds.y)}"')
    attrs.append(f'w="{format_pt(bounds.w)}"')
    attrs.append(f'h="{format_pt(bounds.h)}"')

    classes = extract_sml_element_classes(node)
    if classes:
        class_value = escape(" ".join(classes), {'"': "&quot;"})
        attrs.append(f'class="{class_value}"')

    return " " + " ".join(attrs)


def _generate_text_content(
    node: RenderNode,
    lines: list[str],
    indent: int,
) -> None:
    """Generate text content with paragraph structure."""
    prefix = "  " * indent

    shape = node.element.get("shape", {})
    text = shape.get("text", {})
    text_elements = text.get("textElements", [])

    if not text_elements:
        return

    element_classes = set(extract_sml_element_classes(node))

    # Group each paragraph marker with its following text and auto-text runs.
    paragraphs: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    current_marker: dict[str, Any] = {}
    current_para: list[dict[str, Any]] = []

    have_marker = False
    for te in text_elements:
        if "paragraphMarker" in te:
            if have_marker:
                paragraphs.append((current_marker, current_para))
            current_marker = te["paragraphMarker"]
            current_para = []
            have_marker = True
        elif "textRun" in te:
            current_para.append({"kind": "text", **te["textRun"]})
        elif "autoText" in te:
            current_para.append({"kind": "auto", **te["autoText"]})

    if have_marker:
        paragraphs.append((current_marker, current_para))

    # Generate paragraph elements. Character styles stay on their source runs;
    # they are not promoted to the element because that would change scope.
    for marker, para_runs in paragraphs:
        paragraph_style = ParagraphStyle.from_api(marker.get("style"))
        paragraph_classes = paragraph_style.to_classes() if paragraph_style else []
        run_class_sets = [
            extract_sml_text_classes(run.get("style"))
            for run in para_runs
            if _sanitize_text(run.get("content", "")).removesuffix("\n")
        ]
        if run_class_sets:
            paragraph_classes.extend(common_classes(run_class_sets))
        paragraph_classes = [
            cls for cls in paragraph_classes if cls not in element_classes
        ]
        paragraph_defaults = element_classes | set(paragraph_classes)

        segments: list[tuple[str, tuple[str, ...]]] = []
        for run_index, run in enumerate(para_runs):
            content = _sanitize_text(run.get("content", ""))
            if run_index == len(para_runs) - 1:
                content = content.removesuffix("\n")
            run_classes = tuple(
                cls
                for cls in extract_sml_text_classes(run.get("style"))
                if cls not in paragraph_defaults
            )
            auto_text_type = run.get("type") if run.get("kind") == "auto" else None
            segment_key = run_classes + (
                (f"auto:{auto_text_type}",) if auto_text_type else ()
            )
            if segments and segments[-1][1] == segment_key:
                previous, previous_classes = segments[-1]
                segments[-1] = (previous + content, previous_classes)
            else:
                segments.append((content, segment_key))

        if not segments:
            segments.append(("", ()))

        content_parts: list[str] = []
        for text, segment_key in segments:
            auto_text = next(
                (item[5:] for item in segment_key if item.startswith("auto:")),
                None,
            )
            run_classes = tuple(
                item for item in segment_key if not item.startswith("auto:")
            )
            escaped_text = escape(text)
            if run_classes or auto_text:
                attributes: list[str] = []
                class_value = escape(" ".join(run_classes), {'"': "&quot;"})
                if run_classes:
                    attributes.append(f'class="{class_value}"')
                if auto_text:
                    attributes.append(f'auto-text="{escape(auto_text)}"')
                content_parts.append(f'<T {" ".join(attributes)}>{escaped_text}</T>')
            else:
                content_parts.append(escaped_text)
        paragraph_attr = ""
        if paragraph_classes:
            class_value = escape(" ".join(paragraph_classes), {'"': "&quot;"})
            paragraph_attr = f' class="{class_value}"'
        content = "".join(content_parts)
        if content:
            lines.append(f"{prefix}<P{paragraph_attr}>{content}</P>")
        else:
            lines.append(f"{prefix}<P{paragraph_attr} />")
