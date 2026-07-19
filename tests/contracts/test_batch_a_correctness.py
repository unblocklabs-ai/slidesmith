"""Batch A correctness contracts for lossless pull/diff/push behavior."""

from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from extraslide.bounds import BoundingBox
from extraslide.classes import TextStyle, parse_text_style_classes
from extraslide.client import (
    SlidesClient,
    _collect_request_object_ids,
    diff_folder,
)
from extraslide.content_diff import (
    Change,
    ChangeType,
    DiffResult,
    diff_presentation,
    diff_slide_content,
)
from extraslide.content_generator import generate_slide_content
from extraslide.content_parser import ParsedRun, parse_slide_content
from extraslide.content_requests import (
    _apply_text_style_requests,
    _create_line_request,
    _create_shape_request,
    _create_text_update_requests,
    _order_deletes_for_safe_removal,
    generate_batch_requests,
)
from extraslide.id_manager import assign_ids
from extraslide.render_tree import RenderNode
from extraslide.slide_processor import process_presentation
from extraslide.style_extractor import _extract_color
from extraslide.transport import PresentationData, Transport
from extraslide.units import pt_to_emu
from slidesmith.workspace import materialize

GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)
from slidesmith.workspace import materialize


GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)


def _text_element(object_id: str, paragraphs: list[str]) -> dict[str, Any]:
    text_elements: list[dict[str, Any]] = []
    index = 0
    for paragraph in paragraphs:
        content = paragraph + "\n"
        text_elements.extend(
            [
                {
                    "paragraphMarker": {"style": {}},
                    "startIndex": index,
                    "endIndex": index + len(content),
                },
                {
                    "textRun": {"content": content, "style": {}},
                    "startIndex": index,
                    "endIndex": index + len(content),
                },
            ]
        )
        index += len(content)
    return _shape_element(
        object_id,
        shape={"shapeType": "TEXT_BOX", "text": {"textElements": text_elements}},
    )


def _shape_element(
    object_id: str,
    *,
    shape: dict[str, Any] | None = None,
    scale_x: float = 1,
    scale_y: float = 1,
    translate_x: int = 0,
    translate_y: int = 0,
) -> dict[str, Any]:
    return {
        "objectId": object_id,
        "size": {
            "width": {"magnitude": 3000024, "unit": "EMU"},
            "height": {"magnitude": 3000024, "unit": "EMU"},
        },
        "transform": {
            "scaleX": scale_x,
            "scaleY": scale_y,
            "translateX": translate_x,
            "translateY": translate_y,
            "unit": "EMU",
        },
        "shape": shape or {"shapeType": "RECTANGLE", "shapeProperties": {}},
    }


def _presentation(elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "presentationId": "pid",
        "title": "Batch A",
        "pageSize": {
            "width": {"magnitude": 9144000, "unit": "EMU"},
            "height": {"magnitude": 5143500, "unit": "EMU"},
        },
        "slides": [{"objectId": "slide-google", "pageElements": elements}],
    }


def _requests_for_edit(
    result: dict[str, Any], edited_sml: str
) -> list[dict[str, Any]]:
    pristine = result["slides"][0]["content"]
    diff = diff_presentation(
        {"01": parse_slide_content(pristine)},
        {"01": parse_slide_content(edited_sml)},
        result["styles"],
        result["id_mapping"],
    )
    return generate_batch_requests(
        diff, result["id_mapping"], {"01": "slide-google"}
    )


def test_lossless_text_projection_locks_blank_spaces_and_auto_text_ranges() -> None:
    element = _text_element("text-google", ["Title", "", "  Body  "])
    text_elements = element["shape"]["text"]["textElements"]
    text_elements.extend(
        [
            {"paragraphMarker": {"style": {}}},
            {
                "autoText": {
                    "type": "SLIDE_NUMBER",
                    "content": "7\n",
                    "style": {"bold": True},
                }
            },
        ]
    )
    result = process_presentation(_presentation([element]))
    sml = result["slides"][0]["content"]

    assert "<P />" in sml
    assert "<P>  Body  </P>" in sml
    assert 'auto-text="SLIDE_NUMBER"' in sml
    parsed = parse_slide_content(sml)[0]
    assert parsed.paragraphs == ["Title", "", "  Body  ", "7"]
    assert parsed.runs[3][0].auto_text_type == "SLIDE_NUMBER"
    assert _requests_for_edit(result, sml) == []

    requests = _requests_for_edit(result, sml.replace("Body", "Bodyz"))
    insertion = next(request["insertText"] for request in requests if "insertText" in request)
    assert insertion == {"objectId": "text-google", "insertionIndex": 13, "text": "z"}


def test_move_preserves_scaled_flipped_transform_and_resize_changes_visual_width() -> None:
    element = _shape_element(
        "flipped-shape",
        scale_x=-0.5,
        scale_y=0.5,
        translate_x=pt_to_emu(200),
        translate_y=pt_to_emu(72),
    )
    result = process_presentation(_presentation([element]))
    sml = result["slides"][0]["content"]
    parsed = parse_slide_content(sml)[0]

    moved = sml.replace(f'x="{parsed.x:g}"', f'x="{parsed.x + 20:g}"')
    move_request = _requests_for_edit(result, moved)[0]["updatePageElementTransform"]
    assert move_request["applyMode"] == "RELATIVE"
    assert move_request["transform"]["scaleX"] == 1
    assert move_request["transform"]["scaleY"] == 1
    assert move_request["transform"]["translateX"] == pt_to_emu(20)

    resized = sml.replace(f'w="{parsed.w:g}"', 'w="200"')
    resize_request = _requests_for_edit(result, resized)[0]["updatePageElementTransform"]
    assert resize_request["applyMode"] == "ABSOLUTE"
    assert resize_request["transform"]["scaleX"] < 0
    assert abs(resize_request["transform"]["scaleX"]) * 3000024 == pytest.approx(
        pt_to_emu(200)
    )


def test_delete_order_uses_pristine_hierarchy_not_id_spelling() -> None:
    opaque = {"group-A", "child-Z", "child-A", "unrelated"}
    types = {
        "group-A": "GROUP",
        "child-Z": "RECTANGLE",
        "child-A": "TEXT_BOX",
        "unrelated": "RECTANGLE",
    }
    parents = {
        "group-A": None,
        "child-Z": "group-A",
        "child-A": "group-A",
        "unrelated": None,
    }
    assert _order_deletes_for_safe_removal(opaque, types, parents) == [
        "group-A",
        "unrelated",
    ]

    copy_named = {"copy_root", "copy_root_c0_0"}
    assert _order_deletes_for_safe_removal(
        copy_named,
        {value: "RECTANGLE" for value in copy_named},
        {value: None for value in copy_named},
    ) == ["copy_root", "copy_root_c0_0"]


def test_copying_wrapper_keeps_original_child_edit_and_final_child_position() -> None:
    pristine = (
        '<Slide id="s1"><Rect id="card" x="0" y="0" w="200" h="200">'
        '<TextBox id="label" x="10" y="10" w="50" h="25"><P>old</P>'
        "</TextBox></Rect></Slide>"
    )
    edited = pristine.replace("<P>old</P>", "<P>changed</P>").replace(
        "</Slide>",
        '<Rect id="card" x="300" y="0"><TextBox id="label" x="310" y="10" '
        'w="50" h="25"><P>old</P></TextBox></Rect></Slide>',
    )
    changes = diff_slide_content(
        pristine,
        edited,
        {
            "card": {"type": "RECTANGLE", "position": {"x": 0, "y": 0, "w": 200, "h": 200}},
            "label": {"type": "TEXT_BOX", "position": {"x": 10, "y": 10, "w": 50, "h": 25}},
        },
        "01",
    )
    assert [change.change_type for change in changes] == [
        ChangeType.COPY,
        ChangeType.TEXT_UPDATE,
    ]
    requests = generate_batch_requests(
        DiffResult(
            changes=changes,
            pristine_styles={
                "card": {"type": "RECTANGLE", "position": {"x": 0, "y": 0, "w": 200, "h": 200}},
                "label": {"type": "TEXT_BOX", "position": {"x": 10, "y": 10, "w": 50, "h": 25}},
            },
        ),
        {"card": "card-google", "label": "label-google"},
        {"01": "slide-google"},
    )
    created = [request["createShape"] for request in requests if "createShape" in request]
    assert created[1]["elementProperties"]["transform"]["translateX"] == pt_to_emu(310)
    assert any(request.get("deleteText", {}).get("objectId") == "label-google" for request in requests)


def test_zero_extents_are_non_singular() -> None:
    line = _create_line_request("line", "slide", {"x": 0, "y": 0, "w": 0, "h": 0})
    size = line["createLine"]["elementProperties"]["size"]
    assert size["width"]["magnitude"] == 1
    assert size["height"]["magnitude"] == 1
    shape = _create_shape_request("shape", "slide", "RECTANGLE", {"x": 0, "y": 0, "w": 0, "h": 0})
    transform = shape["createShape"]["elementProperties"]["transform"]
    assert transform["scaleX"] > 0
    assert transform["scaleY"] > 0


def test_generated_namespace_ids_cannot_shadow_slide_order() -> None:
    data = _presentation([_shape_element("s2"), _shape_element("g7"), _shape_element("m3")])
    data["slides"].append({"objectId": "slide-google-2", "pageElements": []})
    result = process_presentation(data)
    assert result["id_mapping"]["s1"] == "slide-google"
    assert result["id_mapping"]["s2"] == "slide-google-2"
    assert "s2" in result["id_mapping"].values()
    client = SlidesClient(_StaticTransport(data))
    assert client._build_slide_id_mapping(
        result["id_mapping"], result["presentation_info"]["slideOrder"]
    ) == {"01": "slide-google", "02": "slide-google-2"}


def test_full_create_tag_map_and_unknown_tag_fail_loudly() -> None:
    triangle = Change(
        ChangeType.CREATE,
        "triangle",
        slide_index="01",
        new_position={"x": 0, "y": 0, "w": 20, "h": 20},
        metadata={"tag": "Triangle"},
    )
    requests = generate_batch_requests(
        DiffResult(changes=[triangle]), {}, {"01": "slide"}
    )
    assert requests[0]["createShape"]["shapeType"] == "TRIANGLE"

    triangle.metadata["tag"] = "TypoShape"
    with pytest.raises(ValueError, match="Unsupported SML element tag 'TypoShape'"):
        generate_batch_requests(DiffResult(changes=[triangle]), {}, {"01": "slide"})


def test_malformed_geometry_names_element_and_attribute() -> None:
    with pytest.raises(ValueError, match=r"Invalid w value '1O0' on element 'bad'"):
        parse_slide_content(
            '<Slide id="s1"><Rect id="bad" x="0" y="0" w="1O0" h="20" /></Slide>'
        )


def test_copy_missing_group_children_or_styles_fails_loudly() -> None:
    change = Change(
        ChangeType.COPY,
        "copy",
        source_id="group",
        slide_index="01",
        new_position={"x": 0, "y": 0, "w": 100, "h": 100},
    )
    with pytest.raises(ValueError, match="pristine style data is missing"):
        generate_batch_requests(DiffResult(changes=[change]), {"group": "g"}, {"01": "s"})
    with pytest.raises(ValueError, match="child data is missing"):
        generate_batch_requests(
            DiffResult(changes=[change], pristine_styles={"group": {"type": "GROUP"}}),
            {"group": "g"},
            {"01": "s"},
        )


def test_font_family_round_trip_preserves_exact_capitalization() -> None:
    style = TextStyle(font_family="IBM Plex Sans")
    classes = style.to_classes()
    assert classes == ["font-family-IBM%20Plex%20Sans"]
    assert parse_text_style_classes(classes).font_family == "IBM Plex Sans"


def test_copied_text_styles_keep_independent_run_ranges() -> None:
    requests = _apply_text_style_requests(
        "copy",
        ["Bold plain"],
        {
            "paragraphs": [
                {
                    "style": {},
                    "runs": [
                        {"content": "Bold ", "style": {"bold": True}},
                        {"content": "plain\n", "style": {"italic": True}},
                    ],
                }
            ]
        },
    )
    assert [request["updateTextStyle"]["textRange"] for request in requests] == [
        {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 5},
        {"type": "FIXED_RANGE", "startIndex": 5, "endIndex": 10},
    ]


def test_color_extraction_uses_shared_rounding() -> None:
    assert _extract_color({"rgbColor": {"red": 0.5, "green": 0.5, "blue": 0.5}}) == "#808080"


def test_removed_run_styling_is_reset_and_utf16_combining_offsets_are_exact() -> None:
    edit_requests = _create_text_update_requests(
        "text", ["e\u0301y"], old_text=["e\u0301x"]
    )
    assert edit_requests == [
        {
            "deleteText": {
                "objectId": "text",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 2,
                    "endIndex": 3,
                },
            }
        },
        {
            "insertText": {
                "objectId": "text",
                "insertionIndex": 2,
                "text": "y",
            }
        },
    ]

    old_runs = [[ParsedRun("e\u0301", TextStyle(bold=True)), ParsedRun("x")]]
    new_runs = [[ParsedRun("e\u0301"), ParsedRun("x")]]
    requests = _create_text_update_requests(
        "text", ["e\u0301x"], new_runs, ["e\u0301x"], old_runs
    )
    assert requests == [
        {
            "updateTextStyle": {
                "objectId": "text",
                "textRange": {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 2},
                "style": {},
                "fields": "bold",
            }
        }
    ]
    fallback = _create_text_update_requests("text", ["a", "b"], old_text=None)
    assert fallback[0]["deleteText"]["textRange"] == {"type": "ALL"}


def test_deep_group_copy_builds_nested_groups_and_reserves_every_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("extraslide.content_requests._get_unique_suffix", lambda: "fixed")
    change = Change(
        ChangeType.COPY,
        "copy",
        source_id="root",
        slide_index="01",
        new_position={"x": 100, "y": 0, "w": 100, "h": 100},
        translation={"dx": 100, "dy": 0},
        children=[
            {
                "id": "nested",
                "tag": "Group",
                "position": {"x": 0, "y": 0, "w": 50, "h": 50},
                "sourcePosition": {"x": 0, "y": 0, "w": 50, "h": 50},
                "children": [
                    {
                        "id": "leaf",
                        "tag": "Rect",
                        "position": {"x": 5, "y": 5, "w": 10, "h": 10},
                        "sourcePosition": {"x": 5, "y": 5, "w": 10, "h": 10},
                    }
                ],
            }
        ],
    )
    styles = {
        "root": {"type": "GROUP"},
        "nested": {"type": "GROUP", "position": {}},
        "leaf": {"type": "RECTANGLE", "position": {}},
    }
    requests = generate_batch_requests(
        DiffResult(changes=[change], pristine_styles=styles),
        {"root": "copy_01_fixed", "occupied": "copy_01_fixed_2_c0_0"},
        {"01": "slide"},
    )
    created_ids = [
        body[key]
        for request in requests
        for body in request.values()
        if isinstance(body, dict)
        for key in ("objectId", "groupObjectId")
        if isinstance(body.get(key), str)
    ]
    assert "copy_01_fixed" not in created_ids
    assert "copy_01_fixed_2_c0_0" not in created_ids
    assert any(object_id.endswith("_c0_0_2") for object_id in created_ids)
    assert len([request for request in requests if "groupObjects" in request]) == 2
    assert len(created_ids) == len(set(created_ids))


def test_golden_group_copy_and_delete_are_end_to_end_contracts(tmp_path: Path) -> None:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    folder = materialize(data, tmp_path, save_raw=True)
    client = SlidesClient(_StaticTransport(data))
    sml_path = folder / "slides" / "02" / "content.sml"
    pristine_sml = sml_path.read_text(encoding="utf-8")
    root = ET.fromstring(pristine_sml)
    source = root.find(".//*[@id='g4']")
    assert source is not None

    copied = copy.deepcopy(source)
    copied.attrib.pop("w")
    copied.attrib.pop("h")
    copied.set("x", str(float(copied.get("x", "0")) + 100))
    root.append(copied)
    sml_path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
    copy_requests = client.diff(folder)
    assert any("groupObjects" in request for request in copy_requests)
    created_ids, _ = _collect_request_object_ids(copy_requests)
    assert len(created_ids) == len(set(created_ids))

    sml_path.write_text(pristine_sml, encoding="utf-8")
    root = ET.fromstring(pristine_sml)
    source = root.find(".//*[@id='g4']")
    assert source is not None
    root.remove(source)
    sml_path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
    delete_requests = client.diff(folder)
    deleted = [request["deleteObject"]["objectId"] for request in delete_requests]
    id_mapping = json.loads((folder / "id_mapping.json").read_text(encoding="utf-8"))
    assert deleted == [id_mapping["g4"]]


def test_golden_fixture_deep_group_copy_preserves_translation_contract(
    tmp_path: Path,
) -> None:
    """T-G2: exercise the real golden group's nested recursive request path."""
    folder = materialize(json.loads(GOLDEN.read_text(encoding="utf-8")), tmp_path)
    sml = folder / "slides" / "01" / "content.sml"
    root = ET.fromstring(sml.read_text(encoding="utf-8"))
    source_group = root.find(".//Group")
    assert source_group is not None

    copied_group = copy.deepcopy(source_group)
    copied_group.attrib.pop("w")
    copied_group.attrib.pop("h")
    copied_group.set("x", str(float(source_group.get("x", "0")) + 10))
    root.append(copied_group)
    sml.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")

    requests = diff_folder(folder)
    group_requests = [request["groupObjects"] for request in requests if "groupObjects" in request]
    assert len(group_requests) == 3
    assert len(group_requests[-1]["childrenObjectIds"]) == len(source_group)

    first_leaf = next(element for element in source_group.iter() if element.tag != "Group")
    first_create = next(
        body
        for request in requests
        for operation, body in request.items()
        if operation in {"createShape", "createLine", "createImage"}
    )
    created_x = first_create["elementProperties"]["transform"]["translateX"]
    assert created_x == pytest.approx(pt_to_emu(float(first_leaf.get("x", "0")) + 10))


class _StaticTransport(Transport):
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    async def get_presentation(self, presentation_id: str) -> PresentationData:
        return PresentationData(presentation_id, copy.deepcopy(self.data))

    async def batch_update(
        self,
        presentation_id: str,
        requests: list[dict[str, Any]],
        required_revision_id: str | None = None,
    ) -> dict[str, Any]:
        return {"replies": []}

    async def close(self) -> None:
        pass


async def test_pull_prunes_stale_slides_and_preserves_edited_orphans(tmp_path: Path) -> None:
    data = _presentation([])
    data["slides"].append({"objectId": "slide-two", "pageElements": []})
    two_slide_data = copy.deepcopy(data)
    transport = _StaticTransport(data)
    client = SlidesClient(transport)
    await client.pull("pid", tmp_path)
    folder = tmp_path / "pid"

    transport.data["slides"] = transport.data["slides"][:1]
    await client.pull("pid", tmp_path)
    assert not (folder / "slides" / "02").exists()

    transport.data = copy.deepcopy(two_slide_data)
    await client.pull("pid", tmp_path)
    stale = folder / "slides" / "02" / "content.sml"
    stale.write_text(stale.read_text(encoding="utf-8") + "\n<!-- local -->", encoding="utf-8")
    transport.data["slides"] = transport.data["slides"][:1]
    await client.pull("pid", tmp_path)
    assert not stale.exists()
    assert (folder / ".orphaned-slides" / "02" / "content.sml").exists()

    transport.data = copy.deepcopy(two_slide_data)
    await client.pull("pid", tmp_path)
    (folder / "slides" / "02" / "notes.txt").write_text(
        "keep me", encoding="utf-8"
    )
    transport.data["slides"] = transport.data["slides"][:1]
    await client.pull("pid", tmp_path)
    assert (
        folder / ".orphaned-slides" / "02-2" / "notes.txt"
    ).read_text(encoding="utf-8") == "keep me"


def test_containment_threshold_tolerates_two_decimal_rounding() -> None:
    outer = BoundingBox(0, 0, 70, 10)
    inner = BoundingBox(0, 0, 100.00000001, 10)
    assert outer.contains(inner, threshold=0.7)
