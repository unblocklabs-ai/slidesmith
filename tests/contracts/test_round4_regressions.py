"""ROUND-4 adversarial regression contracts."""

from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

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


def _materialized_golden_with_elements(
    tmp_path: Path,
    elements: list[dict[str, Any]],
    *,
    save_raw: bool = False,
) -> Path:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    data["slides"][0].setdefault("pageElements", []).extend(elements)
    return materialize(data, tmp_path, save_raw=save_raw)


def _text_page_element(
    object_id: str,
    text: str,
    *,
    x: float,
    y: float,
    auto_text: bool = False,
) -> dict[str, Any]:
    text_run_key = "autoText" if auto_text else "textRun"
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
                        text_run_key: run,
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


def _edit_classes(
    element: ET.Element,
    *,
    add: tuple[str, ...] = (),
    remove: tuple[str, ...] = (),
) -> None:
    classes = element.get("class", "").split()
    classes.extend(class_name for class_name in add if class_name not in classes)
    classes = [class_name for class_name in classes if class_name not in remove]
    if classes:
        element.set("class", " ".join(classes))
    else:
        element.attrib.pop("class", None)


def _mutate_element_in_xml(
    content: str,
    element_id: str,
    edit: Callable[[ET.Element], None],
) -> tuple[str, bool]:
    root = ET.fromstring(content)
    element = root.find(f".//*[@id='{element_id}']")
    if element is None:
        return content, False
    edit(element)
    return ET.tostring(root, encoding="unicode"), True


def _prepare_pristine_and_edit_current(
    folder: Path,
    element_id: str,
    prepare: Callable[[ET.Element], None],
    edit: Callable[[ET.Element], None],
) -> None:
    """Apply setup to both snapshots, then the authored edit to current SML."""
    current_path: Path | None = None
    for sml_path in sorted((folder / "slides").glob("*/content.sml")):
        prepared, found = _mutate_element_in_xml(
            sml_path.read_text(encoding="utf-8"), element_id, prepare
        )
        if not found:
            continue
        current_path = sml_path
        edited, _ = _mutate_element_in_xml(prepared, element_id, edit)
        sml_path.write_text(edited, encoding="utf-8")
        break
    if current_path is None:
        raise AssertionError(f"golden element {element_id!r} not found")

    archive_path = folder / ".pristine" / "presentation.zip"
    with zipfile.ZipFile(archive_path, "r") as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    relative_sml = current_path.relative_to(folder).as_posix()
    pristine, found = _mutate_element_in_xml(
        entries[relative_sml].decode("utf-8"), element_id, prepare
    )
    assert found
    entries[relative_sml] = pristine.encode("utf-8")
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)


def _strip_parent_transforms(folder: Path) -> None:
    """Simulate a workspace pulled before composed group transforms existed."""
    styles_path = folder / "styles.json"
    styles = json.loads(styles_path.read_text(encoding="utf-8"))
    for style in styles.values():
        style.pop("parentTransform", None)
    styles_path.write_text(json.dumps(styles, indent=2), encoding="utf-8")

    archive_path = folder / ".pristine" / "presentation.zip"
    with zipfile.ZipFile(archive_path, "r") as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    pristine_styles = json.loads(entries["styles.json"].decode("utf-8"))
    for style in pristine_styles.values():
        style.pop("parentTransform", None)
    entries["styles.json"] = json.dumps(pristine_styles, indent=2).encode("utf-8")
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)


@pytest.mark.parametrize(
    ("element_id", "prepare_classes", "removed_class", "operation", "field"),
    [
        ("e124", ("bold", "italic"), "bold", "updateTextStyle", "bold"),
        ("e132", (), "stroke-w-0.75", "updateShapeProperties", "outline.weight"),
        ("e142", (), "leading-150", "updateParagraphStyle", "lineSpacing"),
    ],
)
def test_partial_element_class_removal_emits_field_reset(
    tmp_path: Path,
    element_id: str,
    prepare_classes: tuple[str, ...],
    removed_class: str,
    operation: str,
    field: str,
) -> None:
    folder = _materialized_golden(tmp_path)
    _prepare_pristine_and_edit_current(
        folder,
        element_id,
        lambda element: _edit_classes(element, add=prepare_classes),
        lambda element: _edit_classes(element, remove=(removed_class,)),
    )

    from slidesmith.engine.client import diff_folder

    requests = diff_folder(folder)
    assert any(
        request.get(operation, {}).get("fields") == field
        and not request[operation].get("style")
        and not request[operation].get("shapeProperties")
        for request in requests
        if operation in request
    )


def test_removed_original_child_is_deleted_when_only_copy_instance_survives(
    tmp_path: Path,
) -> None:
    folder = _materialized_golden(tmp_path)
    sml_path = folder / "slides" / "02" / "content.sml"
    root = ET.fromstring(sml_path.read_text(encoding="utf-8"))
    original = root.find(".//*[@id='g4']")
    assert original is not None
    badge = original.find("./*[@id='e143']")
    assert badge is not None

    copied = copy.deepcopy(original)
    copied.attrib.pop("w")
    copied.attrib.pop("h")
    copied.set("x", str(float(original.get("x", "0")) + 120))
    original.remove(badge)
    root.append(copied)
    sml_path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")

    from slidesmith.engine.client import diff_folder

    requests = diff_folder(folder)
    id_mapping = json.loads((folder / "id_mapping.json").read_text(encoding="utf-8"))
    assert any("groupObjects" in request for request in requests)
    assert {request["deleteObject"]["objectId"] for request in requests if "deleteObject" in request} == {
        id_mapping["e143"]
    }


def test_same_slide_auto_text_group_copy_applies_edited_child_text(
    tmp_path: Path,
) -> None:
    group = {
        "objectId": "r4_group",
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
                    "r4_number", "1", x=127000, y=127000, auto_text=True
                ),
                _text_page_element(
                    "r4_caption", "Original caption", x=127000, y=635000
                ),
            ]
        },
    }
    folder = _materialized_golden_with_elements(tmp_path, [group])
    sml_path = folder / "slides" / "01" / "content.sml"
    root = ET.fromstring(sml_path.read_text(encoding="utf-8"))
    original = root.find(".//*[@id='r4_group']")
    assert original is not None

    copied = copy.deepcopy(original)
    copied.attrib.pop("w")
    copied.attrib.pop("h")
    copied.set("x", str(float(original.get("x", "0")) + 100))
    caption = copied.find(".//*[@id='r4_caption']/P")
    assert caption is not None
    caption.text = "Edited caption"
    root.append(copied)
    sml_path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")

    from slidesmith.engine.client import diff_folder_with_result

    diff_result, requests = diff_folder_with_result(folder)
    assert any("duplicateObject" in request for request in requests)
    assert any(
        request.get("insertText", {}).get("text") == "Edited"
        for request in requests
    )
    assert diff_result.warnings == []


def test_same_slide_auto_text_copy_applies_root_paragraph_style(
    tmp_path: Path,
) -> None:
    folder = _materialized_golden_with_elements(
        tmp_path,
        [_text_page_element("r4_root_number", "1", x=0, y=0, auto_text=True)],
    )
    sml_path = folder / "slides" / "01" / "content.sml"
    root = ET.fromstring(sml_path.read_text(encoding="utf-8"))
    original = root.find(".//*[@id='r4_root_number']")
    assert original is not None

    copied = copy.deepcopy(original)
    copied.attrib.pop("w")
    copied.attrib.pop("h")
    copied.set("x", str(float(original.get("x", "0")) + 100))
    paragraph = copied.find("P")
    assert paragraph is not None
    _edit_classes(paragraph, remove=("text-align-left",), add=("text-align-center",))
    root.append(copied)
    sml_path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")

    from slidesmith.engine.client import diff_folder

    requests = diff_folder(folder)
    assert any(
        request.get("updateParagraphStyle", {}).get("style", {}).get("alignment")
        == "CENTER"
        for request in requests
    )


def test_old_workspace_enrichment_backfills_composed_parent_transform(
    tmp_path: Path,
) -> None:
    scaled_group = {
        "objectId": "r4_scaled_group",
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
                    "objectId": "r4_scaled_child",
                    "size": {
                        "width": {"magnitude": pt_to_emu(100), "unit": "EMU"},
                        "height": {"magnitude": pt_to_emu(50), "unit": "EMU"},
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
    folder = _materialized_golden_with_elements(
        tmp_path, [scaled_group], save_raw=True
    )
    _strip_parent_transforms(folder)
    sml_path = folder / "slides" / "01" / "content.sml"
    root = ET.fromstring(sml_path.read_text(encoding="utf-8"))
    child = root.find(".//*[@id='r4_scaled_child']")
    assert child is not None
    child.set("x", str(float(child.get("x", "0")) + 20))
    sml_path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")

    from slidesmith.engine.client import diff_folder

    requests = diff_folder(folder)
    transform = next(request["updatePageElementTransform"] for request in requests)
    assert transform["applyMode"] == "RELATIVE"
    assert transform["transform"]["translateX"] == pt_to_emu(10)
