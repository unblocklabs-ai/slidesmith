"""Slidesmith session-store and auth-doctor behavior."""

from __future__ import annotations

import json
import stat
import time
import webbrowser
from pathlib import Path
from typing import Any

import pytest

from slidesmith import cli, credentials
from slidesmith.credentials import (
    CredentialsManager,
    FallbackSessionStore,
    FileSessionStore,
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
