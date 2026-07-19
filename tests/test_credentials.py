"""Slidesmith session-store and auth-doctor behavior."""

from __future__ import annotations

import json
import stat
import time
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


def test_oauth_login_mirrors_refresh_session_without_client_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
