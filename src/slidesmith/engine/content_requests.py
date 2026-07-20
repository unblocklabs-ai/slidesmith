"""Orchestrate Google Slides API requests from diff changes.

Detailed request construction lives in focused sibling modules; this module owns
batch ordering, delete hierarchy handling, and object-ID allocation.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from typing import Any

from slidesmith.engine.class_style_requests import _create_class_style_requests
from slidesmith.engine.content_diff import Change, ChangeType, DiffResult, ParagraphClassUpdate
from slidesmith.engine.copy_requests import _create_copy_requests, _uses_duplicate_object
from slidesmith.engine.element_factories import (
    _create_element_requests,
    _create_move_request,
    _create_slide_request,
)
from slidesmith.engine.id_manager import is_valid_google_object_id
from slidesmith.engine.text_requests import (
    _create_paragraph_class_update_requests,
    _create_run_style_requests,
    _create_text_update_requests,
)

_BATCH_CHANGE_TYPES = (
    ChangeType.DELETE,
    ChangeType.MOVE,
    ChangeType.TEXT_UPDATE,
    ChangeType.STYLE_UPDATE,
    ChangeType.PARAGRAPH_STYLE_UPDATE,
    ChangeType.COPY,
    ChangeType.CREATE,
)


class IdAllocator:
    """Allocate deterministic per-batch suffixes without module-global state."""

    def __init__(self) -> None:
        self._next_suffix = 1
        self._lock = threading.Lock()

    def unique_suffix(self) -> str:
        """Return the next suffix safely when an allocator is shared by threads."""
        with self._lock:
            suffix = self._next_suffix
            self._next_suffix += 1
        return str(suffix)


def _allocate_create_object_id(authored_id: str, reserved_ids: set[str]) -> str:
    """Choose a valid, unoccupied Google object ID for an authored element."""
    if is_valid_google_object_id(authored_id) and authored_id not in reserved_ids:
        return authored_id

    stem = re.sub(r"[^a-zA-Z0-9_-]", "_", authored_id)
    if not stem or not re.match(r"^[a-zA-Z_]", stem):
        stem = f"new_{stem}"
    if len(stem) < 5:
        stem = f"new_{stem}"

    suffix_number = 2
    while True:
        suffix = f"_{suffix_number}"
        candidate = f"{stem[: 50 - len(suffix)]}{suffix}"
        if is_valid_google_object_id(candidate) and candidate not in reserved_ids:
            return candidate
        suffix_number += 1


def _bucket_changes(changes: list[Change]) -> dict[ChangeType, list[Change]]:
    """Group supported changes by type while preserving their input order."""
    buckets = {change_type: [] for change_type in _BATCH_CHANGE_TYPES}
    for change in changes:
        if change.change_type in buckets:
            buckets[change.change_type].append(change)
    return buckets


def _emit_new_slide_requests(
    requests: list[dict[str, Any]],
    copies: list[Change],
    creates: list[Change],
    slide_ids: dict[str, str],
    reserved_object_ids: set[str],
    unique_suffix: Callable[[], str],
) -> None:
    """Create missing target slides before emitting their element requests."""
    new_slide_indices = {
        change.slide_index
        for change in copies + creates
        if change.slide_index and change.slide_index not in slide_ids
    }
    for slide_index in sorted(new_slide_indices):
        while True:
            suffix = unique_suffix()
            new_slide_id = f"new_slide_{slide_index}_{suffix}"
            if new_slide_id not in reserved_object_ids:
                break
        requests.append(_create_slide_request(new_slide_id))
        slide_ids[slide_index] = new_slide_id
        reserved_object_ids.add(new_slide_id)


def _emit_delete_requests(
    requests: list[dict[str, Any]],
    deletes: list[Change],
    id_mapping: dict[str, str],
    pristine_element_types: dict[str, str] | None,
    pristine_element_parents: dict[str, str | None] | None,
) -> None:
    """Emit safe, deterministic deleteObject requests."""
    delete_ids = {
        id_mapping.get(change.target_id)
        for change in deletes
        if id_mapping.get(change.target_id)
    }
    ordered_delete_ids = _order_deletes_for_safe_removal(
        delete_ids,
        pristine_element_types,
        pristine_element_parents,
    )
    for google_id in ordered_delete_ids:
        requests.append({"deleteObject": {"objectId": google_id}})


def _emit_move_requests(
    requests: list[dict[str, Any]],
    moves: list[Change],
    id_mapping: dict[str, str],
    diff_result: DiffResult,
) -> None:
    """Emit transform requests for moved elements."""
    for change in moves:
        move_google_id = id_mapping.get(change.target_id)
        if move_google_id and change.new_position:
            requests.append(
                _create_move_request(
                    move_google_id,
                    change.new_position,
                    diff_result.pristine_styles.get(change.target_id),
                    change.old_position,
                )
            )


def _emit_text_update_requests(
    requests: list[dict[str, Any]],
    text_updates: list[Change],
    id_mapping: dict[str, str],
) -> None:
    """Emit minimal text replacements and run-style updates."""
    for change in text_updates:
        text_google_id = id_mapping.get(change.target_id)
        if text_google_id and change.new_text is not None:
            requests.extend(
                _create_text_update_requests(
                    text_google_id,
                    change.new_text,
                    change.new_runs,
                    change.old_text,
                    change.old_runs,
                )
            )


def _emit_style_update_requests(
    requests: list[dict[str, Any]],
    style_updates: list[Change],
    id_mapping: dict[str, str],
    diff_result: DiffResult,
    pristine_element_types: dict[str, str] | None,
) -> None:
    """Emit class-derived updates for existing elements."""
    for change in style_updates:
        style_google_id = id_mapping.get(change.target_id)
        if not (style_google_id and change.new_styles):
            continue

        edited_element = diff_result.edited_elements.get(change.target_id)
        element_tag = change.tag
        if element_tag is None and edited_element is not None:
            element_tag = edited_element.tag
        if element_tag is None and pristine_element_types is not None:
            element_type = pristine_element_types.get(style_google_id)
            element_tag = "Line" if element_type == "LINE" else None
        requests.extend(
            _create_class_style_requests(
                style_google_id,
                change.new_styles,
                has_text=bool(change.new_text),
                element_tag=element_tag,
                text_style_reset_fields=change.text_style_reset_fields,
                paragraph_style_reset_fields=change.paragraph_style_reset_fields,
                stroke_reset_fields=change.stroke_reset_fields,
                reset_content_alignment=change.reset_content_alignment,
            )
        )
        if not (
            change.new_text
            and (
                change.new_styles.text_style is not None
                or change.new_styles.paragraph_style is not None
            )
        ):
            continue
        if change.new_paragraph_styles:
            paragraph_updates = [
                ParagraphClassUpdate(index, None, styles)
                for index, styles in enumerate(change.new_paragraph_styles)
                if styles is not None
            ]
            requests.extend(
                _create_paragraph_class_update_requests(
                    style_google_id,
                    change.new_text,
                    change.new_runs or [],
                    paragraph_updates,
                    reapply_runs=False,
                )
            )
        if change.new_styles.text_style is not None and change.new_runs:
            requests.extend(_create_run_style_requests(style_google_id, change.new_runs))


def _emit_paragraph_style_update_requests(
    requests: list[dict[str, Any]],
    paragraph_style_updates: list[Change],
    id_mapping: dict[str, str],
) -> None:
    """Emit paragraph-default updates before explicit run overrides."""
    for change in paragraph_style_updates:
        style_google_id = id_mapping.get(change.target_id)
        if (
            style_google_id
            and change.new_text is not None
            and change.paragraph_style_updates
        ):
            requests.extend(
                _create_paragraph_class_update_requests(
                    style_google_id,
                    change.new_text,
                    change.new_runs or [],
                    change.paragraph_style_updates,
                )
            )


def _emit_copy_requests(
    requests: list[dict[str, Any]],
    copies: list[Change],
    id_mapping: dict[str, str],
    slide_ids: dict[str, str],
    diff_result: DiffResult,
    reserved_object_ids: set[str],
    unique_suffix: Callable[[], str],
    pristine_element_types: dict[str, str] | None,
    pristine_element_parents: dict[str, str | None] | None,
) -> None:
    """Emit copies in the ordering selected by the batch orchestrator."""
    for change in copies:
        if not (change.source_id and change.slide_index):
            continue
        slide_google_id = slide_ids.get(change.slide_index)
        source_google_id = id_mapping.get(change.source_id)
        source_style = diff_result.pristine_styles.get(change.source_id, {})
        if slide_google_id and source_google_id:
            requests.extend(
                _create_copy_requests(
                    change,
                    source_style,
                    slide_google_id,
                    diff_result.pristine_styles,
                    reserved_object_ids,
                    source_google_id=source_google_id,
                    id_mapping=id_mapping,
                    allocate_object_id=_allocate_create_object_id,
                    unique_suffix=unique_suffix,
                    warnings=diff_result.warnings,
                    pristine_element_types=pristine_element_types,
                    pristine_element_parents=pristine_element_parents,
                )
            )


def _emit_create_requests(
    requests: list[dict[str, Any]],
    creates: list[Change],
    slide_ids: dict[str, str],
    reserved_object_ids: set[str],
) -> None:
    """Emit requests for newly authored elements."""
    for change in creates:
        if not change.slide_index:
            continue
        slide_google_id = slide_ids.get(change.slide_index)
        if slide_google_id:
            new_object_id = _allocate_create_object_id(
                change.target_id, reserved_object_ids
            )
            reserved_object_ids.add(new_object_id)
            requests.extend(
                _create_element_requests(change, slide_google_id, new_object_id)
            )


def generate_batch_requests(
    diff_result: DiffResult,
    id_mapping: dict[str, str],
    slide_id_mapping: dict[str, str],
    pristine_element_types: dict[str, str] | None = None,
    pristine_element_parents: dict[str, str | None] | None = None,
) -> list[dict[str, Any]]:
    """Generate ordered Google Slides batchUpdate requests from a diff."""
    requests: list[dict[str, Any]] = []
    slide_ids = dict(slide_id_mapping)
    reserved_object_ids = set(id_mapping.values()) | set(slide_ids.values())
    id_allocator = IdAllocator()
    buckets = _bucket_changes(diff_result.changes)
    duplicate_copies = [
        change
        for change in buckets[ChangeType.COPY]
        if _uses_duplicate_object(change)
    ]
    recreated_copies = [
        change
        for change in buckets[ChangeType.COPY]
        if not _uses_duplicate_object(change)
    ]

    _emit_new_slide_requests(
        requests,
        buckets[ChangeType.COPY],
        buckets[ChangeType.CREATE],
        slide_ids,
        reserved_object_ids,
        id_allocator.unique_suffix,
    )
    # duplicateObject must observe the pristine source subtree. Text/style
    # deltas for the copy are also based on that pristine state, and deleting
    # either child of a two-child group can collapse the source group entirely.
    _emit_copy_requests(
        requests,
        duplicate_copies,
        id_mapping,
        slide_ids,
        diff_result,
        reserved_object_ids,
        id_allocator.unique_suffix,
        pristine_element_types,
        pristine_element_parents,
    )
    _emit_delete_requests(
        requests,
        buckets[ChangeType.DELETE],
        id_mapping,
        pristine_element_types,
        pristine_element_parents,
    )
    _emit_move_requests(requests, buckets[ChangeType.MOVE], id_mapping, diff_result)
    _emit_text_update_requests(
        requests, buckets[ChangeType.TEXT_UPDATE], id_mapping
    )
    _emit_style_update_requests(
        requests,
        buckets[ChangeType.STYLE_UPDATE],
        id_mapping,
        diff_result,
        pristine_element_types,
    )
    _emit_paragraph_style_update_requests(
        requests,
        buckets[ChangeType.PARAGRAPH_STYLE_UPDATE],
        id_mapping,
    )
    _emit_copy_requests(
        requests,
        recreated_copies,
        id_mapping,
        slide_ids,
        diff_result,
        reserved_object_ids,
        id_allocator.unique_suffix,
        pristine_element_types,
        pristine_element_parents,
    )
    _emit_create_requests(
        requests,
        buckets[ChangeType.CREATE],
        slide_ids,
        reserved_object_ids,
    )
    return requests


def _order_deletes_for_safe_removal(
    delete_ids: set[str | None],
    pristine_element_types: dict[str, str] | None = None,
    pristine_element_parents: dict[str, str | None] | None = None,
) -> list[str]:
    """Return deterministic deletes with descendants of deleted groups omitted.

    Deleting a group removes its subtree. Emitting child deletes afterward can
    make the atomic Google batch fail because those object IDs no longer exist.
    Hierarchy comes exclusively from the pristine API tree; IDs are opaque.
    """
    valid_ids = {object_id for object_id in delete_ids if object_id is not None}
    types = pristine_element_types or {}
    parents = pristine_element_parents or {}

    def has_deleted_group_ancestor(object_id: str) -> bool:
        seen: set[str] = set()
        parent_id = parents.get(object_id)
        while parent_id and parent_id not in seen:
            if parent_id in valid_ids and types.get(parent_id) == "GROUP":
                return True
            seen.add(parent_id)
            parent_id = parents.get(parent_id)
        return False

    return sorted(
        object_id
        for object_id in valid_ids
        if not has_deleted_group_ancestor(object_id)
    )
