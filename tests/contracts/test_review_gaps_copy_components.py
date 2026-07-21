"""Regression coverage for copied text indices and component child IDs."""

from __future__ import annotations

from pathlib import Path

import pytest

from slidesmith.engine.components import load_components
from slidesmith.engine.content_diff import Change, ChangeType, DiffResult
from slidesmith.engine.content_requests import generate_batch_requests


def test_copied_text_style_ranges_use_utf16_across_paragraphs() -> None:
    change = Change(
        change_type=ChangeType.COPY,
        target_id="copy_of_label",
        source_id="label",
        slide_index="02",
        source_slide_index="01",
        new_position={"x": 10, "y": 20, "w": 200, "h": 60},
        new_text=["A😀e\N{COMBINING ACUTE ACCENT}", "B😀"],
    )
    source_style = {
        "type": "TEXT_BOX",
        "position": {"x": 0, "y": 0, "w": 200, "h": 60},
        "text": {
            "paragraphs": [
                {
                    "runs": [
                        {"content": "A😀", "style": {"bold": True}},
                        {
                            "content": "e\N{COMBINING ACUTE ACCENT}\n",
                            "style": {"italic": True},
                        },
                    ],
                    "style": {},
                },
                {
                    "runs": [
                        {"content": "B", "style": {"underline": True}},
                        {"content": "😀\n", "style": {"fontSize": 20}},
                    ],
                    "style": {},
                },
            ]
        },
    }

    requests = generate_batch_requests(
        DiffResult(changes=[change], pristine_styles={"label": source_style}),
        {"label": "label_google"},
        {"02": "slide_google_02"},
    )

    text_style_ranges = [
        request["updateTextStyle"]["textRange"]
        for request in requests
        if "updateTextStyle" in request
    ]
    assert text_style_ranges == [
        {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 3},
        {"type": "FIXED_RANGE", "startIndex": 3, "endIndex": 5},
        {"type": "FIXED_RANGE", "startIndex": 6, "endIndex": 7},
        {"type": "FIXED_RANGE", "startIndex": 7, "endIndex": 9},
    ]


def test_component_rejects_duplicate_child_id_with_component_context(
    tmp_path: Path,
) -> None:
    (tmp_path / "components.sml").write_text(
        '<Components><Component name="stat-card">'
        '<Rect id="body" /><TextBox id="body"><P>Duplicate</P></TextBox>'
        "</Component></Components>",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="Component 'stat-card' repeats child id 'body'",
    ):
        load_components(tmp_path)
