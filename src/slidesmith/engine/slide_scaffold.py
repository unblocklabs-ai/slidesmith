"""Local scaffolding for first-class new-slide authoring."""

from __future__ import annotations

import re
import secrets
import zipfile
from dataclasses import dataclass
from pathlib import Path

from slidesmith.engine.content_parser import (
    SLIDE_INSERTION_INDEX_ATTRIBUTE,
    flatten_elements,
    parse_slide_document,
)
from slidesmith.engine.components import load_components
from slidesmith.engine.id_manager import is_valid_google_object_id
from slidesmith.engine.json_utils import read_json


@dataclass(frozen=True)
class ScaffoldResult:
    """The planned or written result of ``add-slide``."""

    slide_folder: Path
    content_path: Path
    slide_id: str
    layout: str
    insertion_index: int | None
    dry_run: bool


def scaffold_slide(
    folder: str | Path,
    *,
    after: int | None = None,
    at: int | None = None,
    layout: str | None = None,
    blank: bool = False,
    slide_id: str | None = None,
    dry_run: bool = False,
) -> ScaffoldResult:
    """Plan or write a new local slide folder without contacting Google."""
    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise ValueError(f"Presentation folder does not exist: {folder_path}")
    if after is not None and at is not None:
        raise ValueError("--after and --at are mutually exclusive")
    if layout is not None and blank:
        raise ValueError("--layout and --blank are mutually exclusive")
    if layout not in {None, "title-body"}:
        raise ValueError(f"Unknown slide layout {layout!r}; expected 'title-body'")

    slides_dir = folder_path / "slides"
    existing_indices = _existing_slide_indices(slides_dir)
    slide_count = _original_slide_count(folder_path, slides_dir)
    insertion_index = _resolve_insertion_index(after, at, slide_count)
    target_index = max(existing_indices, default=0) + 1
    while (slides_dir / f"{target_index:02d}").exists():
        target_index += 1
    target_folder = slides_dir / f"{target_index:02d}"
    content_path = target_folder / "content.sml"

    existing_ids = _existing_clean_ids(folder_path, slides_dir)
    chosen_id = _choose_slide_id(slide_id, existing_ids)
    chosen_layout = "blank" if blank or layout is None else layout
    page_width, page_height = _page_size(folder_path)
    content = _slide_content(
        chosen_id,
        insertion_index=insertion_index,
        layout=chosen_layout,
        existing_ids=existing_ids,
        page_width=page_width,
        page_height=page_height,
    )

    if not dry_run:
        slides_dir.mkdir(parents=True, exist_ok=True)
        try:
            target_folder.mkdir()
        except FileExistsError as exc:
            raise ValueError(
                f"Refusing to overwrite existing slide folder: {target_folder}"
            ) from exc
        content_path.write_text(content, encoding="utf-8")

    return ScaffoldResult(
        slide_folder=target_folder,
        content_path=content_path,
        slide_id=chosen_id,
        layout=chosen_layout,
        insertion_index=insertion_index,
        dry_run=dry_run,
    )


def _existing_slide_indices(slides_dir: Path) -> set[int]:
    if not slides_dir.exists():
        return set()
    indices: set[int] = set()
    for child in slides_dir.iterdir():
        if not child.is_dir() or not re.fullmatch(r"\d+", child.name):
            continue
        indices.add(int(child.name))
    return indices


def _original_slide_count(folder: Path, slides_dir: Path) -> int:
    """Count pulled slides, excluding pending authoring scaffolds."""
    current_indices = _current_slide_indices(slides_dir)
    if not current_indices:
        return 0

    # A pulled slide is identified by its clean slide ID or one of its element
    # IDs being present in the pulled mapping.  Pending scaffolds have neither.
    mapping = read_json(folder / "id_mapping.json", missing_ok=True)
    if mapping:
        components = load_components(folder)
        original_indices: set[str] = set()
        for child in sorted(slides_dir.iterdir(), key=lambda path: path.name):
            if child.name not in current_indices:
                continue
            content_path = child / "content.sml"
            roots, metadata = parse_slide_document(
                content_path.read_text(encoding="utf-8"), components=components
            )
            element_ids = flatten_elements(roots)
            if metadata.slide_id in mapping or element_ids.keys() & mapping.keys():
                original_indices.add(child.name)
        if original_indices:
            return len(original_indices)

    # Older or hand-created workspaces may lack a usable mapping.  The pristine
    # archive is the same pulled-slide source used by content_diff, and never
    # contains local pending scaffolds.
    pristine_indices = _pristine_slide_indices(folder)
    if pristine_indices is not None:
        return len(current_indices & pristine_indices)

    # Last-resort compatibility for workspaces without the archive: bound the
    # pulled slide count from the raw pulled deck metadata.  A brand-new
    # workspace has neither signal and therefore correctly reports zero.
    base = read_json(folder / ".pristine" / "base.json", missing_ok=True)
    raw_slides = base.get("slides")
    if not isinstance(raw_slides, list):
        presentation = read_json(folder / "presentation.json", missing_ok=True)
        raw_count = presentation.get("slideCount")
        if not isinstance(raw_count, int):
            slide_order = presentation.get("slideOrder")
            raw_count = len(slide_order) if isinstance(slide_order, list) else 0
    else:
        raw_count = len(raw_slides)
    return sum(1 for index in current_indices if int(index) <= raw_count)


def _current_slide_indices(slides_dir: Path) -> set[str]:
    if not slides_dir.exists():
        return set()
    return {
        child.name
        for child in slides_dir.iterdir()
        if child.is_dir()
        and re.fullmatch(r"\d+", child.name)
        and (child / "content.sml").exists()
    }


def _pristine_slide_indices(folder: Path) -> set[str] | None:
    archive_path = folder / ".pristine" / "presentation.zip"
    if not archive_path.exists():
        return None
    try:
        with zipfile.ZipFile(archive_path) as archive:
            return {
                parts[1]
                for name in archive.namelist()
                if (parts := name.split("/"))
                and len(parts) == 3
                and parts[0] == "slides"
                and parts[2] == "content.sml"
                and re.fullmatch(r"\d+", parts[1])
            }
    except (OSError, zipfile.BadZipFile):
        return None


def _resolve_insertion_index(
    after: int | None,
    at: int | None,
    slide_count: int,
) -> int | None:
    if after is not None:
        if not 1 <= after <= slide_count:
            if after > slide_count:
                raise ValueError(
                    f"--after {after} exceeds deck length {slide_count}; "
                    f"expected an existing 1-based slide (1-{slide_count})"
                )
            raise ValueError(
                f"--after must name an existing 1-based slide (1-{slide_count})"
            )
        return after
    if at is not None:
        if not 1 <= at <= slide_count + 1:
            if at > slide_count + 1:
                raise ValueError(
                    f"--at {at} exceeds deck length {slide_count}; "
                    f"expected a 1-based insertion position from 1-{slide_count + 1}"
                )
            raise ValueError(
                f"--at must be a 1-based position from 1-{slide_count + 1}"
            )
        return at - 1
    return None


def _existing_clean_ids(folder: Path, slides_dir: Path) -> set[str]:
    mapping = read_json(folder / "id_mapping.json", missing_ok=True)
    existing = set(mapping) if isinstance(mapping, dict) else set()
    components = load_components(folder)
    if not slides_dir.exists():
        return existing
    for content_path in sorted(slides_dir.glob("*/content.sml")):
        roots, metadata = parse_slide_document(
            content_path.read_text(encoding="utf-8"), components=components
        )
        if metadata.slide_id:
            existing.add(metadata.slide_id)
        existing.update(flatten_elements(roots))
    return existing


def _page_size(folder: Path) -> tuple[float, float]:
    """Read the QA/lint page size, falling back to the standard slide size."""
    metadata = read_json(folder / "presentation.json", missing_ok=True)
    page_size = metadata.get("pageSize") if isinstance(metadata, dict) else None
    if isinstance(page_size, dict):
        try:
            width = float(page_size["width"])
            height = float(page_size["height"])
        except (KeyError, TypeError, ValueError):
            pass
        else:
            if width > 0 and height > 0:
                return width, height
    return 960.0, 540.0


def _choose_slide_id(requested: str | None, existing_ids: set[str]) -> str:
    if requested is not None:
        if requested.startswith("new_"):
            raise ValueError(
                "Invalid slide ID: IDs beginning with reserved 'new_' are not allowed"
            )
        if not is_valid_google_object_id(requested):
            raise ValueError(
                "Invalid slide ID: use 5-50 characters, starting with a letter or "
                "underscore, containing only letters, digits, underscores, or hyphens"
            )
        if requested in existing_ids:
            raise ValueError(f"Slide ID {requested!r} is already in use")
        return requested

    while True:
        candidate = f"slide_{secrets.token_hex(8)}"
        if candidate not in existing_ids:
            return candidate


def _unique_element_id(slide_id: str, suffix: str, existing_ids: set[str]) -> str:
    stem = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{slide_id}_{suffix}")
    stem = stem[:50]
    candidate = stem
    number = 2
    while candidate in existing_ids:
        marker = f"_{number}"
        candidate = f"{stem[:50 - len(marker)]}{marker}"
        number += 1
    return candidate


def _slide_content(
    slide_id: str,
    *,
    insertion_index: int | None,
    layout: str,
    existing_ids: set[str],
    page_width: float,
    page_height: float,
) -> str:
    root_attrs = [f'id="{slide_id}"']
    if insertion_index is not None:
        root_attrs.append(
            f'{SLIDE_INSERTION_INDEX_ATTRIBUTE}="{insertion_index}"'
        )
    lines = [f"<Slide {' '.join(root_attrs)}>"]
    if layout == "title-body":
        scale_x = page_width / 960.0
        scale_y = page_height / 540.0
        margin_x = min(48.0 * scale_x, page_width / 2)
        title_x = max(0.0, min(margin_x, page_width))
        title_y = max(0.0, min(36.0 * scale_y, page_height))
        title_w = max(0.0, min(864.0 * scale_x, page_width - title_x))
        title_h = max(0.0, min(64.0 * scale_y, page_height - title_y))
        body_x = title_x
        body_y = max(0.0, min(120.0 * scale_y, page_height))
        body_w = max(0.0, min(864.0 * scale_x, page_width - body_x))
        body_h = max(0.0, min(360.0 * scale_y, page_height - body_y))
        title_id = _unique_element_id(slide_id, "title", existing_ids)
        existing_ids = existing_ids | {title_id}
        body_id = _unique_element_id(slide_id, "body", existing_ids)
        lines.extend(
            [
                f'  <TextBox id="{title_id}" x="{title_x:g}" y="{title_y:g}" w="{title_w:g}" h="{title_h:g}" class="text-size-28">',
                "    <P>Title</P>",
                "  </TextBox>",
                f'  <TextBox id="{body_id}" x="{body_x:g}" y="{body_y:g}" w="{body_w:g}" h="{body_h:g}" class="text-size-16">',
                "    <P>Body</P>",
                "  </TextBox>",
            ]
        )
    lines.append("</Slide>")
    return "\n".join(lines) + "\n"
