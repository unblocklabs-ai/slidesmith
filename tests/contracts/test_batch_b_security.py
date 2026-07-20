"""Security regression contracts for review-fix Batch B."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
import urllib.parse
from typing import Any

import httpx
import pytest

from extraslide.transport import GoogleSlidesTransport, TransportError
from slidesmith import credentials
from slidesmith.cli import _presentation_id
from slidesmith.credentials import CredentialsManager, SessionToken


def _invoke_callback(
    path: str, *, expected_state: str
) -> tuple[dict[str, Any], str, int]:
    result: dict[str, Any] = {"code": None, "error": None, "done": False}
    handler_class = CredentialsManager._create_handler_class(
        result, threading.Lock(), expected_state
    )
    handler = object.__new__(handler_class)
    handler.path = path
    handler.wfile = io.BytesIO()
    statuses: list[int] = []
    handler.send_response = statuses.append
    handler.send_header = lambda *_args: None
    handler.end_headers = lambda: None

    handler.do_GET()

    return result, handler.wfile.getvalue().decode("utf-8"), statuses[0]


@pytest.mark.parametrize("flow", ["oauth_client", "extrasuite_session"])
def test_state_mismatch_is_rejected_in_both_loopback_flows(
    flow: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected_state = "expected-state"
    manager = object.__new__(CredentialsManager)
    manager._headless = False
    manager._server_base_url = "https://auth.example"
    monkeypatch.setattr(
        credentials.secrets, "token_urlsafe", lambda _size: expected_state
    )

    def reject_mismatched_callback(
        auth_url_for_port: Any, _display_msg: str
    ) -> tuple[str, int]:
        auth_url = auth_url_for_port(43123, expected_state)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
        assert query["state"] == [expected_state]
        result, _html, status = _invoke_callback(
            "/?code=attacker-code&state=wrong-state",
            expected_state=expected_state,
        )
        assert status == 400
        raise Exception(f"Authentication failed: {result['error']}")

    monkeypatch.setattr(manager, "_run_browser_flow", reject_mismatched_callback)

    with pytest.raises(Exception, match="OAuth state mismatch"):
        if flow == "oauth_client":
            manager._run_oauth_browser_flow("client-id", "client-secret")
        else:
            manager._run_browser_flow_for_session()


def test_session_authorization_url_has_s256_pkce_and_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = object.__new__(CredentialsManager)
    manager._headless = False
    manager._server_base_url = "https://auth.example"
    captured_query: dict[str, list[str]] = {}

    def capture_url(auth_url_for_port: Any, _display_msg: str) -> tuple[str, int]:
        auth_url = auth_url_for_port(43123, "oauth-state")
        captured_query.update(
            urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
        )
        return "auth-code", 43123

    monkeypatch.setattr(manager, "_run_browser_flow", capture_url)

    code, verifier = manager._run_browser_flow_for_session()

    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    assert code == "auth-code"
    assert captured_query == {
        "port": ["43123"],
        "state": ["oauth-state"],
        "code_challenge": [expected_challenge],
        "code_challenge_method": ["S256"],
    }


def test_session_exchange_sends_pkce_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = object.__new__(CredentialsManager)
    manager._server_base_url = "https://auth.example"
    manager._load_session_token = lambda _name: None
    manager._run_browser_flow_for_session = lambda: ("auth-code", "pkce-verifier")
    manager._collect_device_info = lambda: {}
    saved: list[tuple[SessionToken, str]] = []
    manager._save_session_token = lambda session, name: saved.append((session, name))
    request_bodies: list[dict[str, Any]] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def read(self) -> bytes:
            return json.dumps(
                {
                    "session_token": "session-token",
                    "email": "user@example.com",
                    "expires_at": "2030-01-01T00:00:00Z",
                }
            ).encode("utf-8")

    def urlopen(request: Any, **_kwargs: Any) -> Response:
        request_bodies.append(json.loads(request.data.decode("utf-8")))
        return Response()

    monkeypatch.setattr(credentials.urllib.request, "urlopen", urlopen)

    manager._get_or_create_session_token()

    assert request_bodies == [
        {"code": "auth-code", "code_verifier": "pkce-verifier"}
    ]
    assert saved[0][1] == "default"


def test_callback_error_html_is_escaped() -> None:
    result, response_html, status = _invoke_callback(
        "/?state=expected&error=%3Cscript%3Ealert%281%29%3C%2Fscript%3E",
        expected_state="expected",
    )

    assert status == 400
    assert result["error"] == "<script>alert(1)</script>"
    assert "<script>alert(1)</script>" not in response_html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response_html


@pytest.mark.parametrize(
    "value",
    ["", "../escape", "deck id", "deck/id", "https://example.com/not-a-deck"],
)
def test_presentation_id_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError, match="Invalid presentation URL or ID"):
        _presentation_id(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("deck_ID-123", "deck_ID-123"),
        (
            "https://docs.google.com/presentation/d/deck_ID-123/edit",
            "deck_ID-123",
        ),
    ],
)
def test_presentation_id_accepts_google_id_characters(
    value: str, expected: str
) -> None:
    assert _presentation_id(value) == expected


async def test_thumbnail_rejects_non_google_content_url_before_download() -> None:
    api_requests: list[httpx.Request] = []
    download_requests: list[httpx.Request] = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        api_requests.append(request)
        return httpx.Response(
            200,
            json={"contentUrl": "https://attacker.example/thumbnail.png"},
            request=request,
        )

    def download_handler(request: httpx.Request) -> httpx.Response:
        download_requests.append(request)
        return httpx.Response(200, content=b"png", request=request)

    transport = GoogleSlidesTransport("secret-token")
    await transport._client.aclose()
    await transport._thumbnail_client.aclose()
    transport._client = httpx.AsyncClient(
        transport=httpx.MockTransport(api_handler),
        headers={"Authorization": "Bearer secret-token"},
    )
    transport._thumbnail_client = httpx.AsyncClient(
        transport=httpx.MockTransport(download_handler)
    )
    try:
        with pytest.raises(TransportError, match="allowed Google hosts"):
            await transport.get_page_thumbnail("presentation", "slide")
    finally:
        await transport.close()

    assert len(api_requests) == 1
    assert api_requests[0].headers["Authorization"] == "Bearer secret-token"
    assert download_requests == []


async def test_thumbnail_download_client_has_no_authorization_header() -> None:
    download_requests: list[httpx.Request] = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "contentUrl": "https://lh3.googleusercontent.com/thumbnail.png"
            },
            request=request,
        )

    def download_handler(request: httpx.Request) -> httpx.Response:
        download_requests.append(request)
        return httpx.Response(200, content=b"png", request=request)

    transport = GoogleSlidesTransport("secret-token")
    await transport._client.aclose()
    await transport._thumbnail_client.aclose()
    transport._client = httpx.AsyncClient(
        transport=httpx.MockTransport(api_handler),
        headers={"Authorization": "Bearer secret-token"},
    )
    transport._thumbnail_client = httpx.AsyncClient(
        transport=httpx.MockTransport(download_handler)
    )
    try:
        assert await transport.get_page_thumbnail("presentation", "slide") == b"png"
    finally:
        await transport.close()

    assert len(download_requests) == 1
    assert "Authorization" not in download_requests[0].headers


def test_oauth_user_scopes_are_slides_only() -> None:
    assert credentials._OAUTH_USER_SCOPES == [
        "https://www.googleapis.com/auth/presentations",
        "https://www.googleapis.com/auth/drive.file",
        "openid",
        "email",
    ]
