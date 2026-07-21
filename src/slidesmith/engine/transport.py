"""Transport layer for fetching presentation data.

Defines the Transport protocol and the production Google Slides implementation.
"""

from __future__ import annotations

import asyncio
import ssl
import time
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import certifi
import httpx

# API constants
API_BASE = "https://slides.googleapis.com/v1/presentations"
DEFAULT_TIMEOUT = 60
EXPIRY_BUFFER_SECONDS = 120
CredentialRefresh = Callable[[], Awaitable[Any]]


class TransportError(Exception):
    """Base exception for transport errors."""


class AuthenticationError(TransportError):
    """Raised when authentication fails (401/403)."""


class NotFoundError(TransportError):
    """Raised when presentation is not found (404)."""


class APIError(TransportError):
    """Raised when the API returns an error."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PresentationData:
    """Complete presentation data from the API.

    Attributes:
        presentation_id: The presentation identifier
        data: Full API response (presentation JSON)
    """

    presentation_id: str
    data: dict[str, Any]

    @property
    def revision_id(self) -> str | None:
        """The presentation's revisionId from the raw API response.

        Opaque write-guard token for writeControl.requiredRevisionId.
        None when the source data carries no revision (e.g. old fixtures).
        """
        revision = self.data.get("revisionId")
        return revision if isinstance(revision, str) and revision else None


class Transport(ABC):
    """Abstract base class for presentation data transport.

    Implementations must provide methods to fetch presentation data
    and send batch updates to a presentation source (Google API, local files, etc.).
    """

    @abstractmethod
    async def get_presentation(self, presentation_id: str) -> PresentationData:
        """Fetch complete presentation data.

        Args:
            presentation_id: The presentation identifier

        Returns:
            PresentationData with full presentation contents
        """
        ...

    @abstractmethod
    async def batch_update(
        self,
        presentation_id: str,
        requests: list[dict[str, Any]],
        required_revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Send batch update requests to the presentation.

        Args:
            presentation_id: The presentation identifier
            requests: List of Google Slides API request objects
            required_revision_id: If set, the write is guarded with
                writeControl.requiredRevisionId and fails (400) when the
                presentation has been revised since this revision was read.

        Returns:
            API response from batchUpdate
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close any open connections."""
        ...

    async def refresh_if_expiring(self) -> None:
        """Refresh a short-lived credential before a write when supported."""
        return None


class GoogleSlidesTransport(Transport):
    """Production transport that fetches data from Google Slides API.

    Handles authentication, SSL, and HTTP communication.
    """

    def __init__(
        self,
        access_token: str,
        timeout: int = DEFAULT_TIMEOUT,
        *,
        retry_attempts: int = 3,
        retry_backoff: float = 0.1,
        credential_refresh: CredentialRefresh | None = None,
        expires_at: float | None = None,
    ) -> None:
        """Initialize the transport.

        Args:
            access_token: OAuth2 access token with presentations scope
            timeout: Request timeout in seconds
        """
        self._retry_attempts = max(1, retry_attempts)
        self._retry_backoff = max(0.0, retry_backoff)
        if credential_refresh is None:
            credential_refresh = getattr(access_token, "refresh_callback", None)
        if expires_at is None:
            expires_at = getattr(access_token, "expires_at", None)
        self._credential_refresh = credential_refresh
        self._expires_at = expires_at
        self._refresh_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[bool] | None = None
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._client = httpx.AsyncClient(
            timeout=timeout,
            verify=ssl_context,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        self._thumbnail_client = httpx.AsyncClient(
            timeout=timeout,
            verify=ssl_context,
        )

    async def get_presentation(self, presentation_id: str) -> PresentationData:
        """Fetch presentation data from Google Slides API."""
        url = f"{API_BASE}/{presentation_id}"
        response = await self._request(url)

        return PresentationData(
            presentation_id=response.get("presentationId", presentation_id),
            data=response,
        )

    async def get_page_thumbnail(
        self,
        presentation_id: str,
        page_object_id: str,
        size: str = "LARGE",
    ) -> bytes:
        """Fetch a page thumbnail's PNG bytes from its temporary content URL."""
        url = f"{API_BASE}/{presentation_id}/pages/{page_object_id}/thumbnail"
        params = {
            "thumbnailProperties.thumbnailSize": size,
            "thumbnailProperties.mimeType": "PNG",
        }

        metadata_response = await self._get_with_retry(url, params=params)
        content_url = metadata_response.json().get("contentUrl")
        if not isinstance(content_url, str) or not content_url:
            raise TransportError("Thumbnail response did not include contentUrl")

        try:
            parsed_content_url = urllib.parse.urlparse(content_url)
            host = (parsed_content_url.hostname or "").lower()
        except ValueError as exc:
            raise TransportError("Thumbnail contentUrl is not a valid URL") from exc
        allowed_host = (
            host == "googleusercontent.com"
            or host.endswith(".googleusercontent.com")
            or host == "docs.google.com"
        )
        if parsed_content_url.scheme != "https" or not allowed_host:
            raise TransportError(
                "Refusing thumbnail contentUrl outside the allowed Google hosts "
                "(googleusercontent.com subdomains or docs.google.com)"
            )

        content_response = await self._get_with_retry(
            content_url, client=self._thumbnail_client
        )
        return content_response.content

    async def batch_update(
        self,
        presentation_id: str,
        requests: list[dict[str, Any]],
        required_revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Send batch update requests to Google Slides API."""
        url = f"{API_BASE}/{presentation_id}:batchUpdate"
        body: dict[str, Any] = {"requests": requests}
        if required_revision_id is not None:
            body["writeControl"] = {"requiredRevisionId": required_revision_id}

        refresh_attempted = False
        while True:
            observed_authorization = self._client.headers.get("Authorization")
            try:
                response = await self._client.post(url, json=body)
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401 and not refresh_attempted:
                    refresh_attempted = True
                    if await self._refresh_after_401(observed_authorization):
                        continue
                raise self._handle_http_error(e) from e
            except httpx.RequestError as e:
                raise TransportError(f"Network error: {e}") from e

    async def _request(self, url: str) -> dict[str, Any]:
        """Make an authenticated GET request."""
        response = await self._get_with_retry(url)
        result: dict[str, Any] = response.json()
        return result

    async def _get_with_retry(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> httpx.Response:
        """GET with bounded exponential backoff for throttling/server errors."""
        request_client = client or self._client
        refresh_attempted = False
        retry_attempt = 0
        while True:
            observed_authorization = (
                request_client.headers.get("Authorization")
                if request_client is self._client
                else None
            )
            try:
                response = await request_client.get(url, params=params)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if (
                    status == 401
                    and request_client is self._client
                    and not refresh_attempted
                ):
                    refresh_attempted = True
                    if await self._refresh_after_401(observed_authorization):
                        continue
                retryable = status == 429 or 500 <= status <= 599
                if not retryable or retry_attempt + 1 >= self._retry_attempts:
                    raise self._handle_http_error(exc) from exc
                await asyncio.sleep(self._retry_backoff * (2**retry_attempt))
                retry_attempt += 1
            except httpx.RequestError as exc:
                raise TransportError(f"Network error: {exc}") from exc

    async def refresh_if_expiring(self) -> None:
        """Refresh the configured credential when it is within the write buffer."""
        if self._expires_at is None:
            return
        if time.time() < self._expires_at - EXPIRY_BUFFER_SECONDS:
            return
        await self._refresh_access_token()

    async def _refresh_access_token(self) -> bool:
        """Run the one-shot refresh callback and install its new bearer token."""
        current_task = asyncio.current_task()
        if current_task is self._refresh_task:
            raise self._guided_authentication_error()
        async with self._refresh_lock:
            if current_task is self._refresh_task:
                raise self._guided_authentication_error()
            refresh_task = self._refresh_task
            if refresh_task is None or refresh_task.done():
                refresh_task = asyncio.create_task(self._refresh_access_token_unlocked())
                self._refresh_task = refresh_task
        return await asyncio.shield(refresh_task)

    async def _refresh_after_401(self, observed_authorization: str | None) -> bool:
        """Refresh once unless another request already replaced the bearer token."""
        if self._credential_refresh is None:
            return False
        current_task = asyncio.current_task()
        if current_task is self._refresh_task:
            raise self._guided_authentication_error()
        async with self._refresh_lock:
            if current_task is self._refresh_task:
                raise self._guided_authentication_error()
            if self._client.headers.get("Authorization") != observed_authorization:
                return True
            refresh_task = self._refresh_task
            if refresh_task is None or refresh_task.done():
                refresh_task = asyncio.create_task(self._refresh_access_token_unlocked())
                self._refresh_task = refresh_task
        return await asyncio.shield(refresh_task)

    async def _refresh_access_token_unlocked(self) -> bool:
        """Run the refresh callback outside the bookkeeping lock."""
        if self._credential_refresh is None:
            return False
        try:
            result = await self._credential_refresh()
        except Exception:
            return False
        if result is None:
            return False

        token: Any = result
        expires_at: float | None = None
        if isinstance(result, tuple):
            if not result:
                return False
            token = result[0]
            if len(result) > 1 and isinstance(result[1], (int, float)):
                expires_at = float(result[1])
        else:
            candidate_token = getattr(result, "token", None)
            if candidate_token is not None:
                token = candidate_token
                candidate_expiry = getattr(result, "expires_at", None)
                if isinstance(candidate_expiry, (int, float)):
                    expires_at = float(candidate_expiry)
        if not isinstance(token, str) or not token:
            return False

        self._expires_at = expires_at
        self._client.headers["Authorization"] = f"Bearer {token}"
        return True

    def _handle_http_error(self, e: httpx.HTTPStatusError) -> TransportError:
        """Convert HTTP errors to appropriate transport exceptions."""
        status = e.response.status_code
        if status == 401:
            return self._guided_authentication_error()
        if status == 403:
            return AuthenticationError(
                "Access denied. Check your scopes and permissions."
            )
        if status == 404:
            return NotFoundError(
                "Presentation not found. Check the ID and sharing permissions."
            )
        body = e.response.text
        return APIError(f"API error ({status}): {body}", status_code=status)

    @staticmethod
    def _guided_authentication_error() -> AuthenticationError:
        return AuthenticationError(
            "Invalid or expired access token. Please re-export a fresh token and "
            "retry. For --per-slide pushes, use --resume to pick up where "
            "it left off."
        )

    async def close(self) -> None:
        """Close the authenticated API and bare thumbnail HTTP clients."""
        await self._client.aclose()
        await self._thumbnail_client.aclose()
