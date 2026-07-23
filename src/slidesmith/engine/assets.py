"""Local image resolution, Drive upload, and workspace asset caching."""

from __future__ import annotations

import hashlib
from io import BytesIO
import json
import math
import secrets
import urllib.parse
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal, Protocol

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from slidesmith.engine import image_fetch
from slidesmith.engine.image_fetch import redact_image_url, validate_public_image_url
from slidesmith.engine.permissions import (
    DrivePermissionError,
    GoogleDrivePermissionsClient,
)

ASSET_CACHE_FILE = ".assets.json"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
SUPPORTED_IMAGE_MIME_TYPES = {"image/gif", "image/jpeg", "image/png"}
# Bump this whenever cover rasterization changes; old derived rasters must not
# survive an algorithm fix (the current version includes EXIF orientation).
COVER_DERIVATION_VERSION = 2
COVER_MAX_DIMENSION_PX = 4096
COVER_MAX_PIXELS = COVER_MAX_DIMENSION_PX * COVER_MAX_DIMENSION_PX


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
    workspace_root = workspace.resolve()
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme.lower() == "file":
        path = Path(urllib.parse.unquote(parsed.path))
    else:
        path = Path(source).expanduser()
        if not path.is_absolute():
            path = workspace_root / path
    resolved = path.resolve()
    if not resolved.is_relative_to(workspace_root):
        raise ValueError(
            f"Local image source {source!r} resolves outside the presentation "
            f"workspace {workspace_root}; place assets inside the deck folder"
        )
    return resolved


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
        data = self._read()
        return await self._resolve_path(
            local_path,
            uploader,
            data,
            source=source,
        )

    async def resolve_cover(
        self,
        source: str,
        target_aspect: float,
        uploader: AssetUploader,
        *,
        element_id: str | None = None,
    ) -> str:
        """Center-crop a local source once, then resolve it like any asset.

        The derived filename is keyed by the source content hash, the exact
        target aspect-ratio float representation, and the derivation version.
        The resulting PNG is therefore stable across retries while ignoring
        rasters produced before an algorithm fix. The final raster uses the
        largest bounded integer rational target-aspect canvas that fits inside
        the crop when possible, so Google cannot refit a rounded crop with a
        different pixel aspect.
        """
        if not math.isfinite(target_aspect) or target_aspect <= 0:
            raise ValueError("Cover target aspect ratio must be finite and positive")
        local_path = resolve_local_image_path(self.workspace, source)
        label = f"Image element '{element_id}'" if element_id else f"Local image {source!r}"
        try:
            with Image.open(local_path) as opened:
                if getattr(opened, "is_animated", False) or getattr(
                    opened, "n_frames", 1
                ) > 1:
                    raise ValueError(
                        f"{label} uses an animated or multi-frame image; cover fit "
                        "requires a static source"
                    )
                mime_type = opened.get_format_mimetype()
                if mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
                    raise ValueError(
                        f"Local image {source!r} has unsupported format {mime_type!r}; "
                        "Google Slides accepts PNG, JPEG, or GIF"
                    )
                image = ImageOps.exif_transpose(opened).copy()
        except ValueError:
            raise
        except (OSError, UnidentifiedImageError) as exc:
            raise ValueError(
                f"Could not derive cover asset from local image {source!r}: {exc}"
            ) from exc

        source_bytes = local_path.read_bytes()
        return await self._resolve_cover_image(
            image,
            source_bytes,
            target_aspect,
            uploader,
            label=label,
            source_url=None,
            data=self._read(),
        )

    async def resolve_remote_cover(
        self,
        source: str,
        target_aspect: float,
        uploader: AssetUploader,
        *,
        element_id: str | None = None,
    ) -> str:
        """Download, derive, cache, and upload a remote cover source."""
        if not math.isfinite(target_aspect) or target_aspect <= 0:
            raise ValueError("Cover target aspect ratio must be finite and positive")
        label = (
            f"Image element '{element_id}'"
            if element_id
            else f"Remote image {redact_image_url(source)!r}"
        )
        data = self._read()
        aspect_key = target_aspect.hex()
        cached = self._cached_remote_cover(data, source, aspect_key, target_aspect)
        if cached is not None:
            return cached
        try:
            source_bytes = image_fetch.fetch_image_bytes(source)
            if len(source_bytes) > image_fetch.MAX_REMOTE_COVER_BYTES:
                raise ValueError(
                    "image download exceeds the "
                    f"{image_fetch.MAX_REMOTE_COVER_BYTES // (1024 * 1024)} MB limit"
                )
            with Image.open(BytesIO(source_bytes)) as opened:
                if getattr(opened, "is_animated", False) or getattr(
                    opened, "n_frames", 1
                ) > 1:
                    raise ValueError(
                        "remote cover requires a static source; animated or "
                        "multi-frame images are not supported"
                    )
                image = ImageOps.exif_transpose(opened).copy()
        except ValueError as exc:
            raise ValueError(
                f"{label} could not download a valid remote cover source: "
                f"{redact_image_url(str(exc))}"
            ) from exc
        except (OSError, UnidentifiedImageError) as exc:
            raise ValueError(
                f"Could not derive cover asset from remote image "
                f"{redact_image_url(source)!r}: {exc}"
            ) from exc
        return await self._resolve_cover_image(
            image,
            source_bytes,
            target_aspect,
            uploader,
            label=label,
            source_url=source,
            data=data,
        )

    async def _resolve_cover_image(
        self,
        image: Image.Image,
        source_bytes: bytes,
        target_aspect: float,
        uploader: AssetUploader,
        *,
        label: str,
        source_url: str | None,
        data: dict[str, Any],
    ) -> str:
        width, height = image.size
        if width <= 0 or height <= 0:
            raise ValueError(f"{label} has invalid pixel dimensions")
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        aspect_key = target_aspect.hex()
        key_material = (
            f"remote\0{source_url}\0{source_hash}\0{aspect_key}\0"
            f"{COVER_DERIVATION_VERSION}"
            if source_url is not None
            else f"{source_hash}\0{aspect_key}\0{COVER_DERIVATION_VERSION}"
        )
        derived_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        derived_path = self.workspace / ".slidesmith-cover" / f"{derived_key}.png"

        derived_is_valid = False
        if derived_path.is_file():
            try:
                with Image.open(derived_path) as derived:
                    derived_is_valid = _aspect_matches(derived.size, target_aspect)
            except (OSError, UnidentifiedImageError):
                derived_is_valid = False

        if not derived_is_valid:
            derived_path.parent.mkdir(parents=True, exist_ok=True)
            source_aspect = width / height
            if source_aspect > target_aspect:
                crop_width = max(1, min(width, round(height * target_aspect)))
                left = (width - crop_width) // 2
                box = (left, 0, left + crop_width, height)
            else:
                crop_height = max(1, min(height, round(width / target_aspect)))
                top = (height - crop_height) // 2
                box = (0, top, width, top + crop_height)
            cropped = image.crop(box)
            try:
                target_size = _exact_aspect_size(cropped.size, target_aspect)
            except ValueError as exc:
                raise ValueError(f"{label} has no safe cover raster: {exc}") from exc
            if cropped.size != target_size:
                cropped = cropped.resize(target_size, Image.Resampling.LANCZOS)
            if cropped.mode in {"CMYK", "YCbCr"}:
                cropped = cropped.convert("RGB")
            try:
                cropped.save(derived_path, format="PNG", optimize=False)
            except OSError as exc:
                raise ValueError(f"Could not derive cover asset for {label}: {exc}") from exc

        extra = {
            "kind": "cover",
            "sourceSha256": source_hash,
            "targetAspect": aspect_key,
            "derivationVersion": str(COVER_DERIVATION_VERSION),
        }
        if source_url is not None:
            extra["sourceUrl"] = source_url
        return await self._resolve_path(derived_path, uploader, data, extra=extra)

    def _cached_remote_cover(
        self,
        data: dict[str, Any],
        source: str,
        aspect_key: str,
        target_aspect: float,
    ) -> str | None:
        for entry in data["assets"]:
            if (
                entry.get("kind") != "cover"
                or entry.get("sourceUrl") != source
                or entry.get("targetAspect") != aspect_key
                or entry.get("derivationVersion")
                != str(COVER_DERIVATION_VERSION)
            ):
                continue
            url = entry.get("url")
            path_value = entry.get("path")
            if not isinstance(url, str) or not url or not isinstance(path_value, str):
                continue
            derived_path = self.workspace / path_value
            if not derived_path.is_file():
                continue
            try:
                with Image.open(derived_path) as derived:
                    if _aspect_matches(derived.size, target_aspect):
                        return url
            except (OSError, UnidentifiedImageError):
                continue
        return None

    def remote_cover_local_source(
        self,
        source: str,
        target_aspect: float,
    ) -> str | None:
        """Return the cached workspace path for a derived remote cover."""
        if not math.isfinite(target_aspect) or target_aspect <= 0:
            return None
        aspect_key = target_aspect.hex()
        data = self._read()
        for entry in data["assets"]:
            if (
                entry.get("kind") != "cover"
                or entry.get("sourceUrl") != source
                or entry.get("targetAspect") != aspect_key
                or entry.get("derivationVersion")
                != str(COVER_DERIVATION_VERSION)
            ):
                continue
            path_value = entry.get("path")
            if not isinstance(path_value, str) or not path_value:
                continue
            derived_path = (self.workspace / path_value).resolve()
            if (
                not derived_path.is_relative_to(self.workspace)
                or not derived_path.is_file()
            ):
                continue
            try:
                with Image.open(derived_path) as derived:
                    if _aspect_matches(derived.size, target_aspect):
                        return self._path_key(derived_path)
            except (OSError, UnidentifiedImageError):
                continue
        return None

    async def _resolve_path(
        self,
        local_path: Path,
        uploader: AssetUploader,
        data: dict[str, Any],
        *,
        source: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> str:
        label = source or self._path_key(local_path)
        _, _, mime_type = inspect_local_image(local_path, source=label)
        content_hash = hashlib.sha256(local_path.read_bytes()).hexdigest()
        path_key = self._path_key(local_path)

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
                **(extra or {}),
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


def _aspect_matches(size: tuple[int, int], target_aspect: float) -> bool:
    width, height = size
    if width <= 0 or height <= 0:
        return False
    try:
        ratio = _bounded_aspect_ratio(target_aspect)
    except ValueError:
        return False
    return width * ratio.denominator == height * ratio.numerator


def _bounded_aspect_ratio(target_aspect: float) -> Fraction:
    """Return one bounded rational used by both sizing and cache validation."""
    exact = Fraction(str(target_aspect))
    # limit_denominator bounds only the denominator; a wide aspect can land a
    # numerator above the cap even though a coarser in-bounds rational exists
    # (golden ratio -> 4181/2584 rejected while 2584/1597 fits). Walk the
    # denominator limit down until both terms and the pixel product fit.
    limit = COVER_MAX_DIMENSION_PX
    while limit >= 1:
        ratio = exact.limit_denominator(limit)
        if (
            ratio.numerator > 0
            and ratio.denominator > 0
            and ratio.numerator <= COVER_MAX_DIMENSION_PX
            and ratio.denominator <= COVER_MAX_DIMENSION_PX
            and ratio.numerator * ratio.denominator <= COVER_MAX_PIXELS
        ):
            return ratio
        if ratio.numerator > COVER_MAX_DIMENSION_PX:
            # Shrink proportionally so the numerator also lands in bounds.
            limit = min(
                limit - 1,
                (ratio.denominator * COVER_MAX_DIMENSION_PX) // ratio.numerator,
            )
        else:
            limit -= 1
    raise ValueError(
        f"target aspect {target_aspect:g} has no safe rational raster within "
        f"{COVER_MAX_DIMENSION_PX}px per dimension and {COVER_MAX_PIXELS} pixels"
    )


def _exact_aspect_size(
    cropped_size: tuple[int, int], target_aspect: float
) -> tuple[int, int]:
    """Choose the largest bounded deterministic integer canvas at one ratio."""
    cropped_width, cropped_height = cropped_size
    if cropped_width <= 0 or cropped_height <= 0:
        raise ValueError("cover crop has invalid pixel dimensions")
    ratio = _bounded_aspect_ratio(target_aspect)
    max_units = min(
        COVER_MAX_DIMENSION_PX // ratio.numerator,
        COVER_MAX_DIMENSION_PX // ratio.denominator,
        math.isqrt(COVER_MAX_PIXELS // (ratio.numerator * ratio.denominator)),
    )
    crop_units = min(
        cropped_width // ratio.numerator,
        cropped_height // ratio.denominator,
    )
    units = max(1, min(crop_units, max_units))
    width = ratio.numerator * units
    height = ratio.denominator * units
    if width > COVER_MAX_DIMENSION_PX or height > COVER_MAX_DIMENSION_PX:
        raise ValueError("cover raster exceeds the maximum dimension")
    if width * height > COVER_MAX_PIXELS:
        raise ValueError("cover raster exceeds the maximum pixel count")
    return width, height


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
        self._permissions = GoogleDrivePermissionsClient(client=self._client)

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
            # Keep the shared client aligned with the uploader's injectable
            # HTTP seam used by hermetic tests and callers.
            self._permissions._client = self._client
            await self._permissions.create_permission(
                file_id,
                permission_type="anyone",
                role="reader",
            )
        except (AssetUploadError, DrivePermissionError) as permission_error:
            if isinstance(permission_error, DrivePermissionError):
                permission_error = AssetUploadError(str(permission_error))
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
            raise permission_error
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
