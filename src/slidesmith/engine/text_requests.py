"""Build text-edit and text-style Google Slides API requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from slidesmith.engine.classes import ParagraphStyle, TextStyle
from slidesmith.engine.class_style_requests import (
    _class_paragraph_style_to_api,
    _class_text_style_to_api,
    _create_class_text_style_request,
)
from slidesmith.engine.content_diff import (
    _PARAGRAPH_STYLE_FIELD_NAMES,
    _TEXT_STYLE_FIELD_NAMES,
    _utf16_len,
    ParagraphClassUpdate,
    _changed_style_fields,
)
from slidesmith.engine.content_parser import ParagraphStyles, ParsedElement, ParsedRun
from slidesmith.engine.persistence_styles import (
    PARAGRAPH_STYLE_PROPERTY_KEYS,
    TEXT_STYLE_PROPERTY_KEYS,
    api_style_property_keys,
    effective_text_style_ranges,
    effective_text_style_spans,
    text_edit_plan,
)


@dataclass(frozen=True)
class EffectiveStyleRequestPlan:
    """Concrete text requests plus properties safe to omit from class resets."""

    requests: list[dict[str, Any]]
    handled_property_keys: frozenset[str]
    changed_property_keys: frozenset[str]
    new_property_keys: frozenset[str]


@dataclass(frozen=True)
class _InsertedTextRange:
    paragraph_index: int
    start: int
    end: int
    global_start: int
    global_end: int


def _inserted_text_ranges(
    paragraphs: list[str], edit: Any
) -> list[_InsertedTextRange]:
    """Return contiguous inserted character ranges in new UTF-16 space."""
    ranges: list[_InsertedTextRange] = []
    global_index = 0
    for paragraph_index, paragraph in enumerate(paragraphs):
        run_start: int | None = None
        run_global_start: int | None = None
        local_offset = 0
        for offset, character in enumerate(paragraph):
            inserted = edit.new_to_old[global_index + offset] is None
            width = _utf16_len(character)
            if inserted and run_start is None:
                run_start = local_offset
                run_global_start = global_index + offset
            if not inserted and run_start is not None:
                ranges.append(
                    _InsertedTextRange(
                        paragraph_index,
                        run_start,
                        local_offset,
                        run_global_start,
                        global_index + offset,
                    )
                )
                run_start = None
                run_global_start = None
            local_offset += width
        if run_start is not None:
            ranges.append(
                _InsertedTextRange(
                    paragraph_index,
                    run_start,
                    local_offset,
                    run_global_start,
                    global_index + len(paragraph),
                )
            )
        global_index += len(paragraph) + 1
    return ranges


def _inserted_range_for_item(
    item: Any, inserted_ranges: list[_InsertedTextRange]
) -> _InsertedTextRange | None:
    for inserted in inserted_ranges:
        if (
            item.paragraph_index == inserted.paragraph_index
            and inserted.start <= item.start
            and item.end <= inserted.end
        ):
            return inserted
    return None


def _deleted_codepoint_range(edit: Any) -> tuple[int, int] | None:
    if edit.old_mid == edit.new_mid or not edit.old_mid:
        return None
    if edit.styled_replacement:
        return edit.old_mid_start, edit.old_mid_start + len(edit.old_mid)
    deleted = (
        edit.old_mid_start + edit.common_prefix,
        edit.old_mid_start + len(edit.old_mid) - edit.common_suffix,
    )
    return deleted if deleted[0] < deleted[1] else None


def _range_item_at(
    ranges: list[Any], paragraph_index: int, offset: int
) -> Any | None:
    return next(
        (
            item
            for item in ranges
            if item.paragraph_index == paragraph_index
            and item.start <= offset < item.end
        ),
        None,
    )


def _text_properties(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        key: properties.get(key)
        for key in TEXT_STYLE_PROPERTY_KEYS
        if properties.get(key) is not None
    }


def _safe_inserted_range(
    inserted: _InsertedTextRange,
    paragraphs: list[str],
    edit: Any,
    ranges: list[Any],
) -> bool:
    """Apply the sole permitted Google text-inheritance proof."""
    if (
        inserted.start == 0
        or edit.created_paragraph_indices
        or "\n" in edit.new_mid
    ):
        return False
    previous_global = inserted.global_start - 1
    if previous_global < 0 or edit.new_to_old[previous_global] is None:
        return False

    # ``global_start`` is a code-point index while the local range is UTF-16.
    previous_local_codepoint = (
        inserted.global_start
        - sum(len(value) + 1 for value in paragraphs[: inserted.paragraph_index])
        - 1
    )
    previous_local_offset = _utf16_len(
        paragraphs[inserted.paragraph_index][:previous_local_codepoint]
    )
    previous_item = _range_item_at(
        ranges, inserted.paragraph_index, previous_local_offset
    )
    if previous_item is None:
        return False

    inserted_items = [
        item
        for item in ranges
        if item.paragraph_index == inserted.paragraph_index
        and item.start >= inserted.start
        and item.end <= inserted.end
    ]
    if not inserted_items:
        return False
    previous_style = _text_properties(previous_item.old_properties)
    if _text_properties(previous_item.new_properties) != previous_style:
        return False
    if any(
        _text_properties(item.new_properties) != previous_style
        for item in inserted_items
    ):
        return False

    next_local_offset = inserted.end
    next_item = _range_item_at(
        ranges, inserted.paragraph_index, next_local_offset
    )
    paragraph_code_points = len(paragraphs[inserted.paragraph_index])
    next_codepoint = previous_local_codepoint + 1 + (
        inserted.global_end - inserted.global_start - 1
    ) + 1
    if next_codepoint < paragraph_code_points and next_item is not None:
        if _text_properties(next_item.old_properties) != _text_properties(
            next_item.new_properties
        ):
            return False

    deleted = _deleted_codepoint_range(edit)
    if deleted is not None:
        insertion_old_index = edit.new_to_old[previous_global] + 1
        if deleted[0] <= insertion_old_index <= deleted[1]:
            return False
    return True


def _properties_by_paragraph(
    spans: list[Any], paragraph_count: int, keys: frozenset[str]
) -> list[dict[str, Any]]:
    grouped: list[list[Any]] = [[] for _ in range(paragraph_count)]
    for span in spans:
        if 0 <= span.paragraph_index < paragraph_count:
            grouped[span.paragraph_index].append(span)
    result: list[dict[str, Any]] = []
    for paragraph_spans in grouped:
        properties = next(
            (
                span.properties
                for span in paragraph_spans
                if span.start == span.end == 0
            ),
            next(
                (
                    span.properties
                    for span in paragraph_spans
                    if span.start < span.end
                ),
                {},
            ),
        )
        result.append({key: properties[key] for key in keys if key in properties})
    return result


def _create_effective_style_requests(
    object_id: str,
    old_element: ParsedElement,
    new_element: ParsedElement,
) -> EffectiveStyleRequestPlan | None:
    """Plan text updates from resolved old/new element styles.

    Class scope is not a request target: Google applies a field update at its
    requested scope, so moving a value from a paragraph to an element can
    otherwise emit an empty paragraph reset after the element update. Compare
    effective values over fixed UTF-16 ranges and emit concrete values only
    where the effective value changes. A property whose new value is absent is
    reset only over that fixed range; the caller may retain the legacy ALL
    reset only when the property is absent from every new effective range.
    """
    ranges = effective_text_style_ranges(old_element, new_element)
    if ranges is None:
        return None
    edit = text_edit_plan(
        old_element.paragraphs,
        new_element.paragraphs,
        old_element.runs or None,
        new_element.runs or None,
    )
    old_spans = effective_text_style_spans(old_element)
    new_spans = effective_text_style_spans(new_element)
    if old_spans is None or new_spans is None:
        return None
    inserted_ranges = _inserted_text_ranges(new_element.paragraphs, edit)
    safe_inserted_ranges = {
        inserted
        for inserted in inserted_ranges
        if _safe_inserted_range(
            inserted, new_element.paragraphs, edit, ranges
        )
    }

    handled: set[str] = set()
    text_property_attrs = {
        attr: f"text.{attr}" for attr in _TEXT_STYLE_FIELD_NAMES
    }
    paragraph_property_attrs = {
        attr: f"paragraph.{attr}" for attr in _PARAGRAPH_STYLE_FIELD_NAMES
    }

    all_property_keys = (
        *text_property_attrs.values(),
        *paragraph_property_attrs.values(),
    )

    def inserted_range_for(item: Any) -> _InsertedTextRange | None:
        return _inserted_range_for_item(item, inserted_ranges)

    def item_is_safe(item: Any) -> bool:
        inserted = inserted_range_for(item)
        return inserted is not None and inserted in safe_inserted_ranges

    def comparable_old_properties(item: Any) -> dict[str, Any]:
        if not item_is_safe(item):
            return item.old_properties
        return {
            **item.old_properties,
            **_text_properties(item.new_properties),
        }

    all_pairs = [
        (comparable_old_properties(item), item.new_properties)
        for item in ranges
    ]
    new_paragraph_properties = _properties_by_paragraph(
        new_spans, len(new_element.paragraphs), PARAGRAPH_STYLE_PROPERTY_KEYS
    )
    known_text_keys = {
        key
        for span in old_spans + new_spans
        for key in span.properties
        if key in TEXT_STYLE_PROPERTY_KEYS
    }
    known_paragraph_keys = {
        key
        for span in old_spans + new_spans
        for key in span.properties
        if key in PARAGRAPH_STYLE_PROPERTY_KEYS
    }
    changed_keys = {
        property_key
        for property_key in all_property_keys
        if any(
            old_properties.get(property_key) != new_properties.get(property_key)
            for old_properties, new_properties in all_pairs
        )
    }
    for paragraph_index in edit.created_paragraph_indices:
        if paragraph_index >= len(new_paragraph_properties):
            continue
        changed_keys.update(
            key
            for key, value in new_paragraph_properties[paragraph_index].items()
            if value is not None
        )
    changed_property_keys = frozenset(changed_keys)
    new_property_keys = frozenset(
        property_key
        for property_key in all_property_keys
        if any(new_properties.get(property_key) is not None for _, new_properties in all_pairs)
        or any(
            properties.get(property_key) is not None
            for properties in new_paragraph_properties
        )
    )

    # Every resolved property has a concrete per-range outcome: unchanged,
    # changed to a value, or changed to an absent value that needs a reset.
    # Keeping all of them handled is what prevents a mixed removal/addition
    # from falling back to an ALL reset.
    handled.update(all_property_keys)

    requests: list[dict[str, Any]] = []
    for item in ranges:
        inserted = inserted_range_for(item)
        if item_is_safe(item):
            continue
        changed_text_attrs = [
            attr
            for attr, property_key in text_property_attrs.items()
            if item.new_properties.get(property_key) is not None
            and (
                inserted is not None
                or item.old_properties.get(property_key)
                != item.new_properties.get(property_key)
            )
        ]
        reset_text_fields = [
            _TEXT_STYLE_FIELD_NAMES[attr]
            for attr, property_key in text_property_attrs.items()
            if item.old_properties.get(property_key) is not None
            and item.new_properties.get(property_key) is None
        ]
        if inserted is not None:
            reset_text_fields.extend(
                _TEXT_STYLE_FIELD_NAMES[attr]
                for attr, property_key in text_property_attrs.items()
                if property_key in known_text_keys
                and item.new_properties.get(property_key) is None
            )
            reset_text_fields = list(dict.fromkeys(reset_text_fields))
        weighted_font_reset = "weightedFontFamily" in reset_text_fields
        font_family_weight_update = weighted_font_reset or bool(
            set(changed_text_attrs) & {"font_family", "font_weight"}
        )
        old_bold = item.old_properties.get("text.bold")
        new_bold = item.new_properties.get("text.bold")
        bold_is_known = old_bold is not None or new_bold is not None
        bold_reset_pinned = (
            font_family_weight_update and bold_is_known
        )
        bold_is_genuinely_removed = old_bold is True and new_bold is None
        if font_family_weight_update and bold_is_known:
            # Both value updates and weightedFontFamily resets can clear bold
            # on the same range. Pin the known effective value in the same
            # request; emit false only when a true bold value is removed.
            if "bold" not in changed_text_attrs:
                changed_text_attrs.append("bold")
        if font_family_weight_update:
            changed_text_attrs = list(
                dict.fromkeys(
                    [
                        *changed_text_attrs,
                        *(
                            attr
                            for attr in ("font_family", "font_weight")
                            if f"text.{attr}" in handled
                        ),
                    ]
                )
            )
        if changed_text_attrs or weighted_font_reset:
            text_style = TextStyle(
                **{
                    attr: (
                        False
                        if attr == "bold"
                        and item.new_properties.get("text.bold") is None
                        and bold_is_genuinely_removed
                        else old_bold
                        if attr == "bold"
                        and item.new_properties.get("text.bold") is None
                        else item.new_properties[f"text.{attr}"]
                    )
                    for attr in changed_text_attrs
                    if item.new_properties.get(f"text.{attr}") is not None
                    or (attr == "bold" and bold_reset_pinned)
                }
            )
            style, fields = _class_text_style_to_api(text_style)
            if weighted_font_reset:
                # An empty weightedFontFamily reset is still a weighted-font
                # emission. Keep a known bold value in its mask so Google does
                # not clear bold while applying the reset.
                if "weightedFontFamily" not in fields:
                    fields.append("weightedFontFamily")
                if bold_reset_pinned:
                    style["bold"] = (
                        False
                        if bold_is_genuinely_removed
                        else (new_bold if new_bold is not None else old_bold)
                    )
                    if "bold" not in fields:
                        fields.append("bold")
            if fields:
                requests.append(
                    {
                        "updateTextStyle": {
                            "objectId": object_id,
                            "textRange": _fixed_text_range(
                                new_element.paragraphs,
                                item.paragraph_index,
                                item.start,
                                item.end,
                            ),
                            "style": style,
                            "fields": ",".join(fields),
                        }
                    }
                )

        reset_text_fields = [
            field
            for field in reset_text_fields
            if not (field == "weightedFontFamily" and weighted_font_reset)
            and not (field == "bold" and bold_reset_pinned)
        ]
        if reset_text_fields:
            requests.append(
                {
                    "updateTextStyle": {
                        "objectId": object_id,
                        "textRange": _fixed_text_range(
                            new_element.paragraphs,
                            item.paragraph_index,
                            item.start,
                            item.end,
                        ),
                        "style": {},
                        "fields": ",".join(reset_text_fields),
                    }
                }
            )

    for paragraph_index in range(len(new_element.paragraphs)):
        if paragraph_index in edit.created_paragraph_indices:
            continue
        paragraph_ranges = [
            item for item in ranges if item.paragraph_index == paragraph_index
        ]
        if not paragraph_ranges:
            continue
        for item in paragraph_ranges:
            changed_paragraph_attrs = [
                attr
                for attr, property_key in paragraph_property_attrs.items()
                if item.old_properties.get(property_key)
                != item.new_properties.get(property_key)
                and item.new_properties.get(property_key) is not None
            ]
            if changed_paragraph_attrs:
                paragraph_style = ParagraphStyle(
                    **{
                        attr: item.new_properties[f"paragraph.{attr}"]
                        for attr in changed_paragraph_attrs
                    }
                )
                style, fields = _class_paragraph_style_to_api(paragraph_style)
            else:
                style, fields = {}, []
            if fields:
                requests.append(
                    {
                        "updateParagraphStyle": {
                            "objectId": object_id,
                            "textRange": _fixed_text_range(
                                new_element.paragraphs,
                                paragraph_index,
                                item.start,
                                item.end,
                            ),
                            "style": style,
                            "fields": ",".join(fields),
                        }
                    }
                )

            reset_paragraph_fields = [
                _PARAGRAPH_STYLE_FIELD_NAMES[attr]
                for attr, property_key in paragraph_property_attrs.items()
                if item.old_properties.get(property_key) is not None
                and item.new_properties.get(property_key) is None
            ]
            if reset_paragraph_fields:
                requests.append(
                    {
                        "updateParagraphStyle": {
                            "objectId": object_id,
                            "textRange": _fixed_text_range(
                                new_element.paragraphs,
                                paragraph_index,
                                item.start,
                                item.end,
                            ),
                            "style": {},
                            "fields": ",".join(reset_paragraph_fields),
                        }
                    }
                )

    for paragraph_index in edit.created_paragraph_indices:
        if paragraph_index >= len(new_paragraph_properties):
            continue
        properties = new_paragraph_properties[paragraph_index]
        paragraph_attrs = [
            attr
            for attr, property_key in paragraph_property_attrs.items()
            if properties.get(property_key) is not None
        ]
        paragraph_style = ParagraphStyle(
            **{
                attr: properties[f"paragraph.{attr}"]
                for attr in paragraph_attrs
            }
        )
        style, fields = _class_paragraph_style_to_api(paragraph_style)
        reset_fields = [
            _PARAGRAPH_STYLE_FIELD_NAMES[attr]
            for attr, property_key in paragraph_property_attrs.items()
            if property_key in known_paragraph_keys
            and properties.get(property_key) is None
        ]
        reset_fields = [field for field in reset_fields if field not in fields]
        text_range = _paragraph_style_text_range(
            new_element.paragraphs, paragraph_index
        )
        if fields:
            requests.append(
                {
                    "updateParagraphStyle": {
                        "objectId": object_id,
                        "textRange": text_range,
                        "style": style,
                        "fields": ",".join(fields),
                    }
                }
            )
        if reset_fields:
            requests.append(
                {
                    "updateParagraphStyle": {
                        "objectId": object_id,
                        "textRange": text_range,
                        "style": {},
                        "fields": ",".join(reset_fields),
                    }
                }
            )

    return EffectiveStyleRequestPlan(
        requests,
        frozenset(handled),
        changed_property_keys,
        new_property_keys,
    )


def _fixed_text_range(
    paragraphs: list[str], paragraph_index: int, start: int, end: int
) -> dict[str, Any]:
    paragraph_start = sum(
        _utf16_len(text) + 1 for text in paragraphs[:paragraph_index]
    )
    return {
        "type": "FIXED_RANGE",
        "startIndex": paragraph_start + start,
        "endIndex": paragraph_start + end,
    }


def _paragraph_style_text_range(
    paragraphs: list[str], paragraph_index: int
) -> dict[str, Any]:
    """Return a fixed range that includes an empty paragraph's marker."""
    start = sum(_utf16_len(text) + 1 for text in paragraphs[:paragraph_index])
    return {
        "type": "FIXED_RANGE",
        "startIndex": start,
        "endIndex": start + max(1, _utf16_len(paragraphs[paragraph_index])),
    }


def _create_effective_scope_reset_requests(
    object_id: str,
    old_element: ParsedElement,
    new_element: ParsedElement,
    fields: list[str],
) -> list[dict[str, Any]]:
    """Clear removed element-scope fields without widening to ALL."""
    ranges = effective_text_style_ranges(old_element, new_element)
    if ranges is None:
        return []
    text_fields = set(_TEXT_STYLE_FIELD_NAMES.values())
    paragraph_fields = set(_PARAGRAPH_STYLE_FIELD_NAMES.values())
    requests: list[dict[str, Any]] = []
    for field in fields:
        property_keys = api_style_property_keys([field])
        if field in text_fields:
            operation = "updateTextStyle"
        elif field in paragraph_fields:
            operation = "updateParagraphStyle"
        else:
            continue
        for item in ranges:
            if not any(
                item.old_properties.get(property_key) is not None
                for property_key in property_keys
            ):
                continue
            requests.append(
                {
                    operation: {
                        "objectId": object_id,
                        "textRange": _fixed_text_range(
                            old_element.paragraphs,
                            item.paragraph_index,
                            item.start,
                            item.end,
                        ),
                        "style": {},
                        "fields": field,
                    }
                }
            )
    return requests


def _normalize_runs(
    paragraphs: list[str],
    runs: list[list[ParsedRun]] | None,
) -> list[list[ParsedRun]]:
    """Return runs parallel to paragraphs; plain paragraphs get one unstyled run."""
    if runs and len(runs) == len(paragraphs):
        return runs
    return [[ParsedRun(text=text)] for text in paragraphs]


def _delete_text_request(object_id: str, start: int, end: int) -> dict[str, Any]:
    """Create a deleteText request over a FIXED_RANGE of UTF-16 indices."""
    return {
        "deleteText": {
            "objectId": object_id,
            "textRange": {
                "type": "FIXED_RANGE",
                "startIndex": start,
                "endIndex": end,
            },
        }
    }


def _insert_text_request(object_id: str, index: int, text: str) -> dict[str, Any]:
    """Create an insertText request at a UTF-16 insertion index."""
    return {
        "insertText": {
            "objectId": object_id,
            "insertionIndex": index,
            "text": text,
        }
    }


def _create_text_update_requests(
    google_id: str,
    new_text: list[str],
    new_runs: list[list[ParsedRun]] | None = None,
    old_text: list[str] | None = None,
    old_runs: list[list[ParsedRun]] | None = None,
    skip_text_fields: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Create requests to update element text with minimal range edits.

    Strategy (contract C3): trim unchanged paragraphs from both ends,
    then trim the common prefix/suffix of the changed span, and emit
    deleteText/insertText scoped to just the changed range. Untouched
    text is never rewritten, so human-applied character styling survives.

    Paragraphs whose <T> runs changed count as changed: if their text
    also changed they are replaced wholesale and their run styles
    reapplied; other paragraphs stay untouched either way.

    All indices count UTF-16 code units (the Slides API index space).
    Falls back to delete-all + reinsert when pristine text is unknown.
    """
    if old_text is None:
        return _create_full_text_replace_requests(
            google_id,
            new_text,
            new_runs,
            skip_text_fields=skip_text_fields,
        )

    old_paras = old_text
    new_paras = new_text
    edit = text_edit_plan(old_paras, new_paras, old_runs, new_runs)
    old_para_runs = _normalize_runs(old_paras, old_runs)
    new_para_runs = _normalize_runs(new_paras, new_runs)
    m, n = len(old_paras), len(new_paras)
    prefix = edit.prefix
    suffix = edit.suffix
    old_mid = edit.old_mid
    new_mid = edit.new_mid
    old_mid_paras = old_paras[prefix : m - suffix]
    new_mid_paras = new_paras[prefix : n - suffix]
    start = edit.start
    end = edit.end

    requests: list[dict[str, Any]] = []

    if not old_mid_paras and new_mid_paras:
        # Pure paragraph insertion: add a separator toward the changed side.
        # The paragraph count, rather than the joined text, distinguishes an
        # inserted empty paragraph from a no-op.
        if prefix < m:
            requests.append(_insert_text_request(google_id, start, new_mid + "\n"))
        elif m > 0:
            requests.append(_insert_text_request(google_id, start, "\n" + new_mid))
        else:
            requests.append(_insert_text_request(google_id, 0, new_mid))
    elif old_mid_paras and not new_mid_paras:
        # Pure paragraph deletion: remove a separator with the paragraphs.
        if prefix > 0:
            requests.append(_delete_text_request(google_id, start - 1, end))
        elif suffix > 0:
            requests.append(_delete_text_request(google_id, start, end + 1))
        else:
            requests.append(_delete_text_request(google_id, start, end))
    elif old_mid == new_mid:
        # Only run styling changed. Emit field-level deltas so removing a
        # <T> class resets that property instead of leaving stale formatting.
        return _create_run_style_delta_requests(
            google_id,
            old_paras,
            old_para_runs,
            new_para_runs,
            paragraph_range=(prefix, n - suffix),
            skip_text_fields=skip_text_fields,
        )
    elif edit.styled_replacement:
        # Explicit <T> runs: replace the changed paragraphs wholesale and
        # reapply their run styles below. Other paragraphs stay untouched.
        requests.append(_delete_text_request(google_id, start, end))
        requests.append(_insert_text_request(google_id, start, new_mid))
    else:
        # Within the changed paragraphs, trim the common prefix/suffix and
        # touch only the span that actually differs.
        a = edit.common_prefix
        b = edit.common_suffix
        del_start = start + _utf16_len(old_mid[:a])
        del_end = end - _utf16_len(old_mid[len(old_mid) - b :])
        inserted = new_mid[a : len(new_mid) - b]
        if del_end > del_start:
            requests.append(_delete_text_request(google_id, del_start, del_end))
        if inserted:
            requests.append(_insert_text_request(google_id, del_start, inserted))

    if new_runs:
        requests.extend(
            _create_run_style_requests(
                google_id,
                new_runs,
                paragraph_range=(prefix, n - suffix),
                skip_text_fields=skip_text_fields,
            )
        )

    return requests


def _create_full_text_replace_requests(
    google_id: str,
    new_text: list[str],
    new_runs: list[list[ParsedRun]] | None = None,
    skip_text_fields: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Replace an element's entire text (used when pristine text is unknown).

    Deletes all existing text, then inserts the new text. If styled runs
    are provided, per-run text styles are applied after insert.
    """
    requests: list[dict[str, Any]] = []

    requests.append(
        {
            "deleteText": {
                "objectId": google_id,
                "textRange": {
                    "type": "ALL",
                },
            }
        }
    )

    if new_text:
        combined_text = "\n".join(new_text)
        requests.append(
            {
                "insertText": {
                    "objectId": google_id,
                    "insertionIndex": 0,
                    "text": combined_text,
                }
            }
        )

        # Apply per-run text styles from <T> runs
        if new_runs:
            requests.extend(
                _create_run_style_requests(
                    google_id,
                    new_runs,
                    skip_text_fields=skip_text_fields,
                )
            )

    return requests

def _create_run_style_requests(
    object_id: str,
    runs: list[list[ParsedRun]],
    paragraph_range: tuple[int, int] | None = None,
    skip_text_fields: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Create updateTextStyle requests for styled <T> runs.

    Assumes the element's text is the paragraphs joined with newlines
    (matching _create_text_insert_requests), and computes FIXED_RANGE
    indices over that combined text in UTF-16 code units.

    When paragraph_range is given, only runs in paragraphs within
    [start, end) produce requests; other paragraphs stay untouched.
    """
    requests: list[dict[str, Any]] = []
    index = 0

    for para_num, para_runs in enumerate(runs):
        if para_num > 0:
            index += 1  # Newline separator between paragraphs

        in_range = paragraph_range is None or (
            paragraph_range[0] <= para_num < paragraph_range[1]
        )
        for run in para_runs:
            end_index = index + _utf16_len(run.text)
            if run.text_style is not None and in_range:
                request = _create_class_text_style_request(
                    object_id,
                    run.text_style,
                    text_range={
                        "type": "FIXED_RANGE",
                        "startIndex": index,
                        "endIndex": end_index,
                    },
                )
                if request:
                    _filter_text_style_request(request, skip_text_fields)
                    if request["updateTextStyle"]["fields"]:
                        requests.append(request)
            index = end_index

    return requests


def _create_run_style_delta_requests(
    object_id: str,
    paragraphs: list[str],
    old_runs: list[list[ParsedRun]],
    new_runs: list[list[ParsedRun]],
    paragraph_range: tuple[int, int],
    skip_text_fields: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Emit precise TextStyle changes, including resets to inherited values."""
    requests: list[dict[str, Any]] = []

    def spans(runs: list[ParsedRun]) -> list[tuple[int, int, TextStyle | None]]:
        result: list[tuple[int, int, TextStyle | None]] = []
        offset = 0
        for run in runs:
            end = offset + len(run.text)
            if end > offset:
                result.append((offset, end, run.text_style))
            offset = end
        return result

    def style_at(
        style_spans: list[tuple[int, int, TextStyle | None]], offset: int
    ) -> TextStyle | None:
        for start, end, style in style_spans:
            if start <= offset < end:
                return style
        return None

    for paragraph_index in range(*paragraph_range):
        text = paragraphs[paragraph_index]
        old_spans = spans(old_runs[paragraph_index])
        new_spans = spans(new_runs[paragraph_index])
        boundaries = sorted(
            {0, len(text)}
            | {value for start, end, _ in old_spans + new_spans for value in (start, end)}
        )
        paragraph_start = sum(
            _utf16_len(value) + 1 for value in paragraphs[:paragraph_index]
        )
        for start, end in zip(boundaries, boundaries[1:], strict=False):
            if end <= start:
                continue
            old_style = style_at(old_spans, start) or TextStyle()
            new_style = style_at(new_spans, start) or TextStyle()
            fields = _changed_style_fields(
                old_style, new_style, _TEXT_STYLE_FIELD_NAMES
            )
            fields = [field for field in fields if field not in skip_text_fields]
            if not fields:
                continue
            api_style, _ = _class_text_style_to_api(new_style)
            requests.append(
                {
                    "updateTextStyle": {
                        "objectId": object_id,
                        "textRange": {
                            "type": "FIXED_RANGE",
                            "startIndex": paragraph_start
                            + _utf16_len(text[:start]),
                            "endIndex": paragraph_start + _utf16_len(text[:end]),
                        },
                        "style": {
                            key: value
                            for key, value in api_style.items()
                            if key in fields
                        },
                        "fields": ",".join(fields),
                    }
                }
            )
    return requests


def _filter_text_style_request(
    request: dict[str, Any],
    skip_text_fields: set[str] | frozenset[str],
) -> None:
    """Remove planner-owned fields from one legacy run-style request in place."""
    if not skip_text_fields:
        return
    body = request["updateTextStyle"]
    fields = [field for field in body["fields"].split(",") if field not in skip_text_fields]
    body["fields"] = ",".join(fields)
    body["style"] = {
        field: value for field, value in body["style"].items() if field in fields
    }


def _paragraph_text_range(
    paragraphs: list[str], paragraph_index: int
) -> dict[str, Any]:
    """Return the fixed UTF-16 range containing one paragraph's text."""
    start = sum(_utf16_len(text) + 1 for text in paragraphs[:paragraph_index])
    return {
        "type": "FIXED_RANGE",
        "startIndex": start,
        "endIndex": start + _utf16_len(paragraphs[paragraph_index]),
    }


def _create_paragraph_class_update_requests(
    object_id: str,
    paragraphs: list[str],
    runs: list[list[ParsedRun]],
    updates: list[ParagraphClassUpdate],
    *,
    reapply_runs: bool = True,
    skip_text_fields: set[str] | frozenset[str] = frozenset(),
    skip_paragraph_fields: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Create precise-range updates for changed ``<P class>`` defaults."""
    requests: list[dict[str, Any]] = []
    for update in updates:
        if update.paragraph_index >= len(paragraphs):
            continue
        old = update.old_styles or ParagraphStyles()
        new = update.new_styles or ParagraphStyles()
        text_range = _paragraph_text_range(paragraphs, update.paragraph_index)

        paragraph_fields = _changed_style_fields(
            old.paragraph_style,
            new.paragraph_style,
            _PARAGRAPH_STYLE_FIELD_NAMES,
        )
        paragraph_fields = [
            field for field in paragraph_fields if field not in skip_paragraph_fields
        ]
        if paragraph_fields:
            style, _ = _class_paragraph_style_to_api(
                new.paragraph_style or ParagraphStyle()
            )
            requests.append(
                {
                    "updateParagraphStyle": {
                        "objectId": object_id,
                        "textRange": text_range,
                        "style": {
                            key: value
                            for key, value in style.items()
                            if key in paragraph_fields
                        },
                        "fields": ",".join(paragraph_fields),
                    }
                }
            )

        text_fields = _changed_style_fields(
            old.text_style,
            new.text_style,
            _TEXT_STYLE_FIELD_NAMES,
        )
        text_fields = [field for field in text_fields if field not in skip_text_fields]
        if text_fields:
            style, _ = _class_text_style_to_api(new.text_style or TextStyle())
            requests.append(
                {
                    "updateTextStyle": {
                        "objectId": object_id,
                        "textRange": text_range,
                        "style": {
                            key: value
                            for key, value in style.items()
                            if key in text_fields
                        },
                        "fields": ",".join(text_fields),
                    }
                }
            )

        if reapply_runs and text_fields and update.paragraph_index < len(runs):
            requests.extend(
                _create_run_style_requests(
                    object_id,
                    runs,
                    paragraph_range=(
                        update.paragraph_index,
                        update.paragraph_index + 1,
                    ),
                )
            )

    return requests


def _create_text_insert_requests(
    object_id: str,
    text_lines: list[str],
) -> list[dict[str, Any]]:
    """Create requests to insert text into an element."""
    if not text_lines:
        return []

    combined_text = "\n".join(text_lines)
    return [
        {
            "insertText": {
                "objectId": object_id,
                "insertionIndex": 0,
                "text": combined_text,
            }
        }
    ]
