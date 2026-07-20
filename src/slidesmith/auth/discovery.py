"""Discovery and parsing for gws and gogcli OAuth clients."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from extraslide.json_utils import read_json


@dataclass
class OAuthClientCredentials:
    """Client ID + secret borrowed from a gws or gogcli installation."""

    client_id: str
    client_secret: str
    source: str
    location: str = ""


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


def _find_gogcli_client_credentials() -> OAuthClientCredentials | None:
    """Discover gogcli OAuth client credentials without any side effects."""
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    elif system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        base = Path(xdg) if xdg else Path.home() / ".config"

    path = base / "gogcli" / "credentials.json"
    try:
        data = read_json(path, missing_ok=False)
    except (OSError, ValueError):
        return None

    parsed = _parse_oauth_client_json(data)
    if parsed:
        return OAuthClientCredentials(
            client_id=parsed[0],
            client_secret=parsed[1],
            source="gogcli",
            location=str(path),
        )
    return None
