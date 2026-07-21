"""Geometry helpers for authored and pulled image elements."""

from __future__ import annotations

from pathlib import Path

from slidesmith.engine.assets import (
    image_source_kind,
    inspect_local_image,
    resolve_local_image_path,
)
from slidesmith.engine.content_parser import ParsedElement, validate_authored_image_geometry
from slidesmith.engine.image_fetch import fetch_image_dimensions as _fetch_image_dimensions

_ORIGINAL_FETCH_IMAGE_DIMENSIONS = _fetch_image_dimensions


def _fetch_dimensions_at_call_time(url: str) -> tuple[int, int]:
    """Resolve the compatibility fetch hook from its current module owner."""
    from slidesmith.engine import content_diff

    if _fetch_image_dimensions is not _ORIGINAL_FETCH_IMAGE_DIMENSIONS:
        fetch_dimensions = _fetch_image_dimensions
    else:
        underscored = getattr(
            content_diff,
            "_fetch_image_dimensions",
            _ORIGINAL_FETCH_IMAGE_DIMENSIONS,
        )
        legacy = getattr(
            content_diff,
            "fetch_image_dimensions",
            _ORIGINAL_FETCH_IMAGE_DIMENSIONS,
        )
        fetch_dimensions = (
            underscored
            if underscored is not _ORIGINAL_FETCH_IMAGE_DIMENSIONS
            else legacy
        )
    return fetch_dimensions(url)


def get_effective_position(
    elem: ParsedElement,
    *,
    workspace_root: Path | None = None,
    allow_remote_image_fetch: bool = False,
) -> dict[str, float] | None:
    """Resolve authored geometry, fetching remote image pixels only when allowed."""
    if elem.tag == "Image" and elem.src is not None:
        validate_authored_image_geometry(
            elem.clean_id,
            x=elem.x,
            y=elem.y,
            w=elem.w,
            h=elem.h,
        )
        if image_source_kind(elem.src) == "local":
            if workspace_root is None:
                raise ValueError(
                    f"Local image source {elem.src!r} on Image element "
                    f"'{elem.clean_id}' requires a presentation workspace"
                )
            local_path = resolve_local_image_path(workspace_root, elem.src)
            local_pixels = inspect_local_image(local_path, source=elem.src)[:2]
        else:
            local_pixels = None
    from slidesmith.engine import content_diff

    position = content_diff._get_position(elem)
    if elem.tag != "Image" or elem.fit != "contain":
        return position
    if position is None or not elem.src:
        return position

    width = position["w"]
    height = position["h"]
    if width <= 0 or height <= 0:
        raise ValueError(
            f"Image element '{elem.clean_id}' with fit='contain' requires "
            "positive w and h"
        )

    if local_pixels is not None:
        pixel_width, pixel_height = local_pixels
    elif not allow_remote_image_fetch:
        return position
    else:
        pixel_width, pixel_height = _fetch_dimensions_at_call_time(elem.src)
    if pixel_width <= 0 or pixel_height <= 0:
        raise ValueError(
            f"Could not determine positive pixel dimensions for Image element "
            f"'{elem.clean_id}' from {elem.src!r}"
        )

    image_aspect = pixel_width / pixel_height
    frame_aspect = width / height
    contained = dict(position)
    if image_aspect > frame_aspect:
        contained["h"] = width / image_aspect
    elif image_aspect < frame_aspect:
        contained["w"] = height * image_aspect
    return contained
