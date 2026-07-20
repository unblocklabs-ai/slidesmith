"""Local bulk replacement of SML class tokens."""

from __future__ import annotations

import re
import stat
import tempfile
from dataclasses import dataclass
from os import replace as replace_file
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
    content_paths = sorted((folder / "slides").glob("*/content.sml"))
    if not content_paths:
        raise ValueError(f"No content.sml files found under {folder / 'slides'}")

    replacements: dict[Path, str] = {}
    counts: dict[str, int] = {}
    swap_counts = {swap: 0 for swap in swaps}
    for content_path in content_paths:
        content = content_path.read_text(encoding="utf-8")
        updated, per_swap_counts = _replace_in_content(content, swaps)
        parse_slide_content(updated)
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

    def replace_tag(tag_match: re.Match[str]) -> str:
        if tag_match.group("tag") == "Slide":
            return tag_match.group(0)

        def replace_attribute(attribute_match: re.Match[str]) -> str:
            value = attribute_match.group("value")

            def replace_token(token_match: re.Match[str]) -> str:
                old_class = token_match.group(0)
                new_class = replacements.get(old_class)
                if new_class is None:
                    return old_class
                counts[(old_class, new_class)] += 1
                return new_class

            updated = re.sub(r"\S+", replace_token, value)
            return (
                attribute_match.group("prefix")
                + attribute_match.group("quote")
                + updated
                + attribute_match.group("quote")
            )

        return _CLASS_ATTRIBUTE_RE.sub(replace_attribute, tag_match.group(0))

    return _START_TAG_RE.sub(replace_tag, content), counts


def _commit_replacements(replacements: dict[Path, str]) -> None:
    """Commit prepared contents and restore originals if a write fails."""
    changed = {
        path: updated
        for path, updated in replacements.items()
        if updated != path.read_text(encoding="utf-8")
    }
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    committed: list[Path] = []
    try:
        for path, updated in changed.items():
            mode = stat.S_IMODE(path.stat().st_mode)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(updated)
                staged[path] = Path(temporary.name)
            staged[path].chmod(mode)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.backup.",
                suffix=".tmp",
                delete=False,
            ) as backup:
                backup.write(path.read_bytes())
                backups[path] = Path(backup.name)
            backups[path].chmod(mode)
        for path, temporary_path in staged.items():
            replace_file(temporary_path, path)
            committed.append(path)
    except Exception:
        for path in reversed(committed):
            replace_file(backups[path], path)
        raise
    finally:
        for temporary_path in [*staged.values(), *backups.values()]:
            temporary_path.unlink(missing_ok=True)
