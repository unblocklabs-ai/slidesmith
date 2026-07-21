"""Theme document schema, validation, and typed application results."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from slidesmith.engine.atomic_files import commit_text_files
from slidesmith.engine.classes import ClassKind, classify_class
from slidesmith.engine.content_parser import parse_element_classes


THEME_VERSION = 1


@dataclass(frozen=True)
class ThemeApplyResult:
    """Counts for one validated theme application."""

    slide_counts: dict[str, dict[str, int]]
    previews: tuple[ThemeElementPreview, ...] = ()

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


@dataclass(frozen=True)
class ThemeElementPreview:
    """One element-level theme change or retained off-theme color."""

    slide_index: str
    element_id: str
    old_classes: tuple[str, ...]
    new_classes: tuple[str, ...]
    color_notes: tuple[str, ...]


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


def write_theme(theme: dict[str, Any], output_path: str | Path) -> Path:
    """Write a theme JSON document atomically."""
    path = Path(output_path)
    commit_text_files(
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
