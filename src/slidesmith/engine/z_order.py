"""Build and validate Google Slides page-element z-order requests."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any

from slidesmith.engine.conflicts import index_presentation, iter_page_elements
from slidesmith.engine.hierarchy import has_ancestor_in_set
from slidesmith.engine.id_manager import is_valid_google_object_id
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


def build_group_requests(
    folder_path: str | Path,
    selector: str,
) -> list[dict[str, Any]]:
    """Resolve one-slide top-level siblings into a ``groupObjects`` request."""
    folder = Path(folder_path)
    matches = select_elements(folder, selector)
    if len(matches) < 2:
        raise ValueError(
            f"group selector must match at least 2 elements; matched {len(matches)}: "
            f"{selector!r}"
        )

    slide_indices = {match.slide_index for match in matches}
    if len(slide_indices) != 1:
        raise ValueError(
            "Cannot group elements across slides; the selector matched: "
            + ", ".join(sorted(slide_indices))
        )

    mapping = read_json(folder / "id_mapping.json", missing_ok=False)
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

    unsupported = _unsupported_group_members(folder, matches, mapping)
    if unsupported:
        details = ", ".join(
            f"{clean_id} ({element_type})" for clean_id, element_type in unsupported
        )
        raise ValueError(
            "Cannot group unsupported elements; GroupObjectsRequest rejects: "
            + details
        )

    grouped_ids = _group_child_ids(folder, matches, mapping)
    if grouped_ids:
        raise ValueError(
            "Cannot group elements already inside a native group; select only "
            f"top-level siblings. Offending IDs: {', '.join(grouped_ids)}"
        )

    locations = _local_element_locations(folder, mapping)
    missing_locations: list[str] = []
    non_top_level: list[str] = []
    page_ids: set[str] = set()
    for match in matches:
        google_id = mapping[match.element.clean_id]
        location = locations.get(google_id)
        if location is None:
            missing_locations.append(match.element.clean_id)
            continue
        page_id, parent_id = location
        page_ids.add(page_id)
        if parent_id is not None:
            non_top_level.append(match.element.clean_id)
    if missing_locations:
        raise ValueError(
            "Selected elements are not top-level page elements in the pulled "
            "deck: " + ", ".join(missing_locations)
        )
    if non_top_level:
        raise ValueError(
            "Cannot group elements inside a native group; offending IDs: "
            + ", ".join(non_top_level)
        )
    if len(page_ids) != 1:
        raise ValueError(
            "Cannot group elements across live pages; re-pull and select one slide"
        )

    children = [mapping[match.element.clean_id] for match in matches]
    reserved_ids = {
        value for value in mapping.values() if isinstance(value, str)
    }
    base_raw = _read_base_raw(folder)
    if base_raw is not None:
        elements, page_ids, _ = index_presentation(base_raw)
        reserved_ids.update(elements)
        reserved_ids.update(page_ids)
    group_object_id = _allocate_group_object_id(
        mapping,
        matches[0].slide_index,
        reserved_ids=reserved_ids,
    )
    return [
        {
            "groupObjects": {
                "groupObjectId": group_object_id,
                "childrenObjectIds": children,
            }
        }
    ]


def _allocate_group_object_id(
    mapping: dict[str, Any],
    slide_index: str,
    *,
    reserved_ids: set[str] = frozenset(),
) -> str:
    """Allocate a deterministic valid object ID for a group batch request."""
    reserved = {
        value for value in mapping.values() if isinstance(value, str)
    } | reserved_ids
    counter = 1
    while True:
        candidate = f"new_group_{slide_index}_{counter}"
        if is_valid_google_object_id(candidate) and candidate not in reserved:
            return candidate
        counter += 1


def _unsupported_group_members(
    folder_path: Path,
    matches: list[Any],
    mapping: dict[str, Any],
) -> list[tuple[str, str]]:
    """Return selected table/video/placeholder members rejected by Google."""
    base_raw = _read_base_raw(folder_path)
    raw_elements: dict[str, dict[str, Any]] = {}
    if base_raw is not None:
        raw_elements, _, _ = index_presentation(base_raw)

    types, _ = _current_sml_element_metadata(folder_path, mapping)
    unsupported: list[tuple[str, str]] = []
    for match in matches:
        clean_id = match.element.clean_id
        google_id = mapping.get(clean_id)
        raw = raw_elements.get(google_id, {})
        if "table" in raw:
            element_type = "TABLE"
        elif "video" in raw:
            element_type = "VIDEO"
        elif isinstance(raw.get("shape"), dict) and "placeholder" in raw["shape"]:
            element_type = "PLACEHOLDER"
        else:
            element_type = str(types.get(google_id, "")).upper()
        if element_type in {"TABLE", "VIDEO", "PLACEHOLDER"}:
            unsupported.append((clean_id, element_type))
    return unsupported


def _local_element_locations(
    folder_path: Path,
    mapping: dict[str, Any],
) -> dict[str, tuple[str, str | None]]:
    """Return page/parent metadata from the pulled API tree when available."""
    base_raw = _read_base_raw(folder_path)
    if base_raw is not None:
        locations: dict[str, tuple[str, str | None]] = {}
        for _, page_id, element, parent_id in iter_page_elements(base_raw):
            object_id = element.get("objectId")
            if isinstance(object_id, str) and object_id:
                locations[object_id] = (page_id, parent_id)
        return locations

    # Older workspaces lack the raw tree.  In that case the SML parent is the
    # only available hierarchy signal; current pulls take the authoritative path.
    locations = {}
    for elements in _read_current_slides(folder_path).values():
        def walk(items: list[Any], page_id: str) -> None:
            for element in items:
                if element.clean_id and isinstance(mapping.get(element.clean_id), str):
                    locations[mapping[element.clean_id]] = (page_id, element.parent_id)
                walk(element.children, page_id)

        walk(elements, "local-slide")
    return locations


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


def validate_live_group_targets(
    presentation: dict[str, Any],
    requests: list[dict[str, Any]],
    mapping: dict[str, str],
) -> None:
    """Check live existence and top-level same-page constraints for grouping."""
    elements, live_page_ids, _ = index_presentation(presentation)
    locations: dict[str, tuple[str, str | None]] = {}
    for _, page_id, element, parent_id in iter_page_elements(presentation):
        object_id = element.get("objectId")
        if isinstance(object_id, str) and object_id:
            locations[object_id] = (page_id, parent_id)

    reverse_mapping = {google_id: clean_id for clean_id, google_id in mapping.items()}
    live_ids = set(elements) | set(live_page_ids)
    reserved_ids = set(live_ids)
    for request in requests:
        body = request["groupObjects"]
        group_object_id = body["groupObjectId"]
        if group_object_id in reserved_ids:
            # A stale workspace can miss a newly-created slide/page ID even
            # though it is present in the live response. Reallocate locally
            # before the request reaches Google instead of relying on a 400.
            body["groupObjectId"] = _allocate_group_object_id(
                mapping,
                "live",
                reserved_ids=reserved_ids,
            )
            group_object_id = body["groupObjectId"]
        reserved_ids.add(group_object_id)
        page_ids: set[str] = set()
        missing: list[str] = []
        grouped: list[str] = []
        for object_id in body["childrenObjectIds"]:
            location = locations.get(object_id)
            clean_id = reverse_mapping.get(object_id, object_id)
            if location is None:
                missing.append(clean_id)
                continue
            page_id, parent_id = location
            page_ids.add(page_id)
            if parent_id is not None:
                grouped.append(clean_id)
        if missing:
            raise ValueError(
                "Selected elements no longer exist in the live deck: "
                + ", ".join(missing)
            )
        if grouped:
            raise ValueError(
                "Cannot group elements already inside a native group; offending "
                "IDs: " + ", ".join(grouped)
            )
        if len(page_ids) != 1:
            raise ValueError(
                "Group request would span multiple live pages; re-pull and retry"
            )


__all__ = [
    "Z_ORDER_OPERATIONS",
    "build_group_requests",
    "build_reorder_requests",
    "validate_live_group_targets",
    "validate_live_reorder_targets",
]
