"""Offline contracts for per-slide progress and resumable pushes."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from slidesmith.engine.client import (
    PerSlideConflictError,
    PerSlidePushError,
    SlidesClient,
)
from slidesmith.engine.content_diff import Change, ChangeType, DiffResult
from slidesmith.engine.push_progress import (
    PUSH_PROGRESS_FILE,
    partition_requests_by_slide,
)
from slidesmith.engine.transport import APIError, PresentationData, Transport


GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)


class ResumableStubTransport(Transport):
    """Mutable four-slide transport with an optional one-shot batch failure."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = copy.deepcopy(data)
        self.original = copy.deepcopy(data)
        self.get_calls: list[str] = []
        self.batch_calls: list[dict[str, Any]] = []
        self.fail_on_call: int | None = None
        self.conflict_on_call: int | None = None
        self.success_count = 0

    async def get_presentation(self, presentation_id: str) -> PresentationData:
        self.get_calls.append(presentation_id)
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
        if self.fail_on_call == len(self.batch_calls):
            self.fail_on_call = None
            raise APIError("API error (503): temporary failure", status_code=503)
        if self.conflict_on_call == len(self.batch_calls):
            self.conflict_on_call = None
            raise APIError(
                "API error (400): requiredRevisionId does not match revision",
                status_code=400,
            )
        if (
            required_revision_id is not None
            and required_revision_id != self.data.get("revisionId")
        ):
            raise APIError(
                "API error (400): requiredRevisionId does not match revision",
                status_code=400,
            )

        for request in requests:
            update = request.get("updatePageElementTransform")
            if update is None:
                continue
            element = _find_element(self.data, update["objectId"])
            transform = update["transform"]
            if update["applyMode"] == "RELATIVE":
                remote_transform = element.setdefault("transform", {})
                remote_transform["translateX"] = remote_transform.get(
                    "translateX", 0
                ) + transform.get("translateX", 0)
                remote_transform["translateY"] = remote_transform.get(
                    "translateY", 0
                ) + transform.get("translateY", 0)
            else:
                element["transform"] = copy.deepcopy(transform)

        self.success_count += 1
        self.data["revisionId"] = f"resume-rev-{self.success_count}"
        return {"replies": [{}] * len(requests)}

    async def close(self) -> None:
        pass


@pytest.fixture
async def resumable_workspace(
    tmp_path: Path,
) -> tuple[ResumableStubTransport, SlidesClient, Path]:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    transport = ResumableStubTransport(data)
    client = SlidesClient(transport)
    await client.pull(data["presentationId"], tmp_path, save_raw=False)
    transport.get_calls.clear()
    folder = tmp_path / data["presentationId"]
    _move_first_element_on_every_slide(folder, 10)
    return transport, client, folder


def _move_first_element_on_every_slide(folder: Path, delta: float) -> None:
    for content_path in sorted((folder / "slides").glob("*/content.sml")):
        _move_first_element(content_path, delta)


def _move_first_element(content_path: Path, delta: float) -> None:
    content = content_path.read_text(encoding="utf-8")

    def replace(match: re.Match[str]) -> str:
        return f'x="{float(match.group(1)) + delta:g}"'

    edited, count = re.subn(r'x="(-?[0-9]+(?:\.[0-9]+)?)"', replace, content, count=1)
    assert count == 1
    content_path.write_text(edited, encoding="utf-8")


def _find_element(data: dict[str, Any], object_id: str) -> dict[str, Any]:
    def walk(elements: list[dict[str, Any]]) -> dict[str, Any] | None:
        for element in elements:
            if element.get("objectId") == object_id:
                return element
            found = walk(element.get("elementGroup", {}).get("children", []))
            if found is not None:
                return found
        return None

    for slide in data.get("slides", []):
        found = walk(slide.get("pageElements", []))
        if found is not None:
            return found
    raise AssertionError(f"element {object_id!r} not found")


def _target_slide_index(
    call: dict[str, Any], data: dict[str, Any]
) -> str:
    object_to_slide: dict[str, str] = {}

    def walk(elements: list[dict[str, Any]], index: str) -> None:
        for element in elements:
            object_to_slide[element["objectId"]] = index
            walk(element.get("elementGroup", {}).get("children", []), index)

    for position, slide in enumerate(data["slides"], 1):
        index = f"{position:02d}"
        object_to_slide[slide["objectId"]] = index
        walk(slide.get("pageElements", []), index)

    for request in call["requests"]:
        body = next(iter(request.values()))
        object_id = body.get("objectId")
        if object_id in object_to_slide:
            return object_to_slide[object_id]
        properties = body.get("elementProperties", {})
        page_id = properties.get("pageObjectId")
        if page_id in object_to_slide:
            return object_to_slide[page_id]
    raise AssertionError("batch did not target a known slide")


def test_partition_groups_existing_and_new_slide_requests(tmp_path: Path) -> None:
    folder = tmp_path / "deck"
    (folder / "slides" / "01").mkdir(parents=True)
    (folder / "slides" / "02").mkdir(parents=True)
    (folder / "slides" / "01" / "content.sml").write_text(
        '<Slide id="s1"><Rect id="box1" /></Slide>', encoding="utf-8"
    )
    (folder / "slides" / "02" / "content.sml").write_text(
        '<Slide id="s2"><Rect id="box2" /></Slide>', encoding="utf-8"
    )
    diff_result = DiffResult(
        changes=[
            Change(ChangeType.MOVE, "box1", slide_index="01"),
            Change(ChangeType.CREATE, "box2", slide_index="02"),
        ]
    )
    requests = [
        {"createSlide": {"objectId": "new_slide_02_1"}},
        {"updatePageElementTransform": {"objectId": "google_box1"}},
        {
            "createShape": {
                "objectId": "box2",
                "elementProperties": {"pageObjectId": "new_slide_02_1"},
            }
        },
        {"updateShapeProperties": {"objectId": "box2"}},
    ]

    batches = partition_requests_by_slide(
        requests,
        diff_result,
        {"s1": "google_slide1", "box1": "google_box1"},
        {"01": "google_slide1"},
        {
            "slides": [
                {
                    "objectId": "google_slide1",
                    "pageElements": [{"objectId": "google_box1"}],
                }
            ]
        },
        folder,
    )

    assert [batch.slide_index for batch in batches] == ["01", "02"]
    assert batches[0].requests == [requests[1]]
    assert batches[1].requests == [requests[0], requests[2], requests[3]]
    assert all(len(batch.content_hash) == 64 for batch in batches)


async def test_per_slide_push_writes_one_locked_batch_per_slide_in_order(
    monkeypatch: pytest.MonkeyPatch,
    resumable_workspace: tuple[ResumableStubTransport, SlidesClient, Path],
) -> None:
    transport, client, folder = resumable_workspace
    progress: list[tuple[str, str]] = []
    persistence_verifications: list[None] = []
    original_verify = client._append_persistence_warning

    def record_persistence_verification(*args: Any, **kwargs: Any) -> None:
        persistence_verifications.append(None)
        original_verify(*args, **kwargs)

    monkeypatch.setattr(
        client,
        "_append_persistence_warning",
        record_persistence_verification,
    )

    response = await client.push(
        folder,
        per_slide=True,
        progress=lambda event, message: progress.append((event, message)),
    )

    assert len(transport.batch_calls) == 4
    assert [
        _target_slide_index(call, transport.original)
        for call in transport.batch_calls
    ] == ["01", "02", "03", "04"]
    assert [call["required_revision_id"] for call in transport.batch_calls] == [
        transport.original["revisionId"],
        "resume-rev-1",
        "resume-rev-2",
        "resume-rev-3",
    ]
    assert len(response["replies"]) == sum(
        len(call["requests"]) for call in transport.batch_calls
    )
    assert [event for event, _ in progress].count("success") == 4
    assert progress[-1][1].startswith("slide 04/04 ✓ (")
    assert transport.get_calls == [transport.original["presentationId"]] * 5
    assert persistence_verifications == [None]
    assert client.diff(folder) == []
    assert not (folder / PUSH_PROGRESS_FILE).exists()


async def test_mid_deck_failure_records_successful_prefix_and_stops(
    resumable_workspace: tuple[ResumableStubTransport, SlidesClient, Path],
) -> None:
    transport, client, folder = resumable_workspace
    transport.fail_on_call = 2

    with pytest.raises(PerSlidePushError, match=r"slide 02/04 failed:.*503"):
        await client.push(folder, per_slide=True)

    assert len(transport.batch_calls) == 2
    ledger = json.loads((folder / PUSH_PROGRESS_FILE).read_text(encoding="utf-8"))
    assert ledger["presentationId"] == transport.original["presentationId"]
    assert [entry["slideIndex"] for entry in ledger["succeeded"]] == ["01"]
    assert len(ledger["succeeded"][0]["contentHash"]) == 64


async def test_per_slide_force_still_refreshes_and_carries_revision_locks(
    resumable_workspace: tuple[ResumableStubTransport, SlidesClient, Path],
) -> None:
    transport, client, folder = resumable_workspace

    response = await client.push(folder, per_slide=True, force=True)

    assert [call["required_revision_id"] for call in transport.batch_calls] == [
        transport.original["revisionId"],
        "resume-rev-1",
        "resume-rev-2",
        "resume-rev-3",
    ]
    assert any(
        "conflict guard bypassed" in warning for warning in response["warnings"]
    )


async def test_mid_deck_revision_conflict_records_successful_prefix_and_stops(
    resumable_workspace: tuple[ResumableStubTransport, SlidesClient, Path],
) -> None:
    transport, client, folder = resumable_workspace
    transport.conflict_on_call = 2

    with pytest.raises(
        PerSlideConflictError,
        match=r"slide 02/04 failed:.*deck changed mid-push",
    ):
        await client.push(folder, per_slide=True)

    assert len(transport.batch_calls) == 2
    ledger = json.loads((folder / PUSH_PROGRESS_FILE).read_text(encoding="utf-8"))
    assert [entry["slideIndex"] for entry in ledger["succeeded"]] == ["01"]


async def test_resume_skips_matching_hash_and_continues_at_failed_slide(
    resumable_workspace: tuple[ResumableStubTransport, SlidesClient, Path],
) -> None:
    transport, client, folder = resumable_workspace
    transport.fail_on_call = 2
    with pytest.raises(PerSlidePushError):
        await client.push(folder, per_slide=True)

    progress: list[tuple[str, str]] = []
    await client.push(
        folder,
        per_slide=True,
        resume=True,
        progress=lambda event, message: progress.append((event, message)),
    )

    resumed_calls = transport.batch_calls[2:]
    assert [
        _target_slide_index(call, transport.original) for call in resumed_calls
    ] == ["02", "03", "04"]
    assert resumed_calls[0]["required_revision_id"] == "resume-rev-1"
    assert progress[0] == ("skipped", "slide 01/04 ✓ (already pushed)")
    assert not (folder / PUSH_PROGRESS_FILE).exists()


async def test_resume_does_not_skip_a_successful_slide_whose_content_changed(
    resumable_workspace: tuple[ResumableStubTransport, SlidesClient, Path],
) -> None:
    transport, client, folder = resumable_workspace
    transport.fail_on_call = 2
    with pytest.raises(PerSlidePushError):
        await client.push(folder, per_slide=True)

    _move_first_element(folder / "slides" / "01" / "content.sml", 5)
    revision = transport.data["revisionId"]
    transport.data = copy.deepcopy(transport.original)
    transport.data["revisionId"] = revision

    await client.push(folder, per_slide=True, resume=True)

    resumed_calls = transport.batch_calls[2:]
    assert len(resumed_calls) == 4
    assert _target_slide_index(resumed_calls[0], transport.original) == "01"
    assert not (folder / PUSH_PROGRESS_FILE).exists()


async def test_default_push_still_emits_one_atomic_batch(
    resumable_workspace: tuple[ResumableStubTransport, SlidesClient, Path],
) -> None:
    transport, client, folder = resumable_workspace

    await client.push(folder)

    assert len(transport.batch_calls) == 1
    assert transport.batch_calls[0]["required_revision_id"] == (
        transport.original["revisionId"]
    )
    assert not (folder / PUSH_PROGRESS_FILE).exists()
