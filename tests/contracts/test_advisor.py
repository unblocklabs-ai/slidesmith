"""Hermetic contracts for advisor rules and the native group command."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import pytest

from slidesmith import cli
from slidesmith.engine.advisor import advise_folder
from slidesmith.engine.client import SlidesClient
from slidesmith.engine.transport import PresentationData, Transport
from slidesmith.engine.z_order import build_group_requests, validate_live_group_targets
from slidesmith.engine.units import pt_to_emu
from slidesmith.workspace import materialize


def _shape(
    object_id: str,
    x: float,
    y: float,
    *,
    width: float = 100,
    height: float = 50,
    alpha: float = 1.0,
    shape_type: str = "RECTANGLE",
    fill_state: str | None = None,
) -> dict[str, Any]:
    fill = (
        {"propertyState": fill_state}
        if fill_state is not None
        else {
            "solidFill": {
                "color": {"rgbColor": {"red": 1}},
                "alpha": alpha,
            }
        }
    )
    return {
        "objectId": object_id,
        "size": {
            "width": {"magnitude": pt_to_emu(width), "unit": "EMU"},
            "height": {"magnitude": pt_to_emu(height), "unit": "EMU"},
        },
        "transform": {
            "scaleX": 1,
            "scaleY": 1,
            "translateX": pt_to_emu(x),
            "translateY": pt_to_emu(y),
            "unit": "EMU",
        },
        "shape": {
            "shapeType": shape_type,
            "shapeProperties": {
                "shapeBackgroundFill": fill
            },
        },
    }


def _text_box(
    object_id: str,
    x: float,
    y: float,
    *,
    width: float = 200,
    height: float = 45,
    text: str = "one two three four five six",
) -> dict[str, Any]:
    return {
        "objectId": object_id,
        "size": {
            "width": {"magnitude": pt_to_emu(width), "unit": "EMU"},
            "height": {"magnitude": pt_to_emu(height), "unit": "EMU"},
        },
        "transform": {
            "scaleX": 1,
            "scaleY": 1,
            "translateX": pt_to_emu(x),
            "translateY": pt_to_emu(y),
            "unit": "EMU",
        },
        "shape": {
            "shapeType": "TEXT_BOX",
            "shapeProperties": {
                "shapeBackgroundFill": {"propertyState": "INHERIT"},
                "autofit": {"autofitType": "NONE"},
            },
            "text": {
                "textElements": [
                    {"paragraphMarker": {"style": {}}},
                    {
                        "textRun": {
                            "content": f"{text}\n",
                            "style": {
                                "weightedFontFamily": {"fontFamily": "Arial"},
                                "fontSize": {"magnitude": pt_to_emu(12), "unit": "EMU"},
                            },
                        }
                    },
                ]
            },
        },
    }


def _presentation(slides: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "title": "Advisor fixture",
        "presentationId": "advisor-deck",
        "revisionId": "rev-1",
        "pageSize": {
            "width": {"magnitude": pt_to_emu(720), "unit": "EMU"},
            "height": {"magnitude": pt_to_emu(540), "unit": "EMU"},
        },
        "slides": [
            {"objectId": f"slide{index}", "pageElements": elements}
            for index, elements in enumerate(slides, 1)
        ],
    }


def _folder(tmp_path: Path, slides: list[list[dict[str, Any]]]) -> Path:
    return materialize(_presentation(slides), tmp_path)


def _card(
    prefix: str,
    x: float,
    y: float,
    *,
    body_y: float | None = None,
) -> list[dict[str, Any]]:
    body_y = y + 70 if body_y is None else body_y
    return [
        _shape(f"{prefix}_card", x, y, width=200, height=120),
        _text_box(
            f"{prefix}_title",
            x + 20,
            y + 20,
            width=160,
            height=40,
            text="Title",
        ),
        _text_box(
            f"{prefix}_body",
            x + 20,
            body_y,
            width=160,
            height=45,
            text="Body",
        ),
    ]


def _image(
    object_id: str,
    x: float,
    y: float,
    *,
    width: float,
    height: float,
) -> dict[str, Any]:
    return {
        "objectId": object_id,
        "size": {
            "width": {"magnitude": pt_to_emu(width), "unit": "EMU"},
            "height": {"magnitude": pt_to_emu(height), "unit": "EMU"},
        },
        "transform": {
            "scaleX": 1,
            "scaleY": 1,
            "translateX": pt_to_emu(x),
            "translateY": pt_to_emu(y),
            "unit": "EMU",
        },
        "image": {"contentUrl": "https://example.invalid/image.png"},
    }


def test_pseudo_group_card_grid_suggests_each_card_and_expands_api_ids(
    tmp_path: Path,
) -> None:
    folder = _folder(
        tmp_path,
        [[
            *_card("a", 20, 100),
            *_card("b", 300, 100),
            *_card("c", 20, 300),
            *_card("d", 300, 300),
        ]],
    )
    suggestions = advise_folder(folder, rule="pseudo-group")

    assert [item.element_ids for item in suggestions] == [
        ("a_card", "a_title", "a_body"),
        ("b_card", "b_title", "b_body"),
        ("c_card", "c_title", "c_body"),
        ("d_card", "d_title", "d_body"),
    ]
    assert all(item.command_hint and "slidesmith group" in item.command_hint for item in suggestions)
    assert all(
        all(f"id={element_id}" in (item.command_hint or "") for element_id in item.element_ids)
        for item in suggestions
    )
    assert {item.rule for item in advise_folder(folder)} == {"pseudo-group"}


@pytest.mark.parametrize(
    "elements",
    [
        [_text_box("title", 20, 20, height=20, text="Title"), _text_box("subtitle", 20, 50, height=20, text="Subtitle")],
        [_shape("row1_left", 20, 100), _shape("row1_right", 130, 100), _shape("row2_left", 20, 220), _shape("row2_right", 130, 220)],
    ],
)
def test_pseudo_group_near_miss_layouts_are_quiet(
    tmp_path: Path,
    elements: list[dict[str, Any]],
) -> None:
    assert advise_folder(_folder(tmp_path, [elements]), rule="pseudo-group") == []


def test_pseudo_group_excludes_repeated_nested_text_pairs(
    tmp_path: Path,
) -> None:
    """Parsed SML types are "TextBox", not API "TEXT_BOX" — the two-element
    text-over-text exclusion must fire on the parsed vocabulary."""
    folder = _folder(
        tmp_path,
        [[
            _text_box("a_title", 20, 20, height=20, text="Alpha heading"),
            _text_box("a_body", 24, 44, height=40, text="Alpha body"),
            _text_box("b_title", 300, 20, height=20, text="Beta heading"),
            _text_box("b_body", 304, 44, height=40, text="Beta body"),
        ]],
    )

    assert advise_folder(folder, rule="pseudo-group") == []


def test_pseudo_group_tolerates_one_extra_member_per_card(
    tmp_path: Path,
) -> None:
    """A card with one optional extra element (a badge) is still the same
    repeated unit; both cards must fire, not neither."""
    badge = _shape("b_badge", 460, 110, width=30, height=16)
    folder = _folder(
        tmp_path,
        [[*_card("a", 20, 100), *_card("b", 300, 100), badge]],
    )

    suggestions = advise_folder(folder, rule="pseudo-group")

    assert [set(item.element_ids) for item in suggestions] == [
        {"a_card", "a_title", "a_body"},
        {"b_card", "b_title", "b_body", "b_badge"},
    ]


def test_pseudo_group_uses_current_sml_geometry_for_a_moved_member(
    tmp_path: Path,
) -> None:
    folder = _folder(
        tmp_path,
        [[*_card("a", 20, 100), *_card("b", 300, 100)]],
    )
    content_path = folder / "slides" / "01" / "content.sml"
    content = content_path.read_text(encoding="utf-8")
    content = content.replace(
        '<TextBox id="b_body" x="320" y="170"',
        '<TextBox id="b_body" x="320" y="190"',
    )
    content_path.write_text(content, encoding="utf-8")

    assert advise_folder(folder, rule="pseudo-group") == []


def test_pseudo_group_ignores_members_inside_native_groups(tmp_path: Path) -> None:
    data = _presentation(
        [[
            {
                "objectId": "native_a",
                "transform": {"scaleX": 1, "scaleY": 1},
                "elementGroup": {
                    "children": [_shape("a_child", 20, 20), _shape("a_child_two", 20, 80)]
                },
            },
            {
                "objectId": "native_b",
                "transform": {"scaleX": 1, "scaleY": 1},
                "elementGroup": {
                    "children": [_shape("b_child", 220, 20), _shape("b_child_two", 220, 80)]
                },
            },
        ]]
    )
    assert advise_folder(materialize(data, tmp_path), rule="pseudo-group") == []


def test_buried_element_requires_opaque_90_percent_coverage(
    tmp_path: Path,
) -> None:
    positive = _folder(
        tmp_path / "positive",
        [[_shape("buried", 10, 10), _shape("cover", 10, 10)]],
    )
    suggestions = advise_folder(positive, rule="buried-element")
    assert [item.element_ids for item in suggestions] == [("buried", "cover")]
    assert "bring-forward" in (suggestions[0].command_hint or "")

    negative = _folder(
        tmp_path / "negative",
        [[_shape("buried", 10, 10), _shape("cover", 25, 10, width=100)]],
    )
    assert advise_folder(negative, rule="buried-element") == []

    transparent = _folder(
        tmp_path / "transparent",
        [[_shape("buried", 10, 10), _shape("cover", 10, 10, alpha=0.8)]],
    )
    assert advise_folder(transparent, rule="buried-element") == []

    almost_opaque = _folder(
        tmp_path / "almost-opaque",
        [[_shape("buried", 10, 10), _shape("cover", 10, 10, alpha=0.995)]],
    )
    assert [item.element_ids for item in advise_folder(almost_opaque, rule="buried-element")] == [
        ("buried", "cover")
    ]

    inherited = _folder(
        tmp_path / "inherited",
        [[
            _shape("buried", 10, 10),
            _shape("cover", 10, 10, fill_state="INHERIT"),
        ]],
    )
    assert advise_folder(inherited, rule="buried-element") == []

    image_cover = _folder(
        tmp_path / "image-cover",
        [[_shape("buried", 10, 10), _image("cover", 10, 10, width=95, height=50)]],
    )
    assert [item.element_ids for item in advise_folder(image_cover, rule="buried-element")] == [
        ("buried", "cover")
    ]


def test_stack_candidate_requires_equal_non_overlapping_gaps(
    tmp_path: Path,
) -> None:
    positive = _folder(
        tmp_path / "positive",
        [[
            _shape("one", 10, 100),
            _shape("two", 120, 100),
            _shape("three", 230, 100),
        ]],
    )
    suggestions = advise_folder(positive, rule="stack-candidate")
    assert [item.element_ids for item in suggestions] == [
        ("one", "two", "three")
    ]
    assert suggestions[0].command_hint is None

    negative = _folder(
        tmp_path / "negative",
        [[
            _shape("one", 10, 100),
            _shape("two", 120, 100),
            _shape("three", 235, 100),
        ]],
    )
    assert advise_folder(negative, rule="stack-candidate") == []


def test_near_overflow_uses_phase_five_measurement(
    tmp_path: Path,
) -> None:
    folder = _folder(
        tmp_path,
        [[_text_box("near", 10, 10, width=100, height=45)]],
    )
    # The generated SML keeps the same local text/style projection.  The
    # fixture's dimensions put two measured lines inside the 90%-100% band.
    suggestions = advise_folder(folder, rule="near-overflow")
    assert [item.element_ids for item in suggestions] == [("near",)]
    assert suggestions[0].command_hint is None


def test_near_overflow_exactly_full_content_height_is_a_quiet_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = _folder(
        tmp_path,
        [[_text_box("full", 10, 10, width=100, height=45)]],
    )

    def exact_measurement(_element: Any, _style: Any, box: Any, _measurer: Any) -> Any:
        return SimpleNamespace(
            top_inset_pt=0.0,
            bottom_inset_pt=0.0,
            layout=SimpleNamespace(height_pt=box.h),
        )

    monkeypatch.setattr("slidesmith.engine.advisor._measure_text_element", exact_measurement)
    assert advise_folder(folder, rule="near-overflow") == []


def test_clean_deck_has_no_advisor_suggestions(tmp_path: Path) -> None:
    folder = _folder(tmp_path, [[_shape("single", 40, 40)]])
    assert advise_folder(folder) == []


def test_advise_cli_text_filter_json_and_zero_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = _folder(
        tmp_path,
        [[_shape("one", 10, 100), _shape("two", 120, 100), _shape("three", 230, 100)]],
    )
    cli.main(["advise", str(folder), "--rule", "stack-candidate"])
    text = capsys.readouterr().out
    assert "Slide 01" in text
    assert "[stack-candidate]" in text
    assert "Command:" not in text

    cli.main(["advise", str(folder), "--rule", "does-not-exist", "--json"])
    assert json.loads(capsys.readouterr().out) == []

    cli.main(["advise", str(_folder(tmp_path / "clean", [[_shape("one", 0, 0)]]))])
    assert capsys.readouterr().out.strip() == "No suggestions."


def test_advise_rejects_workspace_missing_minimum_schema(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "missing-schema"
    folder.mkdir()

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["advise", str(folder)])

    assert excinfo.value.code == 1
    assert f"Missing Slidesmith workspace file: {folder / 'presentation.json'}" in capsys.readouterr().err


def test_advisor_import_does_not_load_network_modules() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import slidesmith.engine.advisor; "
                "assert 'slidesmith.engine.transport' not in sys.modules; "
                "assert 'httpx' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


class GroupTransport(Transport):
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = copy.deepcopy(data)
        self.batch_calls: list[dict[str, Any]] = []

    async def get_presentation(self, presentation_id: str) -> PresentationData:
        return PresentationData(presentation_id, copy.deepcopy(self.data))

    async def batch_update(
        self,
        presentation_id: str,
        requests: list[dict[str, Any]],
        required_revision_id: str | None = None,
    ) -> dict[str, Any]:
        self.batch_calls.append(
            {
                "presentation_id": presentation_id,
                "requests": copy.deepcopy(requests),
                "required_revision_id": required_revision_id,
            }
        )
        body = requests[0]["groupObjects"]
        selected = set(body["childrenObjectIds"])
        slide = self.data["slides"][0]
        children = [
            element
            for element in slide["pageElements"]
            if element.get("objectId") in selected
        ]
        slide["pageElements"] = [
            element
            for element in slide["pageElements"]
            if element.get("objectId") not in selected
        ] + [
            {
                "objectId": body["groupObjectId"],
                "transform": {"scaleX": 1, "scaleY": 1},
                "elementGroup": {"children": children},
            }
        ]
        self.data["revisionId"] = "rev-2"
        return {"replies": [{}]}

    async def close(self) -> None:
        pass


def _group_fixture(tmp_path: Path) -> tuple[GroupTransport, Path]:
    data = _presentation(
        [
            [_shape("left", 10, 10), _shape("right", 120, 10)],
            [_shape("other", 10, 10)],
        ]
    )
    return GroupTransport(data), materialize(data, tmp_path)


@pytest.mark.asyncio
async def test_group_request_is_revision_locked_and_refreshes_native_group(
    tmp_path: Path,
) -> None:
    transport, folder = _group_fixture(tmp_path)
    response = await SlidesClient(transport).group(folder, "id=left OR id=right")

    assert response["replies"] == [{}]
    assert transport.batch_calls[0]["required_revision_id"] == "rev-1"
    request = transport.batch_calls[0]["requests"][0]["groupObjects"]
    assert request["childrenObjectIds"] == [
        json.loads((folder / "id_mapping.json").read_text())["left"],
        json.loads((folder / "id_mapping.json").read_text())["right"],
    ]
    assert request["groupObjectId"].startswith("new_group_")
    assert SlidesClient(transport).diff(folder) == []
    refreshed = json.loads((folder / "id_mapping.json").read_text())
    assert request["groupObjectId"] in refreshed.values()


@pytest.mark.asyncio
async def test_group_dry_run_makes_no_api_call(tmp_path: Path) -> None:
    transport, folder = _group_fixture(tmp_path)
    response = await SlidesClient(transport).group(
        folder, "id=left OR id=right", dry_run=True
    )
    assert response["dryRun"] is True
    assert response["requests"][0]["groupObjects"]["childrenObjectIds"]
    assert transport.batch_calls == []


def test_group_cli_dry_run_does_not_authenticate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, folder = _group_fixture(tmp_path)
    monkeypatch.setattr(cli, "_warn_if_stale", lambda _folder: None)
    monkeypatch.setattr(cli, "_token", lambda *_args: pytest.fail("no auth in dry-run"))
    cli.main(["group", str(folder), "id=left OR id=right", "--dry-run"])
    request = json.loads(capsys.readouterr().out)[0]
    assert "groupObjects" in request


@pytest.mark.parametrize(
    "selector",
    ["id=left", "id=left OR id=other"],
)
def test_group_rejects_too_few_or_cross_slide_matches(
    tmp_path: Path,
    selector: str,
) -> None:
    transport, folder = _group_fixture(tmp_path)
    with pytest.raises(ValueError, match="at least 2|across slides"):
        build_group_requests(folder, selector)
    assert transport.batch_calls == []


def test_group_rejects_native_group_children(tmp_path: Path) -> None:
    data = _presentation(
        [[
            {
                "objectId": "native_group",
                "transform": {"scaleX": 1, "scaleY": 1},
                "elementGroup": {
                    "children": [_shape("child_a", 10, 10), _shape("child_b", 120, 10)]
                },
            }
        ]]
    )
    folder = materialize(data, tmp_path)
    with pytest.raises(ValueError, match="native group"):
        build_group_requests(folder, "id=child_a OR id=child_b")


@pytest.mark.parametrize(
    ("kind", "element_id"),
    [("table", "table_member"), ("video", "video_member"), ("placeholder", "placeholder_member")],
)
def test_group_rejects_google_unsupported_members_before_api(
    tmp_path: Path,
    kind: str,
    element_id: str,
) -> None:
    unsupported = _shape(element_id, 10, 10)
    if kind == "placeholder":
        unsupported["shape"]["placeholder"] = {"type": "TITLE"}
    else:
        del unsupported["shape"]
        unsupported[kind] = {}
    folder = _folder(tmp_path, [[unsupported, _shape("other", 130, 10)]])

    with pytest.raises(ValueError, match=element_id):
        build_group_requests(folder, f"id={element_id} OR id=other")


def test_live_group_collision_reallocates_against_page_ids() -> None:
    presentation = _presentation([[_shape("left", 10, 10), _shape("right", 120, 10)]])
    presentation["slides"][0]["objectId"] = "new_group_live_1"
    requests = [
        {
            "groupObjects": {
                "groupObjectId": "new_group_live_1",
                "childrenObjectIds": ["left", "right"],
            }
        }
    ]

    validate_live_group_targets(presentation, requests, {})

    allocated = requests[0]["groupObjects"]["groupObjectId"]
    assert allocated != "new_group_live_1"
    assert allocated not in {"new_group_live_1", "left", "right"}
