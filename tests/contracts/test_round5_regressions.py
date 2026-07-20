"""ROUND-5 duplicateObject copy-seam regression contracts."""

from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

from slidesmith.workspace import materialize


GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)


def _text_page_element(
    object_id: str,
    text: str,
    *,
    x: float,
    y: float,
    auto_text: bool = False,
) -> dict[str, Any]:
    run_key = "autoText" if auto_text else "textRun"
    run = (
        {"type": "SLIDE_NUMBER", "content": text, "style": {}}
        if auto_text
        else {"content": text, "style": {}}
    )
    return {
        "objectId": object_id,
        "size": {
            "width": {"magnitude": 1270000, "unit": "EMU"},
            "height": {"magnitude": 381000, "unit": "EMU"},
        },
        "transform": {
            "scaleX": 1,
            "scaleY": 1,
            "translateX": x,
            "translateY": y,
            "unit": "EMU",
        },
        "shape": {
            "shapeType": "TEXT_BOX",
            "text": {
                "textElements": [
                    {
                        "startIndex": 0,
                        "endIndex": len(text) + 1,
                        "paragraphMarker": {"style": {"alignment": "START"}},
                    },
                    {
                        "startIndex": 0,
                        "endIndex": len(text),
                        run_key: run,
                    },
                    {
                        "startIndex": len(text),
                        "endIndex": len(text) + 1,
                        "textRun": {"content": "\n", "style": {}},
                    },
                ]
            },
        },
    }


def _materialized_round5_golden(tmp_path: Path) -> Path:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    data["slides"][0].setdefault("pageElements", []).append(
        {
            "objectId": "rg_group",
            "transform": {
                "scaleX": 1,
                "scaleY": 1,
                "translateX": 0,
                "translateY": 0,
                "unit": "EMU",
            },
            "elementGroup": {
                "children": [
                    _text_page_element(
                        "rg_number", "1", x=127000, y=127000, auto_text=True
                    ),
                    _text_page_element(
                        "rg_caption", "Original caption", x=127000, y=635000
                    ),
                ]
            },
        }
    )
    return materialize(data, tmp_path, save_raw=True)


def _edit_slide(folder: Path, edit: Callable[[ET.Element, ET.Element], None]) -> None:
    sml_path = folder / "slides" / "01" / "content.sml"
    root = ET.fromstring(sml_path.read_text(encoding="utf-8"))
    original = root.find(".//*[@id='rg_group']")
    assert original is not None
    edit(root, original)
    sml_path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")


def _copy_group(original: ET.Element, *, offset: float = 100) -> ET.Element:
    copied = copy.deepcopy(original)
    copied.attrib.pop("w")
    copied.attrib.pop("h")
    copied.set("x", str(float(original.get("x", "0")) + offset))
    return copied


def _request_index(
    requests: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
) -> int:
    return next(index for index, request in enumerate(requests) if predicate(request))


def test_duplicate_precedes_deleting_a_source_group_child(tmp_path: Path) -> None:
    folder = _materialized_round5_golden(tmp_path)

    def edit(root: ET.Element, original: ET.Element) -> None:
        copied = _copy_group(original)
        caption = original.find(".//*[@id='rg_caption']")
        assert caption is not None
        original.remove(caption)
        root.append(copied)

    _edit_slide(folder, edit)

    from extraslide.client import diff_folder

    requests = diff_folder(folder)
    duplicate_index = _request_index(requests, lambda request: "duplicateObject" in request)
    delete_index = _request_index(
        requests,
        lambda request: request.get("deleteObject", {}).get("objectId") == "rg_caption",
    )
    assert duplicate_index < delete_index


def test_duplicates_precede_source_text_edits_for_edited_and_pristine_copies(
    tmp_path: Path,
) -> None:
    folder = _materialized_round5_golden(tmp_path)

    def edit(root: ET.Element, original: ET.Element) -> None:
        edited_copy = _copy_group(original)
        pristine_copy = _copy_group(original, offset=200)
        original_caption = original.find(".//*[@id='rg_caption']/P")
        copied_caption = edited_copy.find(".//*[@id='rg_caption']/P")
        assert original_caption is not None
        assert copied_caption is not None
        original_caption.text = "Short"
        copied_caption.text = "A much longer copied caption"
        root.extend([edited_copy, pristine_copy])

    _edit_slide(folder, edit)

    from extraslide.client import diff_folder

    requests = diff_folder(folder)
    duplicate_indices = [
        index for index, request in enumerate(requests) if "duplicateObject" in request
    ]
    source_text_indices = [
        index
        for index, request in enumerate(requests)
        if any(
            request.get(operation, {}).get("objectId") == "rg_caption"
            for operation in ("deleteText", "insertText", "updateTextStyle")
        )
    ]
    assert len(duplicate_indices) == 2
    assert source_text_indices
    assert max(duplicate_indices) < min(source_text_indices)


def test_duplicate_precedes_deleting_source_group_when_only_copy_survives(
    tmp_path: Path,
) -> None:
    folder = _materialized_round5_golden(tmp_path)

    def edit(root: ET.Element, original: ET.Element) -> None:
        copied = _copy_group(original)
        parent = next(parent for parent in root.iter() if original in list(parent))
        parent.remove(original)
        parent.append(copied)

    _edit_slide(folder, edit)

    from extraslide.client import diff_folder

    requests = diff_folder(folder)
    duplicate_index = _request_index(requests, lambda request: "duplicateObject" in request)
    source_child_delete_indices = [
        index
        for index, request in enumerate(requests)
        if request.get("deleteObject", {}).get("objectId")
        in {"rg_number", "rg_caption"}
    ]
    assert len(source_child_delete_indices) == 2
    assert duplicate_index < min(source_child_delete_indices)


def test_duplicate_maps_and_deletes_descendant_removed_from_authored_copy(
    tmp_path: Path,
) -> None:
    folder = _materialized_round5_golden(tmp_path)

    def edit(root: ET.Element, original: ET.Element) -> None:
        copied = _copy_group(original)
        caption = copied.find(".//*[@id='rg_caption']")
        assert caption is not None
        copied.remove(caption)
        root.append(copied)

    _edit_slide(folder, edit)

    from extraslide.client import diff_folder

    requests = diff_folder(folder)
    duplicate_index = _request_index(requests, lambda request: "duplicateObject" in request)
    object_ids = requests[duplicate_index]["duplicateObject"]["objectIds"]
    assert set(object_ids) == {"rg_group", "rg_number", "rg_caption"}
    copied_caption_id = object_ids["rg_caption"]
    delete_index = _request_index(
        requests,
        lambda request: request.get("deleteObject", {}).get("objectId")
        == copied_caption_id,
    )
    assert duplicate_index < delete_index


def test_duplicate_warns_when_authored_child_position_is_ambiguous(
    tmp_path: Path,
) -> None:
    folder = _materialized_round5_golden(tmp_path)

    def edit(root: ET.Element, original: ET.Element) -> None:
        copied = _copy_group(original)
        caption = copied.find(".//*[@id='rg_caption']")
        assert caption is not None
        caption.set("x", "35")
        root.append(copied)

    _edit_slide(folder, edit)

    from extraslide.client import diff_folder_with_result

    diff_result, requests = diff_folder_with_result(folder)
    assert any("duplicateObject" in request for request in requests)
    assert diff_result.warnings == [
        "copy 'rg_group' child 'rg_caption': authored position (35, 50) matches neither "
        "the source position (10, 50) nor the translated copy position (110, 50); "
        "Slidesmith applied the parent translation, so verify the copied child position"
    ]
