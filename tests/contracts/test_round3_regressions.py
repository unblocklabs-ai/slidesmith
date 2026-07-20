"""ROUND-3 transform regression contracts."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

import pytest

from extraslide.units import pt_to_emu
from slidesmith.workspace import materialize


GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)


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
        from extraslide.client import diff_folder

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
