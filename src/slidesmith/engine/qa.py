"""Offline visual QA for a materialized Slidesmith presentation folder."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from defusedxml import ElementTree as DefusedET
from PIL import Image, ImageDraw, ImageFont

from slidesmith.engine.bounds import BoundingBox
from slidesmith.engine.content_parser import (
    QA_ACCEPT_CLASS_PREFIX,
    ParsedElement,
    ParsedRun,
    parse_all_slides,
)
from slidesmith.engine.json_utils import read_json
from slidesmith.engine.layout import ApproximateTextMeasurer, TextMeasurer

OVERLAP_THRESHOLD = 0.15
TEXT_OVERFLOW_TOLERANCE = 1.10
QA_BASELINE_FILE = "qa-baseline.json"
ACCEPTED_FINDINGS_FILE = "accepted.json"
CONTACT_SHEET_COLUMNS = 2
CONTACT_SHEET_PADDING = 12
CONTACT_SHEET_GAP = 12
CONTACT_SHEET_LABEL_HEIGHT = 24


async def download_thumbnails(
    transport: Any, folder: Path, qa_dir: Path
) -> list[Path]:
    """Download each materialized slide thumbnail in numeric slide order."""
    metadata = read_json(folder / "presentation.json", missing_ok=False)
    id_mapping = read_json(folder / "id_mapping.json", missing_ok=False)
    presentation_id = metadata["presentationId"]
    content_paths = (folder / "slides").glob("*/content.sml")
    output_paths: list[Path] = []
    for content_path in sorted(
        content_paths, key=lambda path: int(path.parent.name)
    ):
        slide_number = content_path.parent.name
        slide_clean_id = DefusedET.fromstring(
            content_path.read_text(encoding="utf-8")
        ).get("id")
        if not slide_clean_id or slide_clean_id not in id_mapping:
            raise ValueError(f"No Google page object ID for slide {slide_number}")
        png = await transport.get_page_thumbnail(
            presentation_id, id_mapping[slide_clean_id]
        )
        output_path = qa_dir / f"slide-{slide_number}.png"
        output_path.write_bytes(png)
        output_paths.append(output_path)
        print(output_path, flush=True)
    return output_paths


def create_contact_sheet(
    qa_dir: str | Path,
    thumbnail_paths: Sequence[Path] | None = None,
) -> Path:
    """Compose downloaded slide PNGs into a labeled two-column contact sheet."""
    qa_path = Path(qa_dir)
    selected_paths = sorted(
        thumbnail_paths if thumbnail_paths is not None else qa_path.glob("slide-*.png"),
        key=lambda path: int(path.stem.removeprefix("slide-")),
    )
    if not selected_paths:
        raise ValueError(f"No slide thumbnails found in {qa_path}")

    thumbnails: list[tuple[Path, Image.Image]] = []
    sheet: Image.Image | None = None
    try:
        for path in selected_paths:
            with Image.open(path) as source:
                thumbnails.append((path, source.convert("RGB")))

        max_width = max(image.width for _, image in thumbnails)
        max_height = max(image.height for _, image in thumbnails)
        cell_width = max_width + 2 * CONTACT_SHEET_PADDING
        cell_height = (
            max_height + CONTACT_SHEET_LABEL_HEIGHT + 2 * CONTACT_SHEET_PADDING
        )
        rows = (len(thumbnails) + CONTACT_SHEET_COLUMNS - 1) // CONTACT_SHEET_COLUMNS
        sheet_width = (
            CONTACT_SHEET_COLUMNS * cell_width
            + (CONTACT_SHEET_COLUMNS - 1) * CONTACT_SHEET_GAP
        )
        sheet_height = rows * cell_height + (rows - 1) * CONTACT_SHEET_GAP
        sheet = Image.new("RGB", (sheet_width, sheet_height), "white")
        draw = ImageDraw.Draw(sheet)
        font = ImageFont.load_default()

        for index, (path, thumbnail) in enumerate(thumbnails):
            row, column = divmod(index, CONTACT_SHEET_COLUMNS)
            cell_x = column * (cell_width + CONTACT_SHEET_GAP)
            cell_y = row * (cell_height + CONTACT_SHEET_GAP)
            slide_number = int(path.stem.removeprefix("slide-"))
            draw.text(
                (cell_x + CONTACT_SHEET_PADDING, cell_y + CONTACT_SHEET_PADDING),
                f"Slide {slide_number}",
                fill="black",
                font=font,
            )
            image_x = cell_x + CONTACT_SHEET_PADDING + (max_width - thumbnail.width) // 2
            image_y = cell_y + CONTACT_SHEET_PADDING + CONTACT_SHEET_LABEL_HEIGHT
            sheet.paste(thumbnail, (image_x, image_y))

        output_path = qa_path / "contact-sheet.png"
        sheet.save(output_path, format="PNG")
        return output_path
    finally:
        if sheet is not None:
            sheet.close()
        for _, thumbnail in thumbnails:
            thumbnail.close()


@dataclass(frozen=True)
class Finding:
    """One actionable geometry issue in a slide."""

    severity: str
    rule: str
    element_ids: tuple[str, ...]
    slide_number: int
    description: str
    suggested_fix: str


def _finding_key(finding: Finding) -> tuple[str, tuple[str, ...], int]:
    """Stable identity for comparing lint results across workspace refreshes."""
    return finding.rule, tuple(sorted(finding.element_ids)), finding.slide_number


def finding_id(finding: Finding) -> str:
    """Return the stable, CLI-facing identity for a QA finding."""
    element_ids = ",".join(sorted(finding.element_ids))
    return f"{finding.rule}:{finding.slide_number}:{element_ids}"


def _accepted_path(folder: Path) -> Path:
    return folder / ".qa" / ACCEPTED_FINDINGS_FILE


def _read_accepted_findings(folder: Path) -> dict[str, dict[str, Any]]:
    path = _accepted_path(folder)
    data = read_json(path, missing_ok=True)
    raw_accepted = data.get("accepted", {})
    if not isinstance(raw_accepted, dict):
        raise ValueError(f"Expected an accepted object in {path}")
    return {
        str(identity): value
        for identity, value in raw_accepted.items()
        if isinstance(value, dict)
    }


def _write_accepted_findings(
    folder: Path, accepted: dict[str, dict[str, Any]]
) -> Path:
    path = _accepted_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"version": 1, "accepted": dict(sorted(accepted.items()))},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _acceptance_record(finding: Finding) -> dict[str, Any]:
    return {
        "rule": finding.rule,
        "slide": finding.slide_number,
        "elementIds": sorted(finding.element_ids),
    }


def _sugar_accepted_ids(folder: Path, findings: list[Finding]) -> set[str]:
    """Match qa-accept-* element classes to current findings."""
    signals: set[tuple[str, str, int]] = set()
    for content_path in sorted((folder / "slides").glob("*/content.sml")):
        slide_number = int(content_path.parent.name)
        root = DefusedET.fromstring(content_path.read_text(encoding="utf-8"))
        for element in root.iter():
            element_id = element.get("id")
            if not element_id:
                continue
            for class_name in element.get("class", "").split():
                if class_name.startswith(QA_ACCEPT_CLASS_PREFIX):
                    rule = class_name.removeprefix(QA_ACCEPT_CLASS_PREFIX)
                    signals.add((rule.replace("-", "_").upper(), element_id, slide_number))

    return {
        finding_id(finding)
        for finding in findings
        if any(
            (finding.rule, element_id, finding.slide_number) in signals
            for element_id in finding.element_ids
        )
    }


def _finding_to_dict(finding: Finding) -> dict[str, Any]:
    return {
        "severity": finding.severity,
        "rule": finding.rule,
        "elementIds": list(finding.element_ids),
        "slideNumber": finding.slide_number,
        "description": finding.description,
        "suggestedFix": finding.suggested_fix,
    }


def _finding_from_dict(data: dict[str, Any]) -> Finding:
    return Finding(
        severity=str(data["severity"]),
        rule=str(data["rule"]),
        element_ids=tuple(str(value) for value in data["elementIds"]),
        slide_number=int(data["slideNumber"]),
        description=str(data["description"]),
        suggested_fix=str(data["suggestedFix"]),
    )


def record_qa_baseline(folder: str | Path) -> Path:
    """Persist the pull-time offline lint findings under ``.pristine``."""
    folder_path = Path(folder)
    baseline_path = folder_path / ".pristine" / QA_BASELINE_FILE
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    findings = lint_folder(folder_path)
    baseline_path.write_text(
        json.dumps(
            {"version": 1, "findings": [_finding_to_dict(item) for item in findings]},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return baseline_path


def _read_qa_baseline(folder: Path) -> list[Finding] | None:
    baseline_path = folder / ".pristine" / QA_BASELINE_FILE
    if not baseline_path.exists():
        return None
    data = read_json(baseline_path, missing_ok=False)
    raw_findings = data.get("findings", [])
    if not isinstance(raw_findings, list):
        raise ValueError(f"Expected a findings list in {baseline_path}")
    return [_finding_from_dict(item) for item in raw_findings]


def lint_folder(
    folder: str | Path,
    *,
    text_measurer: TextMeasurer | None = None,
) -> list[Finding]:
    """Analyze the current SML projection without making network calls."""
    folder_path = Path(folder)
    metadata = read_json(folder_path / "presentation.json", missing_ok=False)
    page_size = metadata.get("pageSize", {})
    try:
        page_width = float(page_size["width"])
        page_height = float(page_size["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid pageSize in {folder_path / 'presentation.json'}"
        ) from exc
    if page_width <= 0 or page_height <= 0:
        raise ValueError("Presentation pageSize width and height must be positive")

    styles_path = folder_path / "styles.json"
    styles = read_json(styles_path, missing_ok=True)
    slides = parse_all_slides(str(folder_path / "slides"))
    measurer = text_measurer or ApproximateTextMeasurer()

    findings: list[Finding] = []
    for slide_name, roots in slides.items():
        try:
            slide_number = int(slide_name)
        except ValueError as exc:
            raise ValueError(f"Slide folder name must be numeric: {slide_name}") from exc

        findings.extend(_find_overlaps(roots, slide_number))
        for element in _walk(roots):
            box = _box(element)
            if box is None:
                continue
            if (
                box.x < 0
                or box.y < 0
                or box.x2 > page_width
                or box.y2 > page_height
            ):
                findings.append(
                    Finding(
                        severity="WARNING",
                        rule="OUT_OF_BOUNDS",
                        element_ids=(element.clean_id,),
                        slide_number=slide_number,
                        description=(
                            f"Element {element.clean_id} extends beyond the "
                            f"{page_width:g} x {page_height:g} pt page."
                        ),
                        suggested_fix=(
                            "Move or resize the element so every edge is inside "
                            "the page."
                        ),
                    )
                )

            text = "\n".join(element.paragraphs)
            if not text:
                continue
            family, size, weight = _text_metrics(
                element, styles.get(element.clean_id, {})
            )
            measured_height = (
                float("inf")
                if box.w <= 0
                else measurer.measure_wrapped_height(
                    text,
                    family,
                    size,
                    weight,
                    box.w,
                )
            )
            if measured_height > box.h * TEXT_OVERFLOW_TOLERANCE:
                needed = (
                    "an unbounded amount"
                    if measured_height == float("inf")
                    else f"about {measured_height:.1f} pt"
                )
                findings.append(
                    Finding(
                        severity="WARNING",
                        rule="TEXT_OVERFLOW",
                        element_ids=(element.clean_id,),
                        slide_number=slide_number,
                        description=(
                            f"Element {element.clean_id} needs {needed} of text height "
                            f"but is {box.h:.1f} pt tall; likely overflow "
                            "(approximate measurement)."
                        ),
                        suggested_fix=(
                            "Increase the element height or width, shorten the "
                            "text, or reduce its font size."
                        ),
                    )
                )

    return findings


def print_report(
    findings: list[Finding],
    output: Callable[[str], None] = print,
    *,
    baseline: list[Finding] | None = None,
    accepted_ids: set[str] | None = None,
) -> None:
    """Print findings in a stable, agent-readable two-line format."""
    accepted_ids = accepted_ids or set()
    accepted_count = sum(finding_id(item) in accepted_ids for item in findings)
    active_count = len(findings) - accepted_count
    if baseline is None:
        if not findings:
            output("QA clean: no issues found.")
            return

        output(f"QA found {active_count} issue(s):")
        for finding in findings:
            _print_finding(
                finding,
                "CURRENT",
                output,
                accepted=finding_id(finding) in accepted_ids,
            )
        if accepted_count:
            output(f"QA accepted: {accepted_count} finding(s).")
        return

    baseline_by_key = {_finding_key(item): item for item in baseline}
    current_keys = {_finding_key(item) for item in findings}
    new = [item for item in findings if _finding_key(item) not in baseline_by_key]
    pre_existing = [
        item for item in findings if _finding_key(item) in baseline_by_key
    ]
    resolved = [item for item in baseline if _finding_key(item) not in current_keys]
    active_new = [item for item in new if finding_id(item) not in accepted_ids]
    active_pre_existing = [
        item for item in pre_existing if finding_id(item) not in accepted_ids
    ]

    output(
        f"{active_count} findings ({len(active_new)} new, "
        f"{len(active_pre_existing)} pre-existing, {len(resolved)} resolved; "
        "NEW = since last pull)"
    )
    for finding in findings:
        label = "PRE-EXISTING" if _finding_key(finding) in baseline_by_key else "NEW"
        _print_finding(
            finding,
            label,
            output,
            accepted=finding_id(finding) in accepted_ids,
        )
    for finding in resolved:
        _print_finding(finding, "RESOLVED", output)
    if accepted_count:
        output(f"QA accepted: {accepted_count} finding(s).")


def _print_finding(
    finding: Finding,
    label: str,
    output: Callable[[str], None],
    *,
    accepted: bool = False,
) -> None:
    """Print one labeled finding and its suggested fix."""
    ids = ", ".join(finding.element_ids)
    accepted_label = "[ACCEPTED] " if accepted else ""
    output(
        f"{accepted_label}[{label}] [{finding.severity}] {finding.rule} "
        f"slide {finding.slide_number:02d} ({ids}) "
        f"[id: {finding_id(finding)}]: {finding.description}"
    )
    output(f"  Suggested fix: {finding.suggested_fix}")


def check_folder(
    folder: str | Path,
    *,
    strict: bool = False,
    output: Callable[[str], None] = print,
    text_measurer: TextMeasurer | None = None,
    accept: Sequence[str] = (),
    unaccept: Sequence[str] = (),
) -> int:
    """Lint and report a folder, returning its CLI exit code."""
    folder_path = Path(folder)
    findings = lint_folder(folder_path, text_measurer=text_measurer)
    findings_by_id = {finding_id(finding): finding for finding in findings}
    accepted = _read_accepted_findings(folder_path)
    original_accepted = dict(accepted)

    for identity in _sugar_accepted_ids(folder_path, findings):
        accepted[identity] = _acceptance_record(findings_by_id[identity])
    for identity in accept:
        finding = findings_by_id.get(identity)
        if finding is None:
            raise ValueError(
                f"Cannot accept unknown current finding ID '{identity}'"
            )
        accepted[identity] = _acceptance_record(finding)
    for identity in unaccept:
        accepted.pop(identity, None)

    if accepted != original_accepted:
        _write_accepted_findings(folder_path, accepted)

    baseline = _read_qa_baseline(folder_path)
    accepted_ids = set(accepted)
    print_report(findings, output, baseline=baseline, accepted_ids=accepted_ids)
    active_findings = [
        finding for finding in findings if finding_id(finding) not in accepted_ids
    ]
    return 1 if strict and active_findings else 0


def _find_overlaps(
    siblings: list[ParsedElement],
    slide_number: int,
) -> list[Finding]:
    findings: list[Finding] = []
    # Divider lines intentionally cross other content. Treating their thin
    # bounding boxes as ordinary shapes produces noisy overlap findings.
    leaves = [
        element
        for element in siblings
        if not element.children and element.tag != "Line"
    ]
    for index, first in enumerate(leaves):
        first_box = _box(first)
        if first_box is None or first_box.area <= 0:
            continue
        for second in leaves[index + 1 :]:
            second_box = _box(second)
            if second_box is None or second_box.area <= 0:
                continue
            if first_box.contains(second_box, threshold=1.0) or second_box.contains(
                first_box, threshold=1.0
            ):
                continue
            intersection = _intersection_area(first_box, second_box)
            smaller_area = min(first_box.area, second_box.area)
            if intersection / smaller_area <= OVERLAP_THRESHOLD:
                continue
            ids = (first.clean_id, second.clean_id)
            findings.append(
                Finding(
                    severity="WARNING",
                    rule="OVERLAP",
                    element_ids=ids,
                    slide_number=slide_number,
                    description=(
                        f"Sibling elements {ids[0]} and {ids[1]} overlap by "
                        f"{intersection / smaller_area:.0%} of the smaller element."
                    ),
                    suggested_fix=(
                        "Move or resize one element to leave intentional spacing "
                        "between them."
                    ),
                )
            )

    for element in siblings:
        findings.extend(_find_overlaps(element.children, slide_number))
    return findings


def _walk(elements: list[ParsedElement]) -> Iterator[ParsedElement]:
    for element in elements:
        yield element
        yield from _walk(element.children)


def _box(element: ParsedElement) -> BoundingBox | None:
    if None in (element.x, element.y, element.w, element.h):
        return None
    assert element.x is not None
    assert element.y is not None
    assert element.w is not None
    assert element.h is not None
    return BoundingBox(element.x, element.y, element.w, element.h)


def _intersection_area(first: BoundingBox, second: BoundingBox) -> float:
    width = max(0.0, min(first.x2, second.x2) - max(first.x, second.x))
    height = max(0.0, min(first.y2, second.y2) - max(first.y, second.y))
    return width * height


def _text_metrics(
    element: ParsedElement,
    raw_style: dict[str, Any],
) -> tuple[str, float, int]:
    families: list[str] = []
    sizes: list[float] = []
    weights: list[int] = []

    text_style = element.styles.text_style if element.styles else None
    if text_style is not None:
        _add_metrics(
            families,
            sizes,
            weights,
            text_style.font_family,
            text_style.font_size_pt,
            text_style.font_weight or (700 if text_style.bold else None),
        )
    for paragraph_runs in element.runs:
        for run in paragraph_runs:
            _add_run_metrics(run, families, sizes, weights)

    for paragraph in raw_style.get("text", {}).get("paragraphs", []):
        for run in paragraph.get("runs", []):
            style = run.get("style", {})
            _add_metrics(
                families,
                sizes,
                weights,
                style.get("fontFamily"),
                style.get("fontSize"),
                style.get("fontWeight") or (700 if style.get("bold") else None),
            )

    return (
        families[0] if families else "Arial",
        max(sizes) if sizes else 12.0,
        max(weights) if weights else 400,
    )


def _add_run_metrics(
    run: ParsedRun,
    families: list[str],
    sizes: list[float],
    weights: list[int],
) -> None:
    style = run.text_style
    if style is None:
        return
    _add_metrics(
        families,
        sizes,
        weights,
        style.font_family,
        style.font_size_pt,
        style.font_weight or (700 if style.bold else None),
    )


def _add_metrics(
    families: list[str],
    sizes: list[float],
    weights: list[int],
    family: Any,
    size: Any,
    weight: Any,
) -> None:
    if isinstance(family, str) and family:
        families.append(family)
    if isinstance(size, (int, float)) and size > 0:
        sizes.append(float(size))
    if isinstance(weight, int) and weight > 0:
        weights.append(weight)
