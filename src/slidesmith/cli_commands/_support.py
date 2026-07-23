"""Shared helpers for slidesmith CLI commands."""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from slidesmith.auth.errors import AuthError
from slidesmith.credentials import GOG_BARE_TOKEN_REMEDIATION
from slidesmith.engine.json_utils import read_json
from slidesmith.engine.diff_model import PushWarning, WarningSeverity

_BARE_TOKEN_EXPIRY_WARNING_SECONDS = 120


def _require_workspace(folder: str | Path) -> Path:
    """Validate a folder argument and explain how to recover from a bad path."""
    path = Path(folder)
    missing = [
        relative
        for relative in (Path("presentation.json"), Path(".pristine") / "presentation.zip")
        if not (path / relative).is_file()
    ]
    if not missing:
        return path
    # Local-only editing helpers are also useful against deliberately small
    # SML projections (including a standalone component library); keep those
    # documented shapes valid while rejecting arbitrary directories/files.
    if (
        not (path / "presentation.json").exists()
        and (
            (
                (path / "slides").is_dir()
                and any((path / "slides").glob("*/content.sml"))
            )
            or (path / "components.sml").is_file()
        )
    ):
        return path
    missing_text = ", ".join(str(relative) for relative in missing)
    raise ValueError(
        f"Not a Slidesmith workspace: {path} (missing {missing_text}). "
        "The folder argument must be the workspace directory created by "
        "slidesmith pull or slidesmith create."
    )


def _presentation_id(url_or_id: str) -> str:
    m = re.search(r"/presentation/d/([a-zA-Z0-9_-]+)", url_or_id)
    presentation_id = m.group(1) if m else url_or_id
    if re.fullmatch(r"[A-Za-z0-9_-]+", presentation_id) is None:
        raise ValueError(
            "Invalid presentation URL or ID. Provide a Google Slides URL or an ID "
            "containing only letters, numbers, underscores, and hyphens."
        )
    return presentation_id


class _AuthToken(str):
    """String-compatible token carrying the invocation's refresh metadata."""

    def __new__(
        cls,
        token: str,
        *,
        expires_at: float | None,
        refresh_callback: Callable[[], Awaitable[Any]] | None,
        auth_mode: str | None = None,
    ) -> _AuthToken:
        value = super().__new__(cls, token)
        value.expires_at = expires_at
        value.refresh_callback = refresh_callback
        value.auth_mode = auth_mode
        return value


def _token(command_type: str, target: str) -> str:
    from slidesmith.credentials import CredentialsManager

    manager = CredentialsManager()
    command = {"type": command_type, "file_url": target, "file_name": ""}
    reason = f"slidesmith {command_type}"
    cred = manager.get_credential(
        command=command,
        reason=reason,
    )
    auth_mode = manager.auth_mode
    if auth_mode == "bare_token":
        probe_status, probed_expires_at = manager.probe_bare_token(cred.token)
        if probe_status == "invalid":
            raise AuthError(GOG_BARE_TOKEN_REMEDIATION)
        if probe_status == "valid" and probed_expires_at is not None:
            cred.expires_at = probed_expires_at
            remaining = max(0, int(probed_expires_at - time.time()))
            if remaining <= _BARE_TOKEN_EXPIRY_WARNING_SECONDS:
                print(
                    "warning: GOG_ACCESS_TOKEN expires in about "
                    f"{remaining} seconds and cannot be refreshed by Slidesmith; "
                    "re-export it after a throwaway `gog` API request before a "
                    "long push",
                    file=sys.stderr,
                )

    async def refresh() -> tuple[str, float | None] | None:
        refreshed = manager.refresh_credential(command=command, reason=reason)
        if refreshed is None:
            return None
        return refreshed.token, refreshed.expires_at

    return _AuthToken(
        cred.token,
        expires_at=cred.expires_at,
        refresh_callback=refresh,
        auth_mode=auth_mode,
    )


def _transport_options(token: object) -> dict[str, Any]:
    """Extract optional refresh metadata without changing test token seams."""
    callback = getattr(token, "refresh_callback", None)
    expires_at = getattr(token, "expires_at", None)
    auth_mode = getattr(token, "auth_mode", None)
    if callback is None and expires_at is None and auth_mode is None:
        return {}
    return {
        "credential_refresh": callback,
        "expires_at": expires_at,
        "auth_mode": auth_mode,
    }


def _warn_if_stale(folder: str | Path, *, now: datetime | None = None) -> None:
    """Warn when a workspace's pull timestamp is more than 24 hours old."""
    metadata_path = Path(folder) / "presentation.json"
    try:
        metadata = read_json(metadata_path, missing_ok=True)
        pulled_at_raw = metadata.get("pulledAt")
        if not isinstance(pulled_at_raw, str):
            return
        pulled_at = datetime.fromisoformat(pulled_at_raw.replace("Z", "+00:00"))
        if pulled_at.tzinfo is None:
            pulled_at = pulled_at.replace(tzinfo=timezone.utc)
    except (OSError, ValueError, AttributeError):
        return

    current = now or datetime.now(timezone.utc)
    if current.astimezone(timezone.utc) - pulled_at.astimezone(
        timezone.utc
    ) > timedelta(hours=24):
        print(
            f"warning: workspace pulled {pulled_at_raw}; deck may have changed — "
            "re-pull recommended",
            file=sys.stderr,
        )


def print_push_warnings(warnings: Iterable[PushWarning]) -> None:
    """Render push notices after actionable warnings with a mixed summary."""
    warnings = list(warnings)
    warnings_by_severity = {
        severity: [warning for warning in warnings if warning.severity == severity]
        for severity in (WarningSeverity.WARNING, WarningSeverity.NOTICE)
    }
    for severity in (WarningSeverity.WARNING, WarningSeverity.NOTICE):
        for warning in warnings_by_severity[severity]:
            print(f"{severity.value}: {warning.message}", file=sys.stderr)
    warning_count = len(warnings_by_severity[WarningSeverity.WARNING])
    notice_count = len(warnings_by_severity[WarningSeverity.NOTICE])
    if warning_count and notice_count:
        print(
            f"push warning summary: {warning_count} warning(s), "
            f"{notice_count} notice(s)",
            file=sys.stderr,
        )


def _request_id_legend(
    requests: list[dict[str, Any]], id_mapping: dict[str, str]
) -> str:
    """Describe request object IDs without making stdout cease to be JSON."""
    reverse_mapping = {google_id: clean_id for clean_id, google_id in id_mapping.items()}
    labels: dict[str, str] = {}
    create_operations = {"createShape", "createLine", "createImage"}
    for request in requests:
        for operation, body in request.items():
            if not isinstance(body, dict):
                continue
            object_id = body.get("objectId")
            if not isinstance(object_id, str) or object_id in labels:
                continue
            if object_id in reverse_mapping:
                labels[object_id] = reverse_mapping[object_id]
            elif object_id.startswith("new_"):
                labels[object_id] = f"{object_id[4:]}(new)"
            elif operation in create_operations:
                labels[object_id] = f"{object_id}(new)"
    return ", ".join(
        f"{object_id} = {clean_id}" for object_id, clean_id in labels.items()
    )
