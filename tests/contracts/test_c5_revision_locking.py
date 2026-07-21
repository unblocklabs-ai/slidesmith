"""Contract C5, offline mechanism: safe pushes while a human may be editing.

Per DESIGN.md, revisionId is a write guard, not a change detector:
- Human changes are detected by comparing the freshly fetched remote deck
  against the pristine base -- but only for the objects this push touches.
- The write itself is guarded with writeControl.requiredRevisionId captured
  at that fetch; a mid-push edit surfaces as a 400 -> ConflictError.
- Remote changes to untouched objects never block a push (field masks keep
  them safe).

The live half of C5 stays in test_contracts.py.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from slidesmith.engine.client import ConflictError, SlidesClient
from slidesmith.engine.components import load_components
from slidesmith.engine.content_diff import ChangeType, diff_presentation
from slidesmith.engine.content_parser import ParsedElement, parse_slide_content
from slidesmith.engine.slide_processor import process_presentation
from slidesmith.engine.diff_model import PushWarning, WarningSeverity
from slidesmith.engine.transport import (
    APIError,
    GoogleSlidesTransport,
    PresentationData,
    Transport,
)
from slidesmith.workspace import materialize

GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)


class StubTransport(Transport):
    """Offline transport: serves a mutable in-memory deck, records writes."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.batch_calls: list[dict[str, Any]] = []
        self.batch_error: Exception | None = None
        self.drop_shape_property_updates = False
        self.apply_create_shapes = False

    async def get_presentation(self, presentation_id: str) -> PresentationData:
        return PresentationData(
            presentation_id=self.data.get("presentationId", presentation_id),
            data=copy.deepcopy(self.data),
        )

    async def batch_update(
        self,
        presentation_id: str,
        requests: list[dict[str, Any]],
        required_revision_id: str | None = None,
    ) -> dict[str, Any]:
        self.batch_calls.append(
            {
                "presentation_id": presentation_id,
                "requests": requests,
                "required_revision_id": required_revision_id,
            }
        )
        if self.batch_error is not None:
            raise self.batch_error

        for request in requests:
            create = request.get("createShape")
            if create is not None and self.apply_create_shapes:
                page_id = create["elementProperties"]["pageObjectId"]
                slide = next(
                    slide
                    for slide in self.data["slides"]
                    if slide["objectId"] == page_id
                )
                slide.setdefault("pageElements", []).append(
                    {
                        "objectId": create["objectId"],
                        "size": copy.deepcopy(create["elementProperties"]["size"]),
                        "transform": copy.deepcopy(
                            create["elementProperties"]["transform"]
                        ),
                        "shape": {"shapeType": create["shapeType"]},
                    }
                )
                continue
            update = request.get("updateShapeProperties")
            if update is None or self.drop_shape_property_updates:
                continue
            element = find_element(self.data, update["objectId"])
            shape_properties = element.setdefault("shape", {}).setdefault(
                "shapeProperties", {}
            )
            if update["fields"] == "shapeBackgroundFill.solidFill":
                shape_properties["shapeBackgroundFill"] = copy.deepcopy(
                    update["shapeProperties"]["shapeBackgroundFill"]
                )

        self.data["revisionId"] = f"rev-after-push-{len(self.batch_calls)}"
        return {"replies": [{}] * len(requests)}

    async def close(self) -> None:
        pass


class Workspace:
    """A pulled folder plus the stub transport that served it."""

    def __init__(self, stub: StubTransport, client: SlidesClient, folder: Path):
        self.stub = stub
        self.client = client
        self.folder = folder
        self.id_mapping: dict[str, str] = json.loads(
            (folder / "id_mapping.json").read_text(encoding="utf-8")
        )


def append_persistence_warning_for_test(
    tmp_path: Path,
    intended: list[Any],
    remote: list[Any],
    author_changes: list[Any],
    *,
    intended_change_keys: set[tuple[str, ChangeType]] | None = None,
    create_copy_targets: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    folder = tmp_path / "deck"
    folder.mkdir()
    client = SlidesClient()
    client._read_pristine = lambda _folder: ({"01": remote}, {})
    response: dict[str, Any] = {}
    client._append_persistence_warning(
        folder,
        {"01": intended},
        intended_change_keys
        if intended_change_keys is not None
        else {
            (change.target_id, change.change_type)
            for change in author_changes
        },
        create_copy_targets or set(),
        response,
        author_changes=author_changes,
    )
    return response


@pytest.fixture
async def ws(tmp_path: Path) -> Workspace:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    stub = StubTransport(data)
    client = SlidesClient(stub)
    await client.pull(data["presentationId"], tmp_path, save_raw=False)
    return Workspace(stub, client, tmp_path / data["presentationId"])


def edit_e121_locally(folder: Path) -> None:
    """Make a local edit whose diff touches exactly element e121."""
    recolor_e121_locally(folder, "#00ff00")


def recolor_e121_locally(folder: Path, color: str) -> None:
    """Set e121's local fill class on a freshly regenerated SML file."""
    sml = folder / "slides" / "01" / "content.sml"
    content = sml.read_text(encoding="utf-8")
    start = content.index('<TextBox id="e121"')
    end = content.index(">", start)
    opening = content[start:end]
    class_marker = 'class="'
    if class_marker in opening:
        class_start = opening.index(class_marker) + len(class_marker)
        class_end = opening.index('"', class_start)
        classes = opening[class_start:class_end].split()
        classes = [cls for cls in classes if not cls.startswith("fill-")]
        classes.append(f"fill-{color}")
        opening = (
            opening[:class_start] + " ".join(classes) + opening[class_end:]
        )
    else:
        opening += f' class="fill-{color}"'
    sml.write_text(
        content[:start] + opening + content[end:],
        encoding="utf-8",
    )


def edit_e121_text_locally(folder: Path, replacement: str) -> str:
    """Replace one existing e121 text segment without changing its styles."""
    sml = folder / "slides" / "01" / "content.sml"
    content = sml.read_text(encoding="utf-8")
    start = content.index('<TextBox id="e121"')
    end = content.index("</TextBox>", start)
    segment = content[start:end]
    paragraph = segment[segment.index("<P") :]
    text_start = paragraph.index(">") + 1
    if paragraph[text_start:].startswith("<T"):
        text_start += paragraph[text_start:].index(">") + 1
    text_end = paragraph.index("<", text_start)
    original = paragraph[text_start:text_end]
    assert original
    segment = segment.replace(original, replacement, 1)
    sml.write_text(content[:start] + segment + content[end:], encoding="utf-8")
    return original


def find_element(data: dict[str, Any], object_id: str) -> dict[str, Any]:
    def walk(elements: list[dict[str, Any]]) -> dict[str, Any] | None:
        for element in elements:
            if element.get("objectId") == object_id:
                return element
            found = walk(element.get("elementGroup", {}).get("children", []))
            if found is not None:
                return found
        return None

    for slide in data["slides"]:
        found = walk(slide.get("pageElements", []))
        if found is not None:
            return found
    raise AssertionError(f"element {object_id} not found in presentation data")


# --- transport: writeControl pass-through ---------------------------------


async def _google_transport_body(
    required_revision_id: str | None,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"replies": []})

    transport = GoogleSlidesTransport("fake-token")
    await transport._client.aclose()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await transport.batch_update(
            "pid",
            [{"deleteObject": {"objectId": "x"}}],
            required_revision_id=required_revision_id,
        )
    finally:
        await transport.close()
    body: dict[str, Any] = captured["body"]
    return body


async def test_batch_update_passes_write_control_through() -> None:
    body = await _google_transport_body("rev-abc")
    assert body["writeControl"] == {"requiredRevisionId": "rev-abc"}
    assert body["requests"] == [{"deleteObject": {"objectId": "x"}}]


async def test_batch_update_omits_write_control_when_unset() -> None:
    body = await _google_transport_body(None)
    assert "writeControl" not in body


# --- pull: revision recorded, base snapshot persisted ---------------------


async def test_pull_records_revision_id_and_base_snapshot(ws: Workspace) -> None:
    golden_revision = json.loads(GOLDEN.read_text(encoding="utf-8"))["revisionId"]

    metadata = json.loads(
        (ws.folder / "presentation.json").read_text(encoding="utf-8")
    )
    assert metadata["revisionId"] == golden_revision
    assert metadata["pulledAt"].endswith("Z")

    base_path = ws.folder / ".pristine" / "base.json"
    assert base_path.exists(), "pull must persist the pristine base raw tree"
    base = json.loads(base_path.read_text(encoding="utf-8"))
    assert base["revisionId"] == golden_revision
    assert "slides" in base


async def test_materialize_records_pulled_workspace_safety_artifacts(
    ws: Workspace, tmp_path: Path
) -> None:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    materialized = materialize(data, tmp_path / "offline")

    for relative_path in (
        Path(".pristine/presentation.zip"),
        Path(".pristine/base.json"),
    ):
        assert (ws.folder / relative_path).exists()
        assert (materialized / relative_path).exists()

    base = json.loads(
        (materialized / ".pristine/base.json").read_text(encoding="utf-8")
    )
    assert base == data


# --- push: conflict on a touched object aborts before any write -----------


async def test_remote_change_to_touched_object_aborts_push(ws: Workspace) -> None:
    edit_e121_locally(ws.folder)

    # Human moved the same element remotely (and the revision drifted).
    remote_e121 = find_element(ws.stub.data, ws.id_mapping["e121"])
    remote_e121["transform"]["translateX"] = (
        remote_e121["transform"].get("translateX", 0) + 123456
    )
    ws.stub.data["revisionId"] = "rev-after-human-edit"

    with pytest.raises(ConflictError) as excinfo:
        await ws.client.push(ws.folder)

    assert ws.stub.batch_calls == [], "conflict must abort before batch_update"
    message = str(excinfo.value)
    assert "e121" in message
    assert "geometry" in message
    assert "Re-pull" in message
    assert excinfo.value.conflicts == [("e121", "geometry changed remotely")]


async def test_remote_change_to_ancestor_group_aborts_child_push(
    tmp_path: Path,
) -> None:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    result = process_presentation(data)
    child_id = result["id_mapping"]["e121"]
    slide = next(
        slide
        for slide in data["slides"]
        if any(
            element.get("objectId") == child_id
            for element in slide.get("pageElements", [])
        )
    )
    child_position, child = next(
        (position, element)
        for position, element in enumerate(slide["pageElements"])
        if element.get("objectId") == child_id
    )
    slide["pageElements"][child_position] = (
        {
            "objectId": "ancestor_group",
            "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU"},
            "elementGroup": {"children": [child]},
        }
    )

    stub = StubTransport(data)
    client = SlidesClient(stub)
    written = await client.pull(data["presentationId"], tmp_path, save_raw=False)
    folder = written[0].parent
    edit_e121_locally(folder)
    remote_group = find_element(stub.data, "ancestor_group")
    remote_group["transform"]["translateX"] = 123456
    stub.data["revisionId"] = "rev-after-group-edit"

    with pytest.raises(ConflictError) as excinfo:
        await client.push(folder)

    assert stub.batch_calls == []
    assert excinfo.value.conflicts == [
        ("ancestor_group", "geometry changed remotely")
    ]


async def test_remote_delete_of_touched_object_aborts_push(ws: Workspace) -> None:
    edit_e121_locally(ws.folder)

    google_id = ws.id_mapping["e121"]
    for slide in ws.stub.data["slides"]:
        slide["pageElements"] = [
            el for el in slide.get("pageElements", []) if el.get("objectId") != google_id
        ]

    with pytest.raises(ConflictError) as excinfo:
        await ws.client.push(ws.folder)

    assert ws.stub.batch_calls == []
    assert ("e121", "deleted remotely") in excinfo.value.conflicts


# --- push: untouched-object remote changes must NOT block -----------------


async def test_remote_change_to_untouched_object_does_not_block(
    ws: Workspace,
) -> None:
    edit_e121_locally(ws.folder)

    # Human edited a DIFFERENT element; field masks keep it safe.
    remote_e122 = find_element(ws.stub.data, ws.id_mapping["e122"])
    remote_e122["transform"]["translateX"] = (
        remote_e122["transform"].get("translateX", 0) + 999
    )
    ws.stub.data["revisionId"] = "rev-after-unrelated-edit"

    response = await ws.client.push(ws.folder)

    assert response["replies"], "push must go through"
    assert len(ws.stub.batch_calls) == 1
    call = ws.stub.batch_calls[0]
    # The lock uses the revision captured at the pre-push fetch, not pull time.
    assert call["required_revision_id"] == "rev-after-unrelated-edit"
    touched = {
        body.get("objectId")
        for request in call["requests"]
        for body in request.values()
        if isinstance(body, dict)
    }
    assert ws.id_mapping["e122"] not in touched


# --- push: API-level revision mismatch surfaces as ConflictError ----------


async def test_revision_mismatch_400_surfaces_as_conflict(ws: Workspace) -> None:
    edit_e121_locally(ws.folder)
    ws.stub.batch_error = APIError(
        "API error (400): The requiredRevisionId does not match the current "
        "revision of the presentation.",
        status_code=400,
    )

    with pytest.raises(ConflictError, match="Re-pull and retry"):
        await ws.client.push(ws.folder)


async def test_unrelated_400_is_not_masked_as_conflict(ws: Workspace) -> None:
    edit_e121_locally(ws.folder)
    ws.stub.batch_error = APIError(
        "API error (400): Invalid requests[0].deleteText", status_code=400
    )

    with pytest.raises(APIError):
        await ws.client.push(ws.folder)


# --- backward compatibility and --force -----------------------------------


async def test_folder_without_base_snapshot_degrades_gracefully(
    ws: Workspace,
) -> None:
    # Simulate a folder pulled by the old code: no base snapshot, no raw.
    (ws.folder / ".pristine" / "base.json").unlink()
    assert not (ws.folder / ".raw").exists()

    edit_e121_locally(ws.folder)
    # A remote change that WOULD conflict, but cannot be detected without base.
    remote_e121 = find_element(ws.stub.data, ws.id_mapping["e121"])
    remote_e121["transform"]["translateX"] = 42

    response = await ws.client.push(ws.folder)

    assert response["replies"], "old folders must still push (guard skipped)"
    assert response["warnings"] == [
        PushWarning(
            WarningSeverity.WARNING,
            "no pristine base snapshot found (.pristine/base.json); this folder was "
            "pulled by an older slidesmith. Remote-change detection skipped for "
            "this push -- re-pull to re-enable the guard.",
        )
    ]
    # The revision lock still applies even in degraded mode.
    assert ws.stub.batch_calls[0]["required_revision_id"] is not None


async def test_force_bypasses_guard_with_warning(
    ws: Workspace,
) -> None:
    edit_e121_locally(ws.folder)
    remote_e121 = find_element(ws.stub.data, ws.id_mapping["e121"])
    remote_e121["transform"]["translateX"] = 42  # would normally conflict

    response = await ws.client.push(ws.folder, force=True)

    assert response["replies"]
    assert len(ws.stub.batch_calls) == 1
    assert ws.stub.batch_calls[0]["required_revision_id"] is None
    assert response["warnings"] == [
        PushWarning(
            WarningSeverity.WARNING,
            "push --force: conflict guard and revision lock bypassed; concurrent "
            "human edits to the touched properties will be overwritten",
        )
    ]
    metadata = json.loads(
        (ws.folder / "presentation.json").read_text(encoding="utf-8")
    )
    base = json.loads(
        (ws.folder / ".pristine" / "base.json").read_text(encoding="utf-8")
    )
    assert metadata["revisionId"] == "rev-after-push-1"
    assert base["revisionId"] == "rev-after-push-1"


# --- push: successful writes refresh the local pristine base -------------


async def test_immediate_second_push_is_a_noop_against_refreshed_pristine(
    ws: Workspace,
) -> None:
    qa_baseline = ws.folder / ".pristine" / "qa-baseline.json"
    assert qa_baseline.exists(), "pull must record the offline QA snapshot"
    baseline_before_push = qa_baseline.read_bytes()
    recolor_e121_locally(ws.folder, "#00ff00")

    await ws.client.push(ws.folder)

    assert len(ws.stub.batch_calls) == 1
    metadata = json.loads(
        (ws.folder / "presentation.json").read_text(encoding="utf-8")
    )
    base = json.loads(
        (ws.folder / ".pristine" / "base.json").read_text(encoding="utf-8")
    )
    assert metadata["revisionId"] == "rev-after-push-1"
    assert base["revisionId"] == "rev-after-push-1"
    assert qa_baseline.read_bytes() == baseline_before_push

    # Re-fetch regeneration is authoritative and now restores the explicit
    # API fill as the canonical class-bearing SML representation.
    sml = ws.folder / "slides" / "01" / "content.sml"
    assert "fill-#00ff00" in sml.read_text(encoding="utf-8")

    response = await ws.client.push(ws.folder)

    assert response == {"replies": [], "message": "No changes detected."}
    assert len(ws.stub.batch_calls) == 1


async def test_push_warns_when_an_intended_change_does_not_persist(
    ws: Workspace,
) -> None:
    recolor_e121_locally(ws.folder, "#00ff00")
    ws.stub.drop_shape_property_updates = True

    response = await ws.client.push(ws.folder)

    assert response["warnings"] == [
        PushWarning(
            WarningSeverity.WARNING,
            "1 change(s) did not persist remotely: style classes on e121 did not "
            "persist (sent 'fill-#00ff00', remote now '(none)') "
            "— the API may not support these values",
        )
    ]


async def test_push_persistence_warning_shows_sent_and_remote_text(
    ws: Workspace,
) -> None:
    replacement = "Locally authored normalized value"
    remote_text = edit_e121_text_locally(ws.folder, replacement)

    response = await ws.client.push(ws.folder)

    assert len(response["warnings"]) == 1
    warning = response["warnings"][0]
    assert warning.severity is WarningSeverity.WARNING
    assert "text on e121 did not persist" in warning.message
    assert "sent " in warning.message and replacement in warning.message
    assert "remote now " in warning.message and remote_text in warning.message


async def test_push_warns_when_created_box_text_does_not_persist(
    ws: Workspace,
) -> None:
    sml = ws.folder / "slides" / "01" / "content.sml"
    content = sml.read_text(encoding="utf-8")
    sml.write_text(
        content.replace(
            "</Slide>",
            '<TextBox id="brand_new_box" x="100" y="100" w="200" h="50">'
            "<P>Authored text</P></TextBox></Slide>",
        ),
        encoding="utf-8",
    )
    ws.stub.apply_create_shapes = True

    response = await ws.client.push(ws.folder)

    assert response.get("warnings"), (
        "a partial CREATE must not be hidden when it re-diffs as TEXT_UPDATE"
    )
    assert (
        "text on brand_new_box did not persist"
        in response["warnings"][0].message
    )


def test_persistence_warning_suppresses_created_weight_and_arial_defaults(
    tmp_path: Path,
) -> None:
    intended = parse_slide_content(
        '<Slide id="s1"><TextBox id="new_box" x="10" y="20" w="100" h="30">'
        "<P><T>Authored</T></P></TextBox></Slide>"
    )
    remote = parse_slide_content(
        '<Slide id="s1"><TextBox id="new_box" x="10" y="20" w="100" h="30">'
        '<P><T class="font-weight-700 font-family-arial">Authored</T></P>'
        "</TextBox></Slide>"
    )
    response = append_persistence_warning_for_test(
        tmp_path,
        intended,
        remote,
        [],
        intended_change_keys={("new_box", ChangeType.CREATE)},
        create_copy_targets={("01", "new_box")},
    )

    assert "warnings" not in response


def test_persistence_warning_notices_existing_paragraph_defaults(
    tmp_path: Path,
) -> None:
    intended = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        '<P class="text-align-center">Authored</P></TextBox></Slide>'
    )
    remote = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        '<P class="text-align-center leading-100 space-above-0 space-below-0 '
        'indent-start-0 indent-first-0 spacing-never-collapse">Authored</P>'
        "</TextBox></Slide>"
    )
    response = append_persistence_warning_for_test(
        tmp_path,
        intended,
        remote,
        [],
        intended_change_keys={("box", ChangeType.PARAGRAPH_STYLE_UPDATE)},
    )

    assert response["warnings"]
    assert response["warnings"][0].severity is WarningSeverity.NOTICE
    assert "normalized by Google" in response["warnings"][0].message


def test_persistence_warning_keeps_arial_replacement_as_warning(
    tmp_path: Path,
) -> None:
    intended = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        '<P><T class="font-family-roboto">Authored</T></P></TextBox></Slide>'
    )
    remote = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        '<P><T class="font-family-arial">Authored</T></P></TextBox></Slide>'
    )
    response = append_persistence_warning_for_test(
        tmp_path,
        intended,
        remote,
        [],
        intended_change_keys={("box", ChangeType.TEXT_UPDATE)},
    )

    assert response["warnings"]
    assert response["warnings"][0].severity is WarningSeverity.WARNING
    assert "font-family-roboto" in response["warnings"][0].message


def test_persistence_warning_warns_when_existing_arial_is_restored(
    tmp_path: Path,
) -> None:
    pristine = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        '<P><T class="font-family-arial">Authored</T></P></TextBox></Slide>'
    )
    intended = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        "<P><T>Authored</T></P></TextBox></Slide>"
    )
    remote = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        '<P><T class="font-family-arial">Authored</T></P></TextBox></Slide>'
    )
    author_change = diff_presentation(
        {"01": pristine}, {"01": intended}, {}
    ).changes[0]
    response = append_persistence_warning_for_test(
        tmp_path, intended, remote, [author_change]
    )

    assert response["warnings"][0].severity is WarningSeverity.WARNING
    assert author_change.change_type is ChangeType.TEXT_UPDATE
    assert author_change.author_removed_classes == frozenset(
        {"font-family-arial"}
    )


def test_persistence_warning_detects_class_removed_by_split_run(
    tmp_path: Path,
) -> None:
    pristine = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        '<P><T class="font-family-arial">Authored</T></P></TextBox></Slide>'
    )
    intended = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        '<P><T class="font-family-arial">Auth</T><T>ored</T></P></TextBox></Slide>'
    )
    remote = pristine
    author_change = diff_presentation(
        {"01": pristine}, {"01": intended}, {}
    ).changes[0]
    response = append_persistence_warning_for_test(
        tmp_path, intended, remote, [author_change]
    )

    assert response["warnings"][0].severity is WarningSeverity.WARNING
    assert author_change.author_removed_classes == frozenset(
        {"font-family-arial"}
    )


def test_persistence_warning_does_not_treat_pure_run_split_as_class_removal(
    tmp_path: Path,
) -> None:
    pristine = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        '<P><T class="font-family-arial">Authored</T></P></TextBox></Slide>'
    )
    intended = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        '<P><T class="font-family-arial">Auth</T><T class="font-family-arial">ored</T></P></TextBox></Slide>'
    )
    remote = intended
    author_change = diff_presentation(
        {"01": pristine}, {"01": intended}, {}
    ).changes[0]
    response = append_persistence_warning_for_test(
        tmp_path, intended, remote, [author_change]
    )

    assert not response.get("warnings")
    assert not author_change.author_removed_classes


def test_persistence_warning_warns_when_existing_leading_is_restored(
    tmp_path: Path,
) -> None:
    pristine = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        '<P class="leading-100">Authored</P></TextBox></Slide>'
    )
    intended = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        "<P>Authored</P></TextBox></Slide>"
    )
    remote = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        '<P class="leading-100">Authored</P></TextBox></Slide>'
    )
    author_change = diff_presentation(
        {"01": pristine}, {"01": intended}, {}
    ).changes[0]
    response = append_persistence_warning_for_test(
        tmp_path, intended, remote, [author_change]
    )

    assert response["warnings"][0].severity is WarningSeverity.WARNING
    assert author_change.author_removed_classes == frozenset({"leading-100"})


def test_persistence_warning_notices_untouched_existing_weight_addition(
    tmp_path: Path,
) -> None:
    pristine = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        "<P><T>Original</T></P></TextBox></Slide>"
    )
    intended = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        "<P><T>Edited</T></P></TextBox></Slide>"
    )
    remote = parse_slide_content(
        '<Slide id="s1"><TextBox id="box" x="10" y="20" w="100" h="30">'
        '<P><T class="font-weight-700">Edited</T></P></TextBox></Slide>'
    )
    author_change = diff_presentation(
        {"01": pristine}, {"01": intended}, {}
    ).changes[0]
    response = append_persistence_warning_for_test(
        tmp_path, intended, remote, [author_change]
    )

    assert response["warnings"][0].severity is WarningSeverity.NOTICE
    assert not author_change.author_removed_classes


async def test_push_without_remote_divergence_returns_no_warning(
    ws: Workspace,
) -> None:
    recolor_e121_locally(ws.folder, "#00ff00")

    response = await ws.client.push(ws.folder)

    assert "warnings" not in response


@pytest.mark.parametrize(
    ("remote_x", "warns"),
    [(99.981, False), (99.9, True)],
)
def test_persistence_warning_suppresses_only_sub_point_zero_two_geometry_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_x: float,
    warns: bool,
) -> None:
    folder = tmp_path / "deck"
    folder.mkdir()
    (folder / "id_mapping.json").write_text("{}", encoding="utf-8")
    intended = ParsedElement("box", "Rect", x=100, y=20, w=30, h=40)
    remote = ParsedElement("box", "Rect", x=remote_x, y=20, w=30, h=40)
    client = SlidesClient()
    monkeypatch.setattr(
        client,
        "_read_pristine",
        lambda _folder: ({"01": [remote]}, {}),
    )
    response: dict[str, Any] = {}

    client._append_persistence_warning(
        folder,
        {"01": [intended]},
        {("box", ChangeType.MOVE)},
        set(),
        response,
    )

    assert ("warnings" in response) is warns


def test_persistence_verification_accepts_canonical_negative_line_geometry(
    tmp_path: Path,
) -> None:
    intended = parse_slide_content(
        '<Slide id="s1"><Line id="rule" x="100" y="20" '
        'w="-30" h="-10" /></Slide>'
    )
    remote = parse_slide_content(
        '<Slide id="s1"><Line id="rule" x="70" y="10" '
        'w="30" h="10" /></Slide>'
    )
    response = append_persistence_warning_for_test(
        tmp_path,
        intended,
        remote,
        [],
        intended_change_keys={("rule", ChangeType.MOVE)},
    )

    assert "warnings" not in response


def test_persistence_warning_ignores_created_text_layout_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "deck"
    folder.mkdir()
    (folder / "id_mapping.json").write_text("{}", encoding="utf-8")
    intended = parse_slide_content(
        '<Slide id="s1"><TextBox id="new_box" x="10" y="20" w="100" h="30">'
        "<P>Authored</P></TextBox></Slide>"
    )
    remote = parse_slide_content(
        '<Slide id="s1"><TextBox id="new_box" x="10" y="20" w="100" h="30" '
        'class="content-align-top text-align-left leading-100 '
        'spacing-collapse-lists"><P>Authored</P></TextBox></Slide>'
    )
    client = SlidesClient()
    monkeypatch.setattr(
        client,
        "_read_pristine",
        lambda _folder: ({"01": remote}, {}),
    )
    response: dict[str, Any] = {}

    client._append_persistence_warning(
        folder,
        {"01": intended},
        {("new_box", ChangeType.CREATE)},
        {("01", "new_box")},
        response,
    )

    assert "warnings" not in response


def test_persistence_warning_ignores_component_create_google_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "deck"
    folder.mkdir()
    (folder / "id_mapping.json").write_text("{}", encoding="utf-8")
    (folder / "components.sml").write_text(
        "<Components><Component name=\"badge\">"
        '<Rect id="label" x="0" y="0" w="100" h="30">'
        "<P>Authored</P></Rect>"
        "</Component></Components>",
        encoding="utf-8",
    )
    intended = parse_slide_content(
        '<Slide id="s1"><Use id="status" component="badge" '
        'x="10" y="20" w="100" h="30" /></Slide>',
        components=load_components(folder),
    )
    remote = parse_slide_content(
        '<Slide id="s1"><Rect id="status__label" x="10" y="20" '
        'w="100" h="30" class="content-align-middle text-align-left '
        'leading-100 space-above-0 space-below-0 indent-start-0 '
        'indent-first-0 spacing-never-collapse">'
        '<P class="font-weight-400">Authored</P></Rect></Slide>'
    )
    client = SlidesClient()
    monkeypatch.setattr(
        client,
        "_read_pristine",
        lambda _folder: ({"01": remote}, {}),
    )
    response: dict[str, Any] = {}

    client._append_persistence_warning(
        folder,
        {"01": intended},
        {("status__label", ChangeType.CREATE)},
        {("01", "status__label")},
        response,
    )

    assert "warnings" not in response


def test_persistence_warning_keeps_dropped_authored_class_on_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "deck"
    folder.mkdir()
    (folder / "id_mapping.json").write_text("{}", encoding="utf-8")
    intended = parse_slide_content(
        '<Slide id="s1"><TextBox id="new_box" x="10" y="20" w="100" h="30">'
        '<P><T class="text-color-#5df2b2">Authored</T></P>'
        "</TextBox></Slide>"
    )
    remote = parse_slide_content(
        '<Slide id="s1"><TextBox id="new_box" x="10" y="20" w="100" h="30" '
        'class="content-align-top text-align-left leading-100 space-above-0 '
        'space-below-0 indent-start-0 indent-first-0 spacing-never-collapse">'
        '<P><T class="font-weight-400">Authored</T></P></TextBox></Slide>'
    )
    client = SlidesClient()
    monkeypatch.setattr(
        client,
        "_read_pristine",
        lambda _folder: ({"01": remote}, {}),
    )
    response: dict[str, Any] = {}

    client._append_persistence_warning(
        folder,
        {"01": intended},
        {("new_box", ChangeType.CREATE)},
        {("01", "new_box")},
        response,
    )

    assert response.get("warnings")
    assert response["warnings"][0].severity is WarningSeverity.WARNING
    assert "text-color-#5df2b2" in response["warnings"][0].message


def test_persistence_warning_keeps_middle_alignment_on_textbox_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "deck"
    folder.mkdir()
    (folder / "id_mapping.json").write_text("{}", encoding="utf-8")
    intended = parse_slide_content(
        '<Slide id="s1"><TextBox id="new_box" x="10" y="20" '
        'w="100" h="30" /></Slide>'
    )
    remote = parse_slide_content(
        '<Slide id="s1"><TextBox id="new_box" x="10" y="20" '
        'w="100" h="30" class="content-align-middle" /></Slide>'
    )
    client = SlidesClient()
    monkeypatch.setattr(
        client,
        "_read_pristine",
        lambda _folder: ({"01": remote}, {}),
    )
    response: dict[str, Any] = {}

    client._append_persistence_warning(
        folder,
        {"01": intended},
        {("new_box", ChangeType.CREATE)},
        {("01", "new_box")},
        response,
    )

    assert response.get("warnings")
    assert "content-align-middle" in response["warnings"][0].message


def test_created_image_keeps_sub_point_zero_two_geometry_suppression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "deck"
    folder.mkdir()
    (folder / "id_mapping.json").write_text("{}", encoding="utf-8")
    intended = ParsedElement("hero", "Image", x=100, y=20, w=30, h=40)
    remote = ParsedElement("hero", "Image", x=99.981, y=20, w=30, h=40)
    client = SlidesClient()
    monkeypatch.setattr(
        client,
        "_read_pristine",
        lambda _folder: ({"01": [remote]}, {}),
    )
    response: dict[str, Any] = {}

    client._append_persistence_warning(
        folder,
        {"01": [intended]},
        {("hero", ChangeType.CREATE)},
        {("01", "hero")},
        response,
    )

    assert "warnings" not in response


def test_persistence_warning_treats_contain_effective_geometry_as_intended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "deck"
    assets = folder / "assets"
    assets.mkdir(parents=True)
    Image.new("RGB", (300, 100)).save(assets / "wide.png")
    (folder / "id_mapping.json").write_text("{}", encoding="utf-8")
    intended = parse_slide_content(
        '<Slide id="s1"><Image id="hero" src="./assets/wide.png" '
        'fit="contain" x="10" y="20" w="120" h="90" /></Slide>'
    )
    remote = parse_slide_content(
        '<Slide id="s1"><Image id="hero" x="10" y="20" w="120" h="40" />'
        "</Slide>"
    )
    client = SlidesClient()
    monkeypatch.setattr(
        client,
        "_read_pristine",
        lambda _folder: ({"01": remote}, {}),
    )
    response: dict[str, Any] = {}

    client._append_persistence_warning(
        folder,
        {"01": intended},
        {("hero", ChangeType.CREATE)},
        {("01", "hero")},
        response,
    )

    assert "warnings" not in response


async def test_second_edit_pushes_against_just_pushed_base(ws: Workspace) -> None:
    recolor_e121_locally(ws.folder, "#00ff00")
    await ws.client.push(ws.folder)

    recolor_e121_locally(ws.folder, "#0000ff")
    response = await ws.client.push(ws.folder)

    assert response["replies"]
    assert len(ws.stub.batch_calls) == 2
    assert ws.stub.batch_calls[1]["required_revision_id"] == "rev-after-push-1"
    base = json.loads(
        (ws.folder / ".pristine" / "base.json").read_text(encoding="utf-8")
    )
    assert base["revisionId"] == "rev-after-push-2"
    assert ws.client.diff(ws.folder) == []
