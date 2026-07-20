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

from slidesmith.engine.client import ConflictError, SlidesClient
from slidesmith.engine.transport import (
    APIError,
    GoogleSlidesTransport,
    PresentationData,
    Transport,
)

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
        "no pristine base snapshot found (.pristine/base.json); this folder was "
        "pulled by an older slidesmith. Remote-change detection skipped for this "
        "push -- re-pull to re-enable the guard."
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
        "push --force: conflict guard and revision lock bypassed; concurrent "
        "human edits to the touched properties will be overwritten"
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
        "1 change(s) did not persist remotely: e121 (style update) "
        "— the API may not support these values"
    ]


async def test_push_without_remote_divergence_returns_no_warning(
    ws: Workspace,
) -> None:
    recolor_e121_locally(ws.folder, "#00ff00")

    response = await ws.client.push(ws.folder)

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
