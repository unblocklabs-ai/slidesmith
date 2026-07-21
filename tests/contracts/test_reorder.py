"""Offline contracts for live Google Slides z-order reordering."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from slidesmith import cli
from slidesmith.engine.client import SlidesClient
from slidesmith.engine.transport import PresentationData, Transport
from slidesmith.workspace import materialize


def _shape(
    object_id: str,
    x: float,
    y: float,
    *,
    width: float = 100,
    height: float = 60,
) -> dict[str, Any]:
    return {
        "objectId": object_id,
        "size": {
            "width": {"magnitude": width, "unit": "PT"},
            "height": {"magnitude": height, "unit": "PT"},
        },
        "transform": {
            "scaleX": 1,
            "scaleY": 1,
            "translateX": x,
            "translateY": y,
            "unit": "PT",
        },
        "shape": {"shapeType": "RECTANGLE"},
    }


def _presentation(
    *,
    include_group: bool = False,
    include_visual_containment: bool = False,
) -> dict[str, Any]:
    first_page_elements: list[dict[str, Any]] = [
        _shape("back01", 0, 0, width=600, height=400)
        if include_visual_containment
        else _shape("back01", 0, 0),
        _shape("card01", 100, 100)
        if include_visual_containment
        else _shape("card01", 200, 0),
    ]
    if include_group:
        first_page_elements.append(
            {
                "objectId": "group01",
                "transform": {"scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 0},
                "elementGroup": {"children": [_shape("child01", 400, 0), _shape("child02", 500, 0)]},
            }
        )
    return {
        "title": "Reorder fixture",
        "presentationId": "reorder-deck",
        "revisionId": "rev-1",
        "pageSize": {
            "width": {"magnitude": 9144000, "unit": "EMU"},
            "height": {"magnitude": 5143500, "unit": "EMU"},
        },
        "slides": [
            {"objectId": "slide01", "pageElements": first_page_elements},
            {
                "objectId": "slide02",
                "pageElements": [_shape("title02", 0, 100), _shape("card02", 200, 100)],
            },
        ],
    }


class ReorderTransport(Transport):
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
        for request in requests:
            z_order = request["updatePageElementsZOrder"]
            selected_ids = set(z_order["pageElementObjectIds"])
            for slide in self.data["slides"]:
                elements = slide.get("pageElements", [])
                if not selected_ids.intersection(
                    {element.get("objectId") for element in elements}
                ):
                    continue
                selected = [
                    element for element in elements if element.get("objectId") in selected_ids
                ]
                remaining = [
                    element for element in elements if element.get("objectId") not in selected_ids
                ]
                operation = z_order["operation"]
                if operation in {"BRING_TO_FRONT", "BRING_FORWARD"}:
                    slide["pageElements"] = remaining + selected
                else:
                    slide["pageElements"] = selected + remaining
        self.data["revisionId"] = "rev-2"
        return {"replies": [{} for _ in requests]}

    async def close(self) -> None:
        pass


def _workspace(
    tmp_path: Path,
    *,
    include_group: bool = False,
    include_visual_containment: bool = False,
) -> tuple[ReorderTransport, Path]:
    data = _presentation(
        include_group=include_group,
        include_visual_containment=include_visual_containment,
    )
    folder = materialize(data, tmp_path)
    return ReorderTransport(data), folder


def _request_ids(folder: Path) -> dict[str, str]:
    return json.loads((folder / "id_mapping.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_selector_groups_multi_slide_matches_into_one_request_per_slide(
    tmp_path: Path,
) -> None:
    _, folder = _workspace(tmp_path)
    mapping = _request_ids(folder)

    result = await SlidesClient().reorder(
        folder, "tag=Rect", "send-to-back", dry_run=True
    )

    assert result["requests"] == [
        {
            "updatePageElementsZOrder": {
                "pageElementObjectIds": [mapping["back01"], mapping["card01"]],
                "operation": "SEND_TO_BACK",
            }
        },
        {
            "updatePageElementsZOrder": {
                "pageElementObjectIds": [mapping["title02"], mapping["card02"]],
                "operation": "SEND_TO_BACK",
            }
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("bring-to-front", "BRING_TO_FRONT"),
        ("bring-forward", "BRING_FORWARD"),
        ("send-backward", "SEND_BACKWARD"),
        ("send-to-back", "SEND_TO_BACK"),
    ],
)
async def test_reorder_operation_maps_to_google_enum(
    tmp_path: Path,
    operation: str,
    expected: str,
) -> None:
    _, folder = _workspace(tmp_path)

    result = await SlidesClient().reorder(
        folder, "id=back01", operation, dry_run=True
    )

    assert result["requests"][0]["updatePageElementsZOrder"]["operation"] == expected


@pytest.mark.asyncio
async def test_reorder_rejects_group_children_with_offending_ids(tmp_path: Path) -> None:
    transport, folder = _workspace(tmp_path, include_group=True)

    with pytest.raises(ValueError, match=r"group children.*child01"):
        await SlidesClient(transport).reorder(folder, "id=child01", "bring-to-front")

    assert transport.batch_calls == []


@pytest.mark.asyncio
async def test_reorder_accepts_visually_contained_non_group_child_and_emits_request(
    tmp_path: Path,
) -> None:
    transport, folder = _workspace(tmp_path, include_visual_containment=True)
    mapping = _request_ids(folder)

    await SlidesClient(transport).reorder(
        folder, "id=card01", "bring-to-front"
    )

    assert transport.batch_calls[-1]["requests"] == [
        {
            "updatePageElementsZOrder": {
                "pageElementObjectIds": [mapping["card01"]],
                "operation": "BRING_TO_FRONT",
            }
        }
    ]


@pytest.mark.asyncio
async def test_reorder_rejects_empty_selection_without_api_call(tmp_path: Path) -> None:
    transport, folder = _workspace(tmp_path)

    with pytest.raises(ValueError, match="matched no elements"):
        await SlidesClient(transport).reorder(folder, "id=does-not-exist", "bring-to-front")

    assert transport.batch_calls == []


@pytest.mark.asyncio
async def test_reorder_refuses_pending_local_diff_before_api_call(tmp_path: Path) -> None:
    transport, folder = _workspace(tmp_path)
    content_path = folder / "slides" / "01" / "content.sml"
    content = content_path.read_text(encoding="utf-8")
    content_path.write_text(
        content.replace("</Slide>", '<Rect id="pending01" x="0" y="0" w="10" h="10" />\n</Slide>'),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requires a clean workspace"):
        await SlidesClient(transport).reorder(folder, "id=back01", "bring-to-front")

    assert transport.batch_calls == []


def test_reorder_dry_run_prints_requests_without_auth_or_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, folder = _workspace(tmp_path)
    expected = [
        {
            "updatePageElementsZOrder": {
                "pageElementObjectIds": [_request_ids(folder)["back01"]],
                "operation": "BRING_TO_FRONT",
            }
        }
    ]

    monkeypatch.setattr(cli, "_warn_if_stale", lambda _folder: None)
    monkeypatch.setattr(
        cli,
        "_token",
        lambda *_args: pytest.fail("dry-run must not authenticate"),
    )

    cli.main(
        [
            "reorder",
            str(folder),
            "id=back01",
            "--op",
            "bring-to-front",
            "--dry-run",
        ]
    )

    assert json.loads(capsys.readouterr().out) == expected


@pytest.mark.parametrize(
    ("include_group", "selector", "message"),
    [
        (False, "id=does-not-exist", "matched no elements"),
        (True, "id=child01", "group children"),
    ],
)
def test_reorder_live_cli_validates_before_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    include_group: bool,
    selector: str,
    message: str,
) -> None:
    _, folder = _workspace(tmp_path, include_group=include_group)

    monkeypatch.setattr(cli, "_warn_if_stale", lambda _folder: None)
    monkeypatch.setattr(
        cli,
        "_token",
        lambda *_args: pytest.fail("live reorder must validate before authentication"),
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            [
                "reorder",
                str(folder),
                selector,
                "--op",
                "bring-to-front",
            ]
        )

    assert excinfo.value.code == 1
    error = capsys.readouterr().err
    assert message in error
    assert selector.split("=", 1)[1] in error


@pytest.mark.asyncio
async def test_reorder_refreshes_authoritative_order_and_preserves_ids_and_roles(
    tmp_path: Path,
) -> None:
    transport, folder = _workspace(tmp_path)
    mapping = _request_ids(folder)
    (folder / "roles.json").write_text(
        json.dumps({"back01": "background"}), encoding="utf-8"
    )
    client = SlidesClient(transport)

    response = await client.reorder(folder, "id=back01", "bring-to-front")

    assert response["replies"] == [{}]
    assert transport.batch_calls[-1]["required_revision_id"] == "rev-1"
    assert transport.batch_calls[-1]["requests"] == [
        {
            "updatePageElementsZOrder": {
                "pageElementObjectIds": [mapping["back01"]],
                "operation": "BRING_TO_FRONT",
            }
        }
    ]
    refreshed_mapping = _request_ids(folder)
    assert refreshed_mapping["back01"] == mapping["back01"]
    assert refreshed_mapping["card01"] == mapping["card01"]
    assert json.loads((folder / "roles.json").read_text(encoding="utf-8")) == {
        "back01": "background"
    }
    content = (folder / "slides" / "01" / "content.sml").read_text(encoding="utf-8")
    assert content.index('id="card01"') < content.index('id="back01"')
    assert client.diff(folder) == []
