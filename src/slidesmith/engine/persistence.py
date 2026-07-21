"""Post-push persistence verification and Google default normalization."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from slidesmith.engine.content_diff import Change, ChangeType, diff_presentation
from slidesmith.engine.content_parser import (
    ParsedElement,
    ParsedRun,
    flatten_elements,
)
from slidesmith.engine.shape_types import TAG_TO_TYPE, VALID_GOOGLE_TYPES


PERSISTENCE_GEOMETRY_TOLERANCE_PT = 0.02
GOOGLE_DEFAULT_TEXT_LAYOUT_CLASSES = frozenset(
    {
        "content-align-top",
        "content-align-middle",
        "text-align-left",
        "leading-100",
        "space-above-0",
        "space-below-0",
        "indent-start-0",
        "indent-first-0",
        "spacing-never-collapse",
        "spacing-collapse-lists",
        "font-weight-400",
    }
)


def _index_parsed_elements(
    slides: dict[str, list[Any]],
) -> dict[tuple[str, str], ParsedElement]:
    """Index refreshed or intended SML elements by slide and clean ID."""
    return {
        (slide_index, clean_id): element
        for slide_index, roots in slides.items()
        for clean_id, element in flatten_elements(roots).items()
    }


def _format_geometry(position: dict[str, float] | None) -> str:
    if position is None:
        return ""
    return ", ".join(
        f"{field}={position[field]:g}"
        for field in ("x", "y", "w", "h")
        if field in position
    )


def _format_run_style_classes(runs: list[list[ParsedRun]] | None) -> str:
    if not runs:
        return "(none)"
    values = [
        " ".join(run.text_style.to_classes()) if run.text_style is not None else ""
        for paragraph in runs
        for run in paragraph
    ]
    return " | ".join(values) if any(values) else "(none)"


def _format_paragraph_style_classes(change: Change, *, remote: bool) -> str:
    values: list[str] = []
    for update in change.paragraph_style_updates or []:
        styles = update.old_styles if remote else update.new_styles
        classes: list[str] = []
        if styles is not None:
            if styles.text_style is not None:
                classes.extend(styles.text_style.to_classes())
            if styles.paragraph_style is not None:
                classes.extend(styles.paragraph_style.to_classes())
        values.append(f"P{update.paragraph_index + 1}={' '.join(classes) or '(none)'}")
    return "; ".join(values)


def _format_changed_element_style_classes(
    change: Change,
    element: ParsedElement,
) -> str:
    styles = element.styles
    if styles is None:
        return "(none)"
    changed = change.new_styles
    classes: list[str] = []
    if changed is not None and changed.fill is not None and styles.fill is not None:
        fill_class = styles.fill.to_class()
        if fill_class:
            classes.append(fill_class)
    if (
        changed is not None and changed.stroke is not None
    ) or change.stroke_reset_fields:
        if styles.stroke is not None:
            classes.extend(styles.stroke.to_classes())
    if (
        changed is not None and changed.text_style is not None
    ) or change.text_style_reset_fields:
        if styles.text_style is not None:
            classes.extend(styles.text_style.to_classes())
    if (
        changed is not None and changed.paragraph_style is not None
    ) or change.paragraph_style_reset_fields:
        if styles.paragraph_style is not None:
            classes.extend(styles.paragraph_style.to_classes())
    if (
        (changed is not None and changed.content_alignment is not None)
        or change.reset_content_alignment
    ) and styles.content_alignment is not None:
        classes.append(styles.content_alignment.to_class())
    return " ".join(classes) or "(none)"


def _normalized_persistence_detail(
    change: Change,
    remote_elements: dict[tuple[str, str], ParsedElement],
    intended_elements: dict[tuple[str, str], ParsedElement],
) -> str | None:
    """Describe sent and refreshed values when both are cheaply available."""
    if change.change_type == ChangeType.MOVE:
        sent = _format_geometry(change.new_position)
        remote = _format_geometry(change.old_position)
        if sent and remote:
            return (
                f"geometry on {change.target_id} did not persist "
                f"(sent {sent!r}, remote now {remote!r})"
            )

    if change.change_type == ChangeType.TEXT_UPDATE:
        sent_text = "\n".join(change.new_text or [])
        remote_text = "\n".join(change.old_text or [])
        if change.new_text != change.old_text:
            return (
                f"text on {change.target_id} did not persist "
                f"(sent {sent_text!r}, remote now {remote_text!r})"
            )
        sent_styles = _format_run_style_classes(change.new_runs)
        remote_styles = _format_run_style_classes(change.old_runs)
        return (
            f"text run style classes on {change.target_id} did not persist "
            f"(sent {sent_styles!r}, remote now {remote_styles!r})"
        )

    if change.change_type == ChangeType.PARAGRAPH_STYLE_UPDATE:
        sent = _format_paragraph_style_classes(change, remote=False)
        remote = _format_paragraph_style_classes(change, remote=True)
        if sent and remote:
            return (
                f"paragraph style classes on {change.target_id} did not persist "
                f"(sent {sent!r}, remote now {remote!r})"
            )

    if change.change_type == ChangeType.STYLE_UPDATE:
        key = (change.slide_index or "", change.target_id)
        remote_element = remote_elements.get(key)
        intended_element = intended_elements.get(key)
        if remote_element is not None and intended_element is not None:
            sent = _format_changed_element_style_classes(change, intended_element)
            remote = _format_changed_element_style_classes(change, remote_element)
            return (
                f"style classes on {change.target_id} did not persist "
                f"(sent {sent!r}, remote now {remote!r})"
            )

    return None


def _is_normalized_persistence_change(
    change: Change,
    remote_elements: dict[tuple[str, str], ParsedElement],
    intended_elements: dict[tuple[str, str], ParsedElement],
    *,
    newly_created: bool,
) -> bool:
    """Return whether a refresh difference is known Google normalization."""
    if change.change_type == ChangeType.MOVE:
        old = change.old_position
        new = change.new_position
        if old is None or new is None or set(old) != set(new):
            return False
        return all(
            abs(float(new[key]) - float(old[key]))
            < PERSISTENCE_GEOMETRY_TOLERANCE_PT
            for key in new
        )

    if not newly_created:
        return False

    if change.change_type == ChangeType.STYLE_UPDATE:
        key = (change.slide_index or "", change.target_id)
        remote_element = remote_elements.get(key)
        intended_element = intended_elements.get(key)
        if remote_element is None or intended_element is None:
            return False
        sent = _format_changed_element_style_classes(change, intended_element)
        remote = _format_changed_element_style_classes(change, remote_element)
        return _only_google_default_class_additions(
            sent,
            remote,
            remote_element,
        )

    if change.change_type == ChangeType.PARAGRAPH_STYLE_UPDATE:
        for update in change.paragraph_style_updates or []:
            sent = _paragraph_style_classes(update.new_styles)
            remote = _paragraph_style_classes(update.old_styles)
            if not _only_google_default_class_additions(sent, remote):
                return False
        return bool(change.paragraph_style_updates)

    if change.change_type == ChangeType.TEXT_UPDATE:
        return (
            change.new_text == change.old_text
            and _runs_only_gain_google_defaults(change.new_runs, change.old_runs)
        )

    return False


def _only_google_default_class_additions(
    sent: str | set[str],
    remote: str | set[str],
    element: ParsedElement | None = None,
) -> bool:
    sent_classes = (
        set()
        if sent == "(none)"
        else set(sent.split() if isinstance(sent, str) else sent)
    )
    remote_classes = (
        set()
        if remote == "(none)"
        else set(remote.split() if isinstance(remote, str) else remote)
    )
    added = remote_classes - sent_classes
    if not added or not sent_classes <= remote_classes:
        return False
    if not added <= GOOGLE_DEFAULT_TEXT_LAYOUT_CLASSES:
        return False
    if "content-align-top" in added:
        if element is None or TAG_TO_TYPE.get(element.tag) != "TEXT_BOX":
            return False
    if "content-align-middle" in added:
        element_type = TAG_TO_TYPE.get(element.tag) if element is not None else None
        if element_type not in VALID_GOOGLE_TYPES or element_type == "TEXT_BOX":
            return False
    return True


def _runs_only_gain_google_defaults(
    sent: list[list[ParsedRun]] | None,
    remote: list[list[ParsedRun]] | None,
) -> bool:
    if sent is None or remote is None or len(sent) != len(remote):
        return False
    saw_default = False
    for sent_paragraph, remote_paragraph in zip(sent, remote, strict=True):
        if len(sent_paragraph) != len(remote_paragraph):
            return False
        for sent_run, remote_run in zip(
            sent_paragraph, remote_paragraph, strict=True
        ):
            if (
                sent_run.text != remote_run.text
                or sent_run.auto_text_type != remote_run.auto_text_type
            ):
                return False
            sent_classes = (
                set(sent_run.text_style.to_classes())
                if sent_run.text_style is not None
                else set()
            )
            remote_classes = (
                set(remote_run.text_style.to_classes())
                if remote_run.text_style is not None
                else set()
            )
            if sent_classes == remote_classes:
                continue
            if not _only_google_default_class_additions(sent_classes, remote_classes):
                return False
            saw_default = True
    return saw_default


def _paragraph_style_classes(styles: Any) -> set[str]:
    if styles is None:
        return set()
    classes: set[str] = set()
    if styles.text_style is not None:
        classes.update(styles.text_style.to_classes())
    if styles.paragraph_style is not None:
        classes.update(styles.paragraph_style.to_classes())
    return classes


def append_persistence_warning(
    folder_path: Path,
    intended_slides: dict[str, list[Any]],
    intended_change_keys: set[tuple[str, ChangeType]],
    create_copy_targets: set[tuple[str, str]],
    response: dict[str, Any],
    *,
    read_pristine: Callable[
        [Path], tuple[dict[str, list[Any]], dict[str, dict[str, Any]]]
    ],
) -> None:
    """Warn when pushed semantic changes differ from refreshed truth."""
    refreshed_slides, refreshed_styles = read_pristine(folder_path)
    divergence = diff_presentation(
        refreshed_slides,
        intended_slides,
        refreshed_styles,
        workspace_root=folder_path,
        allow_remote_image_fetch=True,
    )
    unpersisted = [
        change
        for change in divergence.changes
        if (change.target_id, change.change_type) in intended_change_keys
        or (change.slide_index or "", change.target_id) in create_copy_targets
    ]
    remote_elements = _index_parsed_elements(refreshed_slides)
    intended_elements = _index_parsed_elements(intended_slides)
    meaningful = [
        change
        for change in unpersisted
        if not _is_normalized_persistence_change(
            change,
            remote_elements,
            intended_elements,
            newly_created=(
                change.slide_index or "", change.target_id
            )
            in create_copy_targets,
        )
    ]
    if not meaningful:
        return

    changes = sorted(
        meaningful,
        key=lambda change: (
            change.slide_index or "",
            change.target_id,
            change.change_type.value,
        ),
    )
    details = ", ".join(
        _normalized_persistence_detail(
            change,
            remote_elements,
            intended_elements,
        )
        or f"{change.target_id} ({change.change_type.value.replace('_', ' ')})"
        for change in changes
    )
    response.setdefault("warnings", []).append(
        f"{len(changes)} change(s) did not persist remotely: {details} "
        "— the API may not support these values"
    )


__all__ = [
    "GOOGLE_DEFAULT_TEXT_LAYOUT_CLASSES",
    "PERSISTENCE_GEOMETRY_TOLERANCE_PT",
    "_format_changed_element_style_classes",
    "_format_geometry",
    "_format_paragraph_style_classes",
    "_format_run_style_classes",
    "_index_parsed_elements",
    "_is_normalized_persistence_change",
    "_normalized_persistence_detail",
    "_only_google_default_class_additions",
    "_paragraph_style_classes",
    "_runs_only_gain_google_defaults",
    "append_persistence_warning",
]
