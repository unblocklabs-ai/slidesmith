"""Shared helpers for slidesmith CLI commands."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from slidesmith.engine.json_utils import read_json
from slidesmith.engine.diff_model import PushWarning, WarningSeverity


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
    ) -> _AuthToken:
        value = super().__new__(cls, token)
        value.expires_at = expires_at
        value.refresh_callback = refresh_callback
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

    async def refresh() -> tuple[str, float | None] | None:
        refreshed = manager.refresh_credential(command=command, reason=reason)
        if refreshed is None:
            return None
        return refreshed.token, refreshed.expires_at

    return _AuthToken(
        cred.token,
        expires_at=cred.expires_at,
        refresh_callback=refresh,
    )


def _transport_options(token: object) -> dict[str, Any]:
    """Extract optional refresh metadata without changing test token seams."""
    callback = getattr(token, "refresh_callback", None)
    expires_at = getattr(token, "expires_at", None)
    if callback is None and expires_at is None:
        return {}
    return {"credential_refresh": callback, "expires_at": expires_at}


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
