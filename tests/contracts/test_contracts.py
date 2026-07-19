"""The six contracts slidesmith must satisfy (see DESIGN.md).

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
from pathlib import Path

import pytest

from extraslide.client import diff_folder
from extraslide.content_diff import DiffResult, diff_slide_content
from extraslide.content_requests import generate_batch_requests
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
    assert '<TextBox id="e121"' in content
    sml.write_text(
        content.replace(
            '<TextBox id="e121"',
            '<TextBox id="e121" class="fill-#00ff00 text-size-30"',
        ),
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


def test_c3_single_word_edit_touches_only_changed_range(golden_folder: Path) -> None:
    """C3 (offline half): editing one word in a multi-paragraph element must
    emit range edits scoped to the changed span. A delete-all + reinsert
    would wipe per-run character styling a human applied in the Slides UI."""
    id_mapping = json.loads(
        (golden_folder / "id_mapping.json").read_text(encoding="utf-8")
    )
    sml = golden_folder / "slides" / "02" / "content.sml"
    content = sml.read_text(encoding="utf-8")
    assert "<P>With Innovation teams</P>" in content
    sml.write_text(
        content.replace("<P>With Innovation teams</P>", "<P>With Platform teams</P>"),
        encoding="utf-8",
    )

    requests = diff_folder(golden_folder)
    google_id = id_mapping["e191"]

    blob = json.dumps(requests)
    assert '"ALL"' not in blob, "range edits must never touch the whole text"

    # Pristine text is "GCCs in India\nWith Innovation teams": paragraph 2
    # starts at index 14, "With " is 5 units, so "Innovation" spans 19..29.
    # The untouched paragraph must not appear in any request.
    assert requests == [
        {
            "deleteText": {
                "objectId": google_id,
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 19,
                    "endIndex": 29,
                },
            }
        },
        {
            "insertText": {
                "objectId": google_id,
                "insertionIndex": 19,
                "text": "Platform",
            }
        },
    ]


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
