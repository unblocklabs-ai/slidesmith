"""Theme application planning, preview, and lexical content rewriting."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Sequence

from defusedxml import ElementTree as DefusedET

from slidesmith.engine.atomic_files import commit_text_files
from slidesmith.engine.class_replacement import _start_tag_spans
from slidesmith.engine.classes import ClassKind, classify_class
from slidesmith.engine.components import load_components
from slidesmith.engine.content_parser import parse_slide_content
from slidesmith.engine.selector import (
    _attributes,
    _read_roles,
    _read_slides,
    _replace_class_attribute,
)

from .color_mapping import COLOR_DISTANCE_THRESHOLD, _map_color_class_detail
from .theme_schema import (
    ThemeApplyResult,
    ThemeElementPreview,
    _primary_font_class,
    parse_slide_spec,
)


def apply_theme(
    folder_path: str | Path,
    theme: dict[str, Any],
    *,
    to_slides: str | None = None,
    map_colors: bool = False,
    dry_run: bool = False,
    color_distance_threshold: float = COLOR_DISTANCE_THRESHOLD,
) -> ThemeApplyResult:
    """Apply role styles, font unification, and optional palette mapping."""
    folder = Path(folder_path)
    selected = parse_slide_spec(to_slides)
    components = load_components(folder)
    roles = _read_roles(folder)
    slides = _read_slides(folder)
    target_slides = [
        slide for slide in slides if selected is None or slide.number in selected
    ]
    if not target_slides:
        raise ValueError("Theme application selected no slides")

    role_classes = {
        role: tuple(style["classes"])
        for role, style in theme.get("roles", {}).items()
    }
    primary_font = _primary_font_class(theme["tokens"])
    palette = tuple(color.lower() for color in theme["tokens"].get("palette", []))
    if map_colors and not palette:
        raise ValueError("--map-colors requires at least one RGB color in the theme palette")
    if not math.isfinite(color_distance_threshold) or color_distance_threshold < 0:
        raise ValueError("Color distance threshold must be finite and non-negative")

    pending: dict[Path, str] = {}
    slide_counts: dict[str, dict[str, int]] = {}
    previews: list[ThemeElementPreview] = []
    for slide in target_slides:
        raw_ids = _raw_element_ids(slide.content)
        expanded_role_ids = {
            element.clean_id
            for element in slide.elements
            if roles.get(element.clean_id) in role_classes
            and element.clean_id not in raw_ids
        }
        if expanded_role_ids:
            element_id = sorted(expanded_role_ids)[0]
            raise ValueError(
                f"Cannot role-restyle component-expanded element '{element_id}'; "
                "edit components.sml or parameterize its class with a slot"
            )
        updated, counts, element_previews = _apply_to_content(
            slide.content,
            roles=roles,
            role_classes=role_classes,
            primary_font=primary_font,
            palette=palette if map_colors else (),
            color_distance_threshold=color_distance_threshold,
        )
        previews.extend(
            ThemeElementPreview(slide.index, element_id, old, new, notes)
            for element_id, old, new, notes in element_previews
        )
        # The normal parser owns class-scope and conflict validation. Validate every
        # prospective target before committing any of them.
        parse_slide_content(updated, components=components)
        slide_counts[slide.index] = counts
        if updated != slide.content:
            pending[slide.path] = updated

    if not dry_run:
        commit_text_files(pending)
    return ThemeApplyResult(slide_counts, tuple(previews))


def _raw_element_ids(content: str) -> set[str]:
    result: set[str] = set()
    for start, end, tag_name in _start_tag_spans(content):
        if tag_name in {"Slide", "P", "T"}:
            continue
        start_tag = content[start:end]
        for attribute in _attributes(start_tag, tag_name):
            if attribute.name == "id":
                result.add(start_tag[attribute.value_start : attribute.value_end])
    return result


def _text_element_ids(content: str) -> set[str]:
    try:
        root = DefusedET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid content.sml XML: {exc}") from exc
    return {
        element.get("id", "")
        for element in root.iter()
        if element.tag not in {"Slide", "P", "T"}
        and element.get("id")
        and any(child.tag == "P" for child in element)
    }


def _apply_to_content(
    content: str,
    *,
    roles: dict[str, str],
    role_classes: dict[str, tuple[str, ...]],
    primary_font: str | None,
    palette: Sequence[str],
    color_distance_threshold: float,
) -> tuple[
    str,
    dict[str, int],
    list[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]],
]:
    text_ids = _text_element_ids(content)
    pieces: list[str] = []
    cursor = 0
    counts = {"roleRestyles": 0, "fontChanges": 0, "colorChanges": 0, "changed": 0}
    previews: list[
        tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]
    ] = []
    changed_tags = 0
    for start, end, tag_name in _start_tag_spans(content):
        pieces.append(content[cursor:start])
        start_tag = content[start:end]
        attributes = _attributes(start_tag, tag_name)
        by_name = {attribute.name: attribute for attribute in attributes}
        id_attribute = by_name.get("id")
        element_id = (
            start_tag[id_attribute.value_start : id_attribute.value_end]
            if id_attribute is not None
            else None
        )
        class_attribute = by_name.get("class")
        old_classes = (
            start_tag[
                class_attribute.value_start : class_attribute.value_end
            ].split()
            if class_attribute is not None
            else []
        )
        new_classes = list(old_classes)

        canonical = role_classes.get(roles.get(element_id, ""))
        if canonical is not None and tag_name not in {"Slide", "P", "T"}:
            preserved = [
                value for value in old_classes if value.startswith("qa-accept-")
            ]
            new_classes = [*canonical, *preserved]
            if new_classes != old_classes:
                counts["roleRestyles"] += 1

        if primary_font is not None:
            has_font = any(_is_font_family_class(value) for value in new_classes)
            should_add = element_id in text_ids and tag_name not in {"P", "T"}
            if has_font or should_add:
                replaced = [
                    primary_font if _is_font_family_class(value) else value
                    for value in new_classes
                ]
                if not has_font:
                    replaced.append(primary_font)
                replaced = _dedupe(replaced)
                if replaced != new_classes:
                    counts["fontChanges"] += 1
                new_classes = replaced

        if palette:
            mapped: list[str] = []
            color_notes: list[str] = []
            for class_name in new_classes:
                replacement, note = _map_color_class_detail(
                    class_name,
                    palette,
                    threshold=color_distance_threshold,
                )
                if replacement != class_name:
                    counts["colorChanges"] += 1
                if note is not None:
                    color_notes.append(note)
                mapped.append(replacement)
            new_classes = mapped
        else:
            color_notes = []

        if new_classes != old_classes:
            start_tag = _replace_class_attribute(start_tag, class_attribute, new_classes)
            changed_tags += 1
        if element_id is not None and (new_classes != old_classes or color_notes):
            previews.append(
                (
                    element_id,
                    tuple(old_classes),
                    tuple(new_classes),
                    tuple(color_notes),
                )
            )
        pieces.append(start_tag)
        cursor = end
    pieces.append(content[cursor:])
    counts["changed"] = changed_tags
    return "".join(pieces), counts, previews


def _is_font_family_class(class_name: str) -> bool:
    classified = classify_class(class_name)
    return bool(
        classified is not None
        and classified[0] == ClassKind.TEXT
        and classified[1].font_family is not None
    )


def _dedupe(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result
