"""Extract and safely apply cross-deck design-language themes."""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from defusedxml import ElementTree as DefusedET

from slidesmith.engine.class_replacement import _start_tag_spans
from slidesmith.engine.classes import ClassKind, classify_class
from slidesmith.engine.components import load_components
from slidesmith.engine.content_parser import parse_element_classes, parse_slide_content
from slidesmith.engine.layout import compile_layout
from slidesmith.engine.selector import (
    _attributes,
    _commit_text_files,
    _read_roles,
    _read_slides,
    _replace_class_attribute,
)


THEME_VERSION = 1
COLOR_DISTANCE_THRESHOLD = 48.0
_COLOR_CLASS = re.compile(
    r"^(?P<use>fill|stroke|text-color|bg)-(?P<color>#[0-9a-fA-F]{6})"
    r"(?P<alpha>/[0-9]+)?$"
)


@dataclass(frozen=True)
class ThemeApplyResult:
    """Counts for one validated theme application."""

    slide_counts: dict[str, dict[str, int]]

    @property
    def role_restyles(self) -> int:
        return sum(values["roleRestyles"] for values in self.slide_counts.values())

    @property
    def font_changes(self) -> int:
        return sum(values["fontChanges"] for values in self.slide_counts.values())

    @property
    def color_changes(self) -> int:
        return sum(values["colorChanges"] for values in self.slide_counts.values())

    @property
    def changed_slides(self) -> int:
        return sum(values["changed"] > 0 for values in self.slide_counts.values())


def parse_slide_spec(value: str | None) -> set[int] | None:
    """Parse an inclusive CLI slide set such as ``1-3`` or ``1,3,5-7``."""
    if value is None:
        return None
    selected: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"Invalid slide range {value!r}")
        if "-" in part:
            pieces = part.split("-")
            if len(pieces) != 2 or not all(piece.isdigit() for piece in pieces):
                raise ValueError(f"Invalid slide range {value!r}; expected e.g. 1-3")
            first, last = (int(piece) for piece in pieces)
            if first < 1 or last < first:
                raise ValueError(f"Invalid slide range {value!r}; expected e.g. 1-3")
            selected.update(range(first, last + 1))
        elif part.isdigit() and int(part) >= 1:
            selected.add(int(part))
        else:
            raise ValueError(f"Invalid slide range {value!r}; expected e.g. 1-3")
    return selected


def extract_theme(
    folder_path: str | Path,
    *,
    from_slides: str | None = None,
) -> dict[str, Any]:
    """Derive a readable design-language token set from local SML."""
    folder = Path(folder_path)
    selected = parse_slide_spec(from_slides)
    roles = _read_roles(folder)
    slides = [
        slide
        for slide in _read_slides(folder)
        if selected is None or slide.number in selected
    ]
    if not slides:
        raise ValueError("Theme extraction selected no slides")

    palette_counts: Counter[str] = Counter()
    palette_uses: dict[str, Counter[str]] = defaultdict(Counter)
    family_counts: Counter[tuple[str, str]] = Counter()
    size_counts: Counter[tuple[float, str]] = Counter()
    role_styles: dict[str, Counter[tuple[str, ...]]] = defaultdict(Counter)
    role_elements: dict[str, list[str]] = defaultdict(list)

    components = load_components(folder)
    for slide in slides:
        compiled = compile_layout(slide.content, components=components)
        for class_name in _class_tokens(compiled):
            color_match = _COLOR_CLASS.fullmatch(class_name)
            if color_match:
                color = color_match.group("color").lower()
                palette_counts[color] += 1
                palette_uses[color][color_match.group("use")] += 1
            classified = classify_class(class_name)
            if classified is None or classified[0] != ClassKind.TEXT:
                continue
            text_style = classified[1]
            if text_style.font_family is not None:
                family_counts[(text_style.font_family, class_name)] += 1
            if text_style.font_size_pt is not None:
                size_counts[(text_style.font_size_pt, class_name)] += 1

        for element in slide.elements:
            role = roles.get(element.clean_id)
            if role is None:
                continue
            classes = tuple(
                class_name
                for class_name in slide.classes_by_id.get(element.clean_id, ())
                if not class_name.startswith("qa-accept-")
            )
            role_styles[role][classes] += 1
            role_elements[role].append(element.clean_id)

    palette = sorted(
        palette_counts,
        key=lambda color: (-palette_counts[color], color),
    )
    families = sorted(
        family_counts,
        key=lambda item: (-family_counts[item], item[0].casefold(), item[1]),
    )
    sizes = sorted(size_counts, key=lambda item: (-item[0], item[1]))
    role_map: dict[str, dict[str, Any]] = {}
    for role in sorted(role_styles):
        canonical, count = sorted(
            role_styles[role].items(),
            key=lambda item: (-item[1], item[0]),
        )[0]
        role_map[role] = {
            "classes": list(canonical),
            "samples": sum(role_styles[role].values()),
            "canonicalSamples": count,
            "elementIds": sorted(role_elements[role]),
        }

    primary_font = None
    if families:
        family, class_name = families[0]
        primary_font = {"family": family, "class": class_name}

    return {
        "version": THEME_VERSION,
        "source": {
            "folder": folder.name,
            "slides": [slide.number for slide in slides],
        },
        "tokens": {
            "palette": palette,
            "primaryFontFamily": primary_font,
            "typeScalePt": sorted({size for size, _ in sizes}, reverse=True),
        },
        "roles": role_map,
        "inventory": {
            "palette": [
                {
                    "color": color,
                    "count": palette_counts[color],
                    "uses": dict(sorted(palette_uses[color].items())),
                }
                for color in palette
            ],
            "type": {
                "fontFamilies": [
                    {"family": family, "class": class_name, "count": count}
                    for (family, class_name), count in sorted(
                        family_counts.items(),
                        key=lambda item: (
                            -item[1],
                            item[0][0].casefold(),
                            item[0][1],
                        ),
                    )
                ],
                "fontSizes": [
                    {"pt": size, "class": class_name, "count": count}
                    for (size, class_name), count in sorted(
                        size_counts.items(),
                        key=lambda item: (-item[0][0], item[0][1]),
                    )
                ],
            },
        },
    }


def write_theme(theme: dict[str, Any], output_path: str | Path) -> Path:
    """Write a theme JSON document atomically."""
    path = Path(output_path)
    _commit_text_files(
        {path: json.dumps(theme, indent=2, ensure_ascii=False) + "\n"}
    )
    return path


def load_theme(path: str | Path) -> dict[str, Any]:
    """Load and structurally validate a theme JSON document."""
    theme_path = Path(path)
    try:
        theme = json.loads(theme_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid theme JSON at {theme_path}: {exc}") from exc
    if not isinstance(theme, dict) or theme.get("version") != THEME_VERSION:
        raise ValueError(
            f"Invalid theme JSON at {theme_path}: expected version {THEME_VERSION}"
        )
    tokens = theme.get("tokens")
    roles = theme.get("roles")
    if not isinstance(tokens, dict) or not isinstance(roles, dict):
        raise ValueError(
            f"Invalid theme JSON at {theme_path}: expected tokens and roles objects"
        )
    palette = tokens.get("palette")
    if not isinstance(palette, list) or any(
        not isinstance(color, str) or re.fullmatch(r"#[0-9a-fA-F]{6}", color) is None
        for color in palette
    ):
        raise ValueError(
            f"Invalid theme JSON at {theme_path}: tokens.palette must contain #rrggbb colors"
        )
    for role, style in roles.items():
        if not isinstance(role, str) or not isinstance(style, dict):
            raise ValueError(f"Invalid role style in theme JSON at {theme_path}")
        classes = style.get("classes")
        if not isinstance(classes, list) or any(
            not isinstance(class_name, str) for class_name in classes
        ):
            raise ValueError(
                f"Invalid role style '{role}' in theme JSON at {theme_path}"
            )
        # This catches unknown and internally-conflicting canonical element styles.
        parse_element_classes(" ".join(classes), f"theme role={role}")
    _primary_font_class(tokens, theme_path)
    return theme


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
        updated, counts = _apply_to_content(
            slide.content,
            roles=roles,
            role_classes=role_classes,
            primary_font=primary_font,
            palette=palette if map_colors else (),
            color_distance_threshold=color_distance_threshold,
        )
        # The normal parser owns class-scope and conflict validation. Validate every
        # prospective target before committing any of them.
        parse_slide_content(updated, components=components)
        slide_counts[slide.index] = counts
        if updated != slide.content:
            pending[slide.path] = updated

    if not dry_run:
        _commit_text_files(pending)
    return ThemeApplyResult(slide_counts)


def _primary_font_class(
    tokens: dict[str, Any],
    theme_path: Path | None = None,
) -> str | None:
    primary = tokens.get("primaryFontFamily")
    if primary is None:
        return None
    location = f" at {theme_path}" if theme_path is not None else ""
    if not isinstance(primary, dict) or not isinstance(primary.get("class"), str):
        raise ValueError(f"Invalid primaryFontFamily in theme JSON{location}")
    class_name = primary["class"]
    classified = classify_class(class_name)
    if (
        classified is None
        or classified[0] != ClassKind.TEXT
        or classified[1].font_family is None
    ):
        raise ValueError(f"Invalid primary font class '{class_name}' in theme JSON{location}")
    return class_name


def _class_tokens(content: str) -> Iterable[str]:
    for start, end, tag_name in _start_tag_spans(content):
        start_tag = content[start:end]
        for attribute in _attributes(start_tag, tag_name):
            if attribute.name == "class":
                yield from start_tag[
                    attribute.value_start : attribute.value_end
                ].split()


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
) -> tuple[str, dict[str, int]]:
    text_ids = _text_element_ids(content)
    pieces: list[str] = []
    cursor = 0
    counts = {"roleRestyles": 0, "fontChanges": 0, "colorChanges": 0, "changed": 0}
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
            for class_name in new_classes:
                replacement = _map_color_class(
                    class_name,
                    palette,
                    threshold=color_distance_threshold,
                )
                if replacement != class_name:
                    counts["colorChanges"] += 1
                mapped.append(replacement)
            new_classes = mapped

        if new_classes != old_classes:
            start_tag = _replace_class_attribute(start_tag, class_attribute, new_classes)
            changed_tags += 1
        pieces.append(start_tag)
        cursor = end
    pieces.append(content[cursor:])
    counts["changed"] = changed_tags
    return "".join(pieces), counts


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


def _map_color_class(
    class_name: str,
    palette: Sequence[str],
    *,
    threshold: float,
) -> str:
    match = _COLOR_CLASS.fullmatch(class_name)
    if match is None:
        return class_name
    source = match.group("color").lower()
    if source in palette:
        return class_name
    nearest = min(palette, key=lambda candidate: _rgb_distance(source, candidate))
    if _rgb_distance(source, nearest) > threshold:
        return class_name
    return (
        f"{match.group('use')}-{nearest}"
        f"{match.group('alpha') or ''}"
    )


def _rgb_distance(first: str, second: str) -> float:
    left = tuple(int(first[index : index + 2], 16) for index in (1, 3, 5))
    right = tuple(int(second[index : index + 2], 16) for index in (1, 3, 5))
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))
