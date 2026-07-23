"""Post-push persistence verification and Google default normalization."""

from __future__ import annotations

from collections.abc import Callable
import math
from pathlib import Path
from typing import Any

from slidesmith.engine.content_diff import Change, ChangeType, diff_presentation
from slidesmith.engine.content_parser import (
    ParsedElement,
    ParsedRun,
    flatten_elements,
)
from slidesmith.engine.assets import image_source_kind
from slidesmith.engine.diff_model import PushWarning, WarningSeverity
from slidesmith.engine.image_fetch import redact_image_url
from slidesmith.engine.json_utils import read_json
from slidesmith.engine.persistence_styles import (
    ALL_TEXT_PROPERTY_KEYS,
    EffectiveTextSpan,
    PARAGRAPH_STYLE_PROPERTY_KEYS,
    TEXT_STYLE_PROPERTY_KEYS,
    _text_and_paragraph_classes,
    api_style_property_keys,
    effective_text_style_ranges,
    effective_text_styles_equivalent,
    paragraph_style_property_keys,
    text_style_property_keys,
)
from slidesmith.engine.shape_types import TAG_TO_TYPE, VALID_GOOGLE_TYPES


PERSISTENCE_GEOMETRY_TOLERANCE_PT = 0.02
# Crop offsets are normalized fractions.  A 2.5e-4 bound permits the small
# float jitter observed in Slides responses, while catching a meaningful shift
# between opposing offsets instead of independently forgiving both sides.
PERSISTENCE_CROP_TOLERANCE = 2.5e-4
# Google cannot faithfully persist a line's sub-tenth-point cross-axis.
LINE_NEAR_DEGENERATE_AXIS_THRESHOLD_PT = 0.1
GOOGLE_DEFAULT_TEXT_LAYOUT_CLASSES = frozenset(
    {
        "content-align-top",
        "content-align-middle",
        "content-align-bottom",
        "text-align-left",
        "leading-100",
        "space-above-0",
        "space-below-0",
        "indent-start-0",
        "indent-first-0",
        "spacing-never-collapse",
        "spacing-collapse-lists",
        "font-weight-400",
        "font-weight-700",
        "font-family-arial",
    }
)
GOOGLE_DEFAULT_ELEMENT_STYLE_CLASSES = frozenset(
    {
        "fill-none",
        "stroke-none",
        "stroke-w-0.75",
        "content-align-top",
        "content-align-middle",
        "content-align-bottom",
    }
)
_FONT_FAMILY_WEIGHT_PROPERTY_KEYS = frozenset(
    {"text.font_family", "text.font_weight"}
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


def _remote_image_source_urls(folder_path: Path) -> dict[tuple[str, str], str]:
    """Read sourceUrl from the raw post-push refresh when Google returned it."""
    raw = read_json(folder_path / ".pristine" / "base.json", missing_ok=True)
    id_mapping = read_json(folder_path / "id_mapping.json", missing_ok=True)
    clean_ids = {google_id: clean_id for clean_id, google_id in id_mapping.items()}
    sources: dict[tuple[str, str], str] = {}

    def walk(elements: list[Any], slide_index: str) -> None:
        for element in elements:
            google_id = element.get("objectId")
            image = element.get("image")
            source_url = image.get("sourceUrl") if isinstance(image, dict) else None
            clean_id = clean_ids.get(google_id)
            if clean_id and isinstance(source_url, str) and source_url:
                sources[(slide_index, clean_id)] = source_url
            group = element.get("elementGroup")
            if isinstance(group, dict):
                walk(group.get("children", []), slide_index)

    for index, slide in enumerate(raw.get("slides", []), 1):
        if isinstance(slide, dict):
            walk(slide.get("pageElements", []), f"{index:02d}")
    return sources


def _remote_image_crop_properties(
    folder_path: Path,
) -> dict[tuple[str, str], dict[str, float]]:
    """Read refreshed read-only crop offsets for images that expose them."""
    raw = read_json(folder_path / ".pristine" / "base.json", missing_ok=True)
    id_mapping = read_json(folder_path / "id_mapping.json", missing_ok=True)
    clean_ids = {google_id: clean_id for clean_id, google_id in id_mapping.items()}
    crops: dict[tuple[str, str], dict[str, float]] = {}
    api_fields = {
        "left": "leftOffset",
        "right": "rightOffset",
        "top": "topOffset",
        "bottom": "bottomOffset",
    }

    def walk(elements: list[Any], slide_index: str) -> None:
        for element in elements:
            google_id = element.get("objectId")
            clean_id = clean_ids.get(google_id)
            image = element.get("image")
            image_properties = image.get("imageProperties") if isinstance(image, dict) else None
            crop = (
                image_properties.get("cropProperties")
                if isinstance(image_properties, dict)
                else None
            )
            if clean_id and isinstance(crop, dict):
                offsets: dict[str, float] = {}
                for name, api_name in api_fields.items():
                    value = crop.get(api_name, 0)
                    offsets[name] = (
                        float(value)
                        if isinstance(value, (int, float)) and math.isfinite(value)
                        else math.nan
                    )
                crops[(slide_index, clean_id)] = offsets
            group = element.get("elementGroup")
            if isinstance(group, dict):
                walk(group.get("children", []), slide_index)

    for index, slide in enumerate(raw.get("slides", []), 1):
        if isinstance(slide, dict):
            walk(slide.get("pageElements", []), f"{index:02d}")
    return crops


def _expected_center_crop(change: Change) -> dict[str, float] | None:
    """Calculate CENTER_CROP offsets when the replacement pixels are known."""
    if not change.new_position:
        return None
    if not change.image_pixel_width or not change.image_pixel_height:
        return None
    source_aspect = change.image_pixel_width / change.image_pixel_height
    frame_aspect = change.new_position["w"] / change.new_position["h"]
    if not math.isfinite(source_aspect) or source_aspect <= 0:
        return None
    if source_aspect > frame_aspect:
        horizontal = (1 - frame_aspect / source_aspect) / 2
        return {"left": horizontal, "right": horizontal, "top": 0, "bottom": 0}
    if source_aspect < frame_aspect:
        vertical = (1 - source_aspect / frame_aspect) / 2
        return {"left": 0, "right": 0, "top": vertical, "bottom": vertical}
    return {"left": 0, "right": 0, "top": 0, "bottom": 0}


def _crop_matches_centered(
    actual: dict[str, float], expected: dict[str, float] | None
) -> bool:
    fields = ("left", "right", "top", "bottom")
    if not all(math.isfinite(float(actual.get(field, math.nan))) for field in fields):
        return False
    left = float(actual["left"])
    right = float(actual["right"])
    top = float(actual["top"])
    bottom = float(actual["bottom"])
    # The tolerance is inclusive; pad by one float epsilon so a nominal
    # exactly-at-bound offset (0.12525 - 0.125 = 2.5000000000000002e-4) is
    # not rejected by binary rounding noise.
    bound = PERSISTENCE_CROP_TOLERANCE + 1e-12
    if abs(left - right) > bound or abs(top - bottom) > bound:
        return False
    if expected is not None:
        return (
            abs((left + right) / 2 - (expected["left"] + expected["right"]) / 2)
            <= bound
            and abs((top + bottom) / 2 - (expected["top"] + expected["bottom"]) / 2)
            <= bound
        )
    return True


def _cover_crop_is_exempt_for_local_create(
    change: Change, *, newly_created: bool
) -> bool:
    """Local cover creates already use an aspect-matched derived raster."""
    return (
        newly_created
        and change.fit == "cover"
        and change.src is not None
        and image_source_kind(change.src) == "local"
    )


def _format_crop(crop: dict[str, float]) -> str:
    return ", ".join(f"{field}={crop[field]:g}" for field in ("left", "right", "top", "bottom"))


def _format_geometry(position: dict[str, float] | None) -> str:
    if position is None:
        return ""
    return ", ".join(
        f"{field}={position[field]:g}"
        for field in ("x", "y", "w", "h")
        if field in position
    )


def _geometry_matches_within_tolerance(
    sent: dict[str, float] | None,
    remote: dict[str, float] | None,
    *,
    authored_tag: str | None = None,
) -> bool:
    """Compare effective geometry using the same per-axis MOVE tolerance."""
    if sent is None or remote is None or set(sent) != set(remote):
        return False
    ignored_axes = {
        axis
        for axis in ("w", "h")
        if authored_tag == "Line"
        and axis in sent
        and abs(float(sent[axis])) < LINE_NEAR_DEGENERATE_AXIS_THRESHOLD_PT
    }
    return all(
        key in ignored_axes
        or abs(float(sent[key]) - float(remote[key]))
        < PERSISTENCE_GEOMETRY_TOLERANCE_PT
        for key in sent
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


def _format_changed_element_non_text_style_classes(
    change: Change,
    element: ParsedElement,
) -> str:
    """Format only non-text groups for the persistence style comparison."""
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
        (changed is not None and changed.content_alignment is not None)
        or change.reset_content_alignment
    ) and styles.content_alignment is not None:
        classes.append(styles.content_alignment.to_class())
    return " ".join(classes) or "(none)"


def _format_changed_element_text_style_classes(
    change: Change,
    element: ParsedElement,
) -> str:
    """Format only text and paragraph groups for persistence comparison."""
    styles = element.styles
    if styles is None:
        return "(none)"
    changed = change.new_styles
    classes: list[str] = []
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
    return " ".join(classes) or "(none)"


def _changed_text_property_keys(change: Change) -> set[str]:
    """Return the effective text properties represented by one style change."""
    keys: set[str] = set()
    styles = change.new_styles
    if styles is not None:
        keys.update(text_style_property_keys(styles.text_style))
        keys.update(paragraph_style_property_keys(styles.paragraph_style))
    keys.update(api_style_property_keys(change.text_style_reset_fields))
    keys.update(api_style_property_keys(change.paragraph_style_reset_fields))
    keys &= ALL_TEXT_PROPERTY_KEYS
    if keys & _FONT_FAMILY_WEIGHT_PROPERTY_KEYS:
        keys.update(_FONT_FAMILY_WEIGHT_PROPERTY_KEYS)
    return keys


def _changed_paragraph_property_keys(change: Change) -> set[str]:
    """Return properties touched by all paragraph updates in a change."""
    keys: set[str] = set()
    for update in change.paragraph_style_updates or []:
        for styles in (update.old_styles, update.new_styles):
            if styles is None:
                continue
            keys.update(text_style_property_keys(styles.text_style))
            keys.update(paragraph_style_property_keys(styles.paragraph_style))
    keys &= ALL_TEXT_PROPERTY_KEYS
    if keys & _FONT_FAMILY_WEIGHT_PROPERTY_KEYS:
        keys.update(_FONT_FAMILY_WEIGHT_PROPERTY_KEYS)
    return keys


def _effective_text_styles_match(
    change: Change,
    remote_element: ParsedElement,
    intended_element: ParsedElement,
    *,
    author_removed_classes: frozenset[str] | set[str],
    span_cache: dict[int, list[EffectiveTextSpan] | None] | None = None,
    allow_created_roundrect_center_alignment: bool = False,
) -> bool:
    """Compare only the effective text properties touched by a divergence."""
    if change.change_type == ChangeType.TEXT_UPDATE:
        paragraph_ranges = effective_text_style_ranges(
            remote_element, intended_element
        )
        if paragraph_ranges is None:
            return False
        for range_item in paragraph_ranges:
            for key in PARAGRAPH_STYLE_PROPERTY_KEYS:
                if (
                    range_item.new_properties.get(key) is not None
                    and range_item.old_properties.get(key)
                    != range_item.new_properties.get(key)
                ):
                    return False
        property_keys: set[str] | None = set(TEXT_STYLE_PROPERTY_KEYS)
    elif change.change_type == ChangeType.PARAGRAPH_STYLE_UPDATE:
        property_keys = _changed_paragraph_property_keys(change)
    elif allow_created_roundrect_center_alignment:
        # This exemption is deliberately proof-based: once a created
        # RoundRect's injected center alignment is ignored, compare every
        # remaining effective property rather than only the changed class set.
        property_keys = None
    else:
        property_keys = _changed_text_property_keys(change)
    if property_keys is not None and not property_keys:
        return False
    return effective_text_styles_equivalent(
        remote_element,
        intended_element,
        author_removed_classes=author_removed_classes,
        property_keys=property_keys,
        include_symmetric_effective_difference=(
            bool(author_removed_classes)
            and change.change_type != ChangeType.TEXT_UPDATE
        ),
        span_cache=span_cache,
        allow_created_roundrect_center_alignment=(
            allow_created_roundrect_center_alignment
        ),
    )


def _created_roundrect_center_alignment_default(
    remote_element: ParsedElement,
    intended_element: ParsedElement,
    *,
    newly_created: bool,
    author_removed_classes: frozenset[str] | set[str],
) -> bool:
    """Recognize only Google's center default on a newly created RoundRect."""
    if not newly_created:
        return False
    if remote_element.tag != "RoundRect" or intended_element.tag != "RoundRect":
        return False
    intended_classes = _text_and_paragraph_classes(intended_element)
    remote_classes = _text_and_paragraph_classes(remote_element)
    if "text-align-center" not in remote_classes - intended_classes:
        return False
    authored_classes = intended_classes | set(author_removed_classes)
    return not any(
        class_name.startswith("text-align-") for class_name in authored_classes
    )


def _auto_text_coverage(
    element: ParsedElement,
) -> tuple[tuple[int, int, int, str], ...] | None:
    """Return normalized auto-text types over UTF-16 paragraph intervals."""
    if element.runs:
        if len(element.runs) != len(element.paragraphs):
            return None
        runs_by_paragraph = element.runs
    else:
        runs_by_paragraph = [[ParsedRun(text=text)] for text in element.paragraphs]

    coverage: list[tuple[int, int, int, str]] = []
    for paragraph_index, text in enumerate(element.paragraphs):
        offset = 0
        for run in runs_by_paragraph[paragraph_index]:
            end = offset + _utf16_len(run.text)
            if end > _utf16_len(text):
                return None
            if run.auto_text_type is not None:
                coverage.append((paragraph_index, offset, end, run.auto_text_type))
            offset = end
        if offset != _utf16_len(text):
            return None

    normalized: list[tuple[int, int, int, str]] = []
    for item in coverage:
        if (
            normalized
            and normalized[-1][0] == item[0]
            and normalized[-1][3] == item[3]
            and normalized[-1][2] == item[1]
        ):
            previous = normalized[-1]
            normalized[-1] = (previous[0], previous[1], item[2], item[3])
        else:
            normalized.append(item)
    return tuple(normalized)


def _auto_text_coverage_matches(
    remote: ParsedElement,
    intended: ParsedElement,
) -> bool:
    remote_coverage = _auto_text_coverage(remote)
    intended_coverage = _auto_text_coverage(intended)
    return (
        remote_coverage is not None
        and intended_coverage is not None
        and remote_coverage == intended_coverage
    )


def _utf16_len(text: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in text)


def _non_text_style_matches(
    change: Change,
    remote_element: ParsedElement,
    intended_element: ParsedElement,
    *,
    newly_created: bool,
    author_removed_classes: frozenset[str] | set[str],
) -> bool:
    """Keep the old exact/default comparison for non-text style groups."""
    sent = _format_changed_element_non_text_style_classes(change, intended_element)
    remote = _format_changed_element_non_text_style_classes(change, remote_element)
    if sent == remote:
        return True
    if newly_created and _only_google_default_element_style_additions(
        sent,
        remote,
        author_removed_classes,
    ):
        return True
    return _only_google_default_class_additions(
        sent,
        remote,
        remote_element,
        author_removed_classes,
        allow_created_element_alignment_default=newly_created,
    )


def _normalized_persistence_detail(
    change: Change,
    remote_elements: dict[tuple[str, str], ParsedElement],
    intended_elements: dict[tuple[str, str], ParsedElement],
    remote_image_sources: dict[tuple[str, str], str] | None = None,
    expected_image_sources: dict[tuple[str, str], str] | None = None,
    remote_image_crop_properties: dict[tuple[str, str], dict[str, float]] | None = None,
    *,
    newly_created: bool = False,
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

    if change.change_type == ChangeType.IMAGE_UPDATE:
        if not _geometry_matches_within_tolerance(
            change.new_position, change.old_position
        ):
            sent_geometry = _format_geometry(change.new_position)
            remote_geometry = _format_geometry(change.old_position)
            if sent_geometry and remote_geometry:
                return (
                    f"geometry on {change.target_id} did not persist "
                    f"(sent {sent_geometry!r}, remote now {remote_geometry!r})"
                )
        key = (change.slide_index or "", change.target_id)
        if (
            change.fit == "cover"
            and remote_image_crop_properties is not None
            and not _cover_crop_is_exempt_for_local_create(
                change, newly_created=newly_created
            )
        ):
            actual_crop = remote_image_crop_properties.get(key)
            if actual_crop is None:
                return (
                    f"image crop on {change.target_id} did not persist "
                    "(expected centered crop, remote now missing cropProperties)"
                )
            if not _crop_matches_centered(actual_crop, _expected_center_crop(change)):
                expected_crop = _expected_center_crop(change)
                return (
                    f"image crop on {change.target_id} did not persist "
                    f"(expected centered crop "
                    f"{_format_crop(expected_crop) if expected_crop else 'with equal opposing offsets'}, "
                    f"remote now {_format_crop(actual_crop)})"
                )
        remote_source = (remote_image_sources or {}).get(key)
        expected_source = (expected_image_sources or {}).get(key, change.src)
        if remote_source is not None and expected_source is not None:
            return (
                f"image replacement did not persist on {change.target_id} "
                f"(sent {redact_image_url(expected_source)!r}, "
                f"remote now {redact_image_url(remote_source)!r})"
            )

    return None


def _is_normalized_persistence_change(
    change: Change,
    remote_elements: dict[tuple[str, str], ParsedElement],
    intended_elements: dict[tuple[str, str], ParsedElement],
    *,
    newly_created: bool,
    remote_image_sources: dict[tuple[str, str], str] | None = None,
    expected_image_sources: dict[tuple[str, str], str] | None = None,
    remote_image_crop_properties: dict[tuple[str, str], dict[str, float]] | None = None,
) -> bool:
    """Return whether a refresh difference is known Google normalization."""
    return (
        _persistence_warning_severity(
            change,
            remote_elements,
            intended_elements,
            newly_created=newly_created,
            remote_image_sources=remote_image_sources,
            expected_image_sources=expected_image_sources,
            remote_image_crop_properties=remote_image_crop_properties,
        )
        in (None, WarningSeverity.NOTICE)
    )


def _persistence_warning_severity(
    change: Change,
    remote_elements: dict[tuple[str, str], ParsedElement],
    intended_elements: dict[tuple[str, str], ParsedElement],
    *,
    newly_created: bool,
    author_removed_classes: frozenset[str] | set[str] | None = None,
    related_author_removed_classes: frozenset[str] | set[str] | None = None,
    span_cache: dict[int, list[EffectiveTextSpan] | None] | None = None,
    remote_image_sources: dict[tuple[str, str], str] | None = None,
    expected_image_sources: dict[tuple[str, str], str] | None = None,
    remote_image_crop_properties: dict[tuple[str, str], dict[str, float]] | None = None,
) -> WarningSeverity | None:
    """Classify a refreshed divergence, suppressing harmless geometry/defaults."""
    removed_classes = (
        change.author_removed_classes
        if author_removed_classes is None
        else author_removed_classes
    )
    normalization_removed_classes = set(removed_classes or set()) | set(
        related_author_removed_classes or set()
    )

    if change.change_type == ChangeType.MOVE:
        old = change.old_position
        new = change.new_position
        key = (change.slide_index or "", change.target_id)
        intended_element = intended_elements.get(key)
        return (
            None
            if _geometry_matches_within_tolerance(
                new,
                old,
                authored_tag=(
                    intended_element.tag if intended_element is not None else None
                ),
            )
            else WarningSeverity.WARNING
        )

    if change.change_type == ChangeType.IMAGE_UPDATE:
        if not _geometry_matches_within_tolerance(
            change.new_position, change.old_position
        ):
            return WarningSeverity.WARNING
        key = (change.slide_index or "", change.target_id)
        remote_element = remote_elements.get(key)
        intended_element = intended_elements.get(key)
        if not (
            remote_element is not None
            and intended_element is not None
            and remote_element.tag == "Image"
            and (
                (remote_element.src is None and remote_element.fit is None)
                or (
                    change.fit == "cover"
                    and remote_element.fit == "cover"
                    and remote_element.src is not None
                )
            )
        ):
            return WarningSeverity.WARNING
        remote_source = (remote_image_sources or {}).get(key)
        if (
            change.fit == "cover"
            and remote_image_crop_properties is not None
            and not _cover_crop_is_exempt_for_local_create(
                change, newly_created=newly_created
            )
        ):
            actual_crop = remote_image_crop_properties.get(key)
            if actual_crop is None or not _crop_matches_centered(
                actual_crop, _expected_center_crop(change)
            ):
                return WarningSeverity.WARNING
        if remote_source is None:
            # Google may omit sourceUrl on refresh; retain the prior
            # unverifiable-success behavior rather than inventing a warning.
            return None
        expected_source = (expected_image_sources or {}).get(key, change.src)
        return (
            None
            if expected_source is not None and remote_source == expected_source
            else WarningSeverity.WARNING
        )

    if change.change_type == ChangeType.STYLE_UPDATE:
        key = (change.slide_index or "", change.target_id)
        remote_element = remote_elements.get(key)
        intended_element = intended_elements.get(key)
        if remote_element is None or intended_element is None:
            return WarningSeverity.WARNING
        sent = _format_changed_element_style_classes(change, intended_element)
        remote = _format_changed_element_style_classes(change, remote_element)
        roundrect_center_alignment = _created_roundrect_center_alignment_default(
            remote_element,
            intended_element,
            newly_created=newly_created,
            author_removed_classes=normalization_removed_classes,
        )
        if newly_created:
            sent_non_text = _format_changed_element_non_text_style_classes(
                change, intended_element
            )
            remote_non_text = _format_changed_element_non_text_style_classes(
                change, remote_element
            )
            sent_text = _format_changed_element_text_style_classes(
                change, intended_element
            )
            remote_text = _format_changed_element_text_style_classes(
                change, remote_element
            )
            text_defaults = sent_text == remote_text or _only_google_default_class_additions(
                sent_text,
                remote_text,
                remote_element,
                normalization_removed_classes,
                allow_created_element_alignment_default=True,
                allow_created_roundrect_center_alignment_default=(
                    roundrect_center_alignment
                ),
            )
            if (
                sent_non_text != remote_non_text
                and _only_google_default_element_style_additions(
                    sent_non_text,
                    remote_non_text,
                    normalization_removed_classes,
                )
                and text_defaults
                and not roundrect_center_alignment
            ):
                return None
        if roundrect_center_alignment:
            if (
                _non_text_style_matches(
                    change,
                    remote_element,
                    intended_element,
                    newly_created=newly_created,
                    author_removed_classes=normalization_removed_classes,
                )
                and _effective_text_styles_match(
                    change,
                    remote_element,
                    intended_element,
                    author_removed_classes=normalization_removed_classes,
                    span_cache=span_cache,
                    allow_created_roundrect_center_alignment=True,
                )
            ):
                return None
            return WarningSeverity.WARNING
        if not _only_google_default_class_additions(
            sent,
            remote,
            remote_element,
            normalization_removed_classes,
            allow_created_element_alignment_default=newly_created,
            allow_created_roundrect_center_alignment_default=roundrect_center_alignment,
        ):
            if (
                _non_text_style_matches(
                    change,
                    remote_element,
                    intended_element,
                    newly_created=newly_created,
                    author_removed_classes=normalization_removed_classes,
                )
                and _effective_text_styles_match(
                    change,
                    remote_element,
                    intended_element,
                    author_removed_classes=normalization_removed_classes,
                    span_cache=span_cache,
                )
            ):
                return None
            return WarningSeverity.WARNING
        return None if newly_created else WarningSeverity.NOTICE

    if change.change_type == ChangeType.PARAGRAPH_STYLE_UPDATE:
        all_defaults = True
        for update in change.paragraph_style_updates or []:
            sent = _paragraph_style_classes(update.new_styles)
            remote = _paragraph_style_classes(update.old_styles)
            if not _only_google_default_class_additions(
                sent,
                remote,
                author_removed_classes=normalization_removed_classes,
            ):
                all_defaults = False
        if all_defaults and change.paragraph_style_updates:
            return None if newly_created else WarningSeverity.NOTICE
        key = (change.slide_index or "", change.target_id)
        remote_element = remote_elements.get(key)
        intended_element = intended_elements.get(key)
        if (
            remote_element is not None
            and intended_element is not None
            and _effective_text_styles_match(
                change,
                remote_element,
                intended_element,
                author_removed_classes=normalization_removed_classes,
                span_cache=span_cache,
            )
        ):
            return None
        if not change.paragraph_style_updates:
            return WarningSeverity.WARNING
        return WarningSeverity.WARNING

    if change.change_type == ChangeType.TEXT_UPDATE:
        key = (change.slide_index or "", change.target_id)
        remote_element = remote_elements.get(key)
        intended_element = intended_elements.get(key)
        auto_text_coverage_matches = (
            remote_element is not None
            and intended_element is not None
            and _auto_text_coverage_matches(remote_element, intended_element)
        )
        if (
            change.new_text == change.old_text
            and auto_text_coverage_matches
            and _runs_only_gain_google_defaults(
                change.new_runs,
                change.old_runs,
                author_removed_classes=normalization_removed_classes,
            )
        ):
            return None if newly_created else WarningSeverity.NOTICE
        if (
            change.new_text == change.old_text
            and auto_text_coverage_matches
            and remote_element is not None
            and intended_element is not None
            and _effective_text_styles_match(
                change,
                remote_element,
                intended_element,
                author_removed_classes=normalization_removed_classes,
                span_cache=span_cache,
            )
        ):
            return None
        return WarningSeverity.WARNING

    return WarningSeverity.WARNING


def _only_google_default_class_additions(
    sent: str | set[str],
    remote: str | set[str],
    element: ParsedElement | None = None,
    author_removed_classes: frozenset[str] | set[str] | None = None,
    *,
    allow_created_element_alignment_default: bool = False,
    allow_created_roundrect_center_alignment_default: bool = False,
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
    if added & (author_removed_classes or set()):
        return False
    if "text-align-center" in added:
        if not allow_created_roundrect_center_alignment_default:
            return False
        authored_alignment = {
            class_name
            for class_name in sent_classes | (author_removed_classes or set())
            if class_name.startswith("text-align-")
        }
        if authored_alignment:
            return False
        added.remove("text-align-center")
    if added and not added <= GOOGLE_DEFAULT_TEXT_LAYOUT_CLASSES:
        return False
    if "font-family-arial" in added and any(
        class_name.startswith("font-family-") for class_name in sent_classes
    ):
        return False
    if any(class_name.startswith("font-weight-") for class_name in added):
        if any(
            class_name.startswith("font-weight-") for class_name in sent_classes
        ):
            return False
    alignment_defaults = added & {
        "content-align-top",
        "content-align-middle",
        "content-align-bottom",
    }
    if alignment_defaults:
        if allow_created_element_alignment_default:
            authored_alignment = {
                class_name
                for class_name in sent_classes | (author_removed_classes or set())
                if class_name.startswith("content-align-")
            }
            if authored_alignment:
                return False
        else:
            # Preserve the pre-existing normalization contract for existing
            # elements; the author-authored rule above is for creates only.
            if "content-align-bottom" in alignment_defaults:
                return False
            if "content-align-top" in added:
                if element is None or TAG_TO_TYPE.get(element.tag) != "TEXT_BOX":
                    return False
            if "content-align-middle" in added:
                element_type = (
                    TAG_TO_TYPE.get(element.tag) if element is not None else None
                )
                if (
                    element_type not in VALID_GOOGLE_TYPES
                    or element_type == "TEXT_BOX"
                ):
                    return False
    return True


def _only_google_default_element_style_additions(
    sent: str | set[str],
    remote: str | set[str],
    author_removed_classes: frozenset[str] | set[str] | None = None,
) -> bool:
    """Accept only create-time fill/stroke/default-alignment additions.

    These classes stay separate from the text-layout allowlist: an authored
    paint or stroke class must never be normalized away by text verification.
    """
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
    if not added <= GOOGLE_DEFAULT_ELEMENT_STYLE_CLASSES:
        return False
    if added & (author_removed_classes or set()):
        return False
    authored_alignment = {
        class_name
        for class_name in sent_classes | (author_removed_classes or set())
        if class_name.startswith("content-align-")
    }
    if added & {
        "content-align-top",
        "content-align-middle",
        "content-align-bottom",
    } and authored_alignment:
        return False
    return True


def _runs_only_gain_google_defaults(
    sent: list[list[ParsedRun]] | None,
    remote: list[list[ParsedRun]] | None,
    *,
    author_removed_classes: frozenset[str] | set[str] | None = None,
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
            if not _only_google_default_class_additions(
                sent_classes,
                remote_classes,
                author_removed_classes=author_removed_classes,
            ):
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
    author_changes: list[Change] | None = None,
    read_pristine: Callable[
        [Path], tuple[dict[str, list[Any]], dict[str, dict[str, Any]]]
    ],
    expected_image_sources: dict[tuple[str, str], str] | None = None,
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
    remote_image_sources = _remote_image_source_urls(folder_path)
    remote_image_crop_properties = _remote_image_crop_properties(folder_path)
    span_cache: dict[int, list[EffectiveTextSpan] | None] = {}
    author_removed_by_key: dict[
        tuple[str, str, ChangeType], frozenset[str]
    ] = {}
    author_removed_by_target: dict[tuple[str, str], set[str]] = {}
    for authored_change in author_changes or []:
        if authored_change.author_removed_classes:
            author_removed_by_key[
                (
                    authored_change.slide_index or "",
                    authored_change.target_id,
                    authored_change.change_type,
                )
            ] = authored_change.author_removed_classes
            author_removed_by_target.setdefault(
                (authored_change.slide_index or "", authored_change.target_id),
                set(),
            ).update(authored_change.author_removed_classes)
    classified = [
        (
            change,
            _persistence_warning_severity(
                change,
                remote_elements,
                intended_elements,
                newly_created=(
                    change.slide_index or "", change.target_id
                )
                in create_copy_targets,
                author_removed_classes=author_removed_by_key.get(
                    (change.slide_index or "", change.target_id, change.change_type),
                    frozenset(),
                ),
                related_author_removed_classes=author_removed_by_target.get(
                    (change.slide_index or "", change.target_id),
                    set(),
                ),
                span_cache=span_cache,
                remote_image_sources=remote_image_sources,
                expected_image_sources=expected_image_sources,
                remote_image_crop_properties=remote_image_crop_properties,
            ),
        )
        for change in unpersisted
    ]
    classified = [
        (change, severity)
        for change, severity in classified
        if severity is not None
    ]
    if not classified:
        return

    for severity in (WarningSeverity.WARNING, WarningSeverity.NOTICE):
        changes = sorted(
            [
                change
                for change, item_severity in classified
                if item_severity == severity
            ],
            key=lambda change: (
                change.slide_index or "",
                change.target_id,
                change.change_type.value,
            ),
        )
        if not changes:
            continue
        details = ", ".join(
            _normalized_persistence_detail(
                change,
                remote_elements,
                intended_elements,
                remote_image_sources,
                expected_image_sources,
                remote_image_crop_properties,
                newly_created=(
                    (change.slide_index or "", change.target_id) in create_copy_targets
                ),
            )
            or f"{change.target_id} ({change.change_type.value.replace('_', ' ')})"
            for change in changes
        )
        if severity == WarningSeverity.NOTICE:
            message = (
                f"{len(changes)} change(s) were normalized by Google: {details}"
            )
        else:
            message = (
                f"{len(changes)} change(s) did not persist remotely: {details} "
                "— the API may not support these values"
            )
        response.setdefault("warnings", []).append(
            PushWarning(severity, message)
        )


__all__ = [
    "GOOGLE_DEFAULT_ELEMENT_STYLE_CLASSES",
    "GOOGLE_DEFAULT_TEXT_LAYOUT_CLASSES",
    "LINE_NEAR_DEGENERATE_AXIS_THRESHOLD_PT",
    "PERSISTENCE_GEOMETRY_TOLERANCE_PT",
    "_format_changed_element_style_classes",
    "_format_geometry",
    "_format_paragraph_style_classes",
    "_format_run_style_classes",
    "_index_parsed_elements",
    "_is_normalized_persistence_change",
    "_normalized_persistence_detail",
    "_only_google_default_element_style_additions",
    "_only_google_default_class_additions",
    "_paragraph_style_classes",
    "_runs_only_gain_google_defaults",
    "append_persistence_warning",
]
