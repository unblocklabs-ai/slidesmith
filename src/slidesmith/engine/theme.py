"""Public compatibility façade for cross-deck design-language themes."""

from .color_mapping import COLOR_DISTANCE_THRESHOLD
from .theme_apply import apply_theme
from .theme_extract import extract_theme
from .theme_schema import (
    THEME_VERSION,
    ThemeApplyResult,
    ThemeElementPreview,
    load_theme,
    parse_slide_spec,
    write_theme,
)

__all__ = [
    "COLOR_DISTANCE_THRESHOLD",
    "THEME_VERSION",
    "ThemeApplyResult",
    "ThemeElementPreview",
    "apply_theme",
    "extract_theme",
    "load_theme",
    "parse_slide_spec",
    "write_theme",
]
