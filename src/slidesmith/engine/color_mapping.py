"""Color-class parsing and nearest-palette mapping for themes."""

from __future__ import annotations

import math
import re
from typing import Sequence


COLOR_DISTANCE_THRESHOLD = 48.0
_COLOR_CLASS = re.compile(
    r"^(?P<use>fill|stroke|text-color|bg)-(?P<color>#[0-9a-fA-F]{6})"
    r"(?P<alpha>/[0-9]+)?$"
)
_THEME_COLOR_CLASS = re.compile(
    r"^(?P<use>fill|stroke|text-color)-theme-(?P<color>[a-z0-9-]+)"
    r"(?P<alpha>/[0-9]+)?$"
)


def _map_color_class_detail(
    class_name: str,
    palette: Sequence[str],
    *,
    threshold: float,
) -> tuple[str, str | None]:
    match = _COLOR_CLASS.fullmatch(class_name)
    if match is None:
        return class_name, None
    source = match.group("color").lower()
    if source in palette:
        return class_name, None
    nearest = min(palette, key=lambda candidate: _rgb_distance(source, candidate))
    if _rgb_distance(source, nearest) > threshold:
        return (
            class_name,
            f"{class_name} kept (nearest theme color {nearest} beyond threshold)",
        )
    replacement = (
        f"{match.group('use')}-{nearest}"
        f"{match.group('alpha') or ''}"
    )
    return replacement, f"color: {class_name} -> {replacement}"


def _rgb_distance(first: str, second: str) -> float:
    left = tuple(int(first[index : index + 2], 16) for index in (1, 3, 5))
    right = tuple(int(second[index : index + 2], 16) for index in (1, 3, 5))
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))
