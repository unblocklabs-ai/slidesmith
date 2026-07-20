"""Local bulk replacement of SML class tokens."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from slidesmith.engine.content_parser import parse_element_classes, parse_slide_content

_START_TAG_RE = re.compile(
    r"<(?P<tag>[A-Za-z][^\s/>]*)(?P<attributes>(?:\s+[^<>]*?)?)(?P<close>/?)>",
    re.DOTALL,
)
_CLASS_ATTRIBUTE_RE = re.compile(
    r"(?P<prefix>\bclass\s*=\s*)(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)


@dataclass(frozen=True)
class ClassReplacementResult:
    """Replacement counts for one local deck workspace."""

    counts: dict[str, int]

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
    _validate_class_token(old_class, label="OLD")
    _validate_class_token(new_class, label="NEW")
    parse_element_classes(new_class, "replace-class validation")

    folder = Path(folder_path)
    content_paths = sorted((folder / "slides").glob("*/content.sml"))
    if not content_paths:
        raise ValueError(f"No content.sml files found under {folder / 'slides'}")

    replacements: dict[Path, str] = {}
    counts: dict[str, int] = {}
    for content_path in content_paths:
        content = content_path.read_text(encoding="utf-8")
        updated, count = _replace_in_content(content, old_class, new_class)
        parse_slide_content(updated)
        replacements[content_path] = updated
        counts[content_path.parent.name] = count

    if not dry_run:
        for content_path, updated in replacements.items():
            if updated != content_path.read_text(encoding="utf-8"):
                content_path.write_text(updated, encoding="utf-8")

    return ClassReplacementResult(counts=counts)


def _validate_class_token(value: str, *, label: str) -> None:
    if not value or len(value.split()) != 1:
        raise ValueError(f"{label} must be exactly one whitespace-free class token")


def _replace_in_content(
    content: str, old_class: str, new_class: str
) -> tuple[str, int]:
    """Replace class tokens in XML start tags without reformatting the SML."""
    total = 0

    def replace_tag(tag_match: re.Match[str]) -> str:
        nonlocal total
        if tag_match.group("tag") == "Slide":
            return tag_match.group(0)

        def replace_attribute(attribute_match: re.Match[str]) -> str:
            nonlocal total
            value = attribute_match.group("value")
            token_pattern = rf"(?<!\S){re.escape(old_class)}(?!\S)"
            updated, count = re.subn(token_pattern, new_class, value)
            total += count
            return (
                attribute_match.group("prefix")
                + attribute_match.group("quote")
                + updated
                + attribute_match.group("quote")
            )

        return _CLASS_ATTRIBUTE_RE.sub(replace_attribute, tag_match.group(0))

    return _START_TAG_RE.sub(replace_tag, content), total
