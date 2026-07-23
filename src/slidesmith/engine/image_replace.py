"""Image replacement geometry and source-resolution helpers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from slidesmith.engine.assets import (
    AssetCache,
    AssetUploader,
    image_source_kind,
    inspect_local_image,
    resolve_local_image_path,
)
from slidesmith.engine.image_fetch import redact_image_url
from slidesmith.engine.bounds import BoundingBox, Transform
from slidesmith.engine.units import pt_to_emu


def _replacement_geometry_requests(
    object_id: str,
    old: BoundingBox,
    *,
    pixel_width: int | None,
    pixel_height: int | None,
    fit: str,
    target: BoundingBox | None = None,
) -> tuple[BoundingBox, dict[str, Any]]:
    """Compute a top-left target and undo Google's centered aspect fit."""
    if old.w <= 0 or old.h <= 0:
        raise ValueError(f"Image element {object_id!r} has non-positive geometry")
    if fit == "cover":
        if target is None:
            target = old
        scale_x = target.w / old.w
        scale_y = target.h / old.h
        request = {
            "updatePageElementTransform": {
                "objectId": object_id,
                "transform": {
                    "scaleX": scale_x,
                    "scaleY": scale_y,
                    "translateX": pt_to_emu(target.x - scale_x * old.x),
                    "translateY": pt_to_emu(target.y - scale_y * old.y),
                    "unit": "EMU",
                },
                "applyMode": "RELATIVE",
            }
        }
        return target, request

    if pixel_width is None or pixel_height is None:
        raise ValueError("Replacement image dimensions are required for contain/stretch")
    if pixel_width <= 0 or pixel_height <= 0:
        raise ValueError("Replacement image has non-positive pixel dimensions")

    image_aspect = pixel_width / pixel_height
    old_aspect = old.w / old.h
    if image_aspect > old_aspect:
        fitted_w = old.w
        fitted_h = old.w / image_aspect
    else:
        fitted_w = old.h * image_aspect
        fitted_h = old.h

    centered_x = old.x + (old.w - fitted_w) / 2
    centered_y = old.y + (old.h - fitted_h) / 2
    if target is None:
        if fit == "contain":
            target = BoundingBox(old.x, old.y, fitted_w, fitted_h)
        else:
            target = BoundingBox(old.x, old.y, old.w, old.h)

    # replaceImage(CENTER_INSIDE) first produces the centered fitted rectangle.
    # Pre-multiplying this relative affine transform maps that exact rectangle
    # onto the requested top-left target, independent of Google's internal
    # size/transform refactoring.
    scale_x = target.w / fitted_w
    scale_y = target.h / fitted_h
    translate_x = target.x - scale_x * centered_x
    translate_y = target.y - scale_y * centered_y
    request = {
        "updatePageElementTransform": {
            "objectId": object_id,
            "transform": {
                "scaleX": scale_x,
                "scaleY": scale_y,
                "translateX": pt_to_emu(translate_x),
                "translateY": pt_to_emu(translate_y),
                "unit": "EMU",
            },
            "applyMode": "RELATIVE",
        }
    }
    return target, request


class CoverFitPushError(RuntimeError):
    """A live API rejected a request in the unvalidated cover strategy."""

    def __init__(self, element_ids: list[str], cause: Exception) -> None:
        self.element_ids = tuple(element_ids)
        self.cause = cause
        ids = ", ".join(element_ids)
        super().__init__(
            "cover fit CENTER_CROP was rejected for existing image element "
            f"{ids}: {cause}. The cover batch was not applied; the existing-image "
            "CENTER_CROP path remains live-unvalidated."
        )


def _find_element_with_parent_transform(
    data: dict[str, Any], object_id: str
) -> tuple[dict[str, Any] | None, Transform | None]:
    """Find an element and its composed ancestor-group transform."""

    def walk(
        element: dict[str, Any],
        parent_transform: Transform | None,
    ) -> tuple[dict[str, Any] | None, Transform | None]:
        if element.get("objectId") == object_id:
            return element, parent_transform

        child_parent = parent_transform
        if "elementGroup" in element:
            group_transform = Transform.from_element(element)
            child_parent = (
                parent_transform.compose(group_transform)
                if parent_transform is not None
                else group_transform
            )
        for child in element.get("elementGroup", {}).get("children", []):
            found, found_parent = walk(child, child_parent)
            if found is not None:
                return found, found_parent
        return None, None

    for page_kind in ("slides", "layouts", "masters"):
        for page in data.get(page_kind, []) or []:
            for element in page.get("pageElements", []) or []:
                found, parent_transform = walk(element, None)
                if found is not None:
                    return found, parent_transform
    return None, None


def _replacement_image_dimensions(
    folder_path: Path,
    source: str,
    fetch_dimensions: Callable[[str], tuple[int, int]],
) -> tuple[int, int]:
    """Read replacement pixels through the same bounded source paths as create."""
    if image_source_kind(source) == "local":
        path = resolve_local_image_path(folder_path, source)
        return inspect_local_image(path, source=source)[:2]
    return fetch_dimensions(source)


async def resolve_asset_source(
    folder_path: Path,
    source: str,
    asset_uploader: AssetUploader | None,
    *,
    cover_aspect: float | None = None,
    element_id: str | None = None,
) -> str:
    source_kind = image_source_kind(source)
    if source_kind == "remote" and cover_aspect is not None:
        if asset_uploader is None:
            raise RuntimeError(
                f"Remote cover image {redact_image_url(source)!r} requires an "
                "asset uploader at push time"
            )
        try:
            return await AssetCache(folder_path).resolve_remote_cover(
                source,
                cover_aspect,
                asset_uploader,
                element_id=element_id,
            )
        except ValueError as exc:
            label = f"Image element '{element_id}' " if element_id else "Image "
            raise ValueError(
                f"{label}remote cover source {redact_image_url(source)!r} "
                f"could not be downloaded or derived: {redact_image_url(str(exc))}"
            ) from exc
    if source_kind == "remote":
        return source
    if asset_uploader is None:
        raise RuntimeError(
            f"Local image {source!r} requires a Drive asset uploader at push time"
        )
    cache = AssetCache(folder_path)
    if cover_aspect is not None:
        return await cache.resolve_cover(
            source,
            cover_aspect,
            asset_uploader,
            element_id=element_id,
        )
    return await cache.resolve(source, asset_uploader)


__all__ = [
    "_find_element_with_parent_transform",
    "_replacement_geometry_requests",
    "_replacement_image_dimensions",
    "CoverFitPushError",
    "resolve_asset_source",
]
