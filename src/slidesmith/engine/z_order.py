"""Build and validate Google Slides page-element z-order requests."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any

from slidesmith.engine.conflicts import iter_page_elements
from slidesmith.engine.hierarchy import has_ancestor_in_set
from slidesmith.engine.json_utils import read_json
from slidesmith.engine.selector import select_elements
from slidesmith.engine.workspace_reader import (
    _pristine_element_metadata,
    _read_base_raw,
    _read_current_slides,
)


Z_ORDER_OPERATIONS = {
    "bring-to-front": "BRING_TO_FRONT",
    "bring-forward": "BRING_FORWARD",
    "send-backward": "SEND_BACKWARD",
    "send-to-back": "SEND_TO_BACK",
}


def build_reorder_requests(
    folder_path: str | Path,
    selector: str,
    operation: str,
) -> list[dict[str, Any]]:
    """Resolve a selector and build one z-order request per local slide."""
    try:
        google_operation = Z_ORDER_OPERATIONS[operation]
    except KeyError as exc:
        choices = ", ".join(Z_ORDER_OPERATIONS)
        raise ValueError(
            f"Unknown reorder operation {operation!r}; expected one of {choices}"
        ) from exc

    matches = select_elements(folder_path, selector)
    if not matches:
        raise ValueError(f"reorder selector matched no elements: {selector!r}")

    mapping = read_json(Path(folder_path) / "id_mapping.json", missing_ok=False)
    missing_ids = [
        match.element.clean_id
        for match in matches
        if not isinstance(mapping.get(match.element.clean_id), str)
    ]
    if missing_ids:
        raise ValueError(
            "Selected elements are not mapped to live Google objects: "
            + ", ".join(missing_ids)
        )

    grouped_ids = _group_child_ids(Path(folder_path), matches, mapping)
    if grouped_ids:
        raise ValueError(
            "Cannot reorder group children; only top-level page elements can be "
            f"reordered. Offending IDs: {', '.join(grouped_ids)}"
        )

    by_slide: OrderedDict[str, list[str]] = OrderedDict()
    for match in matches:
        by_slide.setdefault(match.slide_index, []).append(mapping[match.element.clean_id])

    return [
        {
            "updatePageElementsZOrder": {
                "pageElementObjectIds": object_ids,
                "operation": google_operation,
            }
        }
        for object_ids in by_slide.values()
    ]


def _group_child_ids(
    folder_path: Path,
    matches: list[Any],
    mapping: dict[str, Any],
) -> list[str]:
    """Return selected IDs descended from genuine Google element groups.

    The SML projection also nests ordinary page elements by visual containment,
    so ParsedElement.parent_id alone cannot identify API group children. Prefer
    the pristine API hierarchy, whose GROUP types come from ``elementGroup``;
    older workspaces fall back to the pulled SML tags for the same distinction.
    """
    base_raw = _read_base_raw(folder_path)
    if base_raw is not None:
        types, parents = _pristine_element_metadata(base_raw)
    else:
        types, parents = _current_sml_element_metadata(folder_path, mapping)

    candidate_ids = set(types)
    return [
        match.element.clean_id
        for match in matches
        if has_ancestor_in_set(
            mapping[match.element.clean_id], candidate_ids, parents, types
        )
    ]


def _current_sml_element_metadata(
    folder_path: Path,
    mapping: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str | None]]:
    """Build clean-to-Google hierarchy metadata for legacy workspaces."""
    types_by_clean_id: dict[str, str] = {}
    parents_by_clean_id: dict[str, str | None] = {}

    def walk(elements: list[Any]) -> None:
        for element in elements:
            if element.clean_id:
                types_by_clean_id[element.clean_id] = (
                    "GROUP" if element.tag == "Group" else element.tag
                )
                parents_by_clean_id[element.clean_id] = element.parent_id
            walk(element.children)

    for elements in _read_current_slides(folder_path).values():
        walk(elements)

    types = {
        mapping[clean_id]: element_type
        for clean_id, element_type in types_by_clean_id.items()
        if isinstance(mapping.get(clean_id), str)
    }
    parents = {
        mapping[clean_id]: mapping.get(parent_id)
        for clean_id, parent_id in parents_by_clean_id.items()
        if isinstance(mapping.get(clean_id), str)
    }
    return types, parents


def validate_live_reorder_targets(
    presentation: dict[str, Any],
    requests: list[dict[str, Any]],
    mapping: dict[str, str],
) -> None:
    """Check live existence, page grouping, and the API's group constraint."""
    reverse_mapping = {google_id: clean_id for clean_id, google_id in mapping.items()}
    locations: dict[str, tuple[str | None, str | None]] = {}
    for _, page_id, element, parent_id in iter_page_elements(presentation):
        object_id = element.get("objectId")
        if isinstance(object_id, str) and object_id:
            locations[object_id] = (page_id, parent_id)

    missing: list[str] = []
    grouped: list[str] = []
    for request in requests:
        body = request["updatePageElementsZOrder"]
        page_ids: set[str | None] = set()
        for object_id in body["pageElementObjectIds"]:
            clean_id = reverse_mapping.get(object_id, object_id)
            location = locations.get(object_id)
            if location is None:
                missing.append(clean_id)
                continue
            page_id, parent_id = location
            page_ids.add(page_id)
            if parent_id is not None:
                grouped.append(clean_id)
        if len(page_ids) > 1:
            ids = ", ".join(body["pageElementObjectIds"])
            raise ValueError(
                "Reorder request would span multiple live pages; re-pull and "
                f"retry. Object IDs: {ids}"
            )

    if missing:
        raise ValueError(
            "Selected elements no longer exist in the live deck: "
            + ", ".join(missing)
        )
    if grouped:
        raise ValueError(
            "Cannot reorder group children; only top-level page elements can be "
            f"reordered. Offending IDs: {', '.join(grouped)}"
        )


__all__ = [
    "Z_ORDER_OPERATIONS",
    "build_reorder_requests",
    "validate_live_reorder_targets",
]
