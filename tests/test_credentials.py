"""Slidesmith session-store and auth-doctor behavior."""

from __future__ import annotations

import json
import stat
import sys
import time
import types
import webbrowser
from pathlib import Path
from typing import Any

import pytest

from slidesmith import cli, credentials
from slidesmith.auth import browser_flow
from slidesmith.auth.errors import AuthError
from slidesmith.cli_commands._support import _token as cli_token
from slidesmith.cli_commands._support import _transport_options
from slidesmith.credentials import (
    CredentialsManager,
    FallbackSessionStore,
    FileSessionStore,
    InMemorySessionStore,
    KeyringSessionStore,
    OAuthClientCredentials,
    SessionToken,
)


class MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, profile: str) -> str | None:
        return self.values.get((service, profile))

    def set_password(self, service: str, profile: str, value: str) -> None:
        self.values[(service, profile)] = value

    def delete_password(self, service: str, profile: str) -> None:
        self.values.pop((service, profile), None)


class BrokenKeyring:
    def get_password(self, service: str, profile: str) -> str | None:
        raise RuntimeError((-50, "Unknown Error"))

    def set_password(self, service: str, profile: str, value: str) -> None:
        raise RuntimeError((-50, "Unknown Error"))

    def delete_password(self, service: str, profile: str) -> None:
        raise RuntimeError((-50, "Unknown Error"))


def test_oauth_browser_flow_without_refresh_token_returns_expiring_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen_time = 1_700_000_000.0
    monkeypatch.setattr("slidesmith.auth.browser_flow.time.time", lambda: frozen_time)
    manager = object.__new__(CredentialsManager)
    monkeypatch.setattr(
        manager,
        "_run_browser_flow",
        lambda _auth_url_for_port, _display_msg: ("auth-code", 43123),
    )
    monkeypatch.setattr(
        "slidesmith.auth.browser_flow._post_form_json",
        lambda _url, _fields: {"access_token": "access-token", "expires_in": 2700},
    )

    access_token, refresh_token, expires_at = manager._run_oauth_browser_flow(
        "client-id", "client-secret"
    )

    assert access_token == "access-token"
    assert refresh_token is None
    assert expires_at == frozen_time + 2700


def test_oauth_browser_flow_with_refresh_token_keeps_refresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen_time = 1_700_000_000.0
    monkeypatch.setattr("slidesmith.auth.browser_flow.time.time", lambda: frozen_time)
    manager = object.__new__(CredentialsManager)
    monkeypatch.setattr(
        manager,
        "_run_browser_flow",
        lambda _auth_url_for_port, _display_msg: ("auth-code", 43123),
    )
    monkeypatch.setattr(
        "slidesmith.auth.browser_flow._post_form_json",
        lambda _url, _fields: {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 2700,
        },
    )

    access_token, refresh_token, expires_at = manager._run_oauth_browser_flow(
        "client-id", "client-secret"
    )

    assert access_token == "access-token"
    assert refresh_token == "refresh-token"
    assert expires_at == frozen_time + 2700


def _token(*, expires_at: float | None = None) -> SessionToken:
    return SessionToken(
        raw_token="refresh-or-session-token",
        email="agent@example.com",
        expires_at=expires_at or time.time() + 86400,
    )


def _oauth_creds() -> OAuthClientCredentials:
    return OAuthClientCredentials(
        client_id="client-id",
        client_secret="client-secret",
        source="gws",
        location="/tmp/client_secret.json",
    )


def _doctor_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    oauth: bool = True,
    keyring: Any | None = None,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "EXTRASUITE_SERVER_URL",
        "SERVICE_ACCOUNT_PATH",
        "GOOGLE_WORKSPACE_CLI_TOKEN",
        "GOG_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        credentials,
        "_find_gws_client_credentials",
        (lambda: _oauth_creds()) if oauth else (lambda: None),
    )
    monkeypatch.setattr(credentials, "_find_gogcli_client_credentials", lambda: None)
    monkeypatch.setattr(credentials, "_KEYRING_AVAILABLE", True)
    monkeypatch.setattr(credentials, "_keyring", keyring or MemoryKeyring())


def test_file_session_store_round_trip_and_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)
    store = FileSessionStore(path)
    token = _token()

    store.save("default", token)

    assert store.load("default") == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["profiles"]["default"] == token.to_dict()


def test_file_session_store_does_not_double_close_after_managed_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-L4: fdopen owns the descriptor once construction succeeds."""
    store = FileSessionStore(tmp_path / "session.json")
    closed: list[int] = []

    class BrokenHandle:
        exited = False

        def __enter__(self) -> BrokenHandle:
            return self

        def write(self, _value: str) -> None:
            raise OSError("disk full")

        def __exit__(self, *_args: object) -> None:
            self.exited = True

    handle = BrokenHandle()
    monkeypatch.setattr(credentials.os, "open", lambda *_args, **_kwargs: 99)
    monkeypatch.setattr(credentials.os, "fdopen", lambda *_args, **_kwargs: handle)
    monkeypatch.setattr(credentials.os, "close", closed.append)

    with pytest.raises(OSError, match="disk full"):
        store.save("default", _token())

    assert handle.exited
    assert closed == []


def test_browser_flow_binds_callback_once_and_uses_bound_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-L4: the advertised callback port comes from the listening socket."""
    addresses: list[tuple[str, int]] = []
    advertised_ports: list[int] = []

    class FakeServer:
        server_port = 43123
        timeout = 0

        def __init__(self, address: tuple[str, int], _handler: type) -> None:
            addresses.append(address)

        def handle_request(self) -> None:
            pass

        def server_close(self) -> None:
            pass

    manager = object.__new__(CredentialsManager)
    manager.DEFAULT_CALLBACK_TIMEOUT = 0
    monkeypatch.setattr(credentials.http.server, "HTTPServer", FakeServer)
    monkeypatch.setattr(webbrowser, "open", lambda _url: True)

    with pytest.raises(Exception, match="Authentication timed out"):
        manager._run_browser_flow(
            lambda port, _state: (
                advertised_ports.append(port) or f"http://auth/{port}"
            ),
            "Authenticate:",
        )

    assert addresses == [("127.0.0.1", 0)]
    assert advertised_ports == [43123]


def test_keyring_failure_falls_back_once_to_file_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(credentials, "_KEYRING_AVAILABLE", True)
    monkeypatch.setattr(credentials, "_keyring", BrokenKeyring())
    file_store = FileSessionStore(tmp_path / "session.json")
    token = _token()
    file_store.save("default", token)
    store = FallbackSessionStore(KeyringSessionStore(), file_store)

    assert store.load("default") == token
    assert store.load("default") == token

    stderr = capsys.readouterr().err
    assert stderr.count("using file session store") == 1
    assert "Unknown Error" in stderr


def test_empty_keyring_falls_back_to_file_and_repairs_keyring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = MemoryKeyring()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SLIDESMITH_TOKEN_STORE", raising=False)
    monkeypatch.setattr(credentials, "_KEYRING_AVAILABLE", True)
    monkeypatch.setattr(credentials, "_keyring", keyring)
    manager = CredentialsManager(server_url="https://auth.example")
    token = _token()
    assert manager._file_session_store is not None
    manager._file_session_store.save("default", token)

    assert manager._load_session_token("default") == token
    repaired = keyring.get_password("extrasuite", "default")
    assert repaired is not None
    assert SessionToken.from_dict(json.loads(repaired)) == token


def test_service_account_requests_only_the_presentations_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    class FakeCredentials:
        token = "access-token"

        def refresh(self, _request: object) -> None:
            pass

    def fake_from_file(path: str, *, scopes: list[str]) -> FakeCredentials:
        captured["path"] = path
        captured["scopes"] = scopes
        return FakeCredentials()

    class FakeCredentialsFactory:
        from_service_account_file = staticmethod(fake_from_file)

    google = types.ModuleType("google")
    auth = types.ModuleType("google.auth")
    transport = types.ModuleType("google.auth.transport")
    requests = types.ModuleType("google.auth.transport.requests")
    requests.Request = object  # type: ignore[attr-defined]
    oauth2 = types.ModuleType("google.oauth2")
    service_account = types.ModuleType("google.oauth2.service_account")
    service_account.Credentials = FakeCredentialsFactory  # type: ignore[attr-defined]
    google.auth = auth  # type: ignore[attr-defined]
    google.oauth2 = oauth2  # type: ignore[attr-defined]
    auth.transport = transport  # type: ignore[attr-defined]
    transport.requests = requests  # type: ignore[attr-defined]
    oauth2.service_account = service_account  # type: ignore[attr-defined]
    for name, module in {
        "google": google,
        "google.auth": auth,
        "google.auth.transport": transport,
        "google.auth.transport.requests": requests,
        "google.oauth2": oauth2,
        "google.oauth2.service_account": service_account,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    service_account_path = tmp_path / "service-account.json"
    service_account_path.write_text("{}", encoding="utf-8")
    manager = object.__new__(CredentialsManager)
    manager._sa_path = service_account_path

    manager._get_service_account_credential()

    assert captured["scopes"] == [
        "https://www.googleapis.com/auth/presentations"
    ]


def test_invalid_token_store_choice_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SLIDESMITH_TOKEN_STORE", "database")

    with pytest.raises(ValueError, match="must be 'keyring' or 'file'.*database"):
        CredentialsManager(server_url="https://auth.example")


def test_forced_keyring_store_rejects_unavailable_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SLIDESMITH_TOKEN_STORE", "keyring")
    monkeypatch.setattr(credentials, "_KEYRING_AVAILABLE", False)

    with pytest.raises(RuntimeError, match="keyring is not available"):
        CredentialsManager(server_url="https://auth.example")


def test_forced_keyring_store_propagates_backend_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SLIDESMITH_TOKEN_STORE", "keyring")
    monkeypatch.setattr(credentials, "_KEYRING_AVAILABLE", True)
    monkeypatch.setattr(credentials, "_keyring", BrokenKeyring())
    manager = CredentialsManager(server_url="https://auth.example")

    with pytest.raises(RuntimeError, match="Unknown Error"):
        manager._load_session_token("default")


def test_corrupt_file_session_is_treated_as_missing(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("{not valid json", encoding="utf-8")

    # Conscious design choice: corrupt persisted state silently triggers re-auth.
    assert FileSessionStore(path).load("default") is None


def test_fallback_save_reraises_when_both_backends_fail() -> None:
    class FailingStore:
        def load(self, _profile_name: str) -> SessionToken | None:
            return None

        def save(self, _profile_name: str, _token: SessionToken) -> None:
            raise OSError("storage unavailable")

        def delete(self, _profile_name: str) -> None:
            pass

    store = FallbackSessionStore(FailingStore(), FailingStore())

    with pytest.raises(OSError, match="storage unavailable"):
        store.save("default", _token())


def test_file_session_store_loads_legacy_single_payload(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    token = _token()
    path.write_text(json.dumps(token.to_dict()), encoding="utf-8")

    assert FileSessionStore(path).load("default") == token


def test_oauth_sessions_mirror_on_login_and_keyring_load_without_client_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    keyring = MemoryKeyring()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CLIENT_SECRET", "client-secret")
    monkeypatch.delenv("SLIDESMITH_TOKEN_STORE", raising=False)
    monkeypatch.setattr(credentials, "_KEYRING_AVAILABLE", True)
    monkeypatch.setattr(credentials, "_keyring", keyring)
    monkeypatch.setattr(
        CredentialsManager,
        "GATEWAY_CONFIG_PATH",
        tmp_path / "missing-gateway.json",
    )
    manager = CredentialsManager()
    monkeypatch.setattr(
        manager,
        "_run_oauth_browser_flow",
        lambda client_id, client_secret: ("access-token", "refresh-token"),
    )

    session = manager.login(force=True)

    assert session.raw_token == "refresh-token"
    assert FileSessionStore().load("gws-default") == session
    keyring_payload = keyring.get_password("extrasuite", "gws-default")
    assert keyring_payload is not None
    assert json.loads(keyring_payload)["raw_token"] == "refresh-token"
    assert "client-secret" not in FileSessionStore().path.read_text(encoding="utf-8")

    # Reproduce the reviewed delta: a GUI-minted token exists only in Keychain.
    # Loading it for a normal credential exchange must repair the file store
    # without opening a browser or minting a new session.
    FileSessionStore().path.unlink()
    monkeypatch.setattr(
        manager,
        "_run_oauth_browser_flow",
        lambda *args: (_ for _ in ()).throw(AssertionError("unexpected login")),
    )
    monkeypatch.setattr(
        credentials,
        "_exchange_refresh_token",
        lambda *args: ("loaded-access-token", time.time() + 3600),
    )

    credential = manager._get_oauth_client_credential()

    assert credential.token == "loaded-access-token"
    assert FileSessionStore().load("gws-default") == session

    # A peer-store write failure is diagnostic, not an authentication failure.
    FileSessionStore().path.unlink()
    assert manager._file_session_store is not None
    monkeypatch.setattr(
        manager._file_session_store,
        "save",
        lambda *args: (_ for _ in ()).throw(OSError("read-only file store")),
    )

    assert manager._load_session_token("gws-default") == session
    assert "could not mirror session token to file store" in capsys.readouterr().err


def test_oauth_access_only_session_is_persisted_and_reported_to_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CLIENT_SECRET", "client-secret")
    monkeypatch.delenv("GOOGLE_WORKSPACE_CLI_TOKEN", raising=False)
    monkeypatch.delenv("GOG_ACCESS_TOKEN", raising=False)

    manager = CredentialsManager(session_store=InMemorySessionStore())
    expires_at = time.time() + 2700
    monkeypatch.setattr(
        manager,
        "_run_oauth_browser_flow",
        lambda _client_id, _client_secret: ("access-token", None, expires_at),
    )

    credential = manager.get_credential(
        command={"type": "slide.pull"}, reason="test access-only login"
    )
    session = manager._load_session_token("gws-default")

    assert credential.token == "access-token"
    assert credential.expires_at == expires_at
    assert session is not None
    assert session.access_token == "access-token"
    assert session.raw_token is None
    assert session.expires_at == expires_at
    assert not session.is_refreshable
    output = capsys.readouterr().err
    assert "lasts about 1 hour" in output
    assert "myaccount.google.com/permissions" in output
    assert "your own OAuth client" in output


def test_access_only_payload_omits_raw_token_and_old_reader_degrades_to_no_session(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.json"
    token = SessionToken(
        access_token="short-lived-access-token",
        email="",
        expires_at=time.time() + 3600,
        is_refreshable=False,
    )
    FileSessionStore(path).save("gws-default", token)
    payload = json.loads(path.read_text(encoding="utf-8"))["profiles"]["gws-default"]

    assert "raw_token" not in payload
    assert payload["access_token"] == "short-lived-access-token"
    assert payload["is_refreshable"] is False

    # The old reader from 6b33d94 indexes raw_token, email, and expires_at.
    # FileSessionStore catches that KeyError and returns None, so this payload
    # follows the old reader's degrade-to-no-session path.
    def old_reader_fields(payload: dict[str, Any]) -> tuple[Any, Any, Any] | None:
        try:
            return payload["raw_token"], payload["email"], payload["expires_at"]
        except KeyError:
            return None

    assert old_reader_fields(payload) is None


def test_legacy_refresh_payload_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    token = _token()
    path.write_text(json.dumps(token.to_dict()), encoding="utf-8")

    loaded = FileSessionStore(path).load("default")

    assert loaded == token
    assert loaded is not None and loaded.raw_token == token.raw_token


def test_access_only_file_store_entry_is_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    FileSessionStore(path).save(
        "gws-default",
        SessionToken(
            access_token="short-lived-access-token",
            email="",
            expires_at=time.time() + 3600,
            is_refreshable=False,
        ),
    )

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_expired_access_only_session_prompts_for_consent_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLI_CLIENT_SECRET", "client-secret")
    store = FileSessionStore(tmp_path / "session.json")
    store.save(
        "gws-default",
        SessionToken(
            access_token="expired-access-token",
            email="",
            expires_at=time.time() - 1,
            is_refreshable=False,
        ),
    )
    manager = CredentialsManager(session_store=store)
    consent_calls = 0

    def consent(_client_id: str, _client_secret: str) -> tuple[str, None, float]:
        nonlocal consent_calls
        consent_calls += 1
        return "new-access-token", None, time.time() + 3600

    monkeypatch.setattr(manager, "_run_oauth_browser_flow", consent)

    credential = manager.get_credential(
        command={"type": "slide.pull"}, reason="expired access-only test"
    )

    assert credential.token == "new-access-token"
    assert consent_calls == 1


def test_auth_doctor_reports_credential_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _doctor_setup(tmp_path, monkeypatch, oauth=False)

    cli.main(["auth", "doctor"])

    output = capsys.readouterr().out
    assert "OAuth client credentials: ABSENT" in output
    assert "Verdict: CREDENTIAL ABSENT" in output
    assert "Next command: gws auth setup" in output


def test_auth_doctor_shows_keyring_error_and_recovery_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _doctor_setup(tmp_path, monkeypatch, keyring=BrokenKeyring())

    cli.main(["auth", "doctor"])

    output = capsys.readouterr().out
    assert "OAuth client credentials: FOUND (gws: /tmp/client_secret.json)" in output
    assert "Keyring: DENIED OR BROKEN" in output
    assert "Unknown Error" in output
    assert "Verdict: KEYRING DENIED OR BROKEN" in output
    assert "Next command: slidesmith auth login" in output


def test_auth_doctor_reports_expired_file_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _doctor_setup(tmp_path, monkeypatch)
    FileSessionStore().save("gws-default", _token(expires_at=time.time() - 60))

    cli.main(["auth", "doctor"])

    output = capsys.readouterr().out
    assert "File store: PRESENT" in output
    assert "token EXPIRED" in output
    assert "Verdict: TOKEN EXPIRED" in output
    assert "Next command: slidesmith auth login" in output


def test_auth_doctor_reports_access_only_session_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _doctor_setup(tmp_path, monkeypatch)
    FileSessionStore().save(
        "gws-default",
        SessionToken(
            raw_token="access-token",
            email="",
            expires_at=time.time() + 2700,
            is_refreshable=False,
        ),
    )

    cli.main(["auth", "doctor"])

    output = capsys.readouterr().out
    assert "token ACCESS-ONLY" in output
    assert "usable-but-expiring" in output
    assert "myaccount.google.com/permissions" in output
    assert "your own OAuth client" in output


def test_auth_doctor_warns_bare_tokens_about_unknown_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _doctor_setup(tmp_path, monkeypatch, oauth=False)
    monkeypatch.setenv("GOG_ACCESS_TOKEN", "bare-access-token")
    monkeypatch.setattr(
        credentials, "_probe_bare_token", lambda _token: ("unreachable", None)
    )

    cli.main(["auth", "doctor"])

    output = capsys.readouterr().out
    assert "usable now, expiry unknown (~1h typical); long pushes may fail mid-run" in output


def test_tokeninfo_probe_uses_fixed_google_post_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        headers: dict[str, str] = {}

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def read(self) -> bytes:
            return b'{"expires_in": 1800}'

        def getcode(self) -> int:
            return 200

    calls: list[tuple[Any, int]] = []
    opener_handlers: list[tuple[Any, ...]] = []

    class FakeOpener:
        def open(self, request: Any, *, timeout: int) -> FakeResponse:
            calls.append((request, timeout))
            return FakeResponse()

    def fake_build_opener(*handlers: Any) -> FakeOpener:
        opener_handlers.append(handlers)
        return FakeOpener()

    monkeypatch.setattr(
        browser_flow.urllib.request, "build_opener", fake_build_opener
    )

    status, expires_at = browser_flow.probe_bare_token("token with spaces")

    assert status == "valid"
    assert expires_at is not None
    request, timeout = calls[0]
    assert request.get_method() == "POST"
    assert request.full_url == "https://oauth2.googleapis.com/tokeninfo"
    assert request.data == b"access_token=token+with+spaces"
    assert timeout == 30
    assert any(
        isinstance(handler, browser_flow._NoRedirectHandler)
        for handler in opener_handlers[0]
    )


def test_tokeninfo_redirect_is_unreachable_without_forwarding_to_attacker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokeninfo_requests: list[Any] = []
    attacker_requests: list[Any] = []

    class RedirectResponse:
        headers = {"Location": "https://attacker.example/tokeninfo"}

        def getcode(self) -> int:
            return 302

        def close(self) -> None:
            pass

    class FakeOpener:
        def __init__(self, redirect_handler: Any) -> None:
            self.redirect_handler = redirect_handler

        def open(self, request: Any, *, timeout: int) -> Any:
            tokeninfo_requests.append(request)
            response = RedirectResponse()
            self.redirect_handler.redirect_request(
                request,
                response,
                302,
                "Found",
                response.headers,
                response.headers["Location"],
            )
            attacker_requests.append(response.headers["Location"])
            return response

    def fake_build_opener(*handlers: Any) -> FakeOpener:
        redirect_handler = next(
            handler
            for handler in handlers
            if isinstance(handler, browser_flow._NoRedirectHandler)
        )
        return FakeOpener(redirect_handler)

    monkeypatch.setattr(
        browser_flow.urllib.request, "build_opener", fake_build_opener
    )

    assert browser_flow.probe_bare_token("SENSITIVE") == ("unreachable", None)
    assert len(tokeninfo_requests) == 1
    assert tokeninfo_requests[0].full_url == "https://oauth2.googleapis.com/tokeninfo"
    assert tokeninfo_requests[0].data == b"access_token=SENSITIVE"
    assert attacker_requests == []


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (400, "invalid"),
        (401, "invalid"),
        (403, "unreachable"),
        (500, "unreachable"),
        ("timeout", "unreachable"),
        ("non-json", "unreachable"),
    ],
)
def test_bare_token_probe_classifies_tokeninfo_failures(
    monkeypatch: pytest.MonkeyPatch, outcome: int | str, expected: str
) -> None:
    class FakeResponse:
        headers: dict[str, str] = {}

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def getcode(self) -> int:
            return 200

        def read(self) -> bytes:
            return b"not-json" if outcome == "non-json" else b"{}"

    class FakeOpener:
        def open(self, request: Any, *, timeout: int) -> FakeResponse:
            if outcome == "timeout":
                raise TimeoutError("tokeninfo timed out")
            if isinstance(outcome, int):
                raise browser_flow.urllib.error.HTTPError(
                    request.full_url, outcome, "tokeninfo failure", {}, None
                )
            return FakeResponse()

    monkeypatch.setattr(
        browser_flow.urllib.request,
        "build_opener",
        lambda *_handlers: FakeOpener(),
    )

    assert browser_flow.probe_bare_token("probe-token") == (expected, None)


def test_bare_token_expired_fails_before_pull_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _doctor_setup(tmp_path, monkeypatch, oauth=False)
    monkeypatch.setenv("GOG_ACCESS_TOKEN", "stale-token")
    monkeypatch.setattr(
        credentials, "_probe_bare_token", lambda _token: ("invalid", None)
    )
    transport_constructions: list[None] = []

    class UnexpectedTransport:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            transport_constructions.append(None)

    monkeypatch.setattr(
        "slidesmith.engine.transport.GoogleSlidesTransport", UnexpectedTransport
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["pull", "presentation-id"])

    assert excinfo.value.code == 1
    error_output = capsys.readouterr().err
    assert browser_flow.GOG_BARE_TOKEN_REMEDIATION in error_output
    assert "stale-token" not in error_output
    assert transport_constructions == []


def test_bare_token_valid_probe_populates_expiry_and_warns_when_near_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _doctor_setup(tmp_path, monkeypatch, oauth=False)
    monkeypatch.setenv("GOG_ACCESS_TOKEN", "fresh-token")
    expires_at = time.time() + 60
    monkeypatch.setattr(
        credentials, "_probe_bare_token", lambda _token: ("valid", expires_at)
    )

    token = cli_token("slide.pull", "presentation-id")

    assert token.expires_at == expires_at
    assert token.auth_mode == "bare_token"
    assert "warning: GOG_ACCESS_TOKEN expires in about" in capsys.readouterr().err


def test_bare_token_unreachable_probe_proceeds_without_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _doctor_setup(tmp_path, monkeypatch, oauth=False)
    monkeypatch.setenv("GOG_ACCESS_TOKEN", "possibly-working-token")
    monkeypatch.setattr(
        credentials, "_probe_bare_token", lambda _token: ("unreachable", None)
    )

    token = cli_token("slide.pull", "presentation-id")

    assert token == "possibly-working-token"
    assert token.expires_at is None


def test_invalid_bare_token_error_redacts_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _doctor_setup(tmp_path, monkeypatch, oauth=False)
    monkeypatch.setenv("GOG_ACCESS_TOKEN", "secret-invalid-token")
    monkeypatch.setattr(
        credentials, "_probe_bare_token", lambda _token: ("invalid", None)
    )

    with pytest.raises(AuthError) as excinfo:
        cli_token("slide.pull", "presentation-id")

    assert "secret-invalid-token" not in str(excinfo.value)


def test_auth_doctor_reports_expired_bare_token_remediation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _doctor_setup(tmp_path, monkeypatch, oauth=False)
    monkeypatch.setenv("GOG_ACCESS_TOKEN", "stale-token")
    monkeypatch.setattr(
        credentials, "_probe_bare_token", lambda _token: ("invalid", None)
    )

    cli.main(["auth", "doctor"])

    output = capsys.readouterr().out
    assert "Pre-obtained access token: FOUND (environment variable); EXPIRED/INVALID" in output
    assert "gog sometimes exports a stale token" in output
    assert "Verdict: TOKEN EXPIRED OR INVALID" in output
    assert "stale-token" not in output


def test_auth_doctor_reports_valid_bare_token_lifetime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _doctor_setup(tmp_path, monkeypatch, oauth=False)
    monkeypatch.setenv("GOG_ACCESS_TOKEN", "valid-token")
    expires_at = time.time() + 1800
    monkeypatch.setattr(
        credentials, "_probe_bare_token", lambda _token: ("valid", expires_at)
    )

    cli.main(["auth", "doctor"])

    output = capsys.readouterr().out
    assert "Pre-obtained access token: FOUND (environment variable); VALID" in output
    assert "expires in approximately" in output
    assert "Verdict: READY (valid; expires in approximately" in output


def test_local_command_in_bare_token_mode_does_not_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _doctor_setup(tmp_path, monkeypatch, oauth=False)
    monkeypatch.setenv("GOG_ACCESS_TOKEN", "local-command-token")
    probes: list[str] = []

    def unexpected_probe(token: str) -> tuple[str, None]:
        probes.append(token)
        raise AssertionError("local commands must not probe credentials")

    monkeypatch.setattr(credentials, "_probe_bare_token", unexpected_probe)

    slide_dir = tmp_path / "slides" / "01"
    slide_dir.mkdir(parents=True)
    (slide_dir / "content.sml").write_text('<Slide id="s1" />', encoding="utf-8")

    cli.main(["fmt", str(tmp_path)])

    assert "Formatted 1 content.sml file(s)." in capsys.readouterr().out
    assert probes == []


def test_oauth_token_path_does_not_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OAuthManager:
        auth_mode = "oauth_client"

        def get_credential(self, **_kwargs: Any) -> Any:
            return types.SimpleNamespace(token="oauth-token", expires_at=None)

        def probe_bare_token(self, _token: str) -> tuple[str, None]:
            raise AssertionError("OAuth mode must not probe tokeninfo")

        def refresh_credential(self, **_kwargs: Any) -> None:
            return None

    monkeypatch.setattr(credentials, "CredentialsManager", OAuthManager)

    token = cli_token("slide.pull", "presentation-id")

    assert token == "oauth-token"
    assert _transport_options(token)["auth_mode"] == "oauth_client"


def _keyring_store_with(monkeypatch: pytest.MonkeyPatch, raw: str) -> KeyringSessionStore:
    """A KeyringSessionStore whose backend returns `raw` for any profile."""
    keyring = MemoryKeyring()
    keyring.set_password("extrasuite", "default", raw)
    monkeypatch.setattr(credentials, "_KEYRING_AVAILABLE", True)
    monkeypatch.setattr(credentials, "_keyring", keyring)
    return KeyringSessionStore()


@pytest.mark.parametrize(
    "raw",
    [
        "[]",  # valid JSON, wrong shape (list not dict)
        '{"raw_token": "t", "email": "e", "expires_at": "not-a-number"}',  # non-numeric
        '{"raw_token": "t"}',  # missing keys
        "{not json",  # syntactically invalid
    ],
)
def test_keyring_load_malformed_payload_returns_none(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """TG-7: malformed-but-valid keyring JSON degrades to no-session (like
    FileSessionStore), never crashing with TypeError/ValueError."""
    store = _keyring_store_with(monkeypatch, raw)
    assert store.load("default") is None


def test_keyring_load_valid_payload_roundtrips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = SessionToken(raw_token="t", email="e@x.com", expires_at=time.time() + 3600)
    store = _keyring_store_with(monkeypatch, json.dumps(token.to_dict()))
    loaded = store.load("default")
    assert loaded is not None and loaded.raw_token == "t"
