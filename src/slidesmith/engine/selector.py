"""Semantic queries and atomic local edits for SML workspaces."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from slidesmith.engine.atomic_files import commit_text_files
from slidesmith.engine.components import load_components
from slidesmith.engine.content_parser import (
    ParsedElement,
    parse_element_classes,
    parse_slide_content,
)
from slidesmith.engine.json_utils import read_json
from slidesmith.engine.layout import compile_layout
from slidesmith.engine.selector_query import (  # noqa: F401
    Query,
    QueryContext,
    QueryParseError,
    parse_query,
)
from slidesmith.engine.sml_class_edit import (  # noqa: F401
    _Attribute,
    _attributes,
    _classes_by_id,
    _mutate_element_classes,
    _replace_class_attribute,
)

ROLES_FILE = "roles.json"


@dataclass(frozen=True)
class SelectorMatch:
    """One selected SML element."""

    slide_index: str
    slide_number: int
    element: ParsedElement
    classes: tuple[str, ...]
    role: str | None


@dataclass(frozen=True)
class ApplyResult:
    """Per-slide match and mutation counts from an apply operation."""

    matches: tuple[SelectorMatch, ...]
    match_counts: dict[str, int]
    mutation_counts: dict[str, int]

    @property
    def total_matches(self) -> int:
        return len(self.matches)

    @property
    def total_mutations(self) -> int:
        return sum(self.mutation_counts.values())


@dataclass(frozen=True)
class _SlideDocument:
    index: str
    number: int
    path: Path
    content: str
    elements: tuple[ParsedElement, ...]
    classes_by_id: dict[str, tuple[str, ...]]


def select_elements(folder_path: str | Path, query: str | Query) -> list[SelectorMatch]:
    """Select every matching element from the parsed workspace tree."""
    parsed_query = parse_query(query) if isinstance(query, str) else query
    folder = Path(folder_path)
    roles = _read_roles(folder)
    matches: list[SelectorMatch] = []
    for slide in _read_slides(folder):
        for element in slide.elements:
            classes = slide.classes_by_id.get(element.clean_id, ())
            role = roles.get(element.clean_id)
            context = QueryContext(
                slide_number=slide.number,
                element=element,
                classes=frozenset(classes),
                role=role,
            )
            if parsed_query.matches(context):
                matches.append(
                    SelectorMatch(
                        slide_index=slide.index,
                        slide_number=slide.number,
                        element=element,
                        classes=classes,
                        role=role,
                    )
                )
    return matches


def apply_to_elements(
    folder_path: str | Path,
    query: str | Query,
    *,
    add_classes: Sequence[str] = (),
    remove_classes: Sequence[str] = (),
    set_role: str | None = None,
    clear_role: bool = False,
    dry_run: bool = False,
) -> ApplyResult:
    """Apply local metadata/style changes after validating the complete result."""
    if set_role is not None and clear_role:
        raise ValueError("--set-role and --clear-role cannot be used together")
    if not add_classes and not remove_classes and set_role is None and not clear_role:
        raise ValueError(
            "apply requires --add-class, --remove-class, --set-role, or --clear-role"
        )
    additions = _unique_tokens(add_classes, label="--add-class")
    removals = _unique_tokens(remove_classes, label="--remove-class")
    for class_name in additions:
        parse_element_classes(class_name, "apply validation")
    if set_role is not None and not set_role:
        raise ValueError("--set-role requires a non-empty role")

    parsed_query = parse_query(query) if isinstance(query, str) else query
    folder = Path(folder_path)
    components = load_components(folder)
    roles = _read_roles(folder)
    slides = _read_slides(folder)
    matches: list[SelectorMatch] = []
    matches_by_slide: dict[str, list[SelectorMatch]] = {}
    for slide in slides:
        for element in slide.elements:
            classes = slide.classes_by_id.get(element.clean_id, ())
            role = roles.get(element.clean_id)
            context = QueryContext(slide.number, element, frozenset(classes), role)
            if parsed_query.matches(context):
                if not element.clean_id:
                    raise ValueError(
                        f"Cannot mutate matched <{element.tag}> on slide {slide.index}: "
                        "the element has no id"
                    )
                match = SelectorMatch(
                    slide.index,
                    slide.number,
                    element,
                    classes,
                    role,
                )
                matches.append(match)
                matches_by_slide.setdefault(slide.index, []).append(match)

    pending: dict[Path, str] = {}
    mutated_ids_by_slide: dict[str, set[str]] = {
        slide.index: set() for slide in slides
    }
    for slide in slides:
        selected_ids = {
            match.element.clean_id for match in matches_by_slide.get(slide.index, [])
        }
        expanded_ids = selected_ids - set(_classes_by_id(slide.content))
        if expanded_ids and (additions or removals):
            element_id = sorted(expanded_ids)[0]
            raise ValueError(
                f"Cannot mutate classes on component-expanded element '{element_id}'; "
                "edit components.sml or parameterize its class with a slot"
            )
        updated, changed_ids = _mutate_element_classes(
            slide.content,
            selected_ids,
            additions,
            removals,
        )
        # This is the existing parser and class-conflict validator used by diff.
        # Validate all prospective files before the first disk write.
        parse_slide_content(updated, components=components)
        if updated != slide.content:
            pending[slide.path] = updated
        mutated_ids_by_slide[slide.index].update(changed_ids)

    updated_roles = dict(roles)
    for match in matches:
        element_id = match.element.clean_id
        changed = False
        if set_role is not None and updated_roles.get(element_id) != set_role:
            updated_roles[element_id] = set_role
            changed = True
        elif clear_role and element_id in updated_roles:
            del updated_roles[element_id]
            changed = True
        if changed:
            mutated_ids_by_slide[match.slide_index].add(element_id)

    if updated_roles != roles:
        pending[folder / ROLES_FILE] = _serialize_roles(updated_roles)

    if not dry_run:
        commit_text_files(pending)

    match_counts = {
        slide.index: len(matches_by_slide.get(slide.index, [])) for slide in slides
    }
    mutation_counts = {
        slide.index: len(mutated_ids_by_slide[slide.index]) for slide in slides
    }
    return ApplyResult(tuple(matches), match_counts, mutation_counts)


def format_match(match: SelectorMatch) -> str:
    """Format one stable, compact CLI selection row."""
    text = " ".join("".join(match.element.paragraphs).split())
    if text:
        summary = f'text="{_shorten(text)}"'
    elif match.classes:
        summary = f'class="{_shorten(" ".join(match.classes))}"'
    else:
        summary = "(no text or classes)"
    return (
        f"slide {match.slide_number:02d}  {match.element.clean_id}  "
        f"<{match.element.tag}>  {summary}"
    )


def _shorten(value: str, limit: int = 64) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


def _unique_tokens(values: Sequence[str], *, label: str) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if not value or len(value.split()) != 1:
            raise ValueError(f"{label} must be exactly one whitespace-free class token")
        if value not in result:
            result.append(value)
    return tuple(result)


def _read_slides(folder: Path) -> list[_SlideDocument]:
    paths = sorted((folder / "slides").glob("*/content.sml"))
    if not paths:
        raise ValueError(f"No content.sml files found under {folder / 'slides'}")
    components = load_components(folder)
    slides: list[_SlideDocument] = []
    for path in paths:
        try:
            number = int(path.parent.name)
        except ValueError as exc:
            raise ValueError(
                f"Invalid slide folder '{path.parent.name}': expected a numeric index"
            ) from exc
        content = path.read_text(encoding="utf-8")
        compiled = compile_layout(content, components=components)
        roots = parse_slide_content(compiled)
        slides.append(
            _SlideDocument(
                index=path.parent.name,
                number=number,
                path=path,
                content=content,
                elements=tuple(_walk_elements(roots)),
                classes_by_id=_classes_by_id(compiled),
            )
        )
    return slides


def _walk_elements(elements: Sequence[ParsedElement]) -> Iterator[ParsedElement]:
    for element in elements:
        yield element
        yield from _walk_elements(element.children)


def _read_roles(folder: Path) -> dict[str, str]:
    path = folder / ROLES_FILE
    values = read_json(path, missing_ok=True)
    roles: dict[str, str] = {}
    for element_id, role in values.items():
        if not isinstance(role, str) or not role:
            raise ValueError(
                f"Invalid role for element '{element_id}' in {path}: expected a non-empty string"
            )
        roles[element_id] = role
    return roles


def _serialize_roles(roles: dict[str, str]) -> str:
    return json.dumps(roles, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
