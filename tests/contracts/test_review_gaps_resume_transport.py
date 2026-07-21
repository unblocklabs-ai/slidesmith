"""Regression coverage for full-review resume and transport test gaps."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

import slidesmith.engine.transport as transport_module
from slidesmith.engine.client import SlidesClient
from slidesmith.engine.push_progress import PUSH_PROGRESS_FILE
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


class _RecordingTransport(Transport):
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = copy.deepcopy(data)
        self.get_calls: list[str] = []
        self.batch_calls: list[list[dict[str, Any]]] = []

    async def get_presentation(self, presentation_id: str) -> PresentationData:
        self.get_calls.append(presentation_id)
        return PresentationData(presentation_id, copy.deepcopy(self.data))

    async def batch_update(
        self,
        presentation_id: str,
        requests: list[dict[str, Any]],
        required_revision_id: str | None = None,
    ) -> dict[str, Any]:
        self.batch_calls.append(copy.deepcopy(requests))
        return {"replies": [{}] * len(requests)}

    async def close(self) -> None:
        pass


@pytest.fixture
async def resume_workspace(
    tmp_path: Path,
) -> tuple[_RecordingTransport, SlidesClient, Path, str]:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    transport = _RecordingTransport(data)
    client = SlidesClient(transport)
    await client.pull(data["presentationId"], tmp_path, save_raw=False)
    transport.get_calls.clear()
    folder = tmp_path / data["presentationId"]
    content_path = folder / "slides" / "01" / "content.sml"
    content = content_path.read_text(encoding="utf-8")
    edited, count = re.subn(
        r'x="(-?[0-9]+(?:\.[0-9]+)?)"',
        lambda match: f'x="{float(match.group(1)) + 10:g}"',
        content,
        count=1,
    )
    assert count == 1
    content_path.write_text(edited, encoding="utf-8")
    return transport, client, folder, data["presentationId"]


@pytest.mark.parametrize(
    "ledger_contents",
    [
        "{not-json",
        {"version": 2, "presentationId": "PRESENTATION", "succeeded": []},
        {"version": 1, "presentationId": "FOREIGN", "succeeded": []},
        {
            "version": 1,
            "presentationId": "PRESENTATION",
            "succeeded": [{"slideIndex": "01"}],
        },
    ],
    ids=["corrupt-json", "wrong-version", "foreign-presentation", "malformed-entry"],
)
async def test_bad_resume_ledger_aborts_before_transport(
    resume_workspace: tuple[_RecordingTransport, SlidesClient, Path, str],
    ledger_contents: str | dict[str, Any],
) -> None:
    transport, client, folder, presentation_id = resume_workspace
    if isinstance(ledger_contents, str):
        serialized = ledger_contents
    else:
        normalized = copy.deepcopy(ledger_contents)
        if normalized.get("presentationId") == "PRESENTATION":
            normalized["presentationId"] = presentation_id
        serialized = json.dumps(normalized)
    (folder / PUSH_PROGRESS_FILE).write_text(serialized, encoding="utf-8")

    with pytest.raises(ValueError, match="Cannot resume"):
        await client.push(folder, per_slide=True, resume=True)

    assert transport.get_calls == []
    assert transport.batch_calls == []


async def _mocked_transport(
    handler: Any,
    *,
    retry_attempts: int = 4,
    retry_backoff: float = 0.25,
) -> GoogleSlidesTransport:
    transport = GoogleSlidesTransport(
        "token",
        retry_attempts=retry_attempts,
        retry_backoff=retry_backoff,
    )
    await transport._client.aclose()
    await transport._thumbnail_client.aclose()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport._thumbnail_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    return transport


async def test_get_429_and_5xx_use_bounded_exponential_delays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = iter([429, 500, 503, 200])
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            next(statuses),
            json={"presentationId": "pid"},
            request=request,
        )

    monkeypatch.setattr(transport_module.asyncio, "sleep", record_sleep)
    transport = await _mocked_transport(handler)
    try:
        result = await transport.get_presentation("pid")
    finally:
        await transport.close()

    assert result.presentation_id == "pid"
    assert sleeps == [0.25, 0.5, 1.0]


async def test_non_retryable_get_never_sleeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418, text="teapot", request=request)

    monkeypatch.setattr(transport_module.asyncio, "sleep", record_sleep)
    transport = await _mocked_transport(handler)
    try:
        with pytest.raises(APIError, match="418"):
            await transport.get_presentation("pid")
    finally:
        await transport.close()

    assert sleeps == []


async def test_batch_update_429_is_single_attempt_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="throttled", request=request)

    monkeypatch.setattr(transport_module.asyncio, "sleep", record_sleep)
    transport = await _mocked_transport(handler)
    try:
        with pytest.raises(APIError, match="429"):
            await transport.batch_update("pid", [{"deleteObject": {"objectId": "x"}}])
    finally:
        await transport.close()

    assert calls == 1
    assert sleeps == []
