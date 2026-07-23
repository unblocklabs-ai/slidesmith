"""Hermetic contracts for create, workspace materialization, and sharing."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from slidesmith import cli
from slidesmith.engine.client import SlidesClient
from slidesmith.engine.permissions import (
    DrivePermissionError,
    GoogleDrivePermissionsClient,
)
from slidesmith.engine.transport import (
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


class CreateTransport(Transport):
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = copy.deepcopy(data)
        self.created_titles: list[str] = []
        self._client = object()

    async def create_presentation(self, title: str) -> PresentationData:
        self.created_titles.append(title)
        return PresentationData(self.data["presentationId"], copy.deepcopy(self.data))

    async def get_presentation(self, presentation_id: str) -> PresentationData:
        return PresentationData(presentation_id, copy.deepcopy(self.data))

    async def batch_update(
        self,
        presentation_id: str,
        requests: list[dict[str, Any]],
        required_revision_id: str | None = None,
    ) -> dict[str, Any]:
        return {"replies": [{} for _ in requests]}

    async def close(self) -> None:
        pass


def _data() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


async def test_create_materializes_normal_workspace_and_has_zero_diff(
    tmp_path: Path,
) -> None:
    data = _data()
    transport = CreateTransport(data)
    client = SlidesClient(transport)
    output_path = tmp_path / "created"
    output_path.mkdir()

    created = await client.create("Created title", output_path)
    folder = output_path / data["presentationId"]

    assert created.presentation_id == data["presentationId"]
    assert transport.created_titles == ["Created title"]
    assert (folder / "presentation.json").exists()
    assert (folder / ".pristine" / "presentation.zip").exists()
    assert client.diff(folder) == []

    await client.pull(data["presentationId"], tmp_path / "pulled")
    pulled = tmp_path / "pulled" / data["presentationId"]
    for relative in ("id_mapping.json", "styles.json", "slides/01/content.sml"):
        assert (folder / relative).read_bytes() == (pulled / relative).read_bytes()


@pytest.mark.asyncio
async def test_google_transport_create_posts_title() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/v1/presentations"
        assert json.loads(request.content) == {"title": "A new deck"}
        return httpx.Response(200, json=_data(), request=request)

    transport = GoogleSlidesTransport("token")
    await transport._client.aclose()
    await transport._thumbnail_client.aclose()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport._thumbnail_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        result = await transport.create_presentation("A new deck")
    finally:
        await transport.close()

    assert result.presentation_id == _data()["presentationId"]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_permissions_reuse_transport_client_after_create_refresh() -> None:
    create_authorizations: list[str] = []
    permission_authorizations: list[str] = []

    async def refresh() -> tuple[str, None]:
        return "fresh-token", None

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers["Authorization"]
        if request.url.path == "/v1/presentations":
            create_authorizations.append(authorization)
            if authorization == "Bearer stale-token":
                return httpx.Response(401, text="expired", request=request)
            return httpx.Response(200, json=_data(), request=request)
        if request.url.path.startswith("/drive/v3/files/") and request.url.path.endswith(
            "/permissions"
        ):
            permission_authorizations.append(authorization)
            return httpx.Response(200, json={"id": "permission-id"}, request=request)
        raise AssertionError(f"unexpected request: {request.url}")

    transport = GoogleSlidesTransport(
        "stale-token",
        credential_refresh=refresh,
    )
    await transport._client.aclose()
    await transport._thumbnail_client.aclose()
    transport._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={
            "Authorization": "Bearer stale-token",
            "Accept": "application/json",
        },
    )
    transport._thumbnail_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    permissions = GoogleDrivePermissionsClient(client=transport._client)
    try:
        created = await transport.create_presentation("A refreshed deck")
        await permissions.create_permission(
            created.presentation_id,
            permission_type="user",
            role="reader",
            email_address="person@example.com",
        )
    finally:
        await permissions.close()
        await transport.close()

    assert create_authorizations == ["Bearer stale-token", "Bearer fresh-token"]
    assert permission_authorizations == ["Bearer fresh-token"]


@pytest.mark.asyncio
async def test_drive_permission_request_shape_includes_role_email_and_no_notice() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "permission-id"}, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    permissions = GoogleDrivePermissionsClient(client=http_client)
    try:
        await permissions.create_permission(
            "deck-id",
            permission_type="user",
            role="commenter",
            email_address="person@example.com",
            send_notification_email=False,
        )
    finally:
        await permissions.close()
        await http_client.aclose()

    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/drive/v3/files/deck-id/permissions"
    assert request.url.params["fields"] == "id"
    assert request.url.params["sendNotificationEmail"] == "false"
    assert json.loads(request.content) == {
        "type": "user",
        "role": "commenter",
        "emailAddress": "person@example.com",
    }


class RecordingPermissions:
    instances: list["RecordingPermissions"] = []

    def __init__(self, *, client: Any) -> None:
        self.client = client
        self.calls: list[dict[str, Any]] = []
        type(self).instances.append(self)

    async def create_permission(self, file_id: str, **kwargs: Any) -> None:
        self.calls.append({"file_id": file_id, **kwargs})
        if kwargs["email_address"] in {"bad@example.com", "only@example.com"}:
            raise DrivePermissionError("permission denied", status_code=403)

    async def close(self) -> None:
        pass


def _patch_create_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> CreateTransport:
    transport = CreateTransport(_data())
    monkeypatch.setattr(cli, "_token", lambda *_args: "token")
    monkeypatch.setattr(
        "slidesmith.engine.transport.GoogleSlidesTransport",
        lambda *_args, **_kwargs: transport,
    )
    monkeypatch.setattr(
        "slidesmith.engine.permissions.GoogleDrivePermissionsClient",
        RecordingPermissions,
    )
    RecordingPermissions.instances.clear()
    return transport


def test_create_cli_reports_partial_share_failure_and_keeps_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = _patch_create_cli(monkeypatch)

    cli.main(
        [
            "create",
            "--title",
            "CLI deck",
            "--dir",
            str(tmp_path),
            "--share",
            "good@example.com,bad@example.com",
            "--role",
            "reader",
        ]
    )

    captured = capsys.readouterr()
    output = captured.out
    folder = tmp_path / _data()["presentationId"]
    assert transport.created_titles == ["CLI deck"]
    assert folder.is_dir()
    assert "Created presentation:" in output
    assert "URL: https://docs.google.com/presentation/d/" in output
    assert "Workspace:" in output
    assert "Shared with: good@example.com (role=reader)" in output
    assert "bad@example.com: permission denied" in output
    assert "missing the https://www.googleapis.com/auth/drive.file scope" in output
    assert "deck was created and materialized" in output
    assert _data()["presentationId"] in captured.err
    assert _presentation_url_for_test(_data()["presentationId"]) in captured.err
    assert "remote deck exists and can be pulled normally" in captured.err
    assert RecordingPermissions.instances[0].calls[0]["role"] == "reader"


def test_create_cli_all_share_failures_exit_nonzero_but_keep_deck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = _patch_create_cli(monkeypatch)

    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "create",
                "--title",
                "CLI deck",
                "--dir",
                str(tmp_path),
                "--share",
                "only@example.com",
            ]
        )

    assert raised.value.code == 1
    assert (tmp_path / _data()["presentationId"]).is_dir()
    captured = capsys.readouterr()
    assert "The deck was created and materialized" in captured.out
    assert _data()["presentationId"] in captured.err
    assert _presentation_url_for_test(_data()["presentationId"]) in captured.err
    assert "remote deck exists and can be pulled normally" in captured.err
    assert transport.created_titles == ["CLI deck"]


def test_create_cli_rejects_workspace_collision_before_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = tmp_path / "existing-workspace"
    existing.mkdir()
    (existing / "presentation.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "_token", lambda *_args: pytest.fail("auth was called"))

    with pytest.raises(SystemExit) as raised:
        cli.main(["create", "--title", "No API", "--dir", str(existing)])

    assert raised.value.code == 1


def test_create_cli_rejects_unwritable_parent_before_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unwritable = tmp_path / "unwritable"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    monkeypatch.setattr(cli, "_token", lambda *_args: pytest.fail("auth was called"))
    monkeypatch.setattr(
        "slidesmith.engine.transport.GoogleSlidesTransport",
        lambda *_args, **_kwargs: pytest.fail("transport was constructed"),
    )
    try:
        with pytest.raises(SystemExit) as raised:
            cli.main(["create", "--title", "No API", "--dir", str(unwritable)])
    finally:
        unwritable.chmod(0o700)

    assert raised.value.code == 1


def test_create_cli_reports_remote_context_when_materialization_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = _patch_create_cli(monkeypatch)

    def fail_materialization(*_args: Any, **_kwargs: Any) -> list[Path]:
        raise OSError("forced materialization failure")

    monkeypatch.setattr(
        "slidesmith.engine.client.materialize_workspace",
        fail_materialization,
    )

    with pytest.raises(SystemExit) as raised:
        cli.main(["create", "--title", "CLI deck", "--dir", str(tmp_path)])

    assert raised.value.code == 1
    output = capsys.readouterr()
    combined = output.out + output.err
    presentation_id = _data()["presentationId"]
    assert presentation_id in combined
    assert _presentation_url_for_test(presentation_id) in combined
    assert "remote deck exists and can be pulled normally" in combined
    assert "slidesmith pull" in combined
    assert transport.created_titles == ["CLI deck"]


def test_create_cli_reports_remote_context_for_id_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_create_cli(monkeypatch)
    presentation_id = _data()["presentationId"]
    (tmp_path / presentation_id).mkdir()

    with pytest.raises(SystemExit) as raised:
        cli.main(["create", "--title", "CLI deck", "--dir", str(tmp_path)])

    assert raised.value.code == 1
    output = capsys.readouterr()
    combined = output.out + output.err
    assert presentation_id in combined
    assert _presentation_url_for_test(presentation_id) in combined
    assert "remote deck exists and can be pulled normally" in combined


def _presentation_url_for_test(presentation_id: str) -> str:
    return f"https://docs.google.com/presentation/d/{presentation_id}/edit"


def test_create_cli_requires_title() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["create"])
