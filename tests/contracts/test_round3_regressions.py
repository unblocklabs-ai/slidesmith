"""ROUND-3 transform regression contracts."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

import pytest

from slidesmith.engine.client import SlidesClient
from slidesmith.engine.content_diff import (
    Change,
    ChangeType,
    DiffResult,
    diff_slide_content,
)
from slidesmith.engine.content_parser import ParsedRun
from slidesmith.engine.content_requests import generate_batch_requests
from slidesmith.engine.diff_model import PushWarning, WarningSeverity
from slidesmith.engine.transport import PresentationData, Transport
from slidesmith.engine.units import pt_to_emu
from slidesmith.workspace import materialize


GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)


class _WarningTransport(Transport):
    async def get_presentation(self, presentation_id: str) -> PresentationData:
        return PresentationData(presentation_id, {"presentationId": presentation_id})

    async def batch_update(
        self,
        presentation_id: str,
        requests: list[dict[str, object]],
        required_revision_id: str | None = None,
    ) -> dict[str, object]:
        return {"replies": []}

    async def close(self) -> None:
        return None


def _edit_element(
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
    raise AssertionError(f"element {element_id!r} not found")


def _materialized_golden(tmp_path: Path) -> Path:
    return materialize(json.loads(GOLDEN.read_text(encoding="utf-8")), tmp_path)


def _materialized_scaled_group(tmp_path: Path) -> Path:
    presentation = {
        "presentationId": "scaled-group-fixture",
        "title": "Scaled group transform fixture",
        "pageSize": {
            "width": {"magnitude": pt_to_emu(720), "unit": "EMU"},
            "height": {"magnitude": pt_to_emu(405), "unit": "EMU"},
        },
        "slides": [
            {
                "objectId": "slide_scaled",
                "pageElements": [
                    {
                        "objectId": "group_scaled",
                        "transform": {
                            "scaleX": 2,
                            "scaleY": 2,
                            "translateX": pt_to_emu(40),
                            "translateY": pt_to_emu(30),
                            "unit": "EMU",
                        },
                        "elementGroup": {
                            "children": [
                                {
                                    "objectId": "child_shape",
                                    "size": {
                                        "width": {
                                            "magnitude": pt_to_emu(100),
                                            "unit": "EMU",
                                        },
                                        "height": {
                                            "magnitude": pt_to_emu(50),
                                            "unit": "EMU",
                                        },
                                    },
                                    "transform": {
                                        "scaleX": 1,
                                        "scaleY": 1,
                                        "translateX": pt_to_emu(10),
                                        "translateY": pt_to_emu(15),
                                        "unit": "EMU",
                                    },
                                    "shape": {"shapeType": "RECTANGLE"},
                                }
                            ]
                        },
                    }
                ],
            }
        ],
    }
    return materialize(presentation, tmp_path)


def test_group_move_is_relative_translation_without_rescaling(tmp_path: Path) -> None:
    folder = _materialized_golden(tmp_path)

    def move_ten_points(element: ET.Element) -> None:
        element.set("x", str(float(element.get("x", "0")) + 10))

    requests = _edit_element(folder, "g1", move_ten_points)
    transform = next(request["updatePageElementTransform"] for request in requests)

    assert transform == {
        "objectId": "g3b91ac73820_0_49",
        "transform": {
            "scaleX": 1,
            "scaleY": 1,
            "translateX": pt_to_emu(10),
            "translateY": 0,
            "unit": "EMU",
        },
        "applyMode": "RELATIVE",
    }


def test_group_resize_fails_loudly_instead_of_reconstructing_size(tmp_path: Path) -> None:
    folder = _materialized_golden(tmp_path)

    def widen_ten_points(element: ET.Element) -> None:
        element.set("w", str(float(element.get("w", "0")) + 10))

    with pytest.raises(ValueError, match="Resizing groups is not supported"):
        _edit_element(folder, "g1", widen_ten_points)


def test_child_move_converts_page_delta_to_scaled_group_frame(tmp_path: Path) -> None:
    folder = _materialized_scaled_group(tmp_path)

    def move_twenty_points(element: ET.Element) -> None:
        element.set("x", str(float(element.get("x", "0")) + 20))

    requests = _edit_element(folder, "child_shape", move_twenty_points)
    transform = next(request["updatePageElementTransform"] for request in requests)

    assert transform["applyMode"] == "RELATIVE"
    assert transform["transform"]["translateX"] == pt_to_emu(10)
    assert transform["transform"]["translateY"] == 0


def test_child_resize_converts_page_delta_to_scaled_group_frame(tmp_path: Path) -> None:
    folder = _materialized_scaled_group(tmp_path)

    def widen_twenty_points(element: ET.Element) -> None:
        element.set("w", str(float(element.get("w", "0")) + 20))

    requests = _edit_element(folder, "child_shape", widen_twenty_points)
    transform = next(request["updatePageElementTransform"] for request in requests)

    assert transform["applyMode"] == "ABSOLUTE"
    assert transform["transform"]["scaleX"] == pytest.approx(1.1)
    assert transform["transform"]["scaleY"] == pytest.approx(1)


def test_image_copy_emits_only_writable_properties_and_records_dropped_warning() -> None:
    change = Change(
        change_type=ChangeType.COPY,
        target_id="photo_copy0",
        source_id="photo",
        slide_index="01",
        source_slide_index="01",
        new_position={"x": 100, "y": 0, "w": 80, "h": 60},
    )
    source_style = {
        "type": "IMAGE",
        "position": {"x": 0, "y": 0, "w": 80, "h": 60},
        "contentUrl": "https://example.com/photo.png",
        "imageProperties": {
            "transparency": 0.2,
            "brightness": 0.1,
            "contrast": -0.1,
            "crop": {"left": 0.1, "right": 0, "top": 0, "bottom": 0},
            "recolor": "LIGHT1",
            "shadow": {"type": "none"},
            "outline": {"propertyState": "NOT_RENDERED"},
            "link": {"url": "https://example.com/destination"},
        },
    }
    diff_result = DiffResult(
        changes=[change],
        pristine_styles={"photo": source_style},
    )

    requests = generate_batch_requests(
        diff_result,
        {"photo": "photo-google"},
        {"01": "slide-google"},
    )

    image_update = next(
        request["updateImageProperties"]
        for request in requests
        if "updateImageProperties" in request
    )
    assert image_update == {
        "objectId": image_update["objectId"],
        "imageProperties": {
            "outline": {"propertyState": "NOT_RENDERED"},
            "link": {"url": "https://example.com/destination"},
        },
        "fields": "outline,link",
    }
    assert diff_result.warnings == [
        PushWarning(
            WarningSeverity.WARNING,
            "copy 'photo': image adjustments transparency, brightness, contrast, "
            "crop, recolor, shadow cannot be preserved because the Google Slides "
            "API exposes them as read-only; the copy uses the source image without "
            "those adjustments",
        )
    ]


@pytest.mark.asyncio
async def test_copy_generation_warnings_are_returned_by_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "deck"
    folder.mkdir()
    (folder / "presentation.json").write_text(
        json.dumps({"presentationId": "pid"}), encoding="utf-8"
    )
    warning = PushWarning(
        WarningSeverity.WARNING,
        "copy 'photo': image adjustments cannot be preserved",
    )
    diff_result = DiffResult()
    diff_result.warnings = [warning]
    client = SlidesClient(_WarningTransport())
    monkeypatch.setattr(
        client,
        "diff_with_result",
        lambda _folder, **_kwargs: (
            diff_result,
            [{"createImage": {"objectId": "copy"}}],
        ),
    )

    async def no_refresh(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("slidesmith.engine.client.refresh_after_success", no_refresh)

    response = await client.push(folder, force=True)

    assert warning in response["warnings"]


def test_removed_class_groups_emit_field_masked_default_resets() -> None:
    pristine = (
        '<Slide id="s1">'
        '<TextBox id="label" x="0" y="0" w="100" h="30" '
        'class="content-align-middle bold text-align-center"><P>Hi</P></TextBox>'
        '<Line id="rule" x="0" y="40" w="100" h="1" '
        'class="stroke-#ff0000 stroke-w-2 stroke-dash" />'
        "</Slide>"
    )
    edited = (
        '<Slide id="s1">'
        '<TextBox id="label" x="0" y="0" w="100" h="30"><P>Hi</P></TextBox>'
        '<Line id="rule" x="0" y="40" w="100" h="1" />'
        "</Slide>"
    )
    changes = diff_slide_content(pristine, edited, {}, "01")
    requests = generate_batch_requests(
        DiffResult(changes=changes),
        {"label": "label-google", "rule": "rule-google"},
        {"01": "slide-google"},
        {"label-google": "TEXT_BOX", "rule-google": "LINE"},
    )

    assert {
        (next(iter(request)), request[next(iter(request))]["fields"])
        for request in requests
    } == {
        ("updateShapeProperties", "contentAlignment"),
        ("updateTextStyle", "bold"),
        ("updateParagraphStyle", "alignment"),
        ("updateLineProperties", "lineFill,weight,dashStyle"),
    }
    for request in requests:
        body = request[next(iter(request))]
        if "style" in body:
            assert body["style"] == {}
        elif "shapeProperties" in body:
            assert body["shapeProperties"] == {}
        elif "lineProperties" in body:
            assert body["lineProperties"] == {}


def test_single_instance_group_copy_suppresses_descendant_moves() -> None:
    pristine = (
        '<Slide id="s1"><Group id="group" x="0" y="0" w="100" h="100">'
        '<Rect id="child" x="10" y="10" w="20" h="20" />'
        "</Group></Slide>"
    )
    edited = (
        '<Slide id="s1"><Group id="group" x="100" y="0">'
        '<Rect id="child" x="110" y="10" w="20" h="20" />'
        "</Group></Slide>"
    )

    changes = diff_slide_content(pristine, edited, {}, "01")

    assert [change.change_type for change in changes] == [
        ChangeType.COPY,
        ChangeType.DELETE,
    ]
    assert changes[0].source_id == "group"
    assert changes[1].target_id == "child"


def test_nested_auto_text_copy_uses_root_guard_and_fails_cross_slide() -> None:
    change = Change(
        change_type=ChangeType.COPY,
        target_id="group_copy0",
        source_id="group",
        slide_index="02",
        source_slide_index="01",
        new_position={"x": 100, "y": 0, "w": 100, "h": 100},
        children=[
            {
                "id": "number",
                "tag": "TextBox",
                "position": {"x": 110, "y": 10, "w": 20, "h": 20},
                "sourcePosition": {"x": 10, "y": 10, "w": 20, "h": 20},
                "text": ["7"],
                "runs": [[ParsedRun("7", auto_text_type="SLIDE_NUMBER")]],
            }
        ],
    )
    styles = {
        "group": {"type": "GROUP", "position": {"x": 0, "y": 0}},
        "number": {"type": "TEXT_BOX", "position": {"x": 10, "y": 10}},
    }

    with pytest.raises(ValueError, match="Cannot preserve autoText on cross-slide copy"):
        generate_batch_requests(
            DiffResult(changes=[change], pristine_styles=styles),
            {"group": "group-google"},
            {"02": "slide-google-2"},
        )


def test_ambiguous_authored_copy_child_position_records_warning() -> None:
    change = Change(
        change_type=ChangeType.COPY,
        target_id="group_copy0",
        source_id="group",
        slide_index="01",
        source_slide_index="01",
        new_position={"x": 100, "y": 0, "w": 100, "h": 100},
        translation={"dx": 100, "dy": 0},
        children=[
            {
                "id": "child",
                "tag": "Rect",
                "position": {"x": 35, "y": 10, "w": 20, "h": 20},
                "sourcePosition": {"x": 10, "y": 10, "w": 20, "h": 20},
            }
        ],
    )
    styles = {
        "group": {"type": "GROUP", "position": {"x": 0, "y": 0}},
        "child": {"type": "RECTANGLE", "position": {"x": 10, "y": 10}},
    }
    diff_result = DiffResult(changes=[change], pristine_styles=styles)

    generate_batch_requests(
        diff_result,
        {"group": "group-google"},
        {"01": "slide-google"},
    )

    assert diff_result.warnings == [
        PushWarning(
            WarningSeverity.WARNING,
            "copy 'group' child 'child': authored position (35, 10) matches neither "
            "the source position (10, 10) nor the translated copy position (110, 10); "
            "Slidesmith applied the parent translation, so verify the copied child "
            "position",
        )
    ]
