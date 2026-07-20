"""Executable checks for the agent-facing class vocabulary."""

from __future__ import annotations

import re
from pathlib import Path

from slidesmith.engine.content_parser import parse_element_classes, parse_slide_content


GUIDE = Path(__file__).parent.parent / "docs" / "AGENT-GUIDE.md"


def test_every_documented_class_example_parses() -> None:
    """Examples are doctest-style inputs to the real classes.py-backed parser."""
    guide = GUIDE.read_text(encoding="utf-8")
    blocks = re.findall(r"```sml-classes\n(.*?)```", guide, flags=re.DOTALL)

    assert len(blocks) == 5, (
        "shape, fill, stroke, text, and paragraph blocks are required"
    )
    examples = [
        line.strip()
        for block in blocks
        for line in block.splitlines()
        if line.strip()
    ]
    assert len(examples) >= 40

    for index, class_string in enumerate(examples):
        assert parse_element_classes(class_string, f"guide_{index}") is not None


def test_documented_paragraph_class_example_parses() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    blocks = re.findall(r"```sml-paragraph\n(.*?)```", guide, flags=re.DOTALL)
    assert len(blocks) == 1

    elements = parse_slide_content(f'<Slide id="s1">{blocks[0]}</Slide>')
    assert len(elements) == 1
    assert len(elements[0].paragraph_styles) == 2
    assert elements[0].paragraph_styles[1] is not None
    assert elements[0].runs[1][-1].text_style is not None
