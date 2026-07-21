"""Conflict detection for guarded Google Slides pushes."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


class ConflictError(Exception):
    """A push would collide with edits made in Google Slides since the pull.

    Attributes:
        conflicts: List of (clean_id, description) pairs, one per element this
            push would touch that also changed (or was deleted) remotely.
            Empty when the conflict was detected by the API's revision guard
            rather than the pre-push comparison.
    """

    def __init__(
        self, message: str, conflicts: list[tuple[str, str]] | None = None
    ) -> None:
        super().__init__(message)
        self.conflicts: list[tuple[str, str]] = conflicts or []


def iter_page_elements(
    data: dict[str, Any],
) -> Iterator[tuple[str, str | None, dict[str, Any], str | None]]:
    """Yield every raw page element with its page and immediate group parent."""

    def walk(
        page_kind: str,
        page_id: str | None,
        element: dict[str, Any],
        parent_id: str | None,
    ) -> Iterator[tuple[str, str | None, dict[str, Any], str | None]]:
        yield page_kind, page_id, element, parent_id
        object_id = element.get("objectId")
        child_parent_id = object_id if isinstance(object_id, str) else None
        for child in element.get("elementGroup", {}).get("children", []) or []:
            yield from walk(page_kind, page_id, child, child_parent_id)

    for page_kind in ("slides", "layouts", "masters"):
        for page in data.get(page_kind, []) or []:
            raw_page_id = page.get("objectId")
            page_id = raw_page_id if isinstance(raw_page_id, str) else None
            for element in page.get("pageElements", []) or []:
                yield from walk(page_kind, page_id, element, None)


def index_presentation(
    data: dict[str, Any],
) -> tuple[
    dict[str, dict[str, Any]],
    set[str],
    dict[str, str | None],
]:
    """Index a raw presentation JSON tree by objectId.

    Returns:
        (elements, page_ids, parents) where elements maps every page element's
        objectId to its raw JSON subtree (recursing into groups) across
        slides, layouts, and masters, and page_ids is the set of page
        objectIds (slides/layouts/masters). parents maps each element ID to
        its immediate ancestor group ID, or None for a top-level element.
    """
    elements: dict[str, dict[str, Any]] = {}
    page_ids: set[str] = set()
    parents: dict[str, str | None] = {}

    for page_kind in ("slides", "layouts", "masters"):
        for page in data.get(page_kind, []) or []:
            page_id = page.get("objectId")
            if isinstance(page_id, str) and page_id:
                page_ids.add(page_id)
    for _, _, element, parent_id in iter_page_elements(data):
        object_id = element.get("objectId")
        if isinstance(object_id, str) and object_id:
            elements[object_id] = element
            parents[object_id] = parent_id

    return elements, page_ids, parents


def collect_request_object_ids(
    requests: list[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    """Collect the Google objectIds a batch of requests will touch.

    Returns:
        (object_ids, page_ids): element-level ids referenced by the requests,
        and page ids referenced as creation targets (elementProperties.
        pageObjectId). Ids of objects created by the same batch are included
        too; callers filter to ids that exist in the pristine base.
    """
    object_ids: set[str] = set()
    page_ids: set[str] = set()

    for request in requests:
        for body in request.values():
            if not isinstance(body, dict):
                continue
            for key in ("objectId", "groupObjectId"):
                if body.get(key):
                    object_ids.add(body[key])
            for child_id in body.get("childrenObjectIds", []) or []:
                object_ids.add(child_id)
            element_properties = body.get("elementProperties")
            if isinstance(element_properties, dict) and element_properties.get(
                "pageObjectId"
            ):
                page_ids.add(element_properties["pageObjectId"])

    return object_ids, page_ids


def _classify_element_change(
    base_element: dict[str, Any], remote_element: dict[str, Any]
) -> str | None:
    """Describe how an element changed remotely, or None if it did not."""
    if base_element == remote_element:
        return None

    kinds: list[str] = []
    if base_element.get("transform") != remote_element.get(
        "transform"
    ) or base_element.get("size") != remote_element.get("size"):
        kinds.append("geometry")

    if base_element.get("shape", {}).get("text") != remote_element.get(
        "shape", {}
    ).get("text"):
        kinds.append("text")

    def strip(element: dict[str, Any]) -> dict[str, Any]:
        stripped = {
            key: value
            for key, value in element.items()
            if key not in ("transform", "size")
        }
        shape = stripped.get("shape")
        if isinstance(shape, dict):
            stripped["shape"] = {
                key: value for key, value in shape.items() if key != "text"
            }
        return stripped

    if strip(base_element) != strip(remote_element):
        kinds.append("properties")

    return "/".join(kinds) if kinds else "properties"


def _ancestor_state(element: dict[str, Any]) -> dict[str, Any]:
    """Return a group's own state without descendant element subtrees."""
    state = dict(element)
    element_group = state.get("elementGroup")
    if isinstance(element_group, dict):
        state["elementGroup"] = {
            key: value for key, value in element_group.items() if key != "children"
        }
    return state


def detect_conflicts(
    base_raw: dict[str, Any],
    remote_raw: dict[str, Any],
    requests: list[dict[str, Any]],
    id_mapping: dict[str, str],
) -> list[tuple[str, str]]:
    """Find touched objects that changed remotely since the pristine base."""
    base_elements, base_page_ids, base_parents = index_presentation(base_raw)
    remote_elements, remote_page_ids, _ = index_presentation(remote_raw)
    reverse_mapping = {google: clean for clean, google in id_mapping.items()}

    object_ids, page_ids = collect_request_object_ids(requests)
    conflicts: list[tuple[str, str]] = []
    conflicting_ids: set[str] = set()

    def add_conflict(object_id: str, description: str) -> None:
        if object_id in conflicting_ids:
            return
        conflicting_ids.add(object_id)
        clean_id = reverse_mapping.get(object_id, object_id)
        conflicts.append((clean_id, description))

    for object_id in sorted(object_ids):
        base_element = base_elements.get(object_id)
        if base_element is None:
            continue
        remote_element = remote_elements.get(object_id)
        if remote_element is None:
            add_conflict(object_id, "deleted remotely")
            continue
        kind = _classify_element_change(base_element, remote_element)
        if kind:
            add_conflict(object_id, f"{kind} changed remotely")

        ancestor_id = base_parents.get(object_id)
        while ancestor_id is not None:
            base_ancestor = base_elements.get(ancestor_id)
            remote_ancestor = remote_elements.get(ancestor_id)
            if base_ancestor is None:
                break
            if remote_ancestor is None:
                add_conflict(ancestor_id, "deleted remotely")
            else:
                ancestor_kind = _classify_element_change(
                    _ancestor_state(base_ancestor),
                    _ancestor_state(remote_ancestor),
                )
                if ancestor_kind:
                    add_conflict(
                        ancestor_id, f"{ancestor_kind} changed remotely"
                    )
            ancestor_id = base_parents.get(ancestor_id)

    for page_id in sorted(page_ids):
        if page_id in base_page_ids and page_id not in remote_page_ids:
            clean_id = reverse_mapping.get(page_id, page_id)
            conflicts.append((clean_id, "target slide deleted remotely"))

    return conflicts


def ensure_no_conflicts(
    base_raw: dict[str, Any],
    remote_raw: dict[str, Any],
    requests: list[dict[str, Any]],
    id_mapping: dict[str, str],
) -> None:
    """Raise a user-facing error if a guarded push has remote conflicts."""
    conflicts = detect_conflicts(base_raw, remote_raw, requests, id_mapping)
    if not conflicts:
        return

    lines = [
        f"push aborted: {len(conflicts)} element(s) this push "
        "would modify changed in Google Slides since the pull:"
    ]
    lines += [f"  - {clean_id}: {kind}" for clean_id, kind in conflicts]
    lines.append(
        "Re-pull the deck, re-apply your edits, then push again "
        "(or push --force to overwrite the remote edits)."
    )
    raise ConflictError("\n".join(lines), conflicts=conflicts)
