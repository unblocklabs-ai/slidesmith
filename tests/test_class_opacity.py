"""Opacity bounds for authoring color classes."""

import pytest

from slidesmith.engine.classes import (
    parse_fill_class,
    parse_stroke_classes,
    parse_text_style_classes,
)


@pytest.mark.parametrize(
    "class_name",
    ["fill-#112233/101", "stroke-#112233/101", "text-color-#112233/101"],
)
def test_opacity_above_100_is_an_invalid_class(class_name: str) -> None:
    with pytest.raises(ValueError, match=r"Invalid class .*opacity"):
        if class_name.startswith("fill-"):
            parse_fill_class(class_name)
        elif class_name.startswith("stroke-"):
            parse_stroke_classes([class_name])
        else:
            parse_text_style_classes([class_name])


@pytest.mark.parametrize("opacity", [0, 100])
def test_opacity_bounds_are_inclusive(opacity: int) -> None:
    fill = parse_fill_class(f"fill-#112233/{opacity}")
    stroke = parse_stroke_classes([f"stroke-#112233/{opacity}"])
    text = parse_text_style_classes([f"text-color-#112233/{opacity}"])
    assert fill is not None and fill.color is not None
    assert stroke is not None and stroke.color is not None
    assert text.foreground_color is not None
    assert fill.color.alpha == opacity / 100
    assert stroke.color.alpha == opacity / 100
    assert text.foreground_color.alpha == opacity / 100
