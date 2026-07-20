"""Secret-safe authentication diagnostics."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from slidesmith.engine.json_utils import read_json
from slidesmith.auth.discovery import (
    OAuthClientCredentials,
    _find_gogcli_client_credentials,
    _find_gws_client_credentials,
)
from slidesmith.auth.stores import (
    FileSessionStore,
    KeyringSessionStore,
    SessionToken,
    _DEFAULT_PROFILE,
    _KEYRING_SERVICE,
)

ClientDiscovery = Callable[[], OAuthClientCredentials | None]


def _inspect_session_payload(payload: Any) -> tuple[str, SessionToken | None]:
    if payload is None:
        return "absent", None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return "invalid", None
    if not isinstance(payload, dict):
        return "invalid", None
    try:
        token = SessionToken.from_dict(payload)
    except (KeyError, TypeError, ValueError):
        return "invalid", None
    return ("valid" if token.is_valid() else "expired"), token


def _format_expiry(token: SessionToken | None) -> str:
    if token is None:
        return ""
    return datetime.fromtimestamp(token.expires_at).astimezone().isoformat()


def auth_doctor_lines(
    *,
    find_gws_client_credentials: ClientDiscovery = _find_gws_client_credentials,
    find_gogcli_client_credentials: ClientDiscovery = _find_gogcli_client_credentials,
) -> list[str]:
    """Return a layered, secret-safe authentication diagnosis."""
    oauth_creds = (
        find_gws_client_credentials() or find_gogcli_client_credentials()
    )
    server_url = os.environ.get("EXTRASUITE_SERVER_URL", "").strip()
    service_account = os.environ.get("SERVICE_ACCOUNT_PATH", "").strip()
    bare_token = bool(
        os.environ.get("GOOGLE_WORKSPACE_CLI_TOKEN")
        or os.environ.get("GOG_ACCESS_TOKEN")
    )

    if oauth_creds is not None:
        credential_line = (
            f"OAuth client credentials: FOUND ({oauth_creds.source}: "
            f"{oauth_creds.location})"
        )
        profile_name = f"{oauth_creds.source}-default"
    else:
        credential_line = "OAuth client credentials: ABSENT"
        profile_name = _DEFAULT_PROFILE

    gateway_source = "environment variable EXTRASUITE_SERVER_URL"
    if not server_url:
        gateway_path = Path.home() / ".config" / "extrasuite" / "gateway.json"
        try:
            gateway_data = read_json(gateway_path, missing_ok=True)
            candidate = gateway_data.get("EXTRASUITE_SERVER_URL", "")
            if isinstance(candidate, str):
                server_url = candidate.strip()
                gateway_source = str(gateway_path)
        except (OSError, ValueError, AttributeError):
            pass

    lines = [credential_line]
    if server_url:
        lines.append(f"ExtraSuite gateway: FOUND ({gateway_source})")
    if service_account:
        sa_path = Path(service_account).expanduser()
        sa_state = "FOUND" if sa_path.is_file() else "MISSING"
        lines.append(f"Service account: {sa_state} ({sa_path})")
    if bare_token:
        lines.append("Pre-obtained access token: FOUND (environment variable)")
    lines.append(f"Session profile: {profile_name}")

    keyring_error: Exception | None = None
    keyring_status = "absent"
    keyring_token: SessionToken | None = None
    try:
        raw = KeyringSessionStore._backend().get_password(
            _KEYRING_SERVICE, profile_name
        )
        keyring_status, keyring_token = _inspect_session_payload(raw)
        detail = keyring_status.upper()
        if keyring_token is not None:
            detail += f"; expires {_format_expiry(keyring_token)}"
        lines.append(f"Keyring: READABLE; token {detail}")
    except Exception as exc:
        keyring_error = exc
        lines.append(f"Keyring: DENIED OR BROKEN; error: {exc!r}")

    file_store = FileSessionStore()
    file_status = "absent"
    file_token: SessionToken | None = None
    if not file_store.path.exists():
        lines.append(f"File store: ABSENT ({file_store.path})")
    else:
        try:
            data = read_json(file_store.path, missing_ok=False)
        except (OSError, ValueError) as exc:
            file_status = "invalid"
            lines.append(f"File store: INVALID ({file_store.path}); error: {exc!r}")
        else:
            payload: Any = None
            recognized_format = False
            if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
                payload = data["profiles"].get(profile_name)
                recognized_format = True
            elif isinstance(data, dict) and {
                "raw_token",
                "email",
                "expires_at",
            }.issubset(data):
                payload = data
                recognized_format = True
            if not recognized_format:
                file_status = "invalid"
            else:
                file_status, file_token = _inspect_session_payload(payload)
            detail = file_status.upper()
            if file_token is not None:
                detail += f"; expires {_format_expiry(file_token)}"
            lines.append(f"File store: PRESENT ({file_store.path}); token {detail}")

    service_account_valid = bool(service_account and Path(service_account).is_file())
    credentials_found = bool(
        oauth_creds is not None or server_url or service_account_valid or bare_token
    )
    immediate_auth = service_account_valid or bare_token
    token_statuses = {keyring_status, file_status}
    if not credentials_found:
        verdict = "CREDENTIAL ABSENT"
        next_command = "gws auth setup"
    elif "valid" in token_statuses or immediate_auth:
        verdict = "READY"
        next_command = "slidesmith pull <presentation-url-or-id>"
    elif "expired" in token_statuses:
        verdict = "TOKEN EXPIRED"
        next_command = "slidesmith auth login"
    elif keyring_error is not None:
        verdict = "KEYRING DENIED OR BROKEN"
        next_command = "slidesmith auth login"
    else:
        verdict = "SESSION TOKEN ABSENT"
        next_command = "slidesmith auth login"

    lines.extend([f"Verdict: {verdict}", f"Next command: {next_command}"])
    return lines
