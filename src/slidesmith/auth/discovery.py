"""Discovery and parsing for gws and gogcli OAuth clients."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from slidesmith.auth.stores import KeyringSessionStore
from slidesmith.engine.json_utils import read_json


@dataclass
class OAuthClientCredentials:
    """Client ID + secret borrowed from a gws or gogcli installation."""

    client_id: str
    client_secret: str
    source: str
    location: str = ""


GogcliDiscoveryStatus = Literal[
    "missing", "found", "keyring-unreadable", "ambiguous"
]


@dataclass(frozen=True)
class GogcliDiscoveryResult:
    """Detailed gogcli discovery state for auth diagnostics."""

    status: GogcliDiscoveryStatus
    credentials: OAuthClientCredentials | None = None
    client_path: Path | None = None
    keyring_service: str = ""
    keyring_key: str = ""
    ambiguous_paths: tuple[Path, ...] = ()


def _parse_oauth_client_json(data: dict[str, Any]) -> tuple[str, str] | None:
    """Extract (client_id, client_secret) from a Google OAuth client JSON dict."""
    if data.get("type") == "service_account":
        return None
    for key in ("installed", "web"):
        if key in data:
            inner = data[key]
            cid = inner.get("client_id", "")
            csec = inner.get("client_secret", "")
            if cid and csec:
                return cid, csec
    cid = data.get("client_id", "")
    csec = data.get("client_secret", "")
    if cid and csec:
        return str(cid), str(csec)
    return None


def _find_gws_client_credentials() -> OAuthClientCredentials | None:
    """Discover gws OAuth client credentials without any side effects."""
    cid = os.environ.get("GOOGLE_WORKSPACE_CLI_CLIENT_ID", "")
    csec = os.environ.get("GOOGLE_WORKSPACE_CLI_CLIENT_SECRET", "")
    if cid and csec:
        return OAuthClientCredentials(
            client_id=cid,
            client_secret=csec,
            source="gws",
            location=(
                "environment variables GOOGLE_WORKSPACE_CLI_CLIENT_ID + "
                "GOOGLE_WORKSPACE_CLI_CLIENT_SECRET"
            ),
        )

    creds_file = os.environ.get("GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE", "")
    candidates = [Path(creds_file)] if creds_file else []
    candidates.append(Path.home() / ".config" / "gws" / "client_secret.json")

    for path in candidates:
        try:
            data = read_json(path, missing_ok=False)
        except (OSError, ValueError):
            continue
        parsed = _parse_oauth_client_json(data)
        if parsed:
            return OAuthClientCredentials(
                client_id=parsed[0],
                client_secret=parsed[1],
                source="gws",
                location=str(path),
            )

    return None


def _absolute_env_path(name: str) -> Path | None:
    value = os.environ.get(name, "")
    path = Path(value) if value else None
    return path if path is not None and path.is_absolute() else None


def _gogcli_directory(kind: Literal["data", "config"]) -> Path:
    """Resolve one gogcli directory using gog's platform-independent order."""
    override = _absolute_env_path(f"GOG_{kind.upper()}_DIR")
    if override is not None:
        return override

    gog_home = _absolute_env_path("GOG_HOME")
    if gog_home is not None:
        return gog_home / kind

    xdg_name = "XDG_DATA_HOME" if kind == "data" else "XDG_CONFIG_HOME"
    xdg_home = _absolute_env_path(xdg_name)
    if xdg_home is not None:
        return xdg_home / "gogcli"

    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "gogcli"
    elif system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "gogcli"
    elif kind == "data":
        return Path.home() / ".local" / "share" / "gogcli"
    else:
        return Path.home() / ".config" / "gogcli"


def _gogcli_candidate_directories() -> list[Path]:
    """Return gogcli data then config directories without duplicates."""
    candidates: list[Path] = []
    seen: set[Path] = set()
    for path in (_gogcli_directory("data"), _gogcli_directory("config")):
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            candidates.append(path)
    return candidates


def _read_gogcli_file(path: Path) -> tuple[bool, dict[str, Any] | None]:
    """Read a candidate file and distinguish absent from malformed content."""
    if not path.is_file():
        return False, None
    try:
        data = read_json(path, missing_ok=False)
    except (OSError, ValueError):
        return True, None
    return True, data if isinstance(data, dict) else None


def _parse_oauth_client_id(data: dict[str, Any]) -> str | None:
    """Extract a client ID even when gog stores the secret outside the file."""
    if data.get("type") == "service_account":
        return None
    for key in ("installed", "web"):
        inner = data.get(key)
        if isinstance(inner, dict) and inner.get("client_id"):
            return str(inner["client_id"])
    client_id = data.get("client_id")
    return str(client_id) if client_id else None


def _gogcli_keyring_service() -> str:
    return os.environ.get("GOG_KEYRING_SERVICE_NAME", "").strip() or "gogcli"


def _read_gogcli_keyring_secret(service: str, key: str) -> str | None:
    """Read a gogcli secret without allowing keyring failures to escape."""
    try:
        raw = KeyringSessionStore._backend().get_password(service, key)
    except Exception:
        return None
    if not isinstance(raw, str):
        return None
    secret = raw.strip()
    return secret or None


def _gogcli_file_result(
    path: Path, data: dict[str, Any] | None, client_name: str
) -> GogcliDiscoveryResult | None:
    if data is None:
        return None

    parsed = _parse_oauth_client_json(data)
    if parsed:
        return GogcliDiscoveryResult(
            status="found",
            credentials=OAuthClientCredentials(
                client_id=parsed[0],
                client_secret=parsed[1],
                source="gogcli",
                location=str(path),
            ),
            client_path=path,
        )

    client_id = _parse_oauth_client_id(data)
    if not client_id:
        return None

    normalized_name = client_name.strip().lower()
    key = f"client/{normalized_name}/client-secret"
    service = _gogcli_keyring_service()
    secret = _read_gogcli_keyring_secret(service, key)
    if secret is None:
        return GogcliDiscoveryResult(
            status="keyring-unreadable",
            client_path=path,
            keyring_service=service,
            keyring_key=key,
        )
    return GogcliDiscoveryResult(
        status="found",
        credentials=OAuthClientCredentials(
            client_id=client_id,
            client_secret=secret,
            source="gogcli",
            location=f"{path} + client secret from OS keyring (service {service})",
        ),
        client_path=path,
        keyring_service=service,
        keyring_key=key,
    )


def _inspect_gogcli_client_credentials() -> GogcliDiscoveryResult:
    """Inspect gogcli OAuth client storage without any side effects."""
    candidate_dirs = _gogcli_candidate_directories()
    default_exists = False
    for directory in candidate_dirs:
        path = directory / "credentials.json"
        exists, data = _read_gogcli_file(path)
        if not exists:
            continue
        default_exists = True
        result = _gogcli_file_result(path, data, "default")
        if result is not None:
            return result

    if default_exists:
        return GogcliDiscoveryResult(status="missing")

    named_paths_by_name: dict[str, Path] = {}
    for directory in candidate_dirs:
        try:
            paths = sorted(
                path
                for path in directory.glob("credentials-*.json")
                if path.is_file() and path.stem.removeprefix("credentials-")
            )
        except OSError:
            continue
        for path in paths:
            name = path.stem.removeprefix("credentials-")
            normalized_name = name.strip().lower()
            if normalized_name not in named_paths_by_name:
                named_paths_by_name[normalized_name] = path

    named_paths = list(named_paths_by_name.values())

    if len(named_paths) > 1:
        return GogcliDiscoveryResult(
            status="ambiguous", ambiguous_paths=tuple(sorted(named_paths))
        )
    if named_paths:
        path = named_paths[0]
        name = path.stem.removeprefix("credentials-")
        _exists, data = _read_gogcli_file(path)
        result = _gogcli_file_result(path, data, name)
        if result is not None:
            return result
    return GogcliDiscoveryResult(status="missing")


def _find_gogcli_client_credentials() -> OAuthClientCredentials | None:
    """Discover gogcli OAuth client credentials without any side effects."""
    return _inspect_gogcli_client_credentials().credentials
