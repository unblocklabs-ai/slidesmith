"""Shared helpers for walking page-element hierarchies."""

from __future__ import annotations

from collections.abc import Collection, Mapping


def has_ancestor_in_set(
    object_id: str,
    candidates: Collection[str],
    parents: Mapping[str, str | None],
    types: Mapping[str, str],
    required_type: str = "GROUP",
) -> bool:
    """Return whether an ancestor is a candidate of the required type."""
    seen: set[str] = set()
    parent_id = parents.get(object_id)
    while parent_id and parent_id not in seen:
        if parent_id in candidates and types.get(parent_id) == required_type:
            return True
        seen.add(parent_id)
        parent_id = parents.get(parent_id)
    return False
