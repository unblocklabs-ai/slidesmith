"""Secret-safe authentication diagnostics."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from slidesmith.engine.json_utils import read_json
from slidesmith.auth.browser_flow import (
    GOG_BARE_TOKEN_REMEDIATION,
    BareTokenProbeResult,
    probe_bare_token as _probe_bare_token,
)
from slidesmith.auth.discovery import (
    GogcliDiscoveryResult,
    OAuthClientCredentials,
    _find_gogcli_client_credentials,
    _find_gws_client_credentials,
    _inspect_gogcli_client_credentials,
)
from slidesmith.auth.stores import (
    FileSessionStore,
    KeyringSessionStore,
    SessionToken,
    _DEFAULT_PROFILE,
    _KEYRING_SERVICE,
)

ClientDiscovery = Callable[[], OAuthClientCredentials | None]
GogcliDiagnostic = Callable[[], GogcliDiscoveryResult]


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
    if not token.is_valid():
        return "expired", token
    return ("access-only" if not token.is_refreshable else "valid"), token


def _format_expiry(token: SessionToken | None) -> str:
    if token is None:
        return ""
    return datetime.fromtimestamp(token.expires_at).astimezone().isoformat()


def auth_doctor_lines(
    *,
    find_gws_client_credentials: ClientDiscovery = _find_gws_client_credentials,
    find_gogcli_client_credentials: ClientDiscovery = _find_gogcli_client_credentials,
    inspect_gogcli_client_credentials: GogcliDiagnostic = (
        _inspect_gogcli_client_credentials
    ),
    probe_bare_token: Callable[[str], BareTokenProbeResult] = _probe_bare_token,
) -> list[str]:
    """Return a layered, secret-safe authentication diagnosis."""
    oauth_creds = find_gws_client_credentials()
    gogcli_result: GogcliDiscoveryResult | None = None
    if oauth_creds is None:
        if (
            find_gogcli_client_credentials is _find_gogcli_client_credentials
            or inspect_gogcli_client_credentials is not _inspect_gogcli_client_credentials
        ):
            gogcli_result = inspect_gogcli_client_credentials()
            oauth_creds = gogcli_result.credentials
        else:
            oauth_creds = find_gogcli_client_credentials()
    server_url = os.environ.get("EXTRASUITE_SERVER_URL", "").strip()
    service_account = os.environ.get("SERVICE_ACCOUNT_PATH", "").strip()
    bare_token = os.environ.get("GOOGLE_WORKSPACE_CLI_TOKEN") or os.environ.get(
        "GOG_ACCESS_TOKEN"
    )
    bare_probe_status: str = "unreachable"
    bare_expires_at: float | None = None
    if bare_token:
        try:
            bare_probe_status, bare_expires_at = probe_bare_token(bare_token)
        except Exception:
            # Doctor is diagnostic only; a tokeninfo outage must not become an
            # authentication failure or expose the probe's implementation error.
            bare_probe_status = "unreachable"
            bare_expires_at = None

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

    if oauth_creds is not None:
        credential_line = (
            f"OAuth client credentials: FOUND ({oauth_creds.source}: "
            f"{oauth_creds.location})"
        )
        profile_name = (
            _DEFAULT_PROFILE if server_url else f"{oauth_creds.source}-default"
        )
    elif gogcli_result is not None and gogcli_result.status == "keyring-unreadable":
        assert gogcli_result.client_path is not None
        credential_line = (
            "OAuth client credentials: INCOMPLETE (gogcli metadata: "
            f"{gogcli_result.client_path})"
        )
        profile_name = _DEFAULT_PROFILE if server_url else "gogcli-default"
    elif gogcli_result is not None and gogcli_result.status == "ambiguous":
        credential_line = "OAuth client credentials: AMBIGUOUS (gogcli named clients)"
        profile_name = _DEFAULT_PROFILE if server_url else "gogcli-default"
    else:
        credential_line = "OAuth client credentials: ABSENT"
        profile_name = _DEFAULT_PROFILE

    lines = [credential_line]
    if gogcli_result is not None and gogcli_result.status == "keyring-unreadable":
        assert gogcli_result.client_path is not None
        lines.append(
            f"gogcli client id found at {gogcli_result.client_path}, but the client "
            "secret lives in the OS keyring and could not be read "
            f"(service {gogcli_result.keyring_service}, "
            f"key {gogcli_result.keyring_key})."
        )
        lines.append(
            "On macOS, the first read by a new process can trigger a Keychain "
            "authorization prompt; choose 'Always Allow'."
        )
        lines.append(
            "If gog uses a custom keyring service, set "
            "GOG_KEYRING_SERVICE_NAME to the same value."
        )
        lines.append(
            "Fallbacks: export GOG_ACCESS_TOKEN, or re-import with "
            "`gog auth credentials <file> --insecure` (less secure)."
        )
    elif gogcli_result is not None and gogcli_result.status == "ambiguous":
        lines.append("gogcli found multiple named client files and no default client:")
        lines.extend(f"  {path}" for path in gogcli_result.ambiguous_paths)
        lines.append(
            "Disambiguate by keeping one named client file or creating the intended "
            "default client as credentials.json, then run auth doctor again."
        )
    if server_url:
        lines.append(f"ExtraSuite gateway: FOUND ({gateway_source})")
    if service_account:
        sa_path = Path(service_account).expanduser()
        sa_state = "FOUND" if sa_path.is_file() else "MISSING"
        lines.append(f"Service account: {sa_state} ({sa_path})")
    if bare_token:
        if bare_probe_status == "valid":
            if bare_expires_at is None:
                expiry_detail = "expiry unavailable"
            else:
                remaining = max(0, int(bare_expires_at - time.time()))
                expiry_detail = f"expires in approximately {remaining} seconds"
            lines.append(
                "Pre-obtained access token: FOUND (environment variable); VALID; "
                f"{expiry_detail}"
            )
        elif bare_probe_status == "invalid":
            lines.append(
                "Pre-obtained access token: FOUND (environment variable); "
                f"EXPIRED/INVALID. {GOG_BARE_TOKEN_REMEDIATION}"
            )
        else:
            lines.append(
                "Pre-obtained access token: FOUND (environment variable); usable now, "
                "expiry unknown (~1h typical); long pushes may fail mid-run"
            )
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
                "email",
                "expires_at",
            }.issubset(data) and (
                "raw_token" in data or "access_token" in data
            ):
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
    immediate_auth = service_account_valid or bool(bare_token)
    token_statuses = {keyring_status, file_status}
    other_auth_usable = bool(
        server_url
        or service_account_valid
        or (bare_token and bare_probe_status != "invalid")
    )
    credentials_found = bool(
        oauth_creds is not None
        or server_url
        or service_account_valid
        or bool(bare_token)
    )
    if (
        gogcli_result is not None
        and gogcli_result.status == "keyring-unreadable"
        and not other_auth_usable
    ):
        verdict = "GOGCLI CLIENT SECRET UNREADABLE"
        next_command = "export GOG_ACCESS_TOKEN or re-import gog credentials --insecure"
    elif (
        gogcli_result is not None
        and gogcli_result.status == "ambiguous"
        and not other_auth_usable
    ):
        verdict = "GOGCLI CLIENT AMBIGUOUS"
        next_command = "disambiguate the gogcli named client files, then rerun auth doctor"
    elif not credentials_found:
        verdict = "CREDENTIAL ABSENT"
        next_command = "gws auth setup"
    elif bare_probe_status == "invalid" and bare_token:
        verdict = "TOKEN EXPIRED OR INVALID"
        next_command = (
            "run a throwaway `gog` API request, re-export GOG_ACCESS_TOKEN, then retry"
        )
    elif immediate_auth and bare_token:
        if bare_probe_status == "valid" and bare_expires_at is not None:
            remaining = max(0, int(bare_expires_at - time.time()))
            verdict = f"READY (valid; expires in approximately {remaining} seconds)"
        elif bare_probe_status == "valid":
            verdict = "READY (valid; expiry unavailable)"
        else:
            verdict = (
                "READY (usable now, expiry unknown (~1h typical); long pushes may fail "
                "mid-run)"
            )
        next_command = "slidesmith pull <presentation-url-or-id>"
    elif "access-only" in token_statuses:
        verdict = "USABLE BUT EXPIRING"
        lines.append(
            "OAuth session is usable-but-expiring because Google withheld a refresh "
            "token. Revoke access at https://myaccount.google.com/permissions and "
            "try again, or use your own OAuth client."
        )
        next_command = "slidesmith pull <presentation-url-or-id>"
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
