"""Build text-edit and text-style Google Slides API requests."""

from __future__ import annotations

from typing import Any

from extraslide.classes import ParagraphStyle, TextStyle
from extraslide.class_style_requests import (
    _class_paragraph_style_to_api,
    _class_text_style_to_api,
    _create_class_text_style_request,
)
from extraslide.content_diff import ParagraphClassUpdate
from extraslide.content_parser import ParagraphStyles, ParsedRun


def _utf16_len(text: str) -> int:
    """Length of text in UTF-16 code units (the Slides API index space)."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _common_prefix_chars(a: str, b: str) -> int:
    """Number of leading characters (code points) shared by a and b.

    Trimming whole code points never splits a UTF-16 surrogate pair.
    """
    limit = min(len(a), len(b))
    count = 0
    while count < limit and a[count] == b[count]:
        count += 1
    return count


def _common_suffix_chars(a: str, b: str, limit: int) -> int:
    """Number of trailing characters shared by a and b, capped at limit.

    The cap keeps the suffix from overlapping an already-matched prefix.
    """
    count = 0
    while count < limit and a[len(a) - 1 - count] == b[len(b) - 1 - count]:
        count += 1
    return count


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
        return _create_full_text_replace_requests(google_id, new_text, new_runs)

    old_paras = old_text
    new_paras = new_text
    old_para_runs = _normalize_runs(old_paras, old_runs)
    new_para_runs = _normalize_runs(new_paras, new_runs)
    m, n = len(old_paras), len(new_paras)

    def _paragraph_unchanged(i: int, j: int) -> bool:
        return old_paras[i] == new_paras[j] and old_para_runs[i] == new_para_runs[j]

    limit = min(m, n)
    prefix = 0
    while prefix < limit and _paragraph_unchanged(prefix, prefix):
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and _paragraph_unchanged(
        m - 1 - suffix, n - 1 - suffix
    ):
        suffix += 1

    old_mid_paras = old_paras[prefix : m - suffix]
    new_mid_paras = new_paras[prefix : n - suffix]
    old_mid = "\n".join(old_mid_paras)
    new_mid = "\n".join(new_mid_paras)

    # UTF-16 offset of the changed span within the old combined text.
    # When every old paragraph matched the prefix, edits append at the end.
    if prefix == m and m > 0:
        start = _utf16_len("\n".join(old_paras))
    else:
        start = sum(_utf16_len(text) + 1 for text in old_paras[:prefix])
    end = start + _utf16_len(old_mid)

    styled = any(
        run.text_style is not None
        for para_runs in new_para_runs[prefix : n - suffix]
        for run in para_runs
    )

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
        )
    elif styled:
        # Explicit <T> runs: replace the changed paragraphs wholesale and
        # reapply their run styles below. Other paragraphs stay untouched.
        requests.append(_delete_text_request(google_id, start, end))
        requests.append(_insert_text_request(google_id, start, new_mid))
    else:
        # Within the changed paragraphs, trim the common prefix/suffix and
        # touch only the span that actually differs.
        a = _common_prefix_chars(old_mid, new_mid)
        b = _common_suffix_chars(
            old_mid, new_mid, min(len(old_mid), len(new_mid)) - a
        )
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
                google_id, new_runs, paragraph_range=(prefix, n - suffix)
            )
        )

    return requests


def _create_full_text_replace_requests(
    google_id: str,
    new_text: list[str],
    new_runs: list[list[ParsedRun]] | None = None,
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
            requests.extend(_create_run_style_requests(google_id, new_runs))

    return requests

def _create_run_style_requests(
    object_id: str,
    runs: list[list[ParsedRun]],
    paragraph_range: tuple[int, int] | None = None,
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
                    requests.append(request)
            index = end_index

    return requests


def _create_run_style_delta_requests(
    object_id: str,
    paragraphs: list[str],
    old_runs: list[list[ParsedRun]],
    new_runs: list[list[ParsedRun]],
    paragraph_range: tuple[int, int],
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


_TEXT_STYLE_FIELD_NAMES = {
    "bold": "bold",
    "italic": "italic",
    "underline": "underline",
    "strikethrough": "strikethrough",
    "small_caps": "smallCaps",
    "baseline_offset": "baselineOffset",
    "font_family": "fontFamily",
    "font_size_pt": "fontSize",
    "font_weight": "weightedFontFamily",
    "foreground_color": "foregroundColor",
    "background_color": "backgroundColor",
    "link": "link",
}
_PARAGRAPH_STYLE_FIELD_NAMES = {
    "alignment": "alignment",
    "line_spacing": "lineSpacing",
    "space_above_pt": "spaceAbove",
    "space_below_pt": "spaceBelow",
    "indent_start_pt": "indentStart",
    "indent_end_pt": "indentEnd",
    "indent_first_line_pt": "indentFirstLine",
    "direction": "direction",
    "spacing_mode": "spacingMode",
}


def _changed_style_fields(
    old_style: Any,
    new_style: Any,
    field_names: dict[str, str],
) -> list[str]:
    """Return API fields whose typed style values changed."""
    return [
        api_name
        for attr_name, api_name in field_names.items()
        if getattr(old_style, attr_name, None) != getattr(new_style, attr_name, None)
    ]


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
