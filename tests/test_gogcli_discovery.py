"""Hermetic tests for gogcli OAuth client discovery."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from slidesmith import credentials
from slidesmith.auth import discovery
from slidesmith.auth.doctor import auth_doctor_lines


class FakeKeyring:
    def __init__(self, values: dict[tuple[str, str], str | None] | None = None):
        self.values = values or {}
        self.calls: list[tuple[str, str]] = []
        self.error: Exception | None = None

    def get_password(self, service: str, key: str) -> str | None:
        self.calls.append((service, key))
        if self.error is not None:
            raise self.error
        return self.values.get((service, key))


@pytest.fixture
def isolated_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FakeKeyring:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "GOG_DATA_DIR",
        "GOG_CONFIG_DIR",
        "GOG_HOME",
        "GOG_KEYRING_SERVICE_NAME",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "APPDATA",
        "GOG_ACCESS_TOKEN",
        "GOOGLE_WORKSPACE_CLI_TOKEN",
        "EXTRASUITE_SERVER_URL",
        "SERVICE_ACCOUNT_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    fake = FakeKeyring()
    monkeypatch.setattr(credentials, "_KEYRING_AVAILABLE", True)
    monkeypatch.setattr(credentials, "_keyring", fake)
    return fake


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _metadata(client_id: str = "client-id") -> dict[str, str]:
    return {"client_id": client_id}


def _legacy(client_id: str = "client-id") -> dict[str, str]:
    return {"client_id": client_id, "client_secret": "client-secret"}


def test_legacy_full_json_in_config_dir_still_works(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    path = tmp_path / ".config" / "gogcli" / "credentials.json"
    _write(path, _legacy())

    result = discovery._find_gogcli_client_credentials()

    assert result is not None
    assert result.client_id == "client-id"
    assert result.client_secret == "client-secret"
    assert result.location == str(path)
    assert isolated_auth.calls == []


def test_metadata_uses_default_keyring_service_and_key(
    tmp_path: Path, isolated_auth: FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("GOG_DATA_DIR", str(data_dir))
    path = data_dir / "credentials.json"
    _write(path, _metadata())
    isolated_auth.values[("gogcli", "client/default/client-secret")] = (
        "  keyring-secret  "
    )

    result = discovery._find_gogcli_client_credentials()

    assert result is not None
    assert result.client_secret == "keyring-secret"
    assert result.location == (
        f"{path} + client secret from OS keyring (service gogcli)"
    )
    assert ("gogcli", "client/default/client-secret") in isolated_auth.calls


def test_custom_keyring_service_name_is_trimmed_and_used(
    tmp_path: Path, isolated_auth: FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("GOG_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GOG_KEYRING_SERVICE_NAME", "  custom-gog  ")
    _write(data_dir / "credentials.json", _metadata())
    isolated_auth.values[("custom-gog", "client/default/client-secret")] = "secret"

    result = discovery._find_gogcli_client_credentials()

    assert result is not None
    assert ("custom-gog", "client/default/client-secret") in isolated_auth.calls


@pytest.mark.parametrize("value", [None, "   "])
def test_missing_or_whitespace_keyring_secret_is_unreadable(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("GOG_DATA_DIR", str(data_dir))
    _write(data_dir / "credentials.json", _metadata())
    isolated_auth.values[("gogcli", "client/default/client-secret")] = value

    result = discovery._inspect_gogcli_client_credentials()

    assert result.status == "keyring-unreadable"
    assert result.client_path == data_dir / "credentials.json"
    assert discovery._find_gogcli_client_credentials() is None


def test_keyring_error_is_unreadable_and_doctor_is_actionable(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("GOG_DATA_DIR", str(data_dir))
    _write(data_dir / "credentials.json", _metadata())
    isolated_auth.error = RuntimeError("locked keychain")

    result = discovery._inspect_gogcli_client_credentials()
    lines = auth_doctor_lines(
        find_gws_client_credentials=lambda: None,
        probe_bare_token=lambda _token: ("unreachable", None),
    )
    output = "\n".join(lines)

    assert result.status == "keyring-unreadable"
    assert discovery._find_gogcli_client_credentials() is None
    assert "client id found at" in output
    assert "secret lives in the OS keyring and could not be read" in output
    assert "On macOS" in output
    assert "Always Allow" in output
    assert "GOG_KEYRING_SERVICE_NAME" in output
    assert "export GOG_ACCESS_TOKEN" in output
    assert "gog auth credentials <file> --insecure" in output
    assert "Verdict: GOGCLI CLIENT SECRET UNREADABLE" in output
    assert "CREDENTIAL ABSENT" not in output


def test_unreadable_keyring_does_not_make_valid_gogcli_session_runtime_auth(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("GOG_DATA_DIR", str(data_dir))
    _write(data_dir / "credentials.json", _metadata())
    isolated_auth.values[("extrasuite", "gogcli-default")] = json.dumps(
        {
            "raw_token": "session-token",
            "email": "agent@example.com",
            "expires_at": time.time() + 86400,
            "is_refreshable": True,
        }
    )

    lines = auth_doctor_lines(
        find_gws_client_credentials=lambda: None,
        probe_bare_token=lambda _token: ("unreachable", None),
    )
    output = "\n".join(lines)

    assert "Session profile: gogcli-default" in output
    assert "Keyring: READABLE; token VALID" in output
    assert "Verdict: GOGCLI CLIENT SECRET UNREADABLE" in output
    assert "Verdict: CREDENTIAL ABSENT" not in output
    assert "Verdict: READY" not in output


def test_gateway_uses_default_session_for_unreadable_gogcli_metadata(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("GOG_DATA_DIR", str(data_dir))
    monkeypatch.setenv("EXTRASUITE_SERVER_URL", "https://gateway.example")
    _write(data_dir / "credentials.json", _metadata())
    isolated_auth.error = RuntimeError("locked keychain")
    _write(
        tmp_path / ".config" / "slidesmith" / "session.json",
        {
            "profiles": {
                "default": {
                    "raw_token": "session-token",
                    "email": "agent@example.com",
                    "expires_at": time.time() + 86400,
                    "is_refreshable": True,
                }
            }
        },
    )

    lines = auth_doctor_lines(
        find_gws_client_credentials=lambda: None,
        probe_bare_token=lambda _token: ("unreachable", None),
    )
    output = "\n".join(lines)

    assert "Session profile: default" in output
    assert "File store: PRESENT" in output
    assert "token VALID" in output
    assert "SESSION TOKEN ABSENT" not in output
    assert "Verdict: GOGCLI CLIENT SECRET UNREADABLE" not in output


def test_unreadable_keyring_does_not_override_valid_bare_token(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("GOG_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GOG_ACCESS_TOKEN", "valid-token")
    _write(data_dir / "credentials.json", _metadata())
    isolated_auth.error = RuntimeError("locked keychain")

    lines = auth_doctor_lines(
        find_gws_client_credentials=lambda: None,
        probe_bare_token=lambda _token: ("valid", time.time() + 3600),
    )
    output = "\n".join(lines)

    assert "Pre-obtained access token: FOUND" in output
    assert "client id found at" in output
    assert "On macOS" in output
    assert "Verdict: READY" in output
    assert "Verdict: GOGCLI CLIENT SECRET UNREADABLE" not in output


def test_unreadable_keyring_does_not_override_service_account(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    service_account = tmp_path / "service-account.json"
    monkeypatch.setenv("GOG_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SERVICE_ACCOUNT_PATH", str(service_account))
    _write(data_dir / "credentials.json", _metadata())
    service_account.write_text("{}", encoding="utf-8")
    isolated_auth.error = RuntimeError("locked keychain")

    lines = auth_doctor_lines(
        find_gws_client_credentials=lambda: None,
        probe_bare_token=lambda _token: ("unreachable", None),
    )
    output = "\n".join(lines)

    assert "Service account: FOUND" in output
    assert "Verdict: READY" in output
    assert "Verdict: GOGCLI CLIENT SECRET UNREADABLE" not in output


def test_gateway_uses_default_session_profile_with_gogcli_client(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("GOG_DATA_DIR", str(data_dir))
    _write(data_dir / "credentials.json", _metadata())
    isolated_auth.values[("gogcli", "client/default/client-secret")] = "secret"
    _write(
        tmp_path / ".config" / "extrasuite" / "gateway.json",
        {"EXTRASUITE_SERVER_URL": "https://gateway.example"},
    )
    _write(
        tmp_path / ".config" / "slidesmith" / "session.json",
        {
            "profiles": {
                "default": {
                    "raw_token": "session-token",
                    "email": "agent@example.com",
                    "expires_at": time.time() + 86400,
                    "is_refreshable": True,
                }
            }
        },
    )

    lines = auth_doctor_lines(find_gws_client_credentials=lambda: None)
    output = "\n".join(lines)

    assert "ExtraSuite gateway: FOUND" in output
    assert "Session profile: default" in output
    assert "File store: PRESENT" in output
    assert "token VALID" in output
    assert "Verdict: READY" in output
    assert "SESSION TOKEN ABSENT" not in output


@pytest.mark.parametrize(
    ("env_name", "expected"),
    [
        ("GOG_DATA_DIR", "data-explicit"),
        ("GOG_HOME", "gog-home/data"),
        ("XDG_DATA_HOME", "xdg-data/gogcli"),
    ],
)
def test_data_directory_precedence(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    expected: str,
) -> None:
    root = tmp_path / expected.split("/")[0]
    monkeypatch.setenv(env_name, str(root))
    path = tmp_path / expected / "credentials.json"
    _write(path, _legacy())

    result = discovery._find_gogcli_client_credentials()

    assert result is not None
    assert result.location == str(path)


def test_config_directory_override_is_used(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config-explicit"
    monkeypatch.setenv("GOG_CONFIG_DIR", str(config_dir))
    path = config_dir / "credentials.json"
    _write(path, _legacy())

    result = discovery._find_gogcli_client_credentials()

    assert result is not None
    assert result.location == str(path)


@pytest.mark.parametrize(
    ("system", "relative_dir"),
    [
        ("Darwin", Path("Library") / "Application Support" / "gogcli"),
        ("Linux", Path(".local") / "share" / "gogcli"),
    ],
)
def test_platform_default_data_directories(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    relative_dir: Path,
) -> None:
    monkeypatch.setattr(discovery.platform, "system", lambda: system)
    path = tmp_path / relative_dir / "credentials.json"
    _write(path, _legacy())

    result = discovery._find_gogcli_client_credentials()

    assert result is not None
    assert result.location == str(path)


def test_data_directory_is_checked_before_config_directory(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("GOG_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GOG_CONFIG_DIR", str(config_dir))
    _write(data_dir / "credentials.json", _metadata("data-id"))
    _write(config_dir / "credentials.json", _legacy("config-id"))
    isolated_auth.values[("gogcli", "client/default/client-secret")] = "data-secret"

    result = discovery._find_gogcli_client_credentials()

    assert result is not None
    assert result.client_id == "data-id"
    assert result.client_secret == "data-secret"


def test_identical_data_and_config_directories_are_not_double_read(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared"
    monkeypatch.setenv("GOG_DATA_DIR", str(shared))
    monkeypatch.setenv("GOG_CONFIG_DIR", str(shared))
    path = shared / "credentials-work.json"
    _write(path, _metadata("work-id"))
    isolated_auth.values[("gogcli", "client/work/client-secret")] = "work-secret"

    result = discovery._inspect_gogcli_client_credentials()

    assert result.status == "found"
    assert result.credentials is not None
    assert result.credentials.client_id == "work-id"
    assert result.credentials.client_secret == "work-secret"


def test_single_named_client_uses_normalized_key(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("GOG_DATA_DIR", str(data_dir))
    _write(data_dir / "credentials-work.json", _metadata("work-id"))
    isolated_auth.values[("gogcli", "client/work/client-secret")] = "work-secret"

    result = discovery._find_gogcli_client_credentials()

    assert result is not None
    assert result.client_id == "work-id"
    assert result.client_secret == "work-secret"
    assert ("gogcli", "client/work/client-secret") in isolated_auth.calls


def test_same_named_client_in_data_and_config_uses_data_copy(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("GOG_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GOG_CONFIG_DIR", str(config_dir))
    _write(data_dir / "credentials-work.json", _metadata("data-id"))
    _write(config_dir / "credentials-work.json", _metadata("config-id"))
    isolated_auth.values[("gogcli", "client/work/client-secret")] = "work-secret"

    result = discovery._inspect_gogcli_client_credentials()

    assert result.status == "found"
    assert result.credentials is not None
    assert result.credentials.client_id == "data-id"
    assert result.credentials.client_secret == "work-secret"
    assert result.client_path == data_dir / "credentials-work.json"
    assert result.ambiguous_paths == ()


def test_multiple_named_clients_without_default_are_ambiguous(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("GOG_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GOG_CONFIG_DIR", str(config_dir))
    _write(data_dir / "credentials-work.json", _metadata("work-id"))
    _write(config_dir / "credentials-personal.json", _metadata("personal-id"))
    _write(
        tmp_path / ".config" / "slidesmith" / "session.json",
        {
            "profiles": {
                "gogcli-default": {
                    "raw_token": "session-token",
                    "email": "agent@example.com",
                    "expires_at": time.time() + 86400,
                    "is_refreshable": True,
                }
            }
        },
    )

    result = discovery._inspect_gogcli_client_credentials()
    lines = auth_doctor_lines(
        find_gws_client_credentials=lambda: None,
        probe_bare_token=lambda _token: ("unreachable", None),
    )
    output = "\n".join(lines)

    assert result.status == "ambiguous"
    assert result.ambiguous_paths == tuple(
        sorted(
            [
                data_dir / "credentials-work.json",
                config_dir / "credentials-personal.json",
            ]
        )
    )
    assert discovery._find_gogcli_client_credentials() is None
    assert "credentials-work.json" in output
    assert "credentials-personal.json" in output
    assert "Disambiguate" in output
    assert "Session profile: gogcli-default" in output
    assert "File store: PRESENT" in output
    assert "token VALID" in output
    assert "Verdict: GOGCLI CLIENT AMBIGUOUS" in output
    assert "CREDENTIAL ABSENT" not in output


def test_gateway_uses_default_session_for_ambiguous_gogcli_metadata(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("GOG_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GOG_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("EXTRASUITE_SERVER_URL", "https://gateway.example")
    _write(data_dir / "credentials-work.json", _metadata("work-id"))
    _write(config_dir / "credentials-personal.json", _metadata("personal-id"))
    _write(
        tmp_path / ".config" / "slidesmith" / "session.json",
        {
            "profiles": {
                "default": {
                    "raw_token": "session-token",
                    "email": "agent@example.com",
                    "expires_at": time.time() + 86400,
                    "is_refreshable": True,
                }
            }
        },
    )

    result = discovery._inspect_gogcli_client_credentials()
    lines = auth_doctor_lines(
        find_gws_client_credentials=lambda: None,
        probe_bare_token=lambda _token: ("unreachable", None),
    )
    output = "\n".join(lines)

    assert result.status == "ambiguous"
    assert "Session profile: default" in output
    assert "File store: PRESENT" in output
    assert "token VALID" in output
    assert "SESSION TOKEN ABSENT" not in output
    assert "Verdict: GOGCLI CLIENT AMBIGUOUS" not in output


@pytest.mark.parametrize("stale", [False, True], ids=["valid", "stale"])
def test_cached_default_session_does_not_make_runtime_auth_ready(
    tmp_path: Path,
    isolated_auth: FakeKeyring,
    stale: bool,
) -> None:
    _write(
        tmp_path / ".config" / "slidesmith" / "session.json",
        {
            "profiles": {
                "default": {
                    "raw_token": "session-token",
                    "email": "agent@example.com",
                    "expires_at": time.time() + (-86400 if stale else 86400),
                    "is_refreshable": True,
                }
            }
        },
    )

    lines = auth_doctor_lines(
        find_gws_client_credentials=lambda: None,
        probe_bare_token=lambda _token: ("unreachable", None),
    )
    output = "\n".join(lines)

    assert "Session profile: default" in output
    assert "File store: PRESENT" in output
    assert f"token {'EXPIRED' if stale else 'VALID'}" in output
    assert "Verdict: CREDENTIAL ABSENT" in output
    assert "Verdict: READY" not in output
    assert "Verdict: USABLE BUT EXPIRING" not in output
