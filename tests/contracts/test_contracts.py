"""The six contracts slidesmith must satisfy.

C1  Pull -> no edits -> diff produces zero requests.
C2  Create a styled text box using the documented class syntax.
C3  Edit text while preserving all human styling.
C4  Human and agent edit different properties of the same element; both survive.
C5  Human and agent edit the same property; push aborts with a useful conflict.
C6  Pull -> push -> pull is idempotent.

C1/C2/C6 run offline against the golden fixture. C3-C5 need a live deck:
set SLIDESMITH_LIVE_DECK=<presentationId> to enable.
"""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from slidesmith.engine.classes import (
    ContentAlignment,
    ParagraphStyle,
    Stroke,
    TextStyle,
    parse_paragraph_style_classes,
    parse_content_alignment_class,
    parse_stroke_classes,
    parse_text_style_classes,
)
from slidesmith.engine.client import diff_folder
from slidesmith.engine.content_diff import DiffResult, diff_slide_content
from slidesmith.engine.content_requests import generate_batch_requests
from slidesmith.engine.bounds import BoundingBox
from slidesmith.engine.content_generator import generate_slide_content
from slidesmith.engine.content_parser import parse_slide_content
from slidesmith.engine.render_tree import RenderNode
from slidesmith.engine.units import pt_to_emu
from slidesmith.workspace import materialize

GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)

LIVE_DECK = os.environ.get("SLIDESMITH_LIVE_DECK")
live = pytest.mark.skipif(
    not LIVE_DECK,
    reason="set SLIDESMITH_LIVE_DECK=<presentationId> to run live contracts",
)


@pytest.fixture
def golden_folder(tmp_path: Path) -> Path:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return materialize(data, tmp_path)


def test_c1_pull_without_edits_diffs_to_zero_requests(golden_folder: Path) -> None:
    assert diff_folder(golden_folder) == []


def test_styled_pull_emits_explicit_element_and_run_classes(
    golden_folder: Path,
) -> None:
    """Pulled SML exposes explicit design without resolving inheritance."""
    sml = golden_folder / "slides" / "01" / "content.sml"
    content = sml.read_text(encoding="utf-8")
    root = ET.fromstring(content)

    # e75 has an explicitly-set solid shape fill and hidden outline.
    filled = root.find(".//*[@id='e75']")
    assert filled is not None
    assert filled.get("class", "").split() == [
        "content-align-middle",
        "fill-#f4f4f4",
        "stroke-none",
    ]

    # e121 is a placeholder whose fill/outline are explicitly INHERIT. Those
    # must remain absent, while its explicit text-run styles are visible in
    # the per-run representation the parser already understands.
    title = root.find(".//*[@id='e121']")
    assert title is not None
    assert title.get("class", "").split() == ["text-align-left", "leading-90"]
    assert not any(
        cls.startswith(("fill-", "stroke-"))
        for cls in title.get("class", "").split()
    )

    title_paragraph = title.find("./P")
    assert title_paragraph is not None
    assert title_paragraph.text == "Driving GenAI Transformations"
    assert title_paragraph.get("class", "").split() == [
        "font-family-montserrat",
        "text-size-43",
        "font-weight-400",
    ]

    # e122 has two differently-styled runs in one paragraph; they must remain
    # independently visible and editable rather than being flattened.
    footer = root.find(".//*[@id='e122']")
    assert footer is not None
    footer_runs = footer.findall("./P/T")
    assert [run.text for run in footer_runs] == ["Website:", "www.think41.com"]
    assert "bold" in footer_runs[0].get("class", "").split()
    assert "text-size-12" in footer_runs[0].get("class", "").split()
    assert "bold" not in footer_runs[1].get("class", "").split()
    assert "text-size-10" in footer_runs[1].get("class", "").split()

    # Contract C1 now explicitly proves class-bearing current and pristine
    # projections are byte-identical before the no-op diff.
    with zipfile.ZipFile(
        golden_folder / ".pristine" / "presentation.zip"
    ) as pristine:
        assert pristine.read("slides/01/content.sml").decode("utf-8") == content
    assert diff_folder(golden_folder) == []


def test_styled_pull_forward_classes_use_existing_reverse_mappings() -> None:
    """New forward emission is exactly reversible by classes.py parsers."""
    stroke = Stroke.from_api(
        {
            "outlineFill": {
                "solidFill": {"color": {"rgbColor": {"red": 1}}}
            },
            "weight": {"magnitude": 12700, "unit": "EMU"},
            "dashStyle": "SOLID",
        }
    )
    assert stroke is not None
    stroke_classes = stroke.to_classes()
    assert stroke_classes == ["stroke-#ff0000", "stroke-w-1", "stroke-solid"]
    assert parse_stroke_classes(stroke_classes) == stroke

    text_style = TextStyle.from_api(
        {
            "fontSize": {"magnitude": 152400, "unit": "EMU"},
            "weightedFontFamily": {"fontFamily": "Roboto", "weight": 400},
        }
    )
    assert text_style is not None
    text_classes = text_style.to_classes()
    assert text_classes == [
        "font-family-roboto",
        "text-size-12",
        "font-weight-400",
    ]
    assert parse_text_style_classes(text_classes) == text_style

    paragraph_style = ParagraphStyle.from_api(
        {
            "lineSpacing": 100,
            "spaceAbove": {"unit": "PT"},
            "indentStart": {"unit": "PT"},
        }
    )
    assert paragraph_style is not None
    paragraph_classes = paragraph_style.to_classes()
    assert paragraph_classes == ["leading-100", "space-above-0", "indent-start-0"]
    assert parse_paragraph_style_classes(paragraph_classes) == paragraph_style

    assert ContentAlignment.MIDDLE.to_class() == "content-align-middle"
    assert parse_content_alignment_class("content-align-middle") is ContentAlignment.MIDDLE


def test_styled_line_create_uses_line_api_and_round_trips() -> None:
    authored = (
        '<Slide id="s1"><Line id="x" x="10" y="20" w="100" h="0.75" '
        'class="stroke-#5df2b2/40 stroke-w-0.75 stroke-solid" /></Slide>'
    )

    changes = diff_slide_content('<Slide id="s1" />', authored, {}, "01")
    requests = generate_batch_requests(
        DiffResult(changes=changes), {}, {"01": "google_slide"}
    )

    assert len(requests) == 2
    assert requests[0]["createLine"]["lineCategory"] == "STRAIGHT"
    assert "createShape" not in requests[0]
    line_update = requests[1]["updateLineProperties"]
    assert line_update["objectId"] == requests[0]["createLine"]["objectId"]
    assert line_update["fields"] == "lineFill.solidFill,weight,dashStyle"
    assert line_update["lineProperties"] == {
        "lineFill": {
            "solidFill": {
                "color": {
                    "rgbColor": {
                        "red": 93 / 255,
                        "green": 242 / 255,
                        "blue": 178 / 255,
                    }
                },
                "alpha": 0.4,
            }
        },
        "weight": {"magnitude": 9525, "unit": "EMU"},
        "dashStyle": "SOLID",
    }


@pytest.mark.parametrize(
    ("geometry", "expected"),
    [
        ('x="100" y="20" w="-30" h="10"', (70, 20, 30, 10)),
        ('x="100" y="20" w="30" h="-10"', (100, 10, 30, 10)),
    ],
)
def test_negative_line_geometry_is_canonicalized_for_create_request(
    geometry: str,
    expected: tuple[int, int, int, int],
) -> None:
    changes = diff_slide_content(
        '<Slide id="s1" />',
        f'<Slide id="s1"><Line id="rule" {geometry} /></Slide>',
        {},
        "01",
    )
    requests = generate_batch_requests(
        DiffResult(changes=changes), {}, {"01": "google_slide"}
    )

    position = requests[0]["createLine"]["elementProperties"]
    assert position["transform"]["translateX"] == pt_to_emu(expected[0])
    assert position["transform"]["translateY"] == pt_to_emu(expected[1])
    assert position["size"]["width"]["magnitude"] == pt_to_emu(expected[2])
    assert position["size"]["height"]["magnitude"] == pt_to_emu(expected[3])


def test_styled_line_pull_and_style_update_use_stroke_classes() -> None:
    node = RenderNode(
        clean_id="line1",
        bounds=BoundingBox(10, 20, 100, 0.75),
        element={
            "objectId": "google_line",
            "line": {
                "lineProperties": {
                    "lineFill": {
                        "solidFill": {
                            "color": {
                                "rgbColor": {
                                    "red": 93 / 255,
                                    "green": 242 / 255,
                                    "blue": 178 / 255,
                                }
                            },
                            "alpha": 0.4,
                        }
                    },
                    "weight": {"magnitude": 9525, "unit": "EMU"},
                    "dashStyle": "SOLID",
                }
            },
        },
    )

    pulled = generate_slide_content([node])
    assert (
        'class="stroke-#5df2b2/40 stroke-w-0.75 stroke-solid"' in pulled
    )
    assert diff_slide_content(pulled, pulled, {}, "01") == []

    edited = pulled.replace("stroke-solid", "stroke-dash")
    changes = diff_slide_content(pulled, edited, {}, "01")
    requests = generate_batch_requests(
        DiffResult(changes=changes),
        {"line1": "google_line"},
        {"01": "google_slide"},
    )
    assert len(requests) == 1
    assert "updateShapeProperties" not in requests[0]
    assert requests[0]["updateLineProperties"]["fields"] == (
        "lineFill.solidFill,weight,dashStyle"
    )


def test_fill_class_on_line_fails_loudly() -> None:
    with pytest.raises(
        ValueError,
        match=r"Invalid class 'fill-#ffffff' on Line element 'x'",
    ):
        parse_slide_content(
            '<Slide id="s1"><Line id="x" x="0" y="0" w="10" h="1" '
            'class="fill-#ffffff" /></Slide>'
        )


def test_sml_geometry_preserves_two_decimal_point_precision() -> None:
    node = RenderNode(
        clean_id="precise",
        bounds=BoundingBox(64.0, 0.75, 201.93, 0.75),
        element={
            "objectId": "google_precise",
            "shape": {"shapeType": "RECTANGLE"},
        },
    )

    first = generate_slide_content([node])
    second = generate_slide_content([node])
    assert 'x="64"' in first
    assert 'y="0.75"' in first
    assert 'w="201.93"' in first
    assert 'h="0.75"' in first
    assert first == second
    parsed = parse_slide_content(first)[0]
    assert (parsed.x, parsed.y, parsed.w, parsed.h) == (64.0, 0.75, 201.93, 0.75)
    assert diff_slide_content(first, first, {}, "01") == []


def test_content_alignment_existing_element_is_one_field_masked_request(
    golden_folder: Path,
) -> None:
    id_mapping = json.loads(
        (golden_folder / "id_mapping.json").read_text(encoding="utf-8")
    )
    sml = golden_folder / "slides" / "01" / "content.sml"
    content = sml.read_text(encoding="utf-8")
    marker = 'class="text-align-left leading-90"'
    assert marker in content
    sml.write_text(
        content.replace(
            marker,
            marker[:-1] + ' content-align-middle"',
            1,
        ),
        encoding="utf-8",
    )

    requests = diff_folder(golden_folder)
    assert requests == [
        {
            "updateShapeProperties": {
                "objectId": id_mapping["e121"],
                "shapeProperties": {"contentAlignment": "MIDDLE"},
                "fields": "contentAlignment",
            }
        }
    ]


def test_generator_emits_different_paragraph_defaults_and_round_trips() -> None:
    node = RenderNode(
        clean_id="e1",
        bounds=BoundingBox(0, 0, 200, 80),
        element={
            "objectId": "g_e1",
            "shape": {
                "shapeType": "TEXT_BOX",
                "text": {
                    "textElements": [
                        {"paragraphMarker": {"style": {"alignment": "START", "lineSpacing": 100}}},
                        {"textRun": {"content": "Alpha\n", "style": {"fontSize": {"magnitude": 12, "unit": "PT"}}}},
                        {"paragraphMarker": {"style": {"alignment": "END", "lineSpacing": 140}}},
                        {"textRun": {"content": "Beta\n", "style": {"fontSize": {"magnitude": 18, "unit": "PT"}}}},
                    ]
                },
            },
        },
    )

    content = generate_slide_content([node])
    assert '<P class="text-align-left leading-100 text-size-12">Alpha</P>' in content
    assert '<P class="text-align-right leading-140 text-size-18">Beta</P>' in content
    assert diff_slide_content(content, content, {}, "01") == []


def test_editing_one_paragraph_class_touches_only_its_range() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="e1" x="0" y="0" w="100" h="50" '
        'class="text-size-12"><P class="text-align-left leading-100">Alpha</P>'
        '<P class="text-align-right leading-120">Beta</P></TextBox></Slide>'
    )
    edited = pristine.replace(
        "text-align-right leading-120",
        "text-align-center leading-120 bold",
    )

    assert _text_edit_requests(pristine, edited) == [
        {
            "updateParagraphStyle": {
                "objectId": "g_e1",
                "textRange": {"type": "FIXED_RANGE", "startIndex": 6, "endIndex": 10},
                "style": {"alignment": "CENTER"},
                "fields": "alignment",
            }
        },
        {
            "updateTextStyle": {
                "objectId": "g_e1",
                "textRange": {"type": "FIXED_RANGE", "startIndex": 6, "endIndex": 10},
                "style": {"bold": True},
                "fields": "bold",
            }
        },
    ]


def test_paragraph_text_default_reapplies_nested_run_override() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="e1" x="0" y="0" w="100" h="50">'
        '<P class="text-size-12">Plain <T class="text-size-20">large</T></P>'
        "</TextBox></Slide>"
    )
    edited = pristine.replace("text-size-12", "text-size-14", 1)

    assert _text_edit_requests(pristine, edited) == [
        {
            "updateTextStyle": {
                "objectId": "g_e1",
                "textRange": {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 11},
                "style": {"fontSize": {"magnitude": 14.0, "unit": "PT"}},
                "fields": "fontSize",
            }
        },
        {
            "updateTextStyle": {
                "objectId": "g_e1",
                "textRange": {"type": "FIXED_RANGE", "startIndex": 6, "endIndex": 11},
                "style": {"fontSize": {"magnitude": 20.0, "unit": "PT"}},
                "fields": "fontSize",
            }
        },
    ]


def test_c2_create_styled_textbox_via_documented_syntax(golden_folder: Path) -> None:
    slide_dir = sorted((golden_folder / "slides").iterdir())[0]
    sml = slide_dir / "content.sml"
    new_element = (
        '<TextBox id="new_box1" x="100" y="100" w="200" h="50" '
        'class="fill-#ff0000 text-size-24"><P>Hello</P></TextBox>'
    )
    sml.write_text(
        sml.read_text(encoding="utf-8").replace("</Slide>", new_element + "</Slide>"),
        encoding="utf-8",
    )

    requests = diff_folder(golden_folder)

    creates = [r for r in requests if "createShape" in r]
    assert creates, "new element not created at all"
    assert creates[0]["createShape"]["shapeType"] == "TEXT_BOX"
    object_id = creates[0]["createShape"]["objectId"]

    inserts = [
        r
        for r in requests
        if "insertText" in r and r["insertText"]["objectId"] == object_id
    ]
    assert inserts and inserts[0]["insertText"]["text"] == "Hello"

    # fill-#ff0000 must arrive as a red shapeBackgroundFill with a field mask
    # naming only the fill.
    fills = [
        r
        for r in requests
        if "updateShapeProperties" in r
        and r["updateShapeProperties"]["objectId"] == object_id
    ]
    assert len(fills) == 1, "expected exactly one shape style request"
    fill_update = fills[0]["updateShapeProperties"]
    assert fill_update["fields"] == "shapeBackgroundFill.solidFill"
    rgb = fill_update["shapeProperties"]["shapeBackgroundFill"]["solidFill"]["color"][
        "rgbColor"
    ]
    assert rgb == {"red": 1.0, "green": 0.0, "blue": 0.0}

    # text-size-24 must arrive as a 24pt fontSize with a field mask naming
    # only the font size.
    text_styles = [
        r
        for r in requests
        if "updateTextStyle" in r and r["updateTextStyle"]["objectId"] == object_id
    ]
    assert len(text_styles) == 1, "expected exactly one text style request"
    text_update = text_styles[0]["updateTextStyle"]
    assert text_update["fields"] == "fontSize"
    assert text_update["style"]["fontSize"] == {"magnitude": 24.0, "unit": "PT"}


def test_c2b_style_update_on_existing_element(golden_folder: Path) -> None:
    """Adding classes to an element that existed in pristine emits field-masked
    updates for just the class-derived properties -- no recreate, no deletes."""
    id_mapping = json.loads(
        (golden_folder / "id_mapping.json").read_text(encoding="utf-8")
    )
    sml = golden_folder / "slides" / "01" / "content.sml"
    content = sml.read_text(encoding="utf-8")
    original_classes = 'class="text-align-left leading-90"'
    assert original_classes in content
    sml.write_text(
        content.replace(
            original_classes,
            'class="text-align-left leading-90 fill-#00ff00"',
            1,
        ).replace("text-size-43", "text-size-30", 1),
        encoding="utf-8",
    )

    requests = diff_folder(golden_folder)
    google_id = id_mapping["e121"]

    blob = json.dumps(requests)
    assert "createShape" not in blob, "existing element must not be recreated"
    assert "deleteObject" not in blob
    assert "deleteText" not in blob, "style-only change must not rewrite text"

    fills = [r for r in requests if "updateShapeProperties" in r]
    assert len(fills) == 1
    fill_update = fills[0]["updateShapeProperties"]
    assert fill_update["objectId"] == google_id
    assert fill_update["fields"] == "shapeBackgroundFill.solidFill"
    rgb = fill_update["shapeProperties"]["shapeBackgroundFill"]["solidFill"]["color"][
        "rgbColor"
    ]
    assert rgb == {"red": 0.0, "green": 1.0, "blue": 0.0}

    text_styles = [r for r in requests if "updateTextStyle" in r]
    assert len(text_styles) == 1
    text_update = text_styles[0]["updateTextStyle"]
    assert text_update["objectId"] == google_id
    assert text_update["textRange"] == {
        "type": "FIXED_RANGE",
        "startIndex": 0,
        "endIndex": 29,
    }
    assert text_update["fields"] == "fontSize"
    assert text_update["style"]["fontSize"] == {"magnitude": 30.0, "unit": "PT"}
    assert len(requests) == 2, "only the two styled properties may be touched"


def _text_edit_requests(
    pristine_sml: str, edited_sml: str
) -> list[dict[str, object]]:
    """Diff two single-slide SML strings and generate batchUpdate requests.

    Runs the real parser + differ + request generator offline, mapping
    element e1 -> g_e1 on slide g_s1.
    """
    changes = diff_slide_content(pristine_sml, edited_sml, {}, "01")
    return generate_batch_requests(
        DiffResult(changes=changes), {"e1": "g_e1"}, {"01": "g_s1"}
    )


def test_c3_single_word_edit_preserves_explicit_run_style(
    golden_folder: Path,
) -> None:
    """C3: paragraph defaults let a word edit stay minimal and scoped."""
    id_mapping = json.loads(
        (golden_folder / "id_mapping.json").read_text(encoding="utf-8")
    )
    sml = golden_folder / "slides" / "02" / "content.sml"
    content = sml.read_text(encoding="utf-8")
    assert "With Innovation teams" in content
    sml.write_text(
        content.replace("With Innovation teams", "With Platform teams", 1),
        encoding="utf-8",
    )

    requests = diff_folder(golden_folder)
    google_id = id_mapping["e191"]

    blob = json.dumps(requests)
    assert '"ALL"' not in blob, "range edits must never touch the whole text"

    # Paragraph 2's style now lives on <P>, so only the changed word is edited;
    # the paragraph default and paragraph 1 stay untouched.
    assert "GCCs in India" not in blob
    assert requests[0] == {
        "deleteText": {
            "objectId": google_id,
            "textRange": {
                "type": "FIXED_RANGE",
                "startIndex": 19,
                "endIndex": 29,
            },
        }
    }
    assert requests[1] == {
        "insertText": {
            "objectId": google_id,
            "insertionIndex": 19,
            "text": "Platform",
        }
    }
    assert len(requests) == 2


def test_c3_non_bmp_text_indices_count_utf16_units() -> None:
    """C3 index math: Slides API text ranges count UTF-16 code units, so an
    emoji (a surrogate pair) before the edit point shifts indices by 2."""
    pristine = (
        '<Slide id="s1">'
        '<TextBox id="e1" x="10" y="10" w="300" h="80">'
        "<P>Launch \U0001f680 stats</P>"
        "<P>Revenue grew rapidly</P>"
        "</TextBox>"
        "</Slide>"
    )
    edited = pristine.replace("grew", "fell")

    requests = _text_edit_requests(pristine, edited)

    # "Launch \U0001f680 stats" is 15 UTF-16 units (14 characters), plus the
    # newline: paragraph 2 starts at 16 and "Revenue " puts "grew" at 24..28.
    # Python-character counting would produce 23..27 -- one unit short.
    assert requests == [
        {
            "deleteText": {
                "objectId": "g_e1",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 24,
                    "endIndex": 28,
                },
            }
        },
        {
            "insertText": {
                "objectId": "g_e1",
                "insertionIndex": 24,
                "text": "fell",
            }
        },
    ]


def test_c3_paragraph_insertion_and_deletion_stay_scoped() -> None:
    """C3: inserting/deleting whole paragraphs (including at the ends) emits
    one request covering the paragraph plus exactly one newline separator."""
    pristine = (
        '<Slide id="s1">'
        '<TextBox id="e1" x="0" y="0" w="100" h="50">'
        "<P>Alpha</P><P>Beta</P><P>Gamma</P>"
        "</TextBox>"
        "</Slide>"
    )
    # Combined pristine text: "Alpha\nBeta\nGamma" (16 units).

    # Append a paragraph at the end: separator goes before the new text.
    appended = pristine.replace("<P>Gamma</P>", "<P>Gamma</P><P>Delta</P>")
    assert _text_edit_requests(pristine, appended) == [
        {"insertText": {"objectId": "g_e1", "insertionIndex": 16, "text": "\nDelta"}}
    ]

    # Insert a paragraph in the middle: separator goes after the new text.
    inserted = pristine.replace("<P>Beta</P>", "<P>Beta</P><P>Mid</P>")
    assert _text_edit_requests(pristine, inserted) == [
        {"insertText": {"objectId": "g_e1", "insertionIndex": 11, "text": "Mid\n"}}
    ]

    # Delete the first paragraph: the following separator goes with it.
    first_deleted = pristine.replace("<P>Alpha</P>", "")
    assert _text_edit_requests(pristine, first_deleted) == [
        {
            "deleteText": {
                "objectId": "g_e1",
                "textRange": {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 6},
            }
        }
    ]

    # Delete a middle paragraph: the preceding separator goes with it.
    middle_deleted = pristine.replace("<P>Beta</P>", "")
    assert _text_edit_requests(pristine, middle_deleted) == [
        {
            "deleteText": {
                "objectId": "g_e1",
                "textRange": {"type": "FIXED_RANGE", "startIndex": 5, "endIndex": 10},
            }
        }
    ]


def test_c3_styled_runs_replace_only_their_paragraph() -> None:
    """C3 x run styling: explicit <T class> runs may rewrite their own
    paragraph (text + styles), but other paragraphs stay untouched."""
    pristine = (
        '<Slide id="s1">'
        '<TextBox id="e1" x="0" y="0" w="100" h="50">'
        "<P>Keep me</P><P>Make this bold</P>"
        "</TextBox>"
        "</Slide>"
    )

    # Styling added without a text change: only updateTextStyle, no rewrite.
    # "Keep me\n" is 8 units; "Make " puts "this" at 13..17.
    styled_only = pristine.replace(
        "<P>Make this bold</P>", '<P>Make <T class="bold">this</T> bold</P>'
    )
    assert _text_edit_requests(pristine, styled_only) == [
        {
            "updateTextStyle": {
                "objectId": "g_e1",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 13,
                    "endIndex": 17,
                },
                "style": {"bold": True},
                "fields": "bold",
            }
        }
    ]

    # Text and styling changed together: that paragraph is replaced wholesale
    # and its run styles reapplied; "Keep me" must not appear in any request.
    restyled = pristine.replace(
        "<P>Make this bold</P>", '<P>New <T class="bold">bold</T> text</P>'
    )
    requests = _text_edit_requests(pristine, restyled)
    assert json.dumps(requests).count("Keep me") == 0
    assert requests == [
        {
            "deleteText": {
                "objectId": "g_e1",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 8,
                    "endIndex": 22,
                },
            }
        },
        {
            "insertText": {
                "objectId": "g_e1",
                "insertionIndex": 8,
                "text": "New bold text",
            }
        },
        {
            "updateTextStyle": {
                "objectId": "g_e1",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 12,
                    "endIndex": 16,
                },
                "style": {"bold": True},
                "fields": "bold",
            }
        },
    ]


def test_c6_materialize_is_deterministic(tmp_path: Path) -> None:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    a = materialize(data, tmp_path / "a")
    b = materialize(data, tmp_path / "b")
    rels = sorted(p.relative_to(a) for p in a.rglob("content.sml"))
    assert rels == sorted(p.relative_to(b) for p in b.rglob("content.sml"))
    for rel in rels:
        assert (a / rel).read_text(encoding="utf-8") == (b / rel).read_text(
            encoding="utf-8"
        )


def test_c6_noop_rewrite_still_diffs_to_zero(golden_folder: Path) -> None:
    for sml in golden_folder.rglob("content.sml"):
        if ".pristine" in sml.parts:
            continue
        sml.write_text(sml.read_text(encoding="utf-8"), encoding="utf-8")
    assert diff_folder(golden_folder) == []


@live
def test_c3_text_edit_preserves_human_styling() -> None:
    pytest.skip("TODO: live contract — pull, edit one word, push, re-pull, assert styles unchanged")


@live
def test_c4_disjoint_edits_both_survive() -> None:
    pytest.skip("TODO: live contract — agent moves element while human recolors it; both persist")


@live
@pytest.mark.xfail(reason="requires revision locking (writeControl) — not yet implemented")
def test_c5_same_property_conflict_aborts_push() -> None:
    pytest.skip("TODO: live contract — concurrent same-property edit must abort with conflict")
