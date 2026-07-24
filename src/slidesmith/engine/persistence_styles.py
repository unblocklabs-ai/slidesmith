"""Resolve effective text styles for persistence and request planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from slidesmith.engine.classes import TextAlignment
from slidesmith.engine.content_parser import ParsedElement, ParsedRun


TEXT_STYLE_PROPERTY_KEYS = frozenset(
    {
        "text.bold",
        "text.italic",
        "text.underline",
        "text.strikethrough",
        "text.small_caps",
        "text.baseline_offset",
        "text.font_family",
        "text.font_size_pt",
        "text.font_weight",
        "text.foreground_color",
        "text.background_color",
        "text.link",
    }
)
PARAGRAPH_STYLE_PROPERTY_KEYS = frozenset(
    {
        "paragraph.alignment",
        "paragraph.line_spacing",
        "paragraph.space_above_pt",
        "paragraph.space_below_pt",
        "paragraph.indent_start_pt",
        "paragraph.indent_end_pt",
        "paragraph.indent_first_line_pt",
        "paragraph.direction",
        "paragraph.spacing_mode",
    }
)
ALL_TEXT_PROPERTY_KEYS = (
    TEXT_STYLE_PROPERTY_KEYS | PARAGRAPH_STYLE_PROPERTY_KEYS
)
_FONT_FAMILY_WEIGHT_KEYS = frozenset(
    {"text.font_family", "text.font_weight"}
)

TEXT_STYLE_API_FIELDS = {
    "bold": "text.bold",
    "italic": "text.italic",
    "underline": "text.underline",
    "strikethrough": "text.strikethrough",
    "smallCaps": "text.small_caps",
    "baselineOffset": "text.baseline_offset",
    "fontFamily": "text.font_family",
    "weightedFontFamily": "text.font_family",
    "fontSize": "text.font_size_pt",
    "foregroundColor": "text.foreground_color",
    "backgroundColor": "text.background_color",
    "link": "text.link",
}
PARAGRAPH_STYLE_API_FIELDS = {
    "alignment": "paragraph.alignment",
    "lineSpacing": "paragraph.line_spacing",
    "spaceAbove": "paragraph.space_above_pt",
    "spaceBelow": "paragraph.space_below_pt",
    "indentStart": "paragraph.indent_start_pt",
    "indentEnd": "paragraph.indent_end_pt",
    "indentFirstLine": "paragraph.indent_first_line_pt",
    "direction": "paragraph.direction",
    "spacingMode": "paragraph.spacing_mode",
}

_TEXT_STYLE_ATTRIBUTES = {
    key.removeprefix("text."): key for key in TEXT_STYLE_PROPERTY_KEYS
}
_PARAGRAPH_STYLE_ATTRIBUTES = {
    key.removeprefix("paragraph."): key
    for key in PARAGRAPH_STYLE_PROPERTY_KEYS
}


@dataclass(frozen=True)
class EffectiveTextSpan:
    """One UTF-16 text interval and its resolved effective properties."""

    paragraph_index: int
    start: int
    end: int
    properties: dict[str, Any]


@dataclass(frozen=True)
class EffectiveTextRange:
    """One comparable UTF-16 range from two resolved text projections."""

    paragraph_index: int
    start: int
    end: int
    old_properties: dict[str, Any]
    new_properties: dict[str, Any]


@dataclass(frozen=True)
class TextEditPlan:
    """Shared text-edit mapping in code-point and UTF-16 index space."""

    prefix: int
    suffix: int
    old_mid: str
    new_mid: str
    start: int
    end: int
    styled_replacement: bool
    common_prefix: int
    common_suffix: int
    old_mid_start: int
    new_mid_start: int
    insertion_old_index: int
    new_to_old: tuple[int | None, ...]
    created_paragraph_indices: tuple[int, ...]


_ACCEPTED_REMOTE_DEFAULTS = {
    ("text.font_family", "Arial", "font-family-arial"),
    ("text.font_weight", 400, "font-weight-400"),
    ("text.font_weight", 700, "font-weight-700"),
    ("paragraph.alignment", TextAlignment.START, "text-align-left"),
    ("paragraph.line_spacing", 100.0, "leading-100"),
    ("paragraph.space_above_pt", 0.0, "space-above-0"),
    ("paragraph.space_below_pt", 0.0, "space-below-0"),
    ("paragraph.indent_start_pt", 0.0, "indent-start-0"),
    ("paragraph.indent_first_line_pt", 0.0, "indent-first-0"),
    ("paragraph.spacing_mode", "NEVER_COLLAPSE", "spacing-never-collapse"),
    ("paragraph.spacing_mode", "COLLAPSE_LISTS", "spacing-collapse-lists"),
}


def text_edit_plan(
    old_paragraphs: list[str],
    new_paragraphs: list[str],
    old_runs: list[list[ParsedRun]] | None = None,
    new_runs: list[list[ParsedRun]] | None = None,
) -> TextEditPlan:
    """Return the exact edit shape used by text request generation.

    The mapping is deliberately code-point based while its public offsets are
    UTF-16 based. This means a surrogate pair is always retained or inserted
    as one unit and can never be split by a style range.
    """
    old_para_runs = _normalize_edit_runs(old_paragraphs, old_runs)
    new_para_runs = _normalize_edit_runs(new_paragraphs, new_runs)
    old_count, new_count = len(old_paragraphs), len(new_paragraphs)

    def paragraph_unchanged(old_index: int, new_index: int) -> bool:
        return (
            old_paragraphs[old_index] == new_paragraphs[new_index]
            and old_para_runs[old_index] == new_para_runs[new_index]
        )

    limit = min(old_count, new_count)
    prefix = 0
    while prefix < limit and paragraph_unchanged(prefix, prefix):
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and paragraph_unchanged(
        old_count - 1 - suffix,
        new_count - 1 - suffix,
    ):
        suffix += 1

    old_mid_paragraphs = old_paragraphs[prefix : old_count - suffix]
    new_mid_paragraphs = new_paragraphs[prefix : new_count - suffix]
    old_mid = "\n".join(old_mid_paragraphs)
    new_mid = "\n".join(new_mid_paragraphs)
    old_joined = "\n".join(old_paragraphs)
    new_joined = "\n".join(new_paragraphs)

    if prefix == old_count and old_count > 0:
        start = _utf16_len(old_joined)
    else:
        start = sum(
            _utf16_len(text) + 1 for text in old_paragraphs[:prefix]
        )
    end = start + _utf16_len(old_mid)

    styled_replacement = any(
        run.text_style is not None
        for paragraph_runs in new_para_runs[prefix : new_count - suffix]
        for run in paragraph_runs
    )
    if old_mid == new_mid:
        common_prefix = len(old_mid)
        common_suffix = 0
    else:
        common_prefix = _common_prefix_chars(old_mid, new_mid)
        common_suffix = _common_suffix_chars(
            old_mid,
            new_mid,
            min(len(old_mid), len(new_mid)) - common_prefix,
        )

    old_mid_start = _joined_mid_start(old_paragraphs, prefix)
    new_mid_start = _joined_mid_start(new_paragraphs, prefix)
    new_to_old: list[int | None] = [None] * len(new_joined)

    def map_equal(new_start: int, old_start: int, length: int) -> None:
        for offset in range(length):
            if new_start + offset >= len(new_to_old):
                break
            new_to_old[new_start + offset] = old_start + offset

    # Common paragraphs remain untouched by the request sequence. Map their
    # content, and the separator before a changed middle when it exists on
    # both sides. A newly appended/prepended separator is an insertion.
    prefix_content = "\n".join(old_paragraphs[:prefix])
    map_equal(0, 0, len(prefix_content))
    if prefix and prefix < old_count and prefix < new_count:
        map_equal(new_mid_start - 1, old_mid_start - 1, 1)

    if suffix:
        old_suffix = "\n".join(old_paragraphs[old_count - suffix :])
        new_suffix = "\n".join(new_paragraphs[new_count - suffix :])
        old_suffix_start = len(old_joined) - len(old_suffix)
        new_suffix_start = len(new_joined) - len(new_suffix)
        map_equal(new_suffix_start, old_suffix_start, len(new_suffix))

    if old_mid == new_mid:
        map_equal(new_mid_start, old_mid_start, len(new_mid))
        insertion_old_index = old_mid_start
    elif styled_replacement:
        insertion_old_index = old_mid_start
    else:
        map_equal(new_mid_start, old_mid_start, common_prefix)
        if common_suffix:
            map_equal(
                new_mid_start + len(new_mid) - common_suffix,
                old_mid_start + len(old_mid) - common_suffix,
                common_suffix,
            )
        insertion_old_index = old_mid_start + common_prefix

    return TextEditPlan(
        prefix,
        suffix,
        old_mid,
        new_mid,
        start,
        end,
        styled_replacement,
        common_prefix,
        common_suffix,
        old_mid_start,
        new_mid_start,
        insertion_old_index,
        tuple(new_to_old),
        tuple(
            _created_paragraph_indices(
                old_paragraphs, new_paragraphs, new_to_old
            )
        ),
    )


def _normalize_edit_runs(
    paragraphs: list[str],
    runs: list[list[ParsedRun]] | None,
) -> list[list[ParsedRun]]:
    if runs and len(runs) == len(paragraphs):
        return runs
    return [[ParsedRun(text=text)] for text in paragraphs]


def _joined_mid_start(paragraphs: list[str], prefix: int) -> int:
    if prefix == len(paragraphs):
        return len("\n".join(paragraphs))
    return sum(len(text) + 1 for text in paragraphs[:prefix])


def _created_paragraph_indices(
    old_paragraphs: list[str],
    paragraphs: list[str],
    new_to_old: list[int | None],
) -> list[int]:
    """Return paragraph entities created by the batch text edit.

    An inserted newline creates its right-hand paragraph when it splits an
    original paragraph, even if that paragraph's text survives.  Conversely,
    inserting text plus a newline at the start of an existing paragraph
    creates only the inserted left-hand paragraph.  Retained old separators
    distinguish those cases.
    """
    if not old_paragraphs:
        return list(range(len(paragraphs)))

    old_paragraph_by_source: dict[int, int] = {}
    old_source_index = 0
    for old_index, paragraph in enumerate(old_paragraphs):
        for _ in paragraph:
            old_paragraph_by_source[old_source_index] = old_index
            old_source_index += 1
        if old_index < len(old_paragraphs) - 1:
            old_source_index += 1

    created: set[int] = set()
    global_index = 0
    starts: list[int] = []
    for paragraph in paragraphs:
        starts.append(global_index)
        global_index += len(paragraph) + 1

    for paragraph_index, paragraph in enumerate(paragraphs):
        start = starts[paragraph_index]
        end = start + len(paragraph)
        sources = [
            source
            for source in new_to_old[start:end]
            if source is not None
        ]
        if not sources:
            before_inserted = (
                paragraph_index > 0
                and new_to_old[start - 1] is None
            )
            after_inserted = (
                paragraph_index + 1 < len(paragraphs)
                and new_to_old[end] is None
            )
            if before_inserted or after_inserted:
                created.add(paragraph_index)
            continue

        current_old_paragraphs = {
            old_paragraph_by_source[source]
            for source in sources
            if source in old_paragraph_by_source
        }
        if paragraph_index == 0 or not current_old_paragraphs:
            continue

        boundary_index = start - 1
        if new_to_old[boundary_index] is not None:
            continue
        left_sources = [
            source
            for source in new_to_old[:boundary_index]
            if source is not None
        ]
        left_old_paragraphs = {
            old_paragraph_by_source[source]
            for source in left_sources
            if source in old_paragraph_by_source
        }
        if current_old_paragraphs & left_old_paragraphs:
            created.add(paragraph_index)
    return sorted(created)


def _common_prefix_chars(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    count = 0
    while count < limit and a[count] == b[count]:
        count += 1
    return count


def _common_suffix_chars(a: str, b: str, limit: int) -> int:
    count = 0
    while count < limit and a[len(a) - 1 - count] == b[len(b) - 1 - count]:
        count += 1
    return count


def text_style_property_keys(style: Any | None) -> set[str]:
    """Return effective keys explicitly represented by a text style."""
    if style is None:
        return set()
    return {
        key
        for attribute, key in _TEXT_STYLE_ATTRIBUTES.items()
        if getattr(style, attribute, None) is not None
    }


def paragraph_style_property_keys(style: Any | None) -> set[str]:
    """Return effective keys explicitly represented by a paragraph style."""
    if style is None:
        return set()
    return {
        key
        for attribute, key in _PARAGRAPH_STYLE_ATTRIBUTES.items()
        if getattr(style, attribute, None) is not None
    }


def api_style_property_keys(fields: list[str] | None) -> set[str]:
    """Translate a persistence reset field mask to effective property keys."""
    if not fields:
        return set()
    keys: set[str] = set()
    for field in fields:
        if field == "weightedFontFamily":
            keys.update({"text.font_family", "text.font_weight"})
        elif field in TEXT_STYLE_API_FIELDS:
            keys.add(TEXT_STYLE_API_FIELDS[field])
        elif field in PARAGRAPH_STYLE_API_FIELDS:
            keys.add(PARAGRAPH_STYLE_API_FIELDS[field])
    return _couple_font_family_and_weight(keys)


def effective_text_style_spans(
    element: ParsedElement,
) -> list[EffectiveTextSpan] | None:
    """Resolve element -> paragraph -> run styles over UTF-16 text spans.

    SML stores only explicit properties. A missing field at a child scope is
    therefore inherited field-by-field from its parent scope, rather than
    replacing the complete parent style.
    """
    paragraphs = element.paragraphs
    if not paragraphs:
        return []

    if element.runs:
        if len(element.runs) != len(paragraphs):
            return None
        runs_by_paragraph = element.runs
    else:
        runs_by_paragraph = [[ParsedRun(text=text)] for text in paragraphs]

    if element.paragraph_styles:
        if len(element.paragraph_styles) != len(paragraphs):
            return None
        paragraph_styles = element.paragraph_styles
    else:
        paragraph_styles = [None] * len(paragraphs)

    element_text_style = element.styles.text_style if element.styles else None
    element_paragraph_style = (
        element.styles.paragraph_style if element.styles else None
    )
    spans: list[EffectiveTextSpan] = []

    for paragraph_index, text in enumerate(paragraphs):
        runs = runs_by_paragraph[paragraph_index]
        paragraph_style = paragraph_styles[paragraph_index]
        text_length = _utf16_len(text)
        offset = 0
        for run in runs:
            run_length = _utf16_len(run.text)
            end = offset + run_length
            if end > text_length:
                return None
            properties = _resolve_properties(
                element_text_style,
                element_paragraph_style,
                paragraph_style,
                run,
            )
            spans.append(
                EffectiveTextSpan(
                    paragraph_index,
                    offset,
                    end,
                    properties,
                )
            )
            offset = end
        if offset != text_length:
            return None
        if not runs:
            spans.append(
                EffectiveTextSpan(
                    paragraph_index,
                    0,
                    0,
                    _resolve_properties(
                        element_text_style,
                        element_paragraph_style,
                        paragraph_style,
                        None,
                    ),
                )
            )
    return spans


def effective_text_style_ranges(
    old: ParsedElement,
    new: ParsedElement,
) -> list[EffectiveTextRange] | None:
    """Resolve two elements into comparable effective UTF-16 text ranges.

    The request planner uses this alongside persistence verification.  It is
    deliberately based on effective values rather than class ownership, so a
    property moved between element, paragraph, and run scope produces no
    request when the rendered value is unchanged.
    """
    old_spans = effective_text_style_spans(old)
    new_spans = effective_text_style_spans(new)
    if old_spans is None or new_spans is None:
        return None

    edit = text_edit_plan(
        old.paragraphs,
        new.paragraphs,
        old.runs or None,
        new.runs or None,
    )
    old_by_paragraph = _group_spans_by_paragraph(
        old_spans, len(old.paragraphs)
    )
    projected_old_spans = _project_old_spans_after_edit(
        old,
        old_by_paragraph,
        edit,
        new.paragraphs,
    )
    projected_old_by_paragraph = _group_spans_by_paragraph(
        projected_old_spans, len(new.paragraphs)
    )
    new_by_paragraph = _group_spans_by_paragraph(
        new_spans, len(new.paragraphs)
    )
    ranges: list[EffectiveTextRange] = []
    new_offset = 0
    for paragraph_index, text in enumerate(new.paragraphs):
        text_length = _utf16_len(text)
        if text_length == 0:
            new_offset += len(text) + 1
            continue
        old_positive = [
            span
            for span in projected_old_by_paragraph[paragraph_index]
            if span.start < span.end
        ]
        new_positive = [
            span
            for span in new_by_paragraph[paragraph_index]
            if span.start < span.end
        ]
        boundaries = {0, text_length}
        for span in old_positive + new_positive:
            boundaries.update((span.start, span.end))
        inserted_start: int | None = None
        local_offset = 0
        for code_point_index, character in enumerate(text):
            inserted = edit.new_to_old[new_offset + code_point_index] is None
            character_end = local_offset + _utf16_len(character)
            if inserted and inserted_start is None:
                inserted_start = local_offset
            elif not inserted and inserted_start is not None:
                boundaries.update((inserted_start, local_offset))
                inserted_start = None
            local_offset = character_end
        if inserted_start is not None:
            boundaries.update((inserted_start, local_offset))
        ordered = sorted(boundaries)
        for start, end in zip(ordered, ordered[1:], strict=False):
            if end <= start:
                continue
            old_properties = _properties_at(old_positive, start)
            new_properties = _properties_at(new_positive, start)
            ranges.append(
                EffectiveTextRange(
                    paragraph_index,
                    start,
                    end,
                    old_properties,
                    new_properties,
                )
            )
        new_offset += len(text) + 1
    return ranges


def _project_old_spans_after_edit(
    old: ParsedElement,
    old_by_paragraph: list[list[EffectiveTextSpan]],
    edit: TextEditPlan,
    new_paragraphs: list[str],
) -> list[EffectiveTextSpan]:
    """Project old effective values into the text that remains after editing."""
    old_joined = "\n".join(old.paragraphs)
    old_paragraph_starts: list[int] = []
    offset = 0
    for text in old.paragraphs:
        old_paragraph_starts.append(offset)
        offset += len(text) + 1

    def old_properties_at(code_point: int | None) -> dict[str, Any]:
        if code_point is None or not (0 <= code_point < len(old_joined)):
            return {}
        for paragraph_index, text in enumerate(old.paragraphs):
            paragraph_start = old_paragraph_starts[paragraph_index]
            if not paragraph_start <= code_point < paragraph_start + len(text):
                continue
            local_code_point = code_point - paragraph_start
            local_offset = _utf16_len(text[:local_code_point])
            return _properties_at(
                [
                    span
                    for span in old_by_paragraph[paragraph_index]
                    if span.start < span.end
                ],
                local_offset,
            )
        return {}

    def paragraph_properties(paragraph_index: int) -> dict[str, Any]:
        if not 0 <= paragraph_index < len(old_by_paragraph):
            return {}
        spans = old_by_paragraph[paragraph_index]
        return next(
            (
                {
                    key: value
                    for key, value in span.properties.items()
                    if key in PARAGRAPH_STYLE_PROPERTY_KEYS
                }
                for span in spans
                if span.start == span.end == 0
            ),
            next(
                (
                    {
                        key: value
                        for key, value in span.properties.items()
                        if key in PARAGRAPH_STYLE_PROPERTY_KEYS
                    }
                    for span in spans
                    if span.start < span.end
                ),
                {},
            ),
        )

    projected: list[EffectiveTextSpan] = []
    new_offset = 0
    for paragraph_index, text in enumerate(new_paragraphs):
        local_offset = 0
        current_properties: dict[str, Any] | None = None
        current_start = 0
        for code_point_index, character in enumerate(text):
            global_index = new_offset + code_point_index
            source_index = edit.new_to_old[global_index]
            if source_index is None:
                # Inserted characters have no inferred text-style anchor.
                # Paragraph properties for an existing paragraph remain
                # comparable; newly created paragraphs are planned explicitly
                # below and intentionally receive no neighbor proof here.
                properties = (
                    paragraph_properties(paragraph_index)
                    if paragraph_index not in edit.created_paragraph_indices
                    else {}
                )
            else:
                properties = old_properties_at(source_index)
            end = local_offset + (2 if ord(character) > 0xFFFF else 1)
            if current_properties is None:
                current_properties = properties
                current_start = local_offset
            elif properties != current_properties:
                projected.append(
                    EffectiveTextSpan(
                        paragraph_index,
                        current_start,
                        local_offset,
                        current_properties,
                    )
                )
                current_properties = properties
                current_start = local_offset
            local_offset = end
        if current_properties is not None:
            projected.append(
                EffectiveTextSpan(
                    paragraph_index,
                    current_start,
                    local_offset,
                    current_properties,
                )
            )
        new_offset += len(text) + 1
    return projected


def _properties_at(
    spans: list[EffectiveTextSpan], offset: int
) -> dict[str, Any]:
    for span in spans:
        if span.start <= offset < span.end:
            return span.properties
    return {}


def effective_text_styles_equivalent(
    remote: ParsedElement,
    intended: ParsedElement,
    *,
    author_removed_classes: frozenset[str] | set[str] = frozenset(),
    property_keys: set[str] | frozenset[str] | None = None,
    include_symmetric_effective_difference: bool = False,
    span_cache: dict[int, list[EffectiveTextSpan] | None] | None = None,
    allow_created_roundrect_center_alignment: bool = False,
) -> bool:
    """Compare effective text properties while ignoring scope ownership.

    The comparison is directional: only a recognized Google default that is
    present remotely and absent from the intended effective style is ignored.
    A missing or changed authored value is never ignored.
    """
    if remote.paragraphs != intended.paragraphs:
        return False
    remote_spans = _cached_effective_text_style_spans(remote, span_cache)
    intended_spans = _cached_effective_text_style_spans(intended, span_cache)
    if remote_spans is None or intended_spans is None:
        return False

    keys = set(ALL_TEXT_PROPERTY_KEYS if property_keys is None else property_keys)
    keys = _couple_font_family_and_weight(keys)
    if allow_created_roundrect_center_alignment:
        keys.discard("paragraph.alignment")
    remote_classes = _text_and_paragraph_classes(remote)
    removed = set(author_removed_classes)
    paragraphs = len(remote.paragraphs)
    remote_by_paragraph = _group_spans_by_paragraph(remote_spans, paragraphs)
    intended_by_paragraph = _group_spans_by_paragraph(intended_spans, paragraphs)

    # An empty element or paragraph has no character interval on which to
    # prove effective equivalence. Fail closed unless the element-scope
    # properties (and any zero-length paragraph scopes) agree exactly.
    if not any(span.start < span.end for span in remote_spans + intended_spans):
        pairs = [
            (
                _element_scope_properties(remote),
                _element_scope_properties(intended),
            )
        ]
        for paragraph_index in range(paragraphs):
            pairs.append(
                (
                    _zero_length_properties(
                        remote_by_paragraph[paragraph_index], paragraph_index
                    ),
                    _zero_length_properties(
                        intended_by_paragraph[paragraph_index], paragraph_index
                    ),
                )
            )
        if include_symmetric_effective_difference:
            keys.update(_symmetric_difference_keys(pairs))
            keys = _couple_font_family_and_weight(keys)
            if allow_created_roundrect_center_alignment:
                keys.discard("paragraph.alignment")
        return all(
            _maps_equal_exact(remote_properties, intended_properties, keys)
            for remote_properties, intended_properties in pairs
        )

    for paragraph_index in range(paragraphs):
        remote_for_paragraph = remote_by_paragraph[paragraph_index]
        intended_for_paragraph = intended_by_paragraph[paragraph_index]
        text_length = _utf16_len(remote.paragraphs[paragraph_index])
        remote_has_text = any(
            span.start < span.end for span in remote_for_paragraph
        )
        intended_has_text = any(
            span.start < span.end for span in intended_for_paragraph
        )
        if not remote_has_text or not intended_has_text:
            if remote_has_text != intended_has_text:
                return False
            pairs = [
                (
                    _zero_length_properties(remote_for_paragraph, paragraph_index),
                    _zero_length_properties(
                        intended_for_paragraph, paragraph_index
                    ),
                )
            ]
            if include_symmetric_effective_difference:
                keys.update(_symmetric_difference_keys(pairs))
                keys = _couple_font_family_and_weight(keys)
                if allow_created_roundrect_center_alignment:
                    keys.discard("paragraph.alignment")
            if not all(
                _maps_equal_exact(remote_properties, intended_properties, keys)
                for remote_properties, intended_properties in pairs
            ):
                return False
            continue
        pairs = list(
            _sweep_property_pairs(
                remote_for_paragraph,
                intended_for_paragraph,
                text_length,
            )
        )
        if include_symmetric_effective_difference:
            keys.update(_symmetric_difference_keys(pairs))
            keys = _couple_font_family_and_weight(keys)
            if allow_created_roundrect_center_alignment:
                keys.discard("paragraph.alignment")
        for remote_properties, intended_properties in pairs:
            if not _maps_equivalent(
                remote_properties,
                intended_properties,
                keys,
                remote_classes,
                removed,
                allow_created_roundrect_center_alignment=(
                    allow_created_roundrect_center_alignment
                ),
            ):
                return False
    return True


def _cached_effective_text_style_spans(
    element: ParsedElement,
    cache: dict[int, list[EffectiveTextSpan] | None] | None,
) -> list[EffectiveTextSpan] | None:
    if cache is None:
        return effective_text_style_spans(element)
    element_key = id(element)
    if element_key not in cache:
        cache[element_key] = effective_text_style_spans(element)
    return cache[element_key]


def _couple_font_family_and_weight(keys: set[str]) -> set[str]:
    if keys & _FONT_FAMILY_WEIGHT_KEYS:
        keys.update(_FONT_FAMILY_WEIGHT_KEYS)
    return keys


def _group_spans_by_paragraph(
    spans: list[EffectiveTextSpan], paragraphs: int
) -> list[list[EffectiveTextSpan]]:
    grouped: list[list[EffectiveTextSpan]] = [[] for _ in range(paragraphs)]
    for span in spans:
        if 0 <= span.paragraph_index < paragraphs:
            grouped[span.paragraph_index].append(span)
    return grouped


def _element_scope_properties(element: ParsedElement) -> dict[str, Any]:
    styles = element.styles
    return _resolve_properties(
        styles.text_style if styles is not None else None,
        styles.paragraph_style if styles is not None else None,
        None,
        None,
    )


def _zero_length_properties(
    spans: list[EffectiveTextSpan], paragraph_index: int
) -> dict[str, Any]:
    for span in spans:
        if (
            span.paragraph_index == paragraph_index
            and span.start == span.end == 0
        ):
            return span.properties
    return {}


def _symmetric_difference_keys(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> set[str]:
    return {
        key
        for remote_properties, intended_properties in pairs
        for key in ALL_TEXT_PROPERTY_KEYS
        if remote_properties.get(key) != intended_properties.get(key)
    }


def _maps_equal_exact(
    remote: dict[str, Any], intended: dict[str, Any], keys: set[str]
) -> bool:
    return all(remote.get(key) == intended.get(key) for key in keys)


def _sweep_property_pairs(
    remote_spans: list[EffectiveTextSpan],
    intended_spans: list[EffectiveTextSpan],
    text_length: int,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return property pairs with one linear sweep of sorted span boundaries."""
    if text_length == 0:
        return [
            (
                _zero_length_properties(remote_spans, 0),
                _zero_length_properties(intended_spans, 0),
            )
        ]

    remote_positive = [span for span in remote_spans if span.start < span.end]
    intended_positive = [
        span for span in intended_spans if span.start < span.end
    ]
    remote_index = intended_index = 0
    offset = 0
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    while offset < text_length:
        while (
            remote_index < len(remote_positive)
            and remote_positive[remote_index].end <= offset
        ):
            remote_index += 1
        while (
            intended_index < len(intended_positive)
            and intended_positive[intended_index].end <= offset
        ):
            intended_index += 1

        remote_span = (
            remote_positive[remote_index]
            if remote_index < len(remote_positive)
            and remote_positive[remote_index].start <= offset
            else None
        )
        intended_span = (
            intended_positive[intended_index]
            if intended_index < len(intended_positive)
            and intended_positive[intended_index].start <= offset
            else None
        )
        next_offset = text_length
        if remote_span is not None:
            next_offset = min(next_offset, remote_span.end)
        elif remote_index < len(remote_positive):
            next_offset = min(next_offset, remote_positive[remote_index].start)
        if intended_span is not None:
            next_offset = min(next_offset, intended_span.end)
        elif intended_index < len(intended_positive):
            next_offset = min(
                next_offset, intended_positive[intended_index].start
            )
        if next_offset <= offset:
            return []
        pairs.append(
            (
                remote_span.properties if remote_span is not None else {},
                intended_span.properties if intended_span is not None else {},
            )
        )
        offset = next_offset
    return pairs


def _resolve_properties(
    element_text_style: Any | None,
    element_paragraph_style: Any | None,
    paragraph_style: Any | None,
    run: ParsedRun | None,
) -> dict[str, Any]:
    paragraph_text_style = (
        paragraph_style.text_style if paragraph_style is not None else None
    )
    paragraph_paragraph_style = (
        paragraph_style.paragraph_style if paragraph_style is not None else None
    )
    properties: dict[str, Any] = {}
    for attribute, key in _TEXT_STYLE_ATTRIBUTES.items():
        value = _first_value(
            run.text_style if run is not None else None,
            paragraph_text_style,
            element_text_style,
            attribute=attribute,
        )
        if value is not None:
            properties[key] = value
    for attribute, key in _PARAGRAPH_STYLE_ATTRIBUTES.items():
        value = _first_value(
            paragraph_paragraph_style,
            element_paragraph_style,
            attribute=attribute,
        )
        if value is not None:
            properties[key] = value
    return properties


def _first_value(*styles: Any | None, attribute: str) -> Any | None:
    for style in styles:
        if style is None:
            continue
        value = getattr(style, attribute, None)
        if value is not None:
            return value
    return None


def _maps_equivalent(
    remote: dict[str, Any],
    intended: dict[str, Any],
    keys: set[str],
    remote_classes: set[str],
    author_removed_classes: set[str],
    *,
    allow_created_roundrect_center_alignment: bool = False,
) -> bool:
    for key in keys:
        remote_value = remote.get(key)
        intended_value = intended.get(key)
        if (
            allow_created_roundrect_center_alignment
            and key == "paragraph.alignment"
            and remote_value is TextAlignment.CENTER
            and "text-align-center" in remote_classes
        ):
            continue
        if remote_value == intended_value:
            continue
        if intended_value is not None:
            return False
        if not _is_accepted_remote_default(
            key,
            remote_value,
            remote_classes,
            author_removed_classes,
        ):
            return False
    return True


def _is_accepted_remote_default(
    key: str,
    value: Any,
    remote_classes: set[str],
    author_removed_classes: set[str],
) -> bool:
    return any(
        property_key == key
        and default_value == value
        and class_name in remote_classes
        and class_name not in author_removed_classes
        for property_key, default_value, class_name in _ACCEPTED_REMOTE_DEFAULTS
    )


def _text_and_paragraph_classes(element: ParsedElement) -> set[str]:
    classes: set[str] = set()
    if element.styles is not None:
        if element.styles.text_style is not None:
            classes.update(element.styles.text_style.to_classes())
        if element.styles.paragraph_style is not None:
            classes.update(element.styles.paragraph_style.to_classes())
    for paragraph_style in element.paragraph_styles:
        if paragraph_style is None:
            continue
        if paragraph_style.text_style is not None:
            classes.update(paragraph_style.text_style.to_classes())
        if paragraph_style.paragraph_style is not None:
            classes.update(paragraph_style.paragraph_style.to_classes())
    for paragraph in element.runs:
        for run in paragraph:
            if run.text_style is not None:
                classes.update(run.text_style.to_classes())
    return classes


def _utf16_len(text: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in text)


__all__ = [
    "ALL_TEXT_PROPERTY_KEYS",
    "PARAGRAPH_STYLE_API_FIELDS",
    "PARAGRAPH_STYLE_PROPERTY_KEYS",
    "TEXT_STYLE_API_FIELDS",
    "TEXT_STYLE_PROPERTY_KEYS",
    "api_style_property_keys",
    "EffectiveTextRange",
    "TextEditPlan",
    "effective_text_style_spans",
    "effective_text_style_ranges",
    "effective_text_styles_equivalent",
    "paragraph_style_property_keys",
    "text_edit_plan",
    "text_style_property_keys",
]
