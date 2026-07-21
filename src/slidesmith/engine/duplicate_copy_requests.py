"""Build duplicateObject requests for copies that contain dynamic autoText."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from slidesmith.engine.content_diff import Change, ParagraphClassUpdate
from slidesmith.engine.content_parser import ParagraphStyles, ParsedRun
from slidesmith.engine.element_factories import _create_move_request
from slidesmith.engine.hierarchy import has_ancestor_in_set
from slidesmith.engine.text_requests import (
    _create_paragraph_class_update_requests,
    _create_text_update_requests,
)


def _create_duplicate_copy_requests(
    change: Change,
    source_style: dict[str, Any],
    source_google_id: str | None,
    new_object_id: str,
    position: dict[str, float],
    reserved_ids: set[str],
    *,
    id_mapping: dict[str, str] | None,
    allocate_object_id: Callable[[str, set[str]], str],
    pristine_element_types: dict[str, str] | None,
    pristine_element_parents: dict[str, str | None] | None,
    warnings: list[str],
) -> list[dict[str, Any]]:
    if source_google_id is None:
        raise ValueError(
            f"Cannot preserve autoText on copy '{change.source_id}': "
            "source Google object ID is missing"
        )
    if (
        change.source_slide_index is not None
        and change.source_slide_index != change.slide_index
    ):
        raise ValueError(
            f"Cannot preserve autoText on cross-slide copy '{change.source_id}': "
            "the Slides API can only duplicate a page element on its source slide"
        )
    object_ids = {source_google_id: new_object_id}
    removed_descendant_ids = _map_duplicate_descendants(
        source_google_id,
        change.children or [],
        id_mapping or {},
        object_ids,
        new_object_id,
        reserved_ids,
        allocate_object_id,
        pristine_element_types or {},
        pristine_element_parents or {},
    )
    requests: list[dict[str, Any]] = [
        {
            "duplicateObject": {
                "objectId": source_google_id,
                "objectIds": object_ids,
            }
        }
    ]
    requests.append(
        _create_move_request(
            new_object_id,
            position,
            source_style,
            change.old_position,
        )
    )
    if change.new_text != change.old_text or change.new_runs != change.old_runs:
        requests.extend(
            _create_text_update_requests(
                new_object_id,
                change.new_text or [],
                change.new_runs,
                change.old_text,
                change.old_runs,
            )
        )
    requests.extend(
        _duplicate_paragraph_style_requests(
            new_object_id,
            change.new_text or [],
            change.new_runs or [],
            change.old_paragraph_styles or [],
            change.new_paragraph_styles or [],
        )
    )
    _apply_duplicate_descendant_edits(
        change.children or [],
        id_mapping or {},
        object_ids,
        requests,
        change.translation or {"dx": 0, "dy": 0},
        change.source_id or change.target_id,
        warnings,
    )
    requests.extend(
        {"deleteObject": {"objectId": object_id}}
        for object_id in removed_descendant_ids
    )
    return requests


def _map_duplicate_descendants(
    source_google_id: str,
    children: list[dict[str, Any]],
    id_mapping: dict[str, str],
    object_ids: dict[str, str],
    id_prefix: str,
    reserved_ids: set[str],
    allocate_object_id: Callable[[str, set[str]], str],
    pristine_element_types: dict[str, str],
    pristine_element_parents: dict[str, str | None],
) -> list[str]:
    """Map the pristine source subtree and return removed copied descendants."""
    authored_source_ids: set[str] = set()

    def collect_authored(authored_children: list[dict[str, Any]]) -> None:
        for child in authored_children:
            clean_id = str(child.get("id", ""))
            child_source_id = id_mapping.get(clean_id)
            if child_source_id is None:
                raise ValueError(
                    f"Cannot preserve edits on copied child '{clean_id}': "
                    "source Google object ID is missing"
                )
            authored_source_ids.add(child_source_id)
            collect_authored(child.get("children", []))

    collect_authored(children)

    pristine_children: dict[str, list[str]] = {}
    for child_source_id, parent_source_id in pristine_element_parents.items():
        if parent_source_id is not None:
            pristine_children.setdefault(parent_source_id, []).append(child_source_id)

    mapped_source_ids: set[str] = set()

    def map_pristine_children(
        parent_source_id: str,
        parent_new_id: str,
        depth: int,
    ) -> None:
        for index, child_source_id in enumerate(
            sorted(pristine_children.get(parent_source_id, []))
        ):
            new_object_id = allocate_object_id(
                f"{parent_new_id}_c{depth}_{index}", reserved_ids
            )
            reserved_ids.add(new_object_id)
            object_ids[child_source_id] = new_object_id
            mapped_source_ids.add(child_source_id)
            map_pristine_children(child_source_id, new_object_id, depth + 1)

    map_pristine_children(source_google_id, id_prefix, 0)

    # Old workspaces can lack the raw pristine tree. Preserve the previous
    # authored-descendant mapping behavior as a compatibility fallback.
    def map_authored_children(
        authored_children: list[dict[str, Any]],
        parent_new_id: str,
        depth: int,
    ) -> None:
        for index, child in enumerate(authored_children):
            clean_id = str(child.get("id", ""))
            child_source_id = id_mapping.get(clean_id)
            if child_source_id is None:
                raise ValueError(
                    f"Cannot preserve edits on copied child '{clean_id}': "
                    "source Google object ID is missing"
                )
            if child_source_id in object_ids:
                child_new_id = object_ids[child_source_id]
            else:
                child_new_id = allocate_object_id(
                    f"{parent_new_id}_c{depth}_{index}", reserved_ids
                )
                reserved_ids.add(child_new_id)
                object_ids[child_source_id] = child_new_id
            mapped_source_ids.add(child_source_id)
            map_authored_children(
                child.get("children", []), child_new_id, depth + 1
            )

    map_authored_children(children, id_prefix, 0)

    removed_source_ids = mapped_source_ids - authored_source_ids

    return [
        object_ids[child_source_id]
        for child_source_id in sorted(removed_source_ids)
        if not has_ancestor_in_set(
            child_source_id,
            removed_source_ids,
            pristine_element_parents,
            pristine_element_types,
        )
    ]


def _duplicate_paragraph_style_requests(
    object_id: str,
    text: list[str],
    runs: list[list[ParsedRun]],
    old_styles: list[ParagraphStyles | None],
    new_styles: list[ParagraphStyles | None],
) -> list[dict[str, Any]]:
    """Apply paragraph defaults changed on a duplicated text element."""
    updates = [
        ParagraphClassUpdate(index, old_style, new_style)
        for index, (old_style, new_style) in enumerate(
            zip(old_styles, new_styles, strict=False)
        )
        if old_style != new_style
    ]
    if len(new_styles) > len(old_styles):
        updates.extend(
            ParagraphClassUpdate(index, None, new_styles[index])
            for index in range(len(old_styles), len(new_styles))
            if new_styles[index] is not None
        )
    if not updates:
        return []
    return _create_paragraph_class_update_requests(
        object_id,
        text,
        runs,
        updates,
    )


def _apply_duplicate_descendant_edits(
    children: list[dict[str, Any]],
    id_mapping: dict[str, str],
    object_ids: dict[str, str],
    requests: list[dict[str, Any]],
    translation: dict[str, float],
    copy_source_id: str,
    warnings: list[str],
) -> None:
    """Replay authored descendant deltas and report positional ambiguity."""
    for child in children:
        clean_id = str(child.get("id", ""))
        source_google_id = id_mapping.get(clean_id)
        new_object_id = object_ids.get(source_google_id or "")
        if new_object_id is None:
            raise ValueError(
                f"Cannot preserve edits on copied child '{clean_id}': "
                "duplicateObject descendant mapping is missing"
            )
        _warn_for_ambiguous_child_position(
            child,
            translation,
            copy_source_id,
            warnings,
        )
        new_text = child.get("text", [])
        new_runs = child.get("runs", [])
        old_text = child.get("sourceText", [])
        old_runs = child.get("sourceRuns", [])
        if new_text != old_text or new_runs != old_runs:
            requests.extend(
                _create_text_update_requests(
                    new_object_id,
                    new_text,
                    new_runs,
                    old_text,
                    old_runs,
                )
            )
        requests.extend(
            _duplicate_paragraph_style_requests(
                new_object_id,
                new_text,
                new_runs,
                child.get("sourceParagraphStyles", []),
                child.get("paragraphStyles", []),
            )
        )
        _apply_duplicate_descendant_edits(
            child.get("children", []),
            id_mapping,
            object_ids,
            requests,
            translation,
            copy_source_id,
            warnings,
        )


def _contains_auto_text(
    runs: list[list[ParsedRun]] | None,
    children: list[dict[str, Any]] | None = None,
) -> bool:
    """Return whether copied root or descendant text contains dynamic autoText."""
    if any(run.auto_text_type for paragraph in runs or [] for run in paragraph):
        return True
    return any(
        _contains_auto_text(child.get("runs"), child.get("children"))
        for child in children or []
    )


def _uses_duplicate_object(change: Change) -> bool:
    """Return whether a copy must preserve dynamic autoText by duplication."""
    return _contains_auto_text(change.new_runs, change.children)


def _warn_for_ambiguous_child_position(
    child: dict[str, Any],
    translation: dict[str, float],
    copy_source_id: str,
    warnings: list[str],
) -> None:
    """Apply the R3-7 warning contract to either copy implementation path."""
    position = child.get("position", {})
    source_position = child.get("sourcePosition", {})
    if not (position and source_position):
        return

    expected_final_x = source_position.get("x", 0) + translation.get("dx", 0)
    expected_final_y = source_position.get("y", 0) + translation.get("dy", 0)
    matches_source = (
        abs(position.get("x", 0) - source_position.get("x", 0)) <= 0.01
        and abs(position.get("y", 0) - source_position.get("y", 0)) <= 0.01
    )
    matches_translation = (
        abs(position.get("x", 0) - expected_final_x) <= 0.01
        and abs(position.get("y", 0) - expected_final_y) <= 0.01
    )
    if matches_source or matches_translation:
        return

    warnings.append(
        f"copy '{copy_source_id}' child '{child.get('id', '')}': "
        f"authored position ({_format_number(position.get('x', 0))}, "
        f"{_format_number(position.get('y', 0))}) matches neither "
        f"the source position ({_format_number(source_position.get('x', 0))}, "
        f"{_format_number(source_position.get('y', 0))}) nor the translated "
        f"copy position ({_format_number(expected_final_x)}, "
        f"{_format_number(expected_final_y)}); Slidesmith applied the parent "
        "translation, so verify the copied child position"
    )


def _format_number(value: Any) -> str:
    """Format warning coordinates without noisy integral decimal suffixes."""
    number = float(value)
    return f"{number:g}"
