"""Local image resolution, Drive upload, and workspace asset caching."""

from __future__ import annotations

import hashlib
import json
import secrets
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal, Protocol

import httpx
from PIL import Image, UnidentifiedImageError

from slidesmith.engine.image_fetch import validate_public_image_url

ASSET_CACHE_FILE = ".assets.json"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
SUPPORTED_IMAGE_MIME_TYPES = {"image/gif", "image/jpeg", "image/png"}


@dataclass(frozen=True)
class UploadedAsset:
    """A Drive file that Slides can fetch without OAuth credentials."""

    file_id: str
    url: str


class AssetUploader(Protocol):
    """Network seam used by push-time local asset resolution."""

    async def upload(self, path: Path, *, mime_type: str) -> UploadedAsset:
        """Upload one image and return its public Drive identity and URL."""

    async def close(self) -> None:
        """Close uploader-owned network resources."""


class AssetUploadError(RuntimeError):
    """A local image could not be made available through Google Drive."""


def image_source_kind(source: str) -> Literal["local", "remote"]:
    """Classify and syntactically validate an authored image source."""
    if not source:
        raise ValueError("image source must not be empty")

    try:
        parsed = urllib.parse.urlsplit(source)
    except ValueError as exc:
        raise ValueError(f"invalid image source: {exc}") from exc

    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"}:
        validate_public_image_url(source, resolve_host=False)
        return "remote"
    if scheme == "file":
        if parsed.hostname not in {None, "", "localhost"}:
            raise ValueError("file URLs must refer to this machine")
        if parsed.query or parsed.fragment:
            raise ValueError("file URLs cannot contain a query or fragment")
        path = Path(urllib.parse.unquote(parsed.path))
        if not path.is_absolute():
            raise ValueError("file URLs must contain an absolute path")
        return "local"
    if not scheme:
        return "local"
    raise ValueError(
        "expected an http(s) URL, local path, or absolute file:// URL"
    )


def resolve_local_image_path(workspace: Path, source: str) -> Path:
    """Resolve a local source relative to the presentation workspace root."""
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme.lower() == "file":
        path = Path(urllib.parse.unquote(parsed.path))
    else:
        path = Path(source).expanduser()
        if not path.is_absolute():
            path = workspace / path
    return path.resolve()


def inspect_local_image(path: Path, *, source: str | None = None) -> tuple[int, int, str]:
    """Read dimensions and MIME type locally without any network request."""
    label = source or str(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Local image {label!r} was not found at {path}"
        )
    if not path.is_file():
        raise ValueError(f"Local image {label!r} is not a regular file: {path}")

    try:
        with Image.open(path) as image:
            width, height = image.size
            mime_type = image.get_format_mimetype()
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError(f"Could not read local image {label!r} at {path}: {exc}") from exc

    if width <= 0 or height <= 0:
        raise ValueError(f"Local image {label!r} has invalid pixel dimensions")
    if mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
        raise ValueError(
            f"Local image {label!r} has unsupported format {mime_type!r}; "
            "Google Slides accepts PNG, JPEG, or GIF"
        )
    return width, height, mime_type


class AssetCache:
    """Workspace-local mapping from canonical path plus SHA-256 to Drive asset."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.path = self.workspace / ASSET_CACHE_FILE

    async def resolve(self, source: str, uploader: AssetUploader) -> str:
        """Reuse or upload a local image, then return its public URL."""
        local_path = resolve_local_image_path(self.workspace, source)
        _, _, mime_type = inspect_local_image(local_path, source=source)
        content_hash = hashlib.sha256(local_path.read_bytes()).hexdigest()
        path_key = self._path_key(local_path)
        data = self._read()

        for entry in data["assets"]:
            if entry.get("path") == path_key and entry.get("sha256") == content_hash:
                url = entry.get("url")
                file_id = entry.get("fileId")
                if isinstance(url, str) and url and isinstance(file_id, str) and file_id:
                    return url

        uploaded = await uploader.upload(local_path, mime_type=mime_type)
        if not uploaded.file_id or not uploaded.url:
            raise AssetUploadError("Asset uploader returned an empty Drive file ID or URL")
        data["assets"].append(
            {
                "path": path_key,
                "sha256": content_hash,
                "fileId": uploaded.file_id,
                "url": uploaded.url,
            }
        )
        self._write(data)
        return uploaded.url

    def _path_key(self, path: Path) -> str:
        try:
            return path.relative_to(self.workspace).as_posix()
        except ValueError:
            return path.as_posix()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "assets": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid Slidesmith asset cache {self.path}: {exc}") from exc
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("assets"), list)
        ):
            raise ValueError(
                f"Invalid Slidesmith asset cache {self.path}: expected version 1 "
                "with an assets list"
            )
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.workspace,
                prefix=".assets-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(
                    json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
                )
                temporary_path = Path(temporary.name)
            temporary_path.replace(self.path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


class GoogleDriveAssetUploader:
    """Upload local images to the user's Drive and grant link-readable access."""

    def __init__(self, access_token: str, *, timeout: float = 60) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )

    async def upload(self, path: Path, *, mime_type: str) -> UploadedAsset:
        boundary = f"slidesmith-{secrets.token_hex(16)}"
        metadata = json.dumps({"name": path.name}, ensure_ascii=False).encode("utf-8")
        body = b"".join(
            (
                f"--{boundary}\r\n".encode(),
                b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
                metadata,
                f"\r\n--{boundary}\r\n".encode(),
                f"Content-Type: {mime_type}\r\n\r\n".encode(),
                path.read_bytes(),
                f"\r\n--{boundary}--\r\n".encode(),
            )
        )
        upload_response = await self._request(
            "POST",
            f"{DRIVE_UPLOAD_BASE}/files",
            params={"uploadType": "multipart", "fields": "id"},
            content=body,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        )
        file_id = upload_response.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise AssetUploadError("Google Drive upload response did not include a file ID")

        try:
            await self._request(
                "POST",
                f"{DRIVE_API_BASE}/files/{urllib.parse.quote(file_id)}/permissions",
                params={"fields": "id"},
                json={"type": "anyone", "role": "reader"},
            )
        except AssetUploadError as permission_error:
            try:
                await self._request(
                    "DELETE",
                    f"{DRIVE_API_BASE}/files/{urllib.parse.quote(file_id)}",
                    expect_json=False,
                )
            except AssetUploadError as cleanup_error:
                raise AssetUploadError(
                    f"{permission_error}; cleanup of uploaded Drive file "
                    f"{file_id!r} also failed: {cleanup_error}"
                ) from permission_error
            raise
        metadata_response = await self._request(
            "GET",
            f"{DRIVE_API_BASE}/files/{urllib.parse.quote(file_id)}",
            params={"fields": "id,webContentLink"},
        )
        web_content_link = metadata_response.get("webContentLink")
        if not isinstance(web_content_link, str) or not web_content_link:
            web_content_link = (
                "https://drive.google.com/uc?export=download&id="
                f"{urllib.parse.quote(file_id)}"
            )
        return UploadedAsset(file_id=file_id, url=web_content_link)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        expect_json: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, url, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AssetUploadError(
                f"Google Drive asset request failed ({exc.response.status_code}): "
                f"{exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise AssetUploadError(f"Google Drive asset network error: {exc}") from exc
        if not expect_json:
            return {}
        try:
            result = response.json()
        except ValueError as exc:
            raise AssetUploadError(
                "Google Drive asset response contained invalid JSON"
            ) from exc
        if not isinstance(result, dict):
            raise AssetUploadError("Google Drive asset response was not a JSON object")
        return result

    async def close(self) -> None:
        await self._client.aclose()
