"""ROUND-2 adversarial regression contracts."""

from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

import pytest

from slidesmith.engine.content_diff import ChangeType, DiffResult, diff_slide_content
from slidesmith.engine.content_parser import parse_slide_content
from slidesmith.engine.content_requests import generate_batch_requests
from slidesmith.engine.diff_model import PushWarning, WarningSeverity
from slidesmith.engine.text_requests import _create_text_update_requests
from slidesmith.engine.units import pt_to_emu
from slidesmith.workspace import materialize


GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)


def _materialized_golden(tmp_path: Path) -> Path:
    return materialize(json.loads(GOLDEN.read_text(encoding="utf-8")), tmp_path)


def _edit_golden_element(
    folder: Path,
    element_id: str,
    edit: Callable[[ET.Element], None],
) -> list[dict[str, object]]:
    for sml_path in sorted((folder / "slides").glob("*/content.sml")):
        root = ET.fromstring(sml_path.read_text(encoding="utf-8"))
        element = root.find(f".//*[@id='{element_id}']")
        if element is None:
            continue
        edit(element)
        sml_path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
        from slidesmith.engine.client import diff_folder

        return diff_folder(folder)
    raise AssertionError(f"golden element {element_id!r} not found")


def test_nested_move_uses_absolute_sml_delta_not_relative_style_position(
    tmp_path: Path,
) -> None:
    folder = _materialized_golden(tmp_path)

    def move_one_point(element: ET.Element) -> None:
        element.set("x", str(float(element.get("x", "0")) + 1))

    requests = _edit_golden_element(folder, "e76", move_one_point)
    transform = next(
        request["updatePageElementTransform"]
        for request in requests
        if "updatePageElementTransform" in request
    )
    assert transform["applyMode"] == "RELATIVE"
    assert transform["transform"]["translateX"] == pt_to_emu(1)
    assert transform["transform"]["translateY"] == 0


def test_nested_resize_keeps_the_absolute_sml_anchor(tmp_path: Path) -> None:
    folder = _materialized_golden(tmp_path)

    def widen_one_point(element: ET.Element) -> None:
        element.set("w", str(float(element.get("w", "0")) + 1))

    requests = _edit_golden_element(folder, "e76", widen_one_point)
    transform = next(
        request["updatePageElementTransform"]
        for request in requests
        if "updatePageElementTransform" in request
    )
    style = json.loads((folder / "styles.json").read_text(encoding="utf-8"))[
        "e76"
    ]
    native_size = style["nativeSize"]
    native_transform = style["nativeTransform"]
    applied = transform["transform"]
    assert applied["scaleX"] * native_size["w"] == pytest.approx(
        pt_to_emu(237.22)
    )
    # The API transform for a real group child is group-local. Resizing the
    # child in place must preserve that native local anchor, not replace it
    # with the absolute SML x/y coordinates.
    assert applied["translateX"] == pytest.approx(native_transform["translateX"])
    assert applied["translateY"] == pytest.approx(native_transform["translateY"])


@pytest.mark.parametrize(
    ("new_text", "insertion_index"),
    [
        (["A", "", "B"], 2),
        (["A", "B", ""], 3),
    ],
)
def test_empty_paragraph_insertions_emit_the_missing_newline(
    new_text: list[str], insertion_index: int
) -> None:
    assert _create_text_update_requests(
        "text-google", new_text, old_text=["A", "B"]
    ) == [
        {
            "insertText": {
                "objectId": "text-google",
                "insertionIndex": insertion_index,
                "text": "\n",
            }
        }
    ]


def test_removing_fill_and_outline_classes_resets_them_to_inherit() -> None:
    pristine = (
        '<Slide id="s1"><Rect id="card" x="0" y="0" w="100" h="50" '
        'class="fill-#ff0000 stroke-#000000" /></Slide>'
    )
    edited = '<Slide id="s1"><Rect id="card" x="0" y="0" w="100" h="50" /></Slide>'
    changes = diff_slide_content(pristine, edited, {}, "01")
    assert [change.change_type for change in changes] == [ChangeType.STYLE_UPDATE]

    requests = generate_batch_requests(
        DiffResult(changes=changes),
        {"card": "card-google"},
        {"01": "slide-google"},
        {"card": "RECTANGLE"},
    )
    resets = [request["updateShapeProperties"] for request in requests]
    assert resets == [
        {
            "objectId": "card-google",
            "shapeProperties": {
                "shapeBackgroundFill": {"propertyState": "INHERIT"}
            },
            "fields": "shapeBackgroundFill.propertyState",
        },
        {
            "objectId": "card-google",
            "shapeProperties": {"outline": {"propertyState": "INHERIT"}},
            "fields": "outline.propertyState",
        },
    ]


def test_run_family_swap_with_unchanged_weight_applies_weighted_family() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="label"><P>'
        '<T class="font-family-arial font-weight-700">Hi</T>'
        "</P></TextBox></Slide>"
    )
    edited = (
        '<Slide id="s1"><TextBox id="label"><P>'
        '<T class="font-family-montserrat font-weight-700">Hi</T>'
        "</P></TextBox></Slide>"
    )
    changes = diff_slide_content(pristine, edited, {}, "01")
    requests = generate_batch_requests(
        DiffResult(changes=changes),
        {"label": "label-google"},
        {"01": "slide-google"},
    )

    assert requests == [
        {
            "updateTextStyle": {
                "objectId": "label-google",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 0,
                    "endIndex": 2,
                },
                "style": {
                    "weightedFontFamily": {
                        "fontFamily": "Montserrat",
                        "weight": 700,
                    }
                },
                "fields": "weightedFontFamily",
            }
        }
    ]


def test_element_family_swap_with_unchanged_weight_applies_weighted_family() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="label" '
        'class="font-family-arial font-weight-700"><P>Hi</P>'
        "</TextBox></Slide>"
    )
    edited = (
        '<Slide id="s1"><TextBox id="label" '
        'class="font-family-montserrat font-weight-700"><P>Hi</P>'
        "</TextBox></Slide>"
    )
    changes = diff_slide_content(pristine, edited, {}, "01")
    requests = generate_batch_requests(
        DiffResult(changes=changes),
        {"label": "label-google"},
        {"01": "slide-google"},
    )

    assert requests == [
        {
            "updateTextStyle": {
                "objectId": "label-google",
                "textRange": {"type": "ALL"},
                "style": {
                    "weightedFontFamily": {
                        "fontFamily": "Montserrat",
                        "weight": 700,
                    }
                },
                "fields": "weightedFontFamily",
            }
        }
    ]


def test_removing_whole_element_family_class_emits_weighted_family_reset() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="label" '
        'class="font-family-arial font-weight-700"><P>Hi</P>'
        "</TextBox></Slide>"
    )
    edited = '<Slide id="s1"><TextBox id="label"><P>Hi</P></TextBox></Slide>'
    changes = diff_slide_content(pristine, edited, {}, "01")
    requests = generate_batch_requests(
        DiffResult(changes=changes),
        {"label": "label-google"},
        {"01": "slide-google"},
    )

    assert requests == [
        {
            "updateTextStyle": {
                "objectId": "label-google",
                "textRange": {"type": "ALL"},
                "style": {},
                "fields": "weightedFontFamily",
            }
        }
    ]


def test_run_family_swap_without_weight_applies_unweighted_family() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="label"><P>'
        '<T class="font-family-arial">Hi</T>'
        "</P></TextBox></Slide>"
    )
    edited = (
        '<Slide id="s1"><TextBox id="label"><P>'
        '<T class="font-family-montserrat">Hi</T>'
        "</P></TextBox></Slide>"
    )
    changes = diff_slide_content(pristine, edited, {}, "01")
    requests = generate_batch_requests(
        DiffResult(changes=changes),
        {"label": "label-google"},
        {"01": "slide-google"},
    )

    assert requests == [
        {
            "updateTextStyle": {
                "objectId": "label-google",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 0,
                    "endIndex": 2,
                },
                "style": {"fontFamily": "Montserrat"},
                "fields": "fontFamily",
            }
        }
    ]


def test_copy_merges_pristine_and_edited_run_styles_on_new_text_ranges() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="label" x="0" y="0" w="100" h="30">'
        "<P>Old</P></TextBox></Slide>"
    )
    edited = (
        '<Slide id="s1"><TextBox id="label" x="100" y="0">'
        '<P><T class="bold">Longer</T> plain</P></TextBox></Slide>'
    )
    source_style = {
        "type": "TEXT_BOX",
        "position": {"x": 0, "y": 0, "w": 100, "h": 30, "relative": False},
        "text": {
            "paragraphs": [
                {
                    "style": {},
                    "runs": [{"content": "Old\n", "style": {"italic": True}}],
                }
            ]
        },
    }
    changes = diff_slide_content(pristine, edited, {"label": source_style}, "01")
    assert changes[0].new_runs == parse_slide_content(edited)[0].runs

    requests = generate_batch_requests(
        DiffResult(changes=changes, pristine_styles={"label": source_style}),
        {"label": "label-google"},
        {"01": "slide-google"},
    )
    updates = [
        request["updateTextStyle"]
        for request in requests
        if "updateTextStyle" in request
    ]
    assert updates == [
        {
            "objectId": updates[0]["objectId"],
            "textRange": {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 3},
            "style": {"italic": True},
            "fields": "italic",
        },
        {
            "objectId": updates[0]["objectId"],
            "textRange": {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 6},
            "style": {"bold": True},
            "fields": "bold",
        }
    ]


def test_copy_of_auto_text_duplicates_the_dynamic_run_instead_of_inserting_text() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="number" x="0" y="0" w="20" h="20">'
        '<P><T auto-text="SLIDE_NUMBER">1</T></P></TextBox></Slide>'
    )
    edited = (
        '<Slide id="s1"><TextBox id="number" x="50" y="0">'
        '<P><T auto-text="SLIDE_NUMBER">1</T></P></TextBox></Slide>'
    )
    source_style = {
        "type": "TEXT_BOX",
        "position": {"x": 0, "y": 0, "w": 20, "h": 20, "relative": False},
    }
    changes = diff_slide_content(pristine, edited, {"number": source_style}, "01")
    assert changes[0].new_runs and changes[0].new_runs[0][0].auto_text_type == "SLIDE_NUMBER"

    requests = generate_batch_requests(
        DiffResult(changes=changes, pristine_styles={"number": source_style}),
        {"number": "number-google"},
        {"01": "slide-google"},
    )
    assert requests[0].get("duplicateObject", {}).get("objectId") == "number-google"
    assert not any("insertText" in request for request in requests)


def test_golden_image_copy_replays_writable_properties_and_warns_on_dropped_adjustments(
    tmp_path: Path,
) -> None:
    folder = _materialized_golden(tmp_path)

    # Append beside the source, not inside the self-closing Image element.
    for sml_path in sorted((folder / "slides").glob("*/content.sml")):
        root = ET.fromstring(sml_path.read_text(encoding="utf-8"))
        source = root.find(".//*[@id='e125']")
        if source is None:
            continue
        copied = copy.deepcopy(source)
        copied.attrib.pop("w")
        copied.attrib.pop("h")
        copied.set("x", str(float(copied.get("x", "0")) + 10))
        root.append(copied)
        sml_path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
        break
    else:
        raise AssertionError("golden element 'e125' not found")

    from slidesmith.engine.client import diff_folder_with_result

    diff_result, requests = diff_folder_with_result(folder)
    image_update = next(
        request["updateImageProperties"]
        for request in requests
        if "updateImageProperties" in request
    )
    assert image_update["fields"] == "outline"
    assert set(image_update["imageProperties"]) == {"outline"}
    assert diff_result.warnings == [
        PushWarning(
            WarningSeverity.WARNING,
            "copy 'e125': image adjustments crop, shadow cannot be preserved because "
            "the Google Slides API exposes them as read-only; the copy uses the source "
            "image without those adjustments",
        )
    ]
