"""Semantics-preserving canonical formatting for SML workspaces."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

from slidesmith.engine.content_generator import generate_canonical_slide_content
from slidesmith.engine.components import ComponentLibrary, load_components
from slidesmith.engine.content_parser import parse_slide_content


@dataclass(frozen=True)
class FormatResult:
    """Files inspected and changed by one formatting pass."""

    paths: tuple[Path, ...]
    changed_paths: tuple[Path, ...]


def format_slide_content(
    content: str, *, components: ComponentLibrary | None = None
) -> str:
    """Canonicalize one SML document without changing parsed semantics."""
    before = parse_slide_content(content, components=components)
    try:
        root = DefusedET.fromstring(content)
    except (ET.ParseError, DefusedXmlException):
        try:
            legacy_root = DefusedET.fromstring(f"<Root>{content}</Root>")
        except (ET.ParseError, DefusedXmlException) as exc:
            raise ValueError(f"Invalid content.sml XML: {exc}") from exc
        root = ET.Element("Slide", {"id": "s1"})
        root.extend(legacy_root)
    if root.tag != "Slide":
        slide_root = ET.Element("Slide", {"id": "s1"})
        slide_root.append(root)
        root = slide_root

    formatted = generate_canonical_slide_content(root)
    after = parse_slide_content(formatted, components=components)
    assert before == after, "SML formatter changed parsed semantics"
    return formatted


def format_folder(folder: str | Path, *, check: bool = False) -> FormatResult:
    """Format every ``slides/NN/content.sml`` in a presentation folder."""
    root = Path(folder)
    components = load_components(root)
    paths = tuple(sorted((root / "slides").glob("*/content.sml")))
    if not paths:
        raise ValueError(f"No content.sml files found under {root / 'slides'}")

    pending: list[tuple[Path, str]] = []
    for path in paths:
        content = path.read_text(encoding="utf-8")
        formatted = format_slide_content(content, components=components)
        if formatted != content:
            pending.append((path, formatted))

    if not check:
        for path, formatted in pending:
            path.write_text(formatted, encoding="utf-8")

    return FormatResult(
        paths=paths,
        changed_paths=tuple(path for path, _ in pending),
    )
