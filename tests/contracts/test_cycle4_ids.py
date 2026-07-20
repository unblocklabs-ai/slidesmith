"""Cycle-4 contracts for stable authored object IDs."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from slidesmith.cli import main
from slidesmith.engine.client import diff_folder
from slidesmith.engine.content_diff import Change, ChangeType, DiffResult
from slidesmith.engine.content_requests import generate_batch_requests
from slidesmith.engine.id_manager import IDManager
from slidesmith.engine.slide_processor import process_presentation
from slidesmith.workspace import materialize

GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)


def _raw_element(object_id: str) -> dict[str, object]:
    return {
        "objectId": object_id,
        "size": {
            "width": {"magnitude": 914400},
            "height": {"magnitude": 457200},
        },
        "transform": {
            "scaleX": 1,
            "scaleY": 1,
            "translateX": 0,
            "translateY": 0,
        },
        "shape": {"shapeType": "RECTANGLE"},
    }


def test_authored_id_is_sent_directly_in_create_requests(tmp_path: Path) -> None:
    folder = materialize(json.loads(GOLDEN.read_text(encoding="utf-8")), tmp_path)
    sml = folder / "slides" / "01" / "content.sml"
    authored = (
        '<TextBox id="mission_swarm" x="153.4" y="395" w="154.4" h="80">'
        "<P>Mission</P></TextBox>"
    )
    sml.write_text(
        sml.read_text(encoding="utf-8").replace("</Slide>", authored + "</Slide>"),
        encoding="utf-8",
    )

    requests = diff_folder(folder)

    create = next(request["createShape"] for request in requests if "createShape" in request)
    assert create["objectId"] == "mission_swarm"
    assert all(
        body.get("objectId") == "mission_swarm"
        for request in requests
        for body in request.values()
        if isinstance(body, dict) and "objectId" in body
    )


def test_diff_slide_limits_output_to_one_slide(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = materialize(json.loads(GOLDEN.read_text(encoding="utf-8")), tmp_path)
    for slide_index, element_id in (("01", "slide_one_box"), ("02", "slide_two_box")):
        sml = folder / "slides" / slide_index / "content.sml"
        sml.write_text(
            sml.read_text(encoding="utf-8").replace(
                "</Slide>",
                f'<Rect id="{element_id}" x="10" y="10" w="20" h="20" />'
                "</Slide>",
            ),
            encoding="utf-8",
        )

    main(["diff", str(folder), "--slide", "1"])

    requests = json.loads(capsys.readouterr().out)
    created_ids = [
        request["createShape"]["objectId"]
        for request in requests
        if "createShape" in request
    ]
    assert created_ids == ["slide_one_box"]


def test_pull_preserves_authored_and_legacy_ids_but_filters_google_ids() -> None:
    object_ids = [
        "mission_swarm",
        "new_legacy_name",
        "new_mission_swarm",
        "g3b91ac_1_2",
        "p12_i3",
        "SLIDES_API587087046_0",
        "invalid:name",
    ]
    presentation = {
        "title": "IDs",
        "presentationId": "id_fixture",
        "slides": [
            {
                "objectId": "slide_google",
                "pageElements": [_raw_element(object_id) for object_id in object_ids],
            }
        ],
    }

    result = process_presentation(presentation)
    mapping = result["id_mapping"]

    assert mapping["mission_swarm"] == "mission_swarm"
    assert mapping["legacy_name"] == "new_legacy_name"
    assert mapping["e1"] == "new_mission_swarm"
    assert mapping["e2"] == "g3b91ac_1_2"
    assert mapping["e3"] == "p12_i3"
    assert mapping["e4"] == "SLIDES_API587087046_0"
    assert mapping["e5"] == "invalid:name"
    assert 'id="mission_swarm"' in result["slides"][0]["content"]


def test_create_id_collision_and_invalid_id_use_safe_suffixes() -> None:
    changes = [
        Change(
            ChangeType.CREATE,
            "mission_swarm",
            slide_index="01",
            new_position={"x": 0, "y": 0, "w": 100, "h": 50},
            tag="TextBox",
        ),
        Change(
            ChangeType.CREATE,
            "bad:id",
            slide_index="01",
            new_position={"x": 0, "y": 60, "w": 100, "h": 50},
            tag="Rect",
        ),
    ]

    requests = generate_batch_requests(
        DiffResult(changes=changes),
        {"e1": "mission_swarm"},
        {"01": "slide_google"},
    )
    object_ids = [
        request["createShape"]["objectId"]
        for request in requests
        if "createShape" in request
    ]

    assert object_ids == ["mission_swarm_2", "bad_id_2"]
    assert all(re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]{4,49}", value) for value in object_ids)


def test_id_manager_restores_generated_counter_with_authored_mapping() -> None:
    manager = IDManager.from_dict(
        {"mission_swarm": "mission_swarm", "e7": "g3b91ac_1_2"}
    )

    assert manager.assign_element_id("g3b91ac_2_3") == "e8"
