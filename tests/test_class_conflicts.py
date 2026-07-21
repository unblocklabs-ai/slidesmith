"""Loud errors for conflicting classes in single-value style families."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slidesmith.engine.classes import ContentAlignment
from slidesmith.engine.client import diff_folder
from slidesmith.engine.content_parser import parse_slide_content
from slidesmith.workspace import materialize


GOLDEN = (
    Path(__file__).parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)


def test_prepending_content_alignment_to_pulled_class_raises(
    tmp_path: Path,
) -> None:
    """The cycle-3 reproduction must fail before diff can swallow the edit."""
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    folder = materialize(data, tmp_path)
    sml = folder / "slides" / "02" / "content.sml"
    content = sml.read_text(encoding="utf-8")
    assert 'id="e132"' in content
    assert 'class="content-align-top' in content
    target_start = content.index('<Rect id="e132"')
    target_end = content.index(" />", target_start)
    target = content[target_start:target_end]
    assert 'class="content-align-top' in target
    sml.write_text(
        content[:target_start]
        + target.replace(
            'class="content-align-top',
            'class="content-align-middle content-align-top',
            1,
        )
        + content[target_end:],
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        diff_folder(folder)

    message = str(excinfo.value)
    assert "e132" in message
    assert "content-align-middle" in message
    assert "content-align-top" in message
    assert "remove one" in message


@pytest.mark.parametrize(
    ("scope", "first", "second"),
    [
        ("paragraph", "text-align-left", "text-align-right"),
        ("element", "fill-none", "fill-#ffffff"),
        ("element", "stroke-#112233", "stroke-theme-accent1"),
        ("run", "text-size-12", "text-size-18"),
        ("run", "text-color-#112233", "text-color-theme-text1"),
        ("run", "font-family-roboto", "font-family-open-sans"),
        ("paragraph", "leading-100", "leading-140"),
        ("element", "stroke-w-1", "stroke-w-2"),
        ("element", "stroke-solid", "stroke-dash"),
        ("run", "font-weight-400", "font-weight-700"),
        ("run", "bg-#ffffff", "bg-#fff2cc"),
        ("run", "superscript", "subscript"),
        ("paragraph", "space-above-4", "space-above-8"),
        ("paragraph", "space-below-4", "space-below-8"),
        ("paragraph", "indent-start-10", "indent-start-20"),
        ("paragraph", "indent-first-5", "indent-first-10"),
        (
            "paragraph",
            "spacing-never-collapse",
            "spacing-collapse-lists",
        ),
    ],
    ids=[
        "text-alignment",
        "fill",
        "stroke-color",
        "text-size",
        "text-color",
        "font-family",
        "line-spacing",
        "stroke-weight",
        "stroke-dash",
        "font-weight",
        "text-background",
        "baseline-offset",
        "space-above",
        "space-below",
        "start-indent",
        "first-line-indent",
        "paragraph-spacing-mode",
    ],
)
def test_distinct_classes_in_single_value_family_raise(
    scope: str,
    first: str,
    second: str,
) -> None:
    classes = f"{first} {second}"
    if scope == "element":
        body = f'<TextBox id="target" class="{classes}"><P>Text</P></TextBox>'
    elif scope == "paragraph":
        body = f'<TextBox id="target"><P class="{classes}">Text</P></TextBox>'
    else:
        body = (
            f'<TextBox id="target"><P><T class="{classes}">Text</T></P>'
            "</TextBox>"
        )

    with pytest.raises(ValueError) as excinfo:
        parse_slide_content(f'<Slide id="s1">{body}</Slide>')

    message = str(excinfo.value)
    assert "target" in message
    assert first in message
    assert second in message
    assert "remove one" in message


def test_repeated_identical_class_on_element_is_tolerated() -> None:
    elements = parse_slide_content(
        '<Slide id="s1"><TextBox id="target" '
        'class="content-align-top content-align-top"><P>Text</P>'
        "</TextBox></Slide>"
    )

    assert elements[0].styles is not None
    assert elements[0].styles.content_alignment is ContentAlignment.TOP
