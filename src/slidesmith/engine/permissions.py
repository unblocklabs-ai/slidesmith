"""Small authenticated client for Google Drive file permissions."""

from __future__ import annotations

import urllib.parse
from typing import Any

import httpx

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"


class DrivePermissionError(RuntimeError):
    """A Drive permission request failed."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GoogleDrivePermissionsClient:
    """Create permissions on Drive files using an existing bearer token."""

    def __init__(
        self,
        access_token: str | None = None,
        *,
        timeout: float = 60,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )

    async def create_permission(
        self,
        file_id: str,
        *,
        permission_type: str,
        role: str,
        email_address: str | None = None,
        send_notification_email: bool | None = None,
    ) -> dict[str, Any]:
        """Create one Drive permission and return the API response."""
        permission: dict[str, Any] = {
            "type": permission_type,
            "role": role,
        }
        if email_address is not None:
            permission["emailAddress"] = email_address

        params: dict[str, str] = {"fields": "id"}
        if send_notification_email is not None:
            params["sendNotificationEmail"] = str(send_notification_email).lower()

        url = (
            f"{DRIVE_API_BASE}/files/"
            f"{urllib.parse.quote(file_id)}/permissions"
        )
        try:
            response = await self._client.post(url, params=params, json=permission)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise DrivePermissionError(
                f"Google Drive permission request failed ({status}): "
                f"{exc.response.text}",
                status_code=status,
            ) from exc
        except httpx.RequestError as exc:
            raise DrivePermissionError(
                f"Google Drive permission network error: {exc}"
            ) from exc

        try:
            result = response.json()
        except ValueError as exc:
            raise DrivePermissionError(
                "Google Drive permission response contained invalid JSON"
            ) from exc
        if not isinstance(result, dict):
            raise DrivePermissionError(
                "Google Drive permission response was not a JSON object"
            )
        return result

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
