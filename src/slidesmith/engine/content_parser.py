"""Parser for the minimal SML content format.

Parses content.sml files back into structured data for diffing.
Format: <Slide id="s1">...</Slide> with all elements having absolute positions.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import math
from dataclasses import dataclass, field
from pathlib import Path

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

from slidesmith.engine.classes import (
    ClassKind,
    ContentAlignment,
    Fill,
    ParagraphStyle,
    Stroke,
    TextStyle,
    classify_class,
    parse_class_string,
    parse_fill_class,
    parse_paragraph_style_classes,
    parse_stroke_classes,
    parse_text_style_classes,
    validate_mutually_exclusive_classes,
)
from slidesmith.engine.components import ComponentLibrary, load_components
from slidesmith.engine.layout import compile_layout
from slidesmith.engine.image_fetch import validate_public_image_url

QA_ACCEPT_CLASS_PREFIX = "qa-accept-"


@dataclass
class ElementStyles:
    """Styles parsed from an element's class attribute.

    Each field holds the classes.py data structure for one property group,
    or None if no classes of that group were present.
    """

    fill: Fill | None = None
    stroke: Stroke | None = None
    text_style: TextStyle | None = None
    paragraph_style: ParagraphStyle | None = None
    content_alignment: ContentAlignment | None = None


@dataclass
class ParsedRun:
    """A single text run within a paragraph.

    Plain <P>text</P> paragraphs produce one unstyled run; <T class="...">
    children produce styled runs.
    """

    text: str
    text_style: TextStyle | None = None
    auto_text_type: str | None = None


@dataclass
class ParagraphStyles:
    """Paragraph-scoped defaults parsed from one ``<P class>`` attribute."""

    text_style: TextStyle | None = None
    paragraph_style: ParagraphStyle | None = None


@dataclass
class ParsedElement:
    """A parsed element from content.sml."""

    # Clean ID
    clean_id: str

    # Element tag (Rect, TextBox, Group, etc.)
    tag: str

    # Absolute position (all elements have position now)
    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None

    # Authored image source and fit mode. Pulled images omit both and retain
    # their existing styles.json-backed representation.
    src: str | None = None
    fit: str | None = None

    # Text content (list of paragraph texts)
    paragraphs: list[str] = field(default_factory=list)

    # Text content as styled runs (parallel to paragraphs; one list per paragraph)
    runs: list[list[ParsedRun]] = field(default_factory=list)

    # Explicit defaults from each <P class>, parallel to paragraphs/runs.
    paragraph_styles: list[ParagraphStyles | None] = field(default_factory=list)

    # Styles parsed from the class attribute (None if no class attribute)
    styles: ElementStyles | None = None

    # Children
    children: list[ParsedElement] = field(default_factory=list)

    # Parent ID (for tree reconstruction)
    parent_id: str | None = None

    @property
    def has_position(self) -> bool:
        """Check if this element has position attributes.

        For copies, root has x/y but may omit w/h.
        """
        return self.x is not None


def parse_slide_content(
    content: str,
    *,
    components: ComponentLibrary | None = None,
) -> list[ParsedElement]:
    """Parse a slide's content.sml into structured elements.

    Args:
        content: The content.sml XML string (should have <Slide> root)

    Returns:
        List of root ParsedElement objects (children of <Slide>)
    """
    if not content.strip():
        return []

    content = compile_layout(content, components=components)

    try:
        root = DefusedET.fromstring(content)
    except DefusedXmlException as e:
        raise ValueError(f"Invalid content.sml XML: {e}") from e
    except ET.ParseError:
        # Deliberately retained migration path: pre-Slide-root workspaces remain
        # editable and can be normalized by the next pull/materialization.
        try:
            wrapped = f"<Root>{content}</Root>"
            root = DefusedET.fromstring(wrapped)
            return [_parse_element(child, None) for child in root]
        except (ET.ParseError, DefusedXmlException) as e:
            raise ValueError(f"Invalid content.sml XML: {e}") from e

    # If root is <Slide>, parse its children
    if root.tag == "Slide":
        return [_parse_element(child, None) for child in root]

    # Otherwise treat root as a single element (shouldn't happen with new format)
    return [_parse_element(root, None)]


def _parse_element(elem: ET.Element, parent_id: str | None) -> ParsedElement:
    """Parse a single XML element."""
    clean_id = elem.get("id", "")

    # Parse position attributes (all elements have absolute position now)
    x = _parse_float(elem.get("x"), clean_id, "x")
    y = _parse_float(elem.get("y"), clean_id, "y")
    w = _parse_float(elem.get("w"), clean_id, "w")
    h = _parse_float(elem.get("h"), clean_id, "h")

    src, fit = _parse_image_authoring(elem, clean_id)
    if src is not None:
        validate_authored_image_geometry(clean_id, x=x, y=y, w=w, h=h)

    # Parse the class attribute into typed styles (fails loudly on unknown classes)
    styles = parse_element_classes(elem.get("class"), clean_id, elem.tag)

    # Parse text paragraphs (plain text or nested <T> runs)
    paragraphs: list[str] = []
    runs: list[list[ParsedRun]] = []
    paragraph_styles: list[ParagraphStyles | None] = []
    for p_elem in elem.findall("P"):
        para_runs = _parse_paragraph_runs(p_elem, clean_id)
        para_text = "".join(run.text for run in para_runs)
        paragraphs.append(para_text)
        runs.append(para_runs)
        paragraph_styles.append(
            _parse_paragraph_classes(p_elem.get("class"), clean_id)
        )

    # Parse children (excluding P elements)
    children = []
    for child in elem:
        if child.tag != "P":
            children.append(_parse_element(child, clean_id))

    return ParsedElement(
        clean_id=clean_id,
        tag=elem.tag,
        x=x,
        y=y,
        w=w,
        h=h,
        src=src,
        fit=fit,
        paragraphs=paragraphs,
        runs=runs,
        paragraph_styles=paragraph_styles,
        styles=styles,
        children=children,
        parent_id=parent_id,
    )


def _parse_image_authoring(
    elem: ET.Element,
    element_id: str,
) -> tuple[str | None, str | None]:
    """Parse authored Image-only source metadata without changing pulled images."""
    src = elem.get("src")
    fit = elem.get("fit")

    if elem.tag != "Image":
        if src is not None or fit is not None:
            attribute = "src" if src is not None else "fit"
            raise ValueError(
                f"Invalid '{attribute}' attribute on <{elem.tag}> element "
                f"'{element_id}': only <Image> supports src and fit"
            )
        return None, None

    # Pulled images intentionally have neither attribute; their content URL
    # and copy-time data continue to live in styles.json.
    if src is None:
        if fit is not None:
            raise ValueError(
                f"Invalid fit on Image element '{element_id}': fit requires src"
            )
        return None, None

    try:
        validate_public_image_url(src, resolve_host=False)
    except ValueError as exc:
        raise ValueError(
            f"Invalid src on Image element '{element_id}': {exc}, got {src!r}"
        ) from exc

    resolved_fit = fit or "stretch"
    if resolved_fit not in {"stretch", "contain"}:
        raise ValueError(
            f"Invalid fit {resolved_fit!r} on Image element '{element_id}': "
            "expected 'stretch' or 'contain'"
        )
    return src, resolved_fit


def validate_authored_image_geometry(
    element_id: str,
    *,
    x: float | None,
    y: float | None,
    w: float | None,
    h: float | None,
) -> None:
    """Require explicit usable geometry for an authored image source."""
    geometry = {"x": x, "y": y, "w": w, "h": h}
    invalid = [
        name
        for name, value in geometry.items()
        if value is None or not math.isfinite(value) or value <= 0
    ]
    if invalid:
        raise ValueError(
            f"Image element '{element_id}' requires finite, strictly-positive "
            f"x/y/w/h; invalid: {', '.join(invalid)}"
        )


def _parse_paragraph_runs(p_elem: ET.Element, element_id: str) -> list[ParsedRun]:
    """Parse a <P> element into text runs.

    A <P> may contain plain text, <T class="...">text</T> runs, or a mix
    (leading text plus runs with tail text). Plain segments become unstyled
    runs so `"".join(run.text)` always reconstructs the paragraph text.
    """
    runs: list[ParsedRun] = []

    # Pretty-printers put indentation before the first <T> and in each <T>'s
    # tail. Generator-emitted mixed content stays inline, so only newline-bearing
    # whitespace in these between-element positions is formatting. Plain <P>
    # text and <T> text are always significant, including outer spaces.
    if p_elem.text and not (
        len(p_elem) and _is_inter_element_formatting(p_elem.text)
    ):
        runs.append(ParsedRun(text=p_elem.text))

    for child in p_elem:
        if child.tag != "T":
            raise ValueError(
                f"Unsupported element <{child.tag}> inside <P> of element "
                f"'{element_id}': only <T> runs are allowed"
            )
        text_style = _parse_run_classes(child.get("class"), element_id)
        auto_text_type = child.get("auto-text")
        if child.text:
            runs.append(
                ParsedRun(
                    text=child.text,
                    text_style=text_style,
                    auto_text_type=auto_text_type,
                )
            )
        elif auto_text_type:
            runs.append(ParsedRun(text="", auto_text_type=auto_text_type))
        if child.tail and not _is_inter_element_formatting(child.tail):
            runs.append(ParsedRun(text=child.tail))

    return runs


def _is_inter_element_formatting(text: str) -> bool:
    """Return whether mixed-content whitespace came from line formatting."""
    return not text.strip() and ("\n" in text or "\r" in text)


def parse_element_classes(
    class_str: str | None,
    element_id: str,
    element_tag: str | None = None,
) -> ElementStyles | None:
    """Parse an element's class attribute into typed styles.

    Uses the conversion functions in classes.py as the single source of truth.
    Raises ValueError for any class not recognized by classes.py, naming the
    class and the element id.

    Args:
        class_str: The raw class attribute value (may be None)
        element_id: The element's clean id (for error messages)

    Returns:
        ElementStyles, or None if there was no class attribute
    """
    if class_str is None:
        return None

    classes = [
        cls
        for cls in parse_class_string(class_str)
        if not cls.startswith(QA_ACCEPT_CLASS_PREFIX)
    ]
    validate_mutually_exclusive_classes(classes, element_id)

    fill_classes: list[str] = []
    stroke_classes: list[str] = []
    text_classes: list[str] = []
    para_classes: list[str] = []
    content_alignment: ContentAlignment | None = None

    for cls in classes:
        classified = classify_class(cls)
        if classified is None:
            raise ValueError(
                f"Unrecognized class '{cls}' on element '{element_id}': "
                "not a known shape, fill, stroke, text, or paragraph class"
            )
        kind, parsed = classified
        if kind == ClassKind.CONTENT_ALIGNMENT:
            content_alignment = parsed
        elif kind == ClassKind.FILL:
            if element_tag == "Line":
                raise ValueError(
                    f"Invalid class '{cls}' on Line element '{element_id}': "
                    "fill classes are not supported on Line elements"
                )
            fill_classes.append(cls)
        elif kind == ClassKind.STROKE:
            stroke_classes.append(cls)
        elif kind == ClassKind.TEXT:
            text_classes.append(cls)
        elif kind == ClassKind.PARAGRAPH:
            para_classes.append(cls)

    return ElementStyles(
        fill=parse_fill_class(fill_classes[-1]) if fill_classes else None,
        stroke=parse_stroke_classes(stroke_classes) if stroke_classes else None,
        text_style=parse_text_style_classes(text_classes) if text_classes else None,
        paragraph_style=parse_paragraph_style_classes(para_classes)
        if para_classes
        else None,
        content_alignment=content_alignment,
    )


def _parse_run_classes(class_str: str | None, element_id: str) -> TextStyle | None:
    """Parse a <T> run's class attribute into a TextStyle.

    Only text-style classes are valid on runs; anything else fails loudly.
    """
    if class_str is None:
        return None

    classes = parse_class_string(class_str)
    validate_mutually_exclusive_classes(classes, element_id, scope="<T> run")
    for cls in classes:
        classified = classify_class(cls)
        if classified is None or classified[0] != ClassKind.TEXT:
            raise ValueError(
                f"Unrecognized class '{cls}' on <T> run in element "
                f"'{element_id}': only text-style classes are allowed on runs"
            )

    text_style = parse_text_style_classes(classes)
    return text_style if text_style != TextStyle() else None


def _parse_paragraph_classes(
    class_str: str | None, element_id: str
) -> ParagraphStyles | None:
    """Parse paragraph- and text-family defaults from a ``<P class>``."""
    if class_str is None:
        return None

    classes = parse_class_string(class_str)
    validate_mutually_exclusive_classes(classes, element_id, scope="<P>")

    text_classes: list[str] = []
    paragraph_classes: list[str] = []
    for cls in classes:
        classified = classify_class(cls)
        if classified is not None and classified[0] == ClassKind.TEXT:
            text_classes.append(cls)
        elif classified is not None and classified[0] == ClassKind.PARAGRAPH:
            paragraph_classes.append(cls)
        else:
            raise ValueError(
                f"Unrecognized class '{cls}' on <P> in element '{element_id}': "
                "only text- and paragraph-style classes are allowed on paragraphs"
            )

    parsed = ParagraphStyles(
        text_style=parse_text_style_classes(text_classes) if text_classes else None,
        paragraph_style=parse_paragraph_style_classes(paragraph_classes)
        if paragraph_classes
        else None,
    )
    return parsed if parsed != ParagraphStyles() else None


def _parse_float(
    value: str | None,
    element_id: str = "",
    attribute: str = "position",
) -> float | None:
    """Parse a position attribute, failing loudly on malformed author input."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid {attribute} value {value!r} on element "
            f"'{element_id}': expected a number"
        ) from exc


def flatten_elements(roots: list[ParsedElement]) -> dict[str, ParsedElement]:
    """Flatten parsed elements into a dictionary by clean_id.

    Args:
        roots: List of root ParsedElement objects

    Returns:
        Dictionary mapping clean_id to ParsedElement
    """
    result: dict[str, ParsedElement] = {}

    def _collect(elem: ParsedElement) -> None:
        if elem.clean_id:
            result[elem.clean_id] = elem
        for child in elem.children:
            _collect(child)

    for root in roots:
        _collect(root)

    return result


def parse_all_slides(slides_dir: str) -> dict[str, list[ParsedElement]]:
    """Parse all slide content files in a directory.

    Args:
        slides_dir: Path to the slides/ directory

    Returns:
        Dictionary mapping slide index (e.g., "01") to list of ParsedElement
    """
    slides_path = Path(slides_dir)
    components = load_components(slides_path.parent)
    result: dict[str, list[ParsedElement]] = {}

    for slide_folder in sorted(slides_path.iterdir()):
        if slide_folder.is_dir():
            content_file = slide_folder / "content.sml"
            if content_file.exists():
                content = content_file.read_text(encoding="utf-8")
                result[slide_folder.name] = parse_slide_content(
                    content, components=components
                )

    return result
