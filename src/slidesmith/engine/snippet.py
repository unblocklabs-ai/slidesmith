"""Reusable, role-aware SML layout snippets."""

from __future__ import annotations

import copy
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

from slidesmith.engine.atomic_files import commit_text_files
from slidesmith.engine.components import load_components
from slidesmith.engine.content_parser import ParsedElement, parse_slide_content
from slidesmith.engine.layout import compile_layout
from slidesmith.engine.selector import (
    _read_roles,
    _read_slides,
    _serialize_roles,
    select_elements,
)
from slidesmith.engine.units import format_pt


SNIPPET_VERSION = 1


@dataclass(frozen=True)
class SnippetCopyResult:
    """Summary of a copied single-slide subtree selection."""

    path: Path
    slide_number: int
    elements: int
    width: float
    height: float


@dataclass(frozen=True)
class SnippetPasteResult:
    """Summary of one validated snippet insertion."""

    slide_index: str
    inserted_roots: int
    inserted_elements: int
    id_prefix: str
    mapped_roles: int


def copy_snippet(
    folder_path: str | Path,
    selector: str,
    output_path: str | Path,
) -> SnippetCopyResult:
    """Copy matched raw SML subtrees into an origin-relative snippet."""
    folder = Path(folder_path)
    matches = select_elements(folder, selector)
    if not matches:
        raise ValueError("Snippet selector matched no elements")
    slide_numbers = {match.slide_number for match in matches}
    if len(slide_numbers) != 1:
        raise ValueError("A snippet must select elements from exactly one source slide")
    slide_number = next(iter(slide_numbers))
    slide = next(slide for slide in _read_slides(folder) if slide.number == slide_number)
    try:
        source_root = DefusedET.fromstring(
            compile_layout(slide.content, components=load_components(folder))
        )
    except (ET.ParseError, DefusedXmlException) as exc:
        raise ValueError(f"Invalid content.sml XML at {slide.path}: {exc}") from exc
    elements_by_id = {
        element.get("id"): element
        for element in source_root.iter()
        if element.get("id")
    }
    selected_ids = {match.element.clean_id for match in matches}
    missing = selected_ids - elements_by_id.keys()
    if missing:
        element_id = sorted(missing)[0]
        raise ValueError(
            f"Cannot locate selected element '{element_id}' in the compiled source SML"
        )

    parents = {
        child: parent
        for parent in source_root.iter()
        for child in parent
        if child.tag != "P"
    }
    selected_roots = [
        element
        for element in source_root.iter()
        if element.get("id") in selected_ids
        and not _has_selected_ancestor(element, parents, selected_ids)
    ]
    selected_parsed = [
        match.element
        for match in matches
        if match.element.clean_id in {element.get("id") for element in selected_roots}
    ]
    bounds = _bounds(selected_parsed)
    min_x, min_y, max_x, max_y = bounds
    width = max_x - min_x
    height = max_y - min_y
    if width <= 0 or height <= 0:
        raise ValueError("Snippet selection must have a positive-width, positive-height box")

    roles = _read_roles(folder)
    snippet_root = ET.Element(
        "Snippet",
        {
            "version": str(SNIPPET_VERSION),
            "width": format_pt(width),
            "height": format_pt(height),
            "sourceSlide": str(slide_number),
        },
    )
    for selected_root in selected_roots:
        cloned = copy.deepcopy(selected_root)
        for element in cloned.iter():
            if x := element.get("x"):
                element.set("x", format_pt(float(x) - min_x))
            if y := element.get("y"):
                element.set("y", format_pt(float(y) - min_y))
            element_id = element.get("id")
            if element_id and element_id in roles:
                element.set("role", roles[element_id])
        snippet_root.append(cloned)

    ET.indent(snippet_root, space="  ")
    output = Path(output_path)
    commit_text_files(
        {output: ET.tostring(snippet_root, encoding="unicode", short_empty_elements=True) + "\n"}
    )
    return SnippetCopyResult(
        output,
        slide_number,
        sum(1 for root in selected_roots for element in root.iter() if element.tag not in {"P", "T"}),
        width,
        height,
    )


def paste_snippet(
    folder_path: str | Path,
    slide_number: int,
    snippet_path: str | Path,
    *,
    role_maps: Sequence[tuple[str, str]] = (),
    frame: tuple[float, float, float, float] | None = None,
    dry_run: bool = False,
) -> SnippetPasteResult:
    """Insert a snippet as new shapes, optionally filling role slots with text."""
    if slide_number < 1:
        raise ValueError("--slide must be at least 1")
    folder = Path(folder_path)
    slides = _read_slides(folder)
    destination = next(
        (slide for slide in slides if slide.number == slide_number),
        None,
    )
    if destination is None:
        raise ValueError(f"Destination slide {slide_number} does not exist")
    snippet_root = _load_snippet(snippet_path)
    snippet_width = _positive_number(snippet_root, "width")
    snippet_height = _positive_number(snippet_root, "height")
    target_frame = frame or (0.0, 0.0, snippet_width, snippet_height)
    _validate_frame(target_frame)
    x, y, width, height = target_frame

    roots = [copy.deepcopy(element) for element in snippet_root]
    if not roots:
        raise ValueError("Snippet contains no elements")
    snippet_ids = [
        element.get("id")
        for root in roots
        for element in root.iter()
        if element.tag not in {"P", "T"}
    ]
    if any(not element_id for element_id in snippet_ids):
        raise ValueError("Every pasted snippet element must have an id")
    if len(set(snippet_ids)) != len(snippet_ids):
        raise ValueError("Snippet element ids must be unique")

    maps = _validated_role_maps(role_maps)
    destination_roles = _read_roles(folder)
    destination_elements = {
        element.clean_id: element for element in destination.elements
    }
    for snippet_role, destination_role in maps.items():
        slots = [
            element
            for root in roots
            for element in root.iter()
            if element.get("role") == snippet_role
        ]
        if len(slots) != 1:
            raise ValueError(
                f"Snippet role '{snippet_role}' must identify exactly one slot; "
                f"found {len(slots)}"
            )
        sources = [
            destination_elements[element_id]
            for element_id, role in destination_roles.items()
            if role == destination_role and element_id in destination_elements
        ]
        if len(sources) != 1:
            raise ValueError(
                f"Destination role '{destination_role}' on slide {slide_number} "
                f"must identify exactly one text element; found {len(sources)}"
            )
        if not sources[0].paragraphs:
            raise ValueError(
                f"Destination role '{destination_role}' on slide {slide_number} "
                "has no text to map"
            )
        _replace_slot_text(slots[0], sources[0].paragraphs)

    existing_ids = {
        element.clean_id
        for slide in slides
        for element in slide.elements
        if element.clean_id
    }
    prefix = _next_prefix(existing_ids)
    new_roles = dict(destination_roles)
    scale_x = width / snippet_width
    scale_y = height / snippet_height
    inserted_elements = 0
    for root in roots:
        for element in root.iter():
            if element.tag not in {"P", "T"}:
                inserted_elements += 1
            _transform_geometry(element, x, y, scale_x, scale_y)
            element_id = element.get("id")
            role = element.attrib.pop("role", None)
            if element_id:
                remapped_id = f"{prefix}__{element_id}"
                element.set("id", remapped_id)
                if role is not None:
                    new_roles[remapped_id] = maps.get(role, role)

    insertion = _serialize_roots(roots)
    close = destination.content.rfind("</Slide>")
    if close == -1:
        raise ValueError(f"Destination slide {slide_number} has no </Slide> root")
    separator = "" if destination.content[:close].endswith("\n") else "\n"
    updated = (
        destination.content[:close]
        + separator
        + insertion
        + destination.content[close:]
    )
    parse_slide_content(updated, components=load_components(folder))

    pending = {destination.path: updated}
    if new_roles != destination_roles:
        pending[folder / "roles.json"] = _serialize_roles(new_roles)
    if not dry_run:
        commit_text_files(pending)
    return SnippetPasteResult(
        destination.index,
        len(roots),
        inserted_elements,
        prefix,
        len(maps),
    )


def parse_frame(value: str | None) -> tuple[float, float, float, float] | None:
    """Parse ``X,Y,W,H`` from the paste CLI."""
    if value is None:
        return None
    pieces = value.split(",")
    if len(pieces) != 4:
        raise ValueError("--frame must use X,Y,W,H point values")
    try:
        frame = tuple(float(piece) for piece in pieces)
    except ValueError as exc:
        raise ValueError("--frame must use X,Y,W,H point values") from exc
    result = (frame[0], frame[1], frame[2], frame[3])
    _validate_frame(result)
    return result


def _load_snippet(path: str | Path) -> ET.Element:
    snippet_path = Path(path)
    try:
        root = DefusedET.fromstring(snippet_path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError, DefusedXmlException) as exc:
        raise ValueError(f"Invalid snippet at {snippet_path}: {exc}") from exc
    if root.tag != "Snippet" or root.get("version") != str(SNIPPET_VERSION):
        raise ValueError(
            f"Invalid snippet at {snippet_path}: expected Snippet version {SNIPPET_VERSION}"
        )
    return root


def _positive_number(element: ET.Element, attribute: str) -> float:
    try:
        value = float(element.get(attribute, ""))
    except ValueError as exc:
        raise ValueError(f"Snippet {attribute} must be a positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"Snippet {attribute} must be a positive number")
    return value


def _validate_frame(frame: tuple[float, float, float, float]) -> None:
    x, y, width, height = frame
    if not all(math.isfinite(value) for value in frame) or width <= 0 or height <= 0:
        raise ValueError("--frame X,Y,W,H values must be finite with positive W and H")


def _validated_role_maps(
    role_maps: Sequence[tuple[str, str]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for snippet_role, destination_role in role_maps:
        if not snippet_role or not destination_role:
            raise ValueError("--map must use non-empty SNIPPET_ROLE:DESTINATION_ROLE")
        if snippet_role in result:
            raise ValueError(f"Snippet role '{snippet_role}' appears in more than one --map")
        result[snippet_role] = destination_role
    return result


def _has_selected_ancestor(
    element: ET.Element,
    parents: dict[ET.Element, ET.Element],
    selected_ids: set[str],
) -> bool:
    parent = parents.get(element)
    while parent is not None:
        if parent.get("id") in selected_ids:
            return True
        parent = parents.get(parent)
    return False


def _walk_parsed(elements: Iterable[ParsedElement]) -> Iterable[ParsedElement]:
    for element in elements:
        yield element
        yield from _walk_parsed(element.children)


def _bounds(elements: Sequence[ParsedElement]) -> tuple[float, float, float, float]:
    bounded = [
        element
        for element in _walk_parsed(elements)
        if element.x is not None
        and element.y is not None
        and element.w is not None
        and element.h is not None
    ]
    if not bounded:
        raise ValueError("Snippet selection has no elements with complete geometry")
    return (
        min(element.x for element in bounded if element.x is not None),
        min(element.y for element in bounded if element.y is not None),
        max(
            element.x + element.w
            for element in bounded
            if element.x is not None and element.w is not None
        ),
        max(
            element.y + element.h
            for element in bounded
            if element.y is not None and element.h is not None
        ),
    )


def _replace_slot_text(element: ET.Element, paragraphs: Sequence[str]) -> None:
    templates = list(element.findall("P"))
    if not templates:
        raise ValueError(
            f"Snippet role '{element.get('role')}' is not a text slot (it has no P)"
        )
    for paragraph in templates:
        element.remove(paragraph)
    for index, text in enumerate(paragraphs):
        template = templates[min(index, len(templates) - 1)]
        replacement = ET.Element("P", dict(template.attrib))
        first_run = next((child for child in template if child.tag == "T"), None)
        if first_run is None:
            replacement.text = text
        else:
            run = ET.SubElement(replacement, "T", dict(first_run.attrib))
            run.text = text
        element.append(replacement)


def _next_prefix(existing_ids: set[str]) -> str:
    number = 1
    while any(
        element_id == f"snippet_{number}"
        or element_id.startswith(f"snippet_{number}__")
        for element_id in existing_ids
    ):
        number += 1
    return f"snippet_{number}"


def _transform_geometry(
    element: ET.Element,
    x: float,
    y: float,
    scale_x: float,
    scale_y: float,
) -> None:
    if value := element.get("x"):
        element.set("x", format_pt(x + float(value) * scale_x))
    if value := element.get("y"):
        element.set("y", format_pt(y + float(value) * scale_y))
    if value := element.get("w"):
        element.set("w", format_pt(float(value) * scale_x))
    if value := element.get("h"):
        element.set("h", format_pt(float(value) * scale_y))


def _serialize_roots(roots: Sequence[ET.Element]) -> str:
    wrapper = ET.Element("Wrapper")
    wrapper.extend(roots)
    ET.indent(wrapper, space="  ")
    lines = ET.tostring(wrapper, encoding="unicode").splitlines()
    return "\n".join(f"  {line[2:] if line.startswith('  ') else line}" for line in lines[1:-1]) + "\n"
