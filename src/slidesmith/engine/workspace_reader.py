"""Read current and pristine Slidesmith workspace state."""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

from slidesmith.engine.bounds import Transform
from slidesmith.engine.components import load_components
from slidesmith.engine.conflicts import index_presentation, iter_page_elements
from slidesmith.engine.content_parser import parse_slide_content
from slidesmith.engine.json_utils import read_json
from slidesmith.engine.workspace_layout import (
    PRISTINE_BASE_FILE,
    PRISTINE_DIR,
    PRISTINE_ZIP,
    RAW_DIR,
    SLIDES_DIR,
    STYLES_FILE,
)


def _read_base_raw(folder_path: Path) -> dict[str, Any] | None:
    """Read the pristine base raw API tree, if this folder has one.

    Prefers .pristine/base.json (always written by current pulls); falls
    back to .raw/presentation.json (older pulls with save_raw=True).
    Returns None when neither exists (folder pulled by old code).
    """
    candidates = (
        folder_path / PRISTINE_DIR / PRISTINE_BASE_FILE,
        folder_path / RAW_DIR / "presentation.json",
    )
    for candidate in candidates:
        if candidate.exists():
            return read_json(candidate, missing_ok=False)
    return None


def _read_current_slides(folder_path: Path) -> dict[str, list[Any]]:
    """Read current slide content files."""
    slides_dir = folder_path / SLIDES_DIR
    components = load_components(folder_path)
    result: dict[str, list[Any]] = {}

    if not slides_dir.exists():
        return result

    for slide_folder in sorted(slides_dir.iterdir()):
        if slide_folder.is_dir():
            content_file = slide_folder / "content.sml"
            if content_file.exists():
                content = content_file.read_text(encoding="utf-8")
                result[slide_folder.name] = parse_slide_content(
                    content, components=components
                )

    return result


def _read_pristine(
    folder_path: Path,
) -> tuple[dict[str, list[Any]], dict[str, dict[str, Any]]]:
    """Read pristine slides and styles from zip."""
    zip_path = folder_path / PRISTINE_DIR / PRISTINE_ZIP
    if not zip_path.exists():
        raise FileNotFoundError(f"Pristine zip not found: {zip_path}")

    slides: dict[str, list[Any]] = {}
    styles: dict[str, dict[str, Any]] = {}

    with zipfile.ZipFile(zip_path, "r") as zf:
        # Read styles.json
        if STYLES_FILE in zf.namelist():
            styles = json.loads(zf.read(STYLES_FILE).decode("utf-8"))

        # Read slide content files
        for name in zf.namelist():
            if name.startswith(f"{SLIDES_DIR}/") and name.endswith("/content.sml"):
                # Extract slide index from path like "slides/01/content.sml"
                parts = name.split("/")
                if len(parts) >= 2:
                    slide_index = parts[1]
                    content = zf.read(name).decode("utf-8")
                    slides[slide_index] = parse_slide_content(content)

    return slides, styles


def _build_slide_id_mapping(
    id_mapping: dict[str, str],
    slide_order: list[str] | None = None,
) -> dict[str, str]:
    """Build mapping from slide index to Google slide ID.

    Slide clean IDs are like "s1", "s2", etc.
    Slide indices are like "01", "02", etc.
    """
    result: dict[str, str] = {}

    ordered_ids = slide_order
    if not isinstance(ordered_ids, list):
        ordered_ids = sorted(
            (
                clean_id
                for clean_id in id_mapping
                if re.fullmatch(r"s\d+", clean_id)
            ),
            key=lambda value: int(value[1:]),
        )
    for index, clean_id in enumerate(ordered_ids, 1):
        google_id = id_mapping.get(clean_id)
        if google_id:
            result[f"{index:02d}"] = google_id

    return result


def _pristine_element_metadata(
    data: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str | None]]:
    """Extract element types and group parentage from a pristine API tree."""
    types: dict[str, str] = {}
    parents: dict[str, str | None] = {}

    for _, _, element, parent_id in iter_page_elements(data):
        object_id = element.get("objectId")
        if not isinstance(object_id, str) or not object_id:
            continue
        if "elementGroup" in element:
            element_type = "GROUP"
        elif "shape" in element:
            element_type = element.get("shape", {}).get("shapeType", "SHAPE")
        elif "line" in element:
            element_type = "LINE"
        elif "image" in element:
            element_type = "IMAGE"
        elif "table" in element:
            element_type = "TABLE"
        elif "video" in element:
            element_type = "VIDEO"
        elif "sheetsChart" in element:
            element_type = "SHEETS_CHART"
        else:
            element_type = "UNKNOWN"
        types[object_id] = element_type
        parents[object_id] = parent_id
    return types, parents


def _enrich_pristine_geometry(
    styles: dict[str, dict[str, Any]],
    id_mapping: dict[str, str],
    base_raw: dict[str, Any],
) -> None:
    """Backfill native geometry for workspaces pulled by older versions."""
    elements, _, _ = index_presentation(base_raw)
    for clean_id, google_id in id_mapping.items():
        element = elements.get(google_id)
        if element is None:
            continue
        size = element.get("size")
        transform = element.get("transform")
        style = styles.setdefault(clean_id, {})
        if isinstance(size, dict):
            style.setdefault(
                "nativeSize",
                {
                    "w": size.get("width", {}).get("magnitude", 0),
                    "h": size.get("height", {}).get("magnitude", 0),
                },
            )
        if isinstance(transform, dict):
            style.setdefault(
                "nativeTransform",
                {
                    "scaleX": transform.get("scaleX", 1),
                    "scaleY": transform.get("scaleY", 1),
                    "shearX": transform.get("shearX", 0),
                    "shearY": transform.get("shearY", 0),
                    "translateX": transform.get("translateX", 0),
                    "translateY": transform.get("translateY", 0),
                },
            )

    clean_id_by_google_id = {
        google_id: clean_id for clean_id, google_id in id_mapping.items()
    }

    def walk(
        element: dict[str, Any],
        parent_group_transform: Transform | None = None,
    ) -> None:
        google_id = element.get("objectId")
        clean_id = clean_id_by_google_id.get(google_id)
        if clean_id is not None and parent_group_transform is not None:
            styles.setdefault(clean_id, {}).setdefault(
                "parentTransform",
                {
                    "scaleX": parent_group_transform.scale_x,
                    "scaleY": parent_group_transform.scale_y,
                    "shearX": parent_group_transform.shear_x,
                    "shearY": parent_group_transform.shear_y,
                    "translateX": parent_group_transform.translate_x,
                    "translateY": parent_group_transform.translate_y,
                },
            )

        child_group_transform = parent_group_transform
        if "elementGroup" in element:
            group_transform = Transform.from_element(element)
            child_group_transform = (
                parent_group_transform.compose(group_transform)
                if parent_group_transform is not None
                else group_transform
            )
        for child in element.get("elementGroup", {}).get("children", []):
            walk(child, child_group_transform)

    for page_kind in ("slides", "layouts", "masters"):
        for page in base_raw.get(page_kind, []) or []:
            for element in page.get("pageElements", []) or []:
                walk(element)


__all__ = [
    "_build_slide_id_mapping",
    "_enrich_pristine_geometry",
    "_pristine_element_metadata",
    "_read_base_raw",
    "_read_current_slides",
    "_read_pristine",
]
