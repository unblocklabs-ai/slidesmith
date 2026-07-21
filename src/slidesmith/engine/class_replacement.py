"""Local bulk replacement of SML class tokens."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from slidesmith.engine.atomic_files import commit_text_files
from slidesmith.engine.content_parser import parse_element_classes, parse_slide_content
from slidesmith.engine.components import load_components

@dataclass(frozen=True)
class ClassReplacementResult:
    """Replacement counts for one local deck workspace."""

    counts: dict[str, int]
    swap_counts: dict[tuple[str, str], int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def replace_class(
    folder_path: str | Path,
    old_class: str,
    new_class: str,
    *,
    dry_run: bool = False,
) -> ClassReplacementResult:
    """Replace one class token in every slide, validating before any write."""
    return replace_classes(
        folder_path,
        [(old_class, new_class)],
        dry_run=dry_run,
    )


def replace_classes(
    folder_path: str | Path,
    swaps: list[tuple[str, str]],
    *,
    dry_run: bool = False,
) -> ClassReplacementResult:
    """Replace class tokens together, validating the whole deck before writing."""
    if not swaps:
        raise ValueError("At least one OLD=NEW class swap is required")

    seen_old: set[str] = set()
    for old_class, new_class in swaps:
        _validate_class_token(old_class, label="OLD")
        _validate_class_token(new_class, label="NEW")
        parse_element_classes(new_class, "replace-class validation")
        if old_class in seen_old:
            raise ValueError(f"OLD class '{old_class}' appears in more than one swap")
        seen_old.add(old_class)

    folder = Path(folder_path)
    components = load_components(folder)
    content_paths = sorted((folder / "slides").glob("*/content.sml"))
    if not content_paths:
        raise ValueError(f"No content.sml files found under {folder / 'slides'}")

    replacements: dict[Path, str] = {}
    counts: dict[str, int] = {}
    swap_counts = {swap: 0 for swap in swaps}
    for content_path in content_paths:
        content = content_path.read_text(encoding="utf-8")
        updated, per_swap_counts = _replace_in_content(content, swaps)
        parse_slide_content(updated, components=components)
        replacements[content_path] = updated
        counts[content_path.parent.name] = sum(per_swap_counts.values())
        for swap, count in per_swap_counts.items():
            swap_counts[swap] += count

    if not dry_run:
        _commit_replacements(replacements)

    return ClassReplacementResult(counts=counts, swap_counts=swap_counts)


def _validate_class_token(value: str, *, label: str) -> None:
    if not value or len(value.split()) != 1:
        raise ValueError(f"{label} must be exactly one whitespace-free class token")


def _replace_in_content(
    content: str, swaps: list[tuple[str, str]]
) -> tuple[str, dict[tuple[str, str], int]]:
    """Replace class tokens in XML start tags without reformatting the SML."""
    replacements = dict(swaps)
    counts = {swap: 0 for swap in swaps}
    pieces: list[str] = []
    cursor = 0
    for start, end, tag_name in _start_tag_spans(content):
        pieces.append(content[cursor:start])
        start_tag = content[start:end]
        if tag_name != "Slide":
            start_tag = _replace_class_values(
                start_tag,
                tag_name,
                replacements,
                counts,
            )
        pieces.append(start_tag)
        cursor = end
    pieces.append(content[cursor:])
    return "".join(pieces), counts


def _start_tag_spans(content: str) -> list[tuple[int, int, str]]:
    """Tokenize XML start tags while respecting quoted ``>`` characters."""
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    while (start := content.find("<", cursor)) != -1:
        if content.startswith("<!--", start):
            comment_end = content.find("-->", start + 4)
            cursor = len(content) if comment_end == -1 else comment_end + 3
            continue
        name_start = start + 1
        if name_start >= len(content) or not content[name_start].isalpha():
            cursor = name_start
            continue
        name_end = name_start + 1
        while (
            name_end < len(content)
            and not content[name_end].isspace()
            and content[name_end] not in "/>"
        ):
            name_end += 1
        tag_name = content[name_start:name_end]
        quote: str | None = None
        position = name_end
        while position < len(content):
            character = content[position]
            if quote is not None:
                if character == quote:
                    quote = None
            elif character in {"\"", "'"}:
                quote = character
            elif character == ">":
                spans.append((start, position + 1, tag_name))
                cursor = position + 1
                break
            position += 1
        else:
            break
    return spans


def _replace_class_values(
    start_tag: str,
    tag_name: str,
    replacements: dict[str, str],
    counts: dict[tuple[str, str], int],
) -> str:
    """Replace tokens only inside attributes whose complete name is class."""
    value_spans: list[tuple[int, int]] = []
    position = 1 + len(tag_name)
    while position < len(start_tag):
        while position < len(start_tag) and start_tag[position].isspace():
            position += 1
        if position >= len(start_tag) or start_tag[position] in "/>":
            break
        name_start = position
        while (
            position < len(start_tag)
            and not start_tag[position].isspace()
            and start_tag[position] not in "=/>"
        ):
            position += 1
        attribute_name = start_tag[name_start:position]
        while position < len(start_tag) and start_tag[position].isspace():
            position += 1
        if position >= len(start_tag) or start_tag[position] != "=":
            continue
        position += 1
        while position < len(start_tag) and start_tag[position].isspace():
            position += 1
        if position >= len(start_tag) or start_tag[position] not in {"\"", "'"}:
            continue
        quote = start_tag[position]
        value_start = position + 1
        value_end = start_tag.find(quote, value_start)
        if value_end == -1:
            break
        if attribute_name == "class":
            value_spans.append((value_start, value_end))
        position = value_end + 1

    pieces: list[str] = []
    cursor = 0
    for value_start, value_end in value_spans:
        pieces.append(start_tag[cursor:value_start])

        def replace_token(token_match: re.Match[str]) -> str:
            old_class = token_match.group(0)
            new_class = replacements.get(old_class)
            if new_class is None:
                return old_class
            counts[(old_class, new_class)] += 1
            return new_class

        pieces.append(
            re.sub(r"\S+", replace_token, start_tag[value_start:value_end])
        )
        cursor = value_end
    pieces.append(start_tag[cursor:])
    return "".join(pieces)


def _commit_replacements(replacements: dict[Path, str]) -> None:
    """Commit prepared contents and restore originals if a write fails."""
    commit_text_files(replacements, allow_create=False)
