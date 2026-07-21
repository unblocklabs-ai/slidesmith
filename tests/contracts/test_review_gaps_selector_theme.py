"""Regression coverage for selector paragraph joins and theme color mapping."""

from __future__ import annotations

from pathlib import Path

import pytest

from slidesmith.engine.content_parser import ParsedElement
from slidesmith.engine.selector import QueryContext, parse_query
from slidesmith.engine.theme import apply_theme


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("text=foobar", False),
        ('text="foo\nbar"', True),
        ('text^="foo\nb"', True),
        ('text$="o\nbar"', True),
        ('text~="o\nb"', True),
    ],
)
def test_selector_joins_paragraphs_with_newlines(query: str, expected: bool) -> None:
    context = QueryContext(
        slide_number=1,
        element=ParsedElement(
            clean_id="two_paragraphs",
            tag="TextBox",
            paragraphs=["foo", "bar"],
        ),
        classes=frozenset(),
        role=None,
    )

    assert parse_query(query).matches(context) is expected


def test_theme_color_at_exact_threshold_is_mapped(tmp_path: Path) -> None:
    folder, content_path = _color_workspace(tmp_path, "fill-#000000")

    result = apply_theme(
        folder,
        _theme_with_palette("#030400"),
        map_colors=True,
        color_distance_threshold=5,
    )

    assert 'class="fill-#030400"' in content_path.read_text(encoding="utf-8")
    assert result.color_changes == 1


def test_theme_color_mapping_preserves_alpha_suffix(tmp_path: Path) -> None:
    folder, content_path = _color_workspace(tmp_path, "fill-#000000/50")

    result = apply_theme(
        folder,
        _theme_with_palette("#010101"),
        map_colors=True,
    )

    assert 'class="fill-#010101/50"' in content_path.read_text(encoding="utf-8")
    assert result.color_changes == 1


def _color_workspace(
    tmp_path: Path,
    color_class: str,
) -> tuple[Path, Path]:
    folder = tmp_path / "deck"
    slide = folder / "slides" / "01"
    slide.mkdir(parents=True)
    content_path = slide / "content.sml"
    content_path.write_text(
        f'<Slide id="slide"><Rect id="box" class="{color_class}" /></Slide>',
        encoding="utf-8",
    )
    return folder, content_path


def _theme_with_palette(color: str) -> dict[str, object]:
    return {
        "version": 1,
        "tokens": {"palette": [color], "primaryFontFamily": None},
        "roles": {},
    }
