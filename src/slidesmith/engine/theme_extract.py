"""Theme extraction from a local workspace."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from slidesmith.engine.class_replacement import _start_tag_spans
from slidesmith.engine.classes import ClassKind, classify_class
from slidesmith.engine.components import load_components
from slidesmith.engine.layout import compile_layout
from slidesmith.engine.selector import _attributes, _read_roles, _read_slides

from .color_mapping import _COLOR_CLASS, _THEME_COLOR_CLASS
from .theme_schema import THEME_VERSION, parse_slide_spec


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
    theme_color_counts: Counter[str] = Counter()
    theme_color_uses: dict[str, Counter[str]] = defaultdict(Counter)
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
            theme_match = _THEME_COLOR_CLASS.fullmatch(class_name)
            if theme_match:
                color = f"theme:{theme_match.group('color')}"
                theme_color_counts[color] += 1
                theme_color_uses[color][theme_match.group("use")] += 1
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
    theme_colors = sorted(
        theme_color_counts,
        key=lambda color: (-theme_color_counts[color], color),
    )
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
    if family_counts:
        family_totals: Counter[str] = Counter()
        for (family, _), count in family_counts.items():
            family_totals[family] += count
        family = sorted(
            family_totals,
            key=lambda value: (-family_totals[value], value.casefold(), value),
        )[0]
        class_name = sorted(
            (
                (candidate_class, count)
                for (candidate_family, candidate_class), count in family_counts.items()
                if candidate_family == family
            ),
            key=lambda item: (-item[1], item[0]),
        )[0][0]
        primary_font = {"family": family, "class": class_name}
    type_scale = _type_scale(size_counts)

    return {
        "version": THEME_VERSION,
        "source": {
            "folder": folder.name,
            "slides": [slide.number for slide in slides],
        },
        "tokens": {
            "palette": palette,
            "themeColors": theme_colors,
            "primaryFontFamily": primary_font,
            "typeScale": type_scale,
            "typeScalePt": [entry["pt"] for entry in type_scale],
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
            ]
            + [
                {
                    "color": color,
                    "count": theme_color_counts[color],
                    "uses": dict(sorted(theme_color_uses[color].items())),
                }
                for color in theme_colors
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


def _class_tokens(content: str) -> Iterable[str]:
    for start, end, tag_name in _start_tag_spans(content):
        start_tag = content[start:end]
        for attribute in _attributes(start_tag, tag_name):
            if attribute.name == "class":
                yield from start_tag[
                    attribute.value_start : attribute.value_end
                ].split()


def _type_scale(
    size_counts: Counter[tuple[float, str]],
) -> list[dict[str, Any]]:
    names = ("display", "title", "subtitle", "body", "caption", "micro")
    by_size: dict[float, Counter[str]] = defaultdict(Counter)
    for (size, class_name), count in size_counts.items():
        by_size[size][class_name] += count
    result: list[dict[str, Any]] = []
    for index, size in enumerate(sorted(by_size, reverse=True)):
        class_name, _ = sorted(
            by_size[size].items(),
            key=lambda item: (-item[1], item[0]),
        )[0]
        result.append(
            {
                "tier": names[index] if index < len(names) else f"tier-{index + 1}",
                "pt": size,
                "class": class_name,
                "count": sum(by_size[size].values()),
            }
        )
    return result
