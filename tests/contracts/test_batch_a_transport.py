"""Transport and post-commit recovery contracts for Batch A."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from slidesmith.engine.client import SlidesClient
from slidesmith.engine.conflicts import collect_request_object_ids, detect_conflicts
from slidesmith.engine.diff_model import PushWarning, WarningSeverity
from slidesmith.engine.transport import (
    APIError,
    AuthenticationError,
    GoogleSlidesTransport,
    NotFoundError,
    PresentationData,
    Transport,
)


async def _mocked_transport(
    handler: Any, *, retry_attempts: int = 3
) -> GoogleSlidesTransport:
    transport = GoogleSlidesTransport(
        "token", retry_attempts=retry_attempts, retry_backoff=0
    )
    await transport._client.aclose()
    await transport._thumbnail_client.aclose()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport._thumbnail_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    return transport


@pytest.mark.parametrize(
    ("status", "error_type", "message"),
    [
        (401, AuthenticationError, "Invalid or expired"),
        (403, AuthenticationError, "Access denied"),
        (404, NotFoundError, "Presentation not found"),
        (418, APIError, "API error (418): teapot"),
    ],
)
async def test_every_http_error_branch(
    status: int, error_type: type[Exception], message: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="teapot", request=request)

    transport = await _mocked_transport(handler)
    try:
        with pytest.raises(error_type, match=re.escape(message)):
            await transport.get_presentation("pid")
    finally:
        await transport.close()


async def test_401_refreshes_authorization_and_retries_once() -> None:
    calls = 0
    auth_headers: list[str | None] = []

    async def refresh() -> tuple[str, float]:
        assert calls == 1
        return "refreshed-token", time.time() + 3600

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        auth_headers.append(request.headers.get("Authorization"))
        if calls == 1:
            return httpx.Response(401, request=request)
        return httpx.Response(200, json={"presentationId": "pid"}, request=request)

    transport = await _mocked_transport(handler)
    transport._client.headers["Authorization"] = "Bearer stale-token"
    transport._credential_refresh = refresh
    try:
        result = await transport.get_presentation("pid")
    finally:
        await transport.close()

    assert result.presentation_id == "pid"
    assert auth_headers == ["Bearer stale-token", "Bearer refreshed-token"]


async def test_batch_update_401_also_refreshes_and_retries_once() -> None:
    calls = 0

    async def refresh() -> str:
        return "refreshed-token"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(401, request=request)
        return httpx.Response(200, json={"replies": [{}]}, request=request)

    transport = await _mocked_transport(handler)
    transport._client.headers["Authorization"] = "Bearer stale-token"
    transport._credential_refresh = refresh
    try:
        result = await transport.batch_update("pid", [{"deleteObject": {"objectId": "x"}}])
    finally:
        await transport.close()

    assert result == {"replies": [{}]}
    assert calls == 2


async def test_concurrent_401s_share_one_refresh_and_retry_with_new_token() -> None:
    stale_requests = 0
    refresh_calls = 0
    stale_requests_ready = asyncio.Event()
    request_headers: list[str | None] = []

    async def refresh() -> tuple[str, float]:
        nonlocal refresh_calls
        refresh_calls += 1
        await asyncio.sleep(0)
        return "new-token", time.time() + 3600

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stale_requests
        authorization = request.headers.get("Authorization")
        request_headers.append(authorization)
        if authorization == "Bearer stale-token":
            stale_requests += 1
            if stale_requests == 2:
                stale_requests_ready.set()
            await stale_requests_ready.wait()
            return httpx.Response(401, request=request)
        return httpx.Response(200, json={"presentationId": "pid"}, request=request)

    transport = await _mocked_transport(handler)
    transport._client.headers["Authorization"] = "Bearer stale-token"
    transport._credential_refresh = refresh
    try:
        results = await asyncio.gather(
            transport.get_presentation("first"),
            transport.get_presentation("second"),
        )
    finally:
        await transport.close()

    assert [result.presentation_id for result in results] == ["pid", "pid"]
    assert refresh_calls == 1
    assert request_headers.count("Bearer stale-token") == 2
    assert request_headers.count("Bearer new-token") == 2


async def test_reentrant_refresh_attempt_fails_promptly() -> None:
    async def refresh() -> str:
        await transport.get_presentation("reentrant")
        return "unreachable"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request)

    transport = await _mocked_transport(handler)
    transport._credential_refresh = refresh
    try:
        with pytest.raises(AuthenticationError, match="re-export a fresh token"):
            await asyncio.wait_for(transport.get_presentation("pid"), timeout=5.0)
    finally:
        await transport.close()


async def test_get_auth_retry_is_independent_of_retry_attempts() -> None:
    statuses = iter([401, 200])

    async def refresh() -> str:
        return "refreshed-token"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses), json={"presentationId": "pid"}, request=request)

    transport = await _mocked_transport(handler, retry_attempts=1)
    transport._credential_refresh = refresh
    try:
        result = await transport.get_presentation("pid")
    finally:
        await transport.close()

    assert result.presentation_id == "pid"


async def test_persistent_401_after_refresh_is_authentication_error() -> None:
    async def refresh() -> str:
        return "still-invalid-token"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request)

    transport = await _mocked_transport(handler, retry_attempts=1)
    transport._credential_refresh = refresh
    try:
        with pytest.raises(AuthenticationError, match="re-export a fresh token"):
            await transport.get_presentation("pid")
    finally:
        await transport.close()


async def test_refresh_callback_exception_preserves_guided_authentication_error() -> None:
    async def refresh() -> str:
        raise RuntimeError("refresh backend unavailable")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request)

    transport = await _mocked_transport(handler, retry_attempts=1)
    transport._credential_refresh = refresh
    try:
        with pytest.raises(AuthenticationError, match="re-export a fresh token"):
            await transport.get_presentation("pid")
    finally:
        await transport.close()


async def test_get_auth_retry_then_429_uses_normal_retry_budget() -> None:
    statuses = iter([401, 429, 200])
    request_headers: list[str | None] = []

    async def refresh() -> str:
        return "refreshed-token"

    def handler(request: httpx.Request) -> httpx.Response:
        request_headers.append(request.headers.get("Authorization"))
        return httpx.Response(next(statuses), json={"presentationId": "pid"}, request=request)

    transport = await _mocked_transport(handler, retry_attempts=2)
    transport._client.headers["Authorization"] = "Bearer token"
    transport._credential_refresh = refresh
    try:
        result = await transport.get_presentation("pid")
    finally:
        await transport.close()

    assert result.presentation_id == "pid"
    assert request_headers == [
        "Bearer token",
        "Bearer refreshed-token",
        "Bearer refreshed-token",
    ]


async def test_retried_batch_update_preserves_body_and_write_control() -> None:
    bodies: list[dict[str, Any]] = []
    statuses = iter([401, 200])

    async def refresh() -> str:
        return "refreshed-token"

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(next(statuses), json={"replies": [{}]}, request=request)

    transport = await _mocked_transport(handler, retry_attempts=1)
    transport._credential_refresh = refresh
    requests = [{"updateShapeProperties": {"objectId": "shape"}}]
    try:
        result = await transport.batch_update(
            "pid", requests, required_revision_id="revision-7"
        )
    finally:
        await transport.close()

    assert result == {"replies": [{}]}
    assert bodies == [
        {
            "requests": requests,
            "writeControl": {"requiredRevisionId": "revision-7"},
        },
        {
            "requests": requests,
            "writeControl": {"requiredRevisionId": "revision-7"},
        },
    ]


async def test_401_without_refresh_has_reexport_and_resume_guidance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request)

    transport = await _mocked_transport(handler)
    try:
        with pytest.raises(AuthenticationError) as excinfo:
            await transport.get_presentation("pid")
    finally:
        await transport.close()

    assert "re-export a fresh token" in str(excinfo.value)
    assert "--resume" in str(excinfo.value)


async def test_expiring_credential_refreshes_with_named_buffer() -> None:
    refreshed: list[str] = []

    async def refresh() -> tuple[str, float]:
        refreshed.append("called")
        return "fresh-token", time.time() + 3600

    transport = GoogleSlidesTransport(
        "stale-token",
        credential_refresh=refresh,
        expires_at=time.time() + 60,
    )
    try:
        await transport.refresh_if_expiring()
        assert transport._client.headers["Authorization"] == "Bearer fresh-token"
    finally:
        await transport.close()

    assert refreshed == ["called"]


async def test_get_retries_429_and_5xx_with_exponential_bound() -> None:
    statuses = iter([429, 503, 200])
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        status = next(statuses)
        return httpx.Response(
            status,
            json={"presentationId": "pid"},
            request=request,
        )

    transport = await _mocked_transport(handler)
    try:
        result = await transport.get_presentation("pid")
    finally:
        await transport.close()
    assert result.presentation_id == "pid"
    assert calls == 3


async def test_get_stops_after_three_retryable_failures() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="unavailable", request=request)

    transport = await _mocked_transport(handler)
    try:
        with pytest.raises(APIError, match="503"):
            await transport.get_presentation("pid")
    finally:
        await transport.close()
    assert calls == 3


async def test_thumbnail_retries_metadata_and_content_gets() -> None:
    metadata_calls = 0
    content_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal metadata_calls, content_calls
        if request.url.host == "slides.googleapis.com":
            metadata_calls += 1
            if metadata_calls == 1:
                return httpx.Response(500, request=request)
            return httpx.Response(
                200,
                json={"contentUrl": "https://lh3.googleusercontent.com/image"},
                request=request,
            )
        content_calls += 1
        if content_calls == 1:
            return httpx.Response(502, request=request)
        return httpx.Response(200, content=b"png", request=request)

    transport = await _mocked_transport(handler)
    try:
        assert await transport.get_page_thumbnail("pid", "slide") == b"png"
    finally:
        await transport.close()
    assert (metadata_calls, content_calls) == (2, 2)


def test_conflict_collection_includes_group_children_and_deleted_target_slide() -> None:
    requests = [
        {
            "groupObjects": {
                "groupObjectId": "group",
                "childrenObjectIds": ["a", "b"],
            }
        },
        {
            "createShape": {
                "objectId": "new-shape",
                "shapeType": "RECTANGLE",
                "elementProperties": {"pageObjectId": "slide"},
            }
        },
    ]
    object_ids, page_ids = collect_request_object_ids(requests)
    assert object_ids == {"group", "a", "b", "new-shape"}
    assert page_ids == {"slide"}

    base = {"slides": [{"objectId": "slide", "pageElements": []}]}
    assert detect_conflicts(base, {"slides": []}, requests, {"s1": "slide"}) == [
        ("s1", "target slide deleted remotely")
    ]


def _deck() -> dict[str, Any]:
    return {
        "presentationId": "pid",
        "title": "retry",
        "pageSize": {
            "width": {"magnitude": 9144000, "unit": "EMU"},
            "height": {"magnitude": 5143500, "unit": "EMU"},
        },
        "slides": [
            {
                "objectId": "slide",
                "pageElements": [
                    {
                        "objectId": "shape",
                        "size": {
                            "width": {"magnitude": 3000024, "unit": "EMU"},
                            "height": {"magnitude": 3000024, "unit": "EMU"},
                        },
                        "transform": {
                            "scaleX": 1,
                            "scaleY": 1,
                            "translateX": 0,
                            "translateY": 0,
                            "unit": "EMU",
                        },
                        "shape": {"shapeType": "RECTANGLE", "shapeProperties": {}},
                    }
                ],
            }
        ],
    }


class _RefreshFailTransport(Transport):
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.fail_refresh = False
        self.batch_calls = 0

    async def get_presentation(self, presentation_id: str) -> PresentationData:
        if self.fail_refresh:
            raise APIError("refresh unavailable", 503)
        return PresentationData(presentation_id, copy.deepcopy(self.data))

    async def batch_update(
        self,
        presentation_id: str,
        requests: list[dict[str, Any]],
        required_revision_id: str | None = None,
    ) -> dict[str, Any]:
        self.batch_calls += 1
        self.fail_refresh = True
        return {"replies": [{}]}

    async def close(self) -> None:
        pass


async def test_committed_push_with_failed_refresh_returns_warning_and_keeps_workspace_consistent(
    tmp_path: Path,
) -> None:
    transport = _RefreshFailTransport(_deck())
    client = SlidesClient(transport)
    await client.pull("pid", tmp_path)
    folder = tmp_path / "pid"
    sml = folder / "slides" / "01" / "content.sml"
    sml.write_text(
        sml.read_text(encoding="utf-8").replace('x="0"', 'x="10"', 1),
        encoding="utf-8",
    )
    before = {
        path.relative_to(folder): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file()
    }

    response = await client.push(folder, force=True)

    after = {
        path.relative_to(folder): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file()
    }
    assert transport.batch_calls == 1
    assert before == after
    assert response["warnings"] == [
        PushWarning(
            WarningSeverity.WARNING,
            "push --force: conflict guard and revision lock bypassed; concurrent "
            "human edits to the touched properties will be overwritten",
        ),
        PushWarning(
            WarningSeverity.WARNING,
            "push applied; workspace stale; re-pull required "
            "(post-push refresh failed: refresh unavailable)",
        ),
    ]
