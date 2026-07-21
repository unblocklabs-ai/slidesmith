"""Human-readable diff summary rendering."""

from __future__ import annotations

from slidesmith.engine.content_parser import ElementStyles
from slidesmith.engine.diff_model import Change, ChangeType, DiffResult


def format_diff_summary(diff_result: DiffResult, request_count: int) -> str:
    """Render a compact, slide-grouped description of semantic changes."""
    changes_by_slide: dict[str, list[Change]] = {}
    for change in diff_result.changes:
        changes_by_slide.setdefault(change.slide_index or "?", []).append(change)

    lines: list[str] = []
    for slide_index in sorted(changes_by_slide, key=_slide_sort_key):
        changes = changes_by_slide[slide_index]
        lines.append(f"Slide {slide_index}")

        deleted_ids = [
            change.target_id
            for change in changes
            if change.change_type == ChangeType.DELETE
        ]
        if deleted_ids:
            lines.append(f"  DELETE {', '.join(deleted_ids)}")

        for change_type in (
            ChangeType.CREATE,
            ChangeType.MOVE,
            ChangeType.IMAGE_UPDATE,
            ChangeType.COPY,
            ChangeType.STYLE_UPDATE,
            ChangeType.PARAGRAPH_STYLE_UPDATE,
            ChangeType.TEXT_UPDATE,
        ):
            for change in changes:
                if change.change_type == change_type:
                    lines.append(f"  {_format_summary_change(change)}")

    if lines:
        lines.append("")
    lines.append(f"{request_count} requests total")
    return "\n".join(lines)


def _slide_sort_key(slide_index: str) -> tuple[int, int | str]:
    if slide_index.isdigit():
        return (0, int(slide_index))
    return (1, slide_index)


def _format_summary_change(change: Change) -> str:
    if change.change_type == ChangeType.CREATE:
        tag = change.tag or "Element"
        details = f" ({tag}{_format_frame(change.new_position)})"
        additions: list[str] = []
        if change.new_styles is not None:
            if change.new_styles.fill is not None:
                additions.append("+fill")
            if change.new_styles.stroke is not None:
                additions.append("+stroke")
        if change.new_text:
            count = len(change.new_text)
            noun = "paragraph" if count == 1 else "paragraphs"
            additions.append(f"+{count} {noun}")
        suffix = f" {' '.join(additions)}" if additions else ""
        return f"CREATE {change.target_id}{details}{suffix}"

    if change.change_type == ChangeType.MOVE:
        return f"MOVE {change.target_id}{_format_frame(change.new_position)}"

    if change.change_type == ChangeType.IMAGE_UPDATE:
        return f"IMAGE {change.target_id}: replace src={change.src!r} fit={change.fit}"

    if change.change_type == ChangeType.COPY:
        source_id = change.source_id or change.target_id
        return f"COPY {source_id} -> {change.target_id}{_format_frame(change.new_position)}"

    if change.change_type == ChangeType.STYLE_UPDATE:
        return f"STYLE {change.target_id}: {_format_style_delta(change.new_styles)}"

    if change.change_type == ChangeType.PARAGRAPH_STYLE_UPDATE:
        count = len(change.paragraph_style_updates or [])
        noun = "edit" if count == 1 else "edits"
        return f"STYLE {change.target_id}: {count} paragraph range {noun}"

    if change.change_type == ChangeType.TEXT_UPDATE:
        return f"TEXT {change.target_id}: 1 range edit"

    return f"{change.change_type.value.upper()} {change.target_id}"


def _format_frame(position: dict[str, float] | None) -> str:
    if not position:
        return ""
    x = _format_number(position.get("x"))
    y = _format_number(position.get("y"))
    if position.get("w") is None or position.get("h") is None:
        return f" @{x},{y}"
    width = _format_number(position.get("w"))
    height = _format_number(position.get("h"))
    return f" {width}x{height} @{x},{y}"


def _format_number(value: float | None) -> str:
    if value is None:
        return "?"
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _format_style_delta(styles: ElementStyles | None) -> str:
    if styles is None:
        return "style"
    parts: list[str] = []
    if styles.fill is not None:
        parts.append("fill")
    if styles.stroke is not None:
        parts.append("stroke")
    if styles.text_style is not None:
        parts.append("textStyle")
    if styles.paragraph_style is not None:
        parts.append("paragraphStyle")
    if styles.content_alignment is not None:
        value = getattr(styles.content_alignment, "value", styles.content_alignment)
        parts.append(f"contentAlignment {value}")
    return ", ".join(parts) or "style"
