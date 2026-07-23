"""Offline visual QA for a materialized Slidesmith presentation folder."""

from __future__ import annotations

import json
import math
import zipfile
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
    flatten_elements,
    parse_all_slides,
)
from slidesmith.engine.content_diff import diff_presentation, get_effective_position
from slidesmith.engine.components import load_components
from slidesmith.engine.diff_model import ChangeType
from slidesmith.engine.json_utils import read_json
from slidesmith.engine.layout import (
    ApproximateTextMeasurer,
    TextMeasurer,
    compile_layout,
)
from slidesmith.engine.layout_measure import (
    ParagraphMetrics,
    ParagraphLayoutMeasurement,
    TextLayoutMeasurement,
    TextRunMetrics,
)
from slidesmith.engine.units import emu_to_pt
from slidesmith.engine.workspace_reader import _read_pristine

OVERLAP_THRESHOLD = 0.15
# A small edge protrusion is containment, not an actionable content overlap.
OVERLAP_CONTAINMENT_THRESHOLD = 0.95
# Near-full-slide leaves are backgrounds or intentional scrims, not content.
OVERLAP_BACKGROUND_AREA_RATIO = 0.90
# The layout measurement carries a calibrated 2% residual metric margin. Keep
# only a separate 5% decision tolerance for wrap-boundary and renderer drift;
# the former 10% tolerance was paired with a blanket 8% measurement margin and
# was too forgiving once those uncertainties were modeled explicitly.
TEXT_OVERFLOW_TOLERANCE = 1.05
DEFAULT_TEXT_INSET_PT = 7.2  # Google's default 0.1in inset, in points.
# One estimated line is an uncertainty budget for large, short display text.
TEXT_OVERFLOW_LARGE_FONT_SIZE_PT = 28.0
TEXT_OVERFLOW_MAX_UNCERTAIN_LINES = 2.0
QA_BASELINE_FILE = "qa-baseline.json"
ACCEPTED_FINDINGS_FILE = "accepted.json"
CONTACT_SHEET_COLUMNS = 2
CONTACT_SHEET_PADDING = 12
CONTACT_SHEET_GAP = 12
CONTACT_SHEET_LABEL_HEIGHT = 24


@dataclass(frozen=True)
class _MeasuredTextBox:
    """Text layout plus the frame geometry needed by both QA rules."""

    layout: TextLayoutMeasurement
    left_inset_pt: float
    top_inset_pt: float
    right_inset_pt: float
    bottom_inset_pt: float

    @property
    def required_frame_height_pt(self) -> float:
        if math.isinf(self.layout.height_pt):
            return float("inf")
        return (
            self.layout.height_pt
            + self.top_inset_pt
            + self.bottom_inset_pt
        )


async def download_thumbnails(
    transport: Any,
    folder: Path,
    qa_dir: Path,
    *,
    output: Callable[[str], None] = print,
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
        output(str(output_path))
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
    slide_id: str | None
    description: str
    suggested_fix: str
    # Current findings serialize this key even when the slide is id-less.
    # Baselines read from pre-slide-ID files set this false for the legacy case.
    had_slide_id_key: bool = True


def _finding_key(finding: Finding) -> tuple[str, tuple[str, ...], str | None]:
    """Stable identity for comparing lint results across workspace refreshes."""
    return finding.rule, tuple(sorted(finding.element_ids)), finding.slide_id


def _legacy_finding_key(finding: Finding) -> tuple[str, tuple[str, ...], int]:
    """Identity used by baseline/acceptance files written before slide IDs."""
    return finding.rule, tuple(sorted(finding.element_ids)), finding.slide_number


def _findings_match(first: Finding, second: Finding) -> bool:
    """Match findings while keeping legacy, id-less, and identified identities apart."""
    # Three cases are intentionally distinct:
    # 1. A missing slideId key is a pre-slide-ID legacy record; it may use the
    #    positional fallback against either a current id-less or identified slide.
    # 2. An explicit null slideId is a current id-less slide; it matches only
    #    another id-less finding at the same position.
    # 3. Two real slide IDs match by stable ID, never by position.
    if not first.had_slide_id_key or not second.had_slide_id_key:
        return _legacy_finding_key(first) == _legacy_finding_key(second)
    if first.slide_id is None or second.slide_id is None:
        return (
            first.slide_id is None
            and second.slide_id is None
            and _legacy_finding_key(first) == _legacy_finding_key(second)
        )
    return _finding_key(first) == _finding_key(second)


def _legacy_finding_id(finding: Finding) -> str:
    element_ids = ",".join(sorted(finding.element_ids))
    return f"{finding.rule}:{finding.slide_number}:{element_ids}"


def finding_id(finding: Finding) -> str:
    """Return the stable, CLI-facing identity for a QA finding."""
    element_ids = ",".join(sorted(finding.element_ids))
    slide_identity = (
        finding.slide_id if finding.slide_id is not None else str(finding.slide_number)
    )
    return f"{finding.rule}:{slide_identity}:{element_ids}"


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


def _accepted_record_matches(record: dict[str, Any], finding: Finding) -> bool:
    """Match a serialized acceptance to a current finding."""
    if str(record.get("rule", "")) != finding.rule:
        return False
    raw_element_ids = record.get("elementIds")
    if not isinstance(raw_element_ids, list):
        return False
    if tuple(sorted(str(value) for value in raw_element_ids)) != tuple(
        sorted(finding.element_ids)
    ):
        return False

    # A missing key is a genuinely legacy record and keeps its old positional
    # fallback, including against an identified current slide.
    if "slideId" not in record:
        raw_slide_number = record.get("slide", record.get("slideNumber"))
        try:
            return finding.slide_number == int(raw_slide_number)
        except (TypeError, ValueError):
            return False

    raw_slide_id = record["slideId"]
    if raw_slide_id is not None:
        return finding.slide_id == str(raw_slide_id)

    raw_slide_number = record.get("slide", record.get("slideNumber"))
    try:
        slide_number_matches = finding.slide_number == int(raw_slide_number)
    except (TypeError, ValueError):
        return False

    # Explicit null means a current id-less slide, not legacy data. It may
    # never migrate an acceptance onto an identified slide.
    return finding.slide_id is None and slide_number_matches


def _current_finding_for_identity(
    identity: str, findings: list[Finding]
) -> Finding | None:
    """Resolve a current stable or pre-slide-ID CLI identity."""
    for finding in findings:
        if identity in {finding_id(finding), _legacy_finding_id(finding)}:
            return finding
    return None


def _normalize_accepted_findings(
    accepted: dict[str, dict[str, Any]], findings: list[Finding]
) -> dict[str, dict[str, Any]]:
    """Migrate matching old acceptance keys/records to stable slide IDs."""
    normalized: dict[str, dict[str, Any]] = {}
    for identity, record in accepted.items():
        finding = next(
            (
                candidate
                for candidate in findings
                if _accepted_record_matches(record, candidate)
            ),
            None,
        )
        # Preserve the old identity-only migration fallback for malformed or
        # partially populated legacy records, but never for explicit null.
        if finding is None and "slideId" not in record:
            finding = _current_finding_for_identity(identity, findings)
        if finding is None:
            normalized[identity] = record
        else:
            normalized[finding_id(finding)] = _acceptance_record(finding)
    return normalized


def _remove_accepted_identity(
    accepted: dict[str, dict[str, Any]], identity: str, findings: list[Finding]
) -> None:
    """Remove an acceptance by its current or legacy identity."""
    target = _current_finding_for_identity(identity, findings)
    for accepted_identity, record in list(accepted.items()):
        if accepted_identity == identity or (
            target is not None
            and (
                accepted_identity in {
                    finding_id(target),
                }
                or _accepted_record_matches(record, target)
            )
        ):
            accepted.pop(accepted_identity, None)


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
        "slideId": finding.slide_id,
        "elementIds": sorted(finding.element_ids),
    }


def _sugar_accepted_ids(folder: Path, findings: list[Finding]) -> set[str]:
    """Match qa-accept-* element classes to current findings."""
    signals: set[tuple[str, str, int]] = set()
    components = load_components(folder)
    for content_path in sorted((folder / "slides").glob("*/content.sml")):
        slide_number = int(content_path.parent.name)
        content = compile_layout(
            content_path.read_text(encoding="utf-8"), components=components
        )
        root = DefusedET.fromstring(content)
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
        "slideId": finding.slide_id,
        "description": finding.description,
        "suggestedFix": finding.suggested_fix,
    }


def _finding_from_dict(data: dict[str, Any]) -> Finding:
    raw_slide_id = data.get("slideId")
    return Finding(
        severity=str(data["severity"]),
        rule=str(data["rule"]),
        element_ids=tuple(str(value) for value in data["elementIds"]),
        slide_number=int(data["slideNumber"]),
        slide_id=str(raw_slide_id) if raw_slide_id is not None else None,
        description=str(data["description"]),
        suggested_fix=str(data["suggestedFix"]),
        had_slide_id_key="slideId" in data,
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


def _read_slide_id(content_path: Path, slide_number: int) -> str | None:
    """Read the stable clean ID from a materialized slide root."""
    root = DefusedET.fromstring(content_path.read_text(encoding="utf-8"))
    if root.tag != "Slide":
        raise ValueError(
            f"Slide {slide_number:02d} must have a clean id on its <Slide> root"
        )
    return root.get("id") or None


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
    autofit_invalidated_instances = _pending_autofit_invalidations(folder_path, slides)

    findings: list[Finding] = []
    for slide_name, roots in slides.items():
        try:
            slide_number = int(slide_name)
        except ValueError as exc:
            raise ValueError(f"Slide folder name must be numeric: {slide_name}") from exc
        slide_id = _read_slide_id(
            folder_path / "slides" / slide_name / "content.sml", slide_number
        )

        findings.extend(
            _find_overlaps(
                roots,
                slide_number,
                slide_id,
                folder_path,
                slide_area=page_width * page_height,
                styles=styles,
                text_measurer=measurer,
                autofit_invalidated_instances=autofit_invalidated_instances,
            )
        )
        for element in _walk(roots):
            box = _box(element, folder_path)
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
                        slide_id=slide_id,
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

            if not any(element.paragraphs):
                continue
            raw_style = styles.get(element.clean_id, {})
            if not isinstance(raw_style, dict):
                raw_style = {}
            measurement = _measure_text_element(
                element,
                raw_style,
                box,
                measurer,
                autofit_deactivated=id(element) in autofit_invalidated_instances,
            )
            measured_height = measurement.required_frame_height_pt
            if measured_height > _text_overflow_limit(
                box.h,
                measured_height,
                measurement.layout.max_font_size_pt,
                first_line_height=measurement.layout.first_line_height_pt,
                line_count=measurement.layout.line_count,
                inset_height=measurement.top_inset_pt + measurement.bottom_inset_pt,
            ):
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
                        slide_id=slide_id,
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

    new = [
        item
        for item in findings
        if not any(_findings_match(item, baseline_item) for baseline_item in baseline)
    ]
    pre_existing = [
        item
        for item in findings
        if any(_findings_match(item, baseline_item) for baseline_item in baseline)
    ]
    resolved = [
        item
        for item in baseline
        if not any(_findings_match(item, current) for current in findings)
    ]
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
        label = (
            "PRE-EXISTING"
            if any(_findings_match(finding, baseline_item) for baseline_item in baseline)
            else "NEW"
        )
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
        finding = findings_by_id.get(identity) or _current_finding_for_identity(
            identity, findings
        )
        if finding is None:
            raise ValueError(
                f"Cannot accept unknown current finding ID '{identity}'"
            )
        accepted[identity] = _acceptance_record(finding)
    for identity in unaccept:
        _remove_accepted_identity(accepted, identity, findings)

    accepted = _normalize_accepted_findings(accepted, findings)

    if accepted != original_accepted:
        _write_accepted_findings(folder_path, accepted)

    baseline = _read_qa_baseline(folder_path)
    accepted_ids = set(accepted)
    print_report(findings, output, baseline=baseline, accepted_ids=accepted_ids)
    active_findings = [
        finding for finding in findings if finding_id(finding) not in accepted_ids
    ]
    return 1 if strict and active_findings else 0


def push_preflight(
    folder: str | Path,
    *,
    output: Callable[[str], None] = print,
) -> int:
    """Report offline QA and return the number of active findings new since pull."""
    folder_path = Path(folder)
    findings = lint_folder(folder_path)
    baseline = _read_qa_baseline(folder_path) or []
    accepted_ids = set(
        _normalize_accepted_findings(
            _read_accepted_findings(folder_path), findings
        )
    )
    accepted_ids.update(_sugar_accepted_ids(folder_path, findings))
    print_report(
        findings,
        output,
        baseline=baseline,
        accepted_ids=accepted_ids,
    )
    return sum(
        not any(_findings_match(finding, baseline_item) for baseline_item in baseline)
        and finding_id(finding) not in accepted_ids
        for finding in findings
    )


def _raw_element_style(
    element: ParsedElement,
    styles: dict[str, Any] | None,
) -> dict[str, Any]:
    if styles is None:
        return {}
    raw_style = styles.get(element.clean_id, {})
    return raw_style if isinstance(raw_style, dict) else {}


_METRIC_TEXT_STYLE_ATTRIBUTES = (
    "bold",
    "italic",
    "small_caps",
    "font_family",
    "font_size_pt",
    "font_weight",
    "baseline_offset",
)
_METRIC_PARAGRAPH_STYLE_ATTRIBUTES = (
    "line_spacing",
    "space_above_pt",
    "space_below_pt",
    "indent_start_pt",
    "indent_end_pt",
    "indent_first_line_pt",
)


def _metric_text_style_signature(style: Any) -> tuple[Any, ...]:
    return tuple(
        getattr(style, attribute, None)
        for attribute in _METRIC_TEXT_STYLE_ATTRIBUTES
    )


def _metric_paragraph_style_signature(style: Any) -> tuple[Any, ...]:
    return tuple(
        getattr(style, attribute, None)
        for attribute in _METRIC_PARAGRAPH_STYLE_ATTRIBUTES
    )


def _element_metric_signature(element: ParsedElement) -> tuple[Any, ...]:
    """Return all authored metrics that can change text layout."""
    element_styles = element.styles
    element_text_style = element_styles.text_style if element_styles else None
    element_paragraph_style = element_styles.paragraph_style if element_styles else None
    paragraph_signatures = tuple(
        (
            _metric_text_style_signature(
                paragraph_style.text_style if paragraph_style else None
            ),
            _metric_paragraph_style_signature(
                paragraph_style.paragraph_style if paragraph_style else None
            ),
        )
        for paragraph_style in element.paragraph_styles
    )
    run_signatures = tuple(
        tuple(
            _metric_text_style_signature(run.text_style)
            for run in paragraph_runs
        )
        for paragraph_runs in element.runs
    )
    return (
        _metric_text_style_signature(element_text_style),
        _metric_paragraph_style_signature(element_paragraph_style),
        paragraph_signatures,
        run_signatures,
    )


def _run_metric_profile(
    runs: list[list[ParsedRun]] | None,
) -> tuple[tuple[Any, ...], ...]:
    """Expand run metrics to text positions, ignoring non-metric styling."""
    profile: list[tuple[Any, ...]] = []
    for paragraph_runs in runs or []:
        paragraph_profile: list[Any] = []
        for run in paragraph_runs:
            signature = _metric_text_style_signature(run.text_style)
            if run.text:
                paragraph_profile.extend([signature] * len(run.text))
            else:
                # An empty authored run can still establish the style for an
                # empty paragraph, so retain its metric signature explicitly.
                paragraph_profile.append(("", signature))
        profile.append(tuple(paragraph_profile))
    return tuple(profile)


def _text_change_affects_autofit(change: Any) -> bool:
    """Return whether text content or run metrics changed."""
    return (
        change.new_text != change.old_text
        or _run_metric_profile(change.new_runs)
        != _run_metric_profile(change.old_runs)
    )


def _paragraph_change_affects_autofit(change: Any) -> bool:
    """Return whether a paragraph default can change glyph flow or height."""
    for update in getattr(change, "paragraph_style_updates", None) or []:
        old_styles = update.old_styles
        new_styles = update.new_styles
        if _metric_text_style_signature(
            old_styles.text_style if old_styles else None
        ) != _metric_text_style_signature(
            new_styles.text_style if new_styles else None
        ):
            return True
        if _metric_paragraph_style_signature(
            old_styles.paragraph_style if old_styles else None
        ) != _metric_paragraph_style_signature(
            new_styles.paragraph_style if new_styles else None
        ):
            return True
    return False


def _source_instances(
    current_instances: list[tuple[str, ParsedElement]],
    source_id: str,
    pristine: ParsedElement | None,
    source_slide: str | None,
) -> tuple[ParsedElement | None, list[ParsedElement]]:
    """Split current instances like the content diff's original/copy logic."""
    instances = [
        element
        for slide_index, element in current_instances
        if element.clean_id == source_id
        and (source_slide is None or slide_index == source_slide)
    ]
    if not instances:
        return None, []
    if pristine is None:
        return instances[0], instances[1:]

    original: ParsedElement | None = None
    for element in instances:
        if (
            element.w is not None
            and element.x == pristine.x
            and element.y == pristine.y
        ):
            original = element
            break
    if original is None and len(instances) == 1 and instances[0].w is not None:
        original = instances[0]
    return original, [element for element in instances if element is not original]


def _change_element(
    change: Any,
    current_instances: list[tuple[str, ParsedElement]],
    pristine_elements: dict[str, ParsedElement],
) -> ParsedElement | None:
    """Resolve a diff change to the actual current element instance."""
    if change.change_type is ChangeType.COPY:
        source_id = change.source_id or change.target_id
        _, copies = _source_instances(
            current_instances,
            source_id,
            pristine_elements.get(source_id),
            None,
        )
        try:
            copy_index = int(change.target_id.rsplit("_copy", 1)[1])
        except (IndexError, ValueError):
            return None
        return copies[copy_index] if copy_index < len(copies) else None

    candidates = [
        element
        for slide_index, element in current_instances
        if element.clean_id == change.target_id
        and (change.slide_index is None or slide_index == change.slide_index)
    ]
    if not candidates:
        return None
    original, _ = _source_instances(
        current_instances,
        change.target_id,
        pristine_elements.get(change.target_id),
        change.slide_index,
    )
    return original or candidates[0]


def _current_element_instances(
    current_slides: dict[str, list[ParsedElement]],
) -> list[tuple[str, ParsedElement]]:
    return [
        (slide_index, element)
        for slide_index, roots in current_slides.items()
        for element in _walk(roots)
    ]


def _pending_autofit_invalidations(
    folder_path: Path,
    current_slides: dict[str, list[ParsedElement]],
) -> set[int]:
    """Find edited element instances whose captured autofit is no longer safe.

    Captured autofit belongs to a pulled element instance. Text content and
    metric-affecting text/paragraph attributes invalidate it; decoration-only
    run changes such as color, underline, and links do not.
    """
    try:
        pristine_slides, pristine_styles = _read_pristine(folder_path)
        diff_result = diff_presentation(
            pristine_slides,
            current_slides,
            pristine_styles,
            workspace_root=folder_path,
        )
    except (FileNotFoundError, KeyError, OSError, ValueError, zipfile.BadZipFile):
        # Older/materialized folders without a readable pristine archive still
        # get the historical standalone QA behavior.
        return set()

    current_instances = _current_element_instances(current_slides)
    pristine_elements = {
        element_id: element
        for roots in pristine_slides.values()
        for element_id, element in flatten_elements(roots).items()
    }
    invalidated: set[int] = set()
    for change in diff_result.changes:
        element = _change_element(change, current_instances, pristine_elements)
        if element is None:
            continue
        if change.change_type is ChangeType.CREATE:
            invalidated.add(id(element))
        elif change.change_type in {ChangeType.TEXT_UPDATE, ChangeType.COPY}:
            if _text_change_affects_autofit(change):
                invalidated.add(id(element))
        elif change.change_type is ChangeType.PARAGRAPH_STYLE_UPDATE:
            if _paragraph_change_affects_autofit(change):
                invalidated.add(id(element))
        elif change.change_type is ChangeType.STYLE_UPDATE:
            current = element
            pristine = pristine_elements.get(change.target_id)
            if (
                pristine is not None
                and _element_metric_signature(current)
                != _element_metric_signature(pristine)
            ):
                invalidated.add(id(current))
    return invalidated


def _number(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, dict):
        magnitude = value.get("magnitude")
        if isinstance(magnitude, (int, float)) and math.isfinite(float(magnitude)):
            unit = str(value.get("unit", "PT")).upper()
            return emu_to_pt(float(magnitude)) if unit == "EMU" else float(magnitude)
    return default


def _text_insets(raw_style: dict[str, Any]) -> tuple[float, float, float, float]:
    """Return left/top/right/bottom insets, defaulting to Google's 0.1in."""
    default = DEFAULT_TEXT_INSET_PT
    values = {
        "left": default,
        "top": default,
        "right": default,
        "bottom": default,
    }
    inset_style: dict[str, Any] = {}
    for key in ("textInsets", "text_insets", "insets"):
        candidate = raw_style.get(key)
        if isinstance(candidate, dict):
            inset_style.update(candidate)
            break

    aliases = {
        "left": ("left", "insetLeft", "leftPt", "left_pt"),
        "top": ("top", "insetTop", "topPt", "top_pt"),
        "right": ("right", "insetRight", "rightPt", "right_pt"),
        "bottom": ("bottom", "insetBottom", "bottomPt", "bottom_pt"),
    }
    for side, side_aliases in aliases.items():
        for key in side_aliases:
            candidate = inset_style.get(key, raw_style.get(key))
            parsed = _number(candidate)
            if parsed is not None:
                values[side] = max(0.0, parsed)
                break
    return values["left"], values["top"], values["right"], values["bottom"]


def _text_style_values(style: Any) -> dict[str, Any]:
    if style is None:
        return {}
    return {
        key: value
        for key, value in {
            "fontFamily": getattr(style, "font_family", None),
            "fontSize": getattr(style, "font_size_pt", None),
            "fontWeight": getattr(style, "font_weight", None),
            "bold": getattr(style, "bold", None),
        }.items()
        if value is not None
    }


def _resolved_run_metrics(
    text: str,
    raw_run_style: dict[str, Any],
    element_style: Any,
    paragraph_style: Any,
    run_style: Any,
) -> TextRunMetrics:
    values = dict(raw_run_style)
    for authored_style in (
        _text_style_values(element_style),
        _text_style_values(paragraph_style),
        _text_style_values(run_style),
    ):
        values.update(authored_style)

    family = values.get("fontFamily") or "Arial"
    size = _number(values.get("fontSize"), 12.0) or 12.0
    raw_weight = values.get("fontWeight")
    weight = _number(raw_weight)
    if weight is None:
        weight = 700.0 if values.get("bold") else 400.0
    return TextRunMetrics(
        text=text,
        font_family=str(family),
        font_size_pt=max(0.01, size),
        font_weight=max(1, int(weight)),
    )


def _raw_run_style(raw_paragraph: dict[str, Any], offset: int) -> dict[str, Any]:
    raw_runs = raw_paragraph.get("runs", [])
    if not isinstance(raw_runs, list):
        return {}
    cursor = 0
    for raw_run in raw_runs:
        if not isinstance(raw_run, dict):
            continue
        content = str(raw_run.get("content", ""))
        if cursor <= offset < cursor + len(content):
            style = raw_run.get("style", {})
            return style if isinstance(style, dict) else {}
        cursor += len(content)
    if raw_runs and isinstance(raw_runs[0], dict):
        style = raw_runs[0].get("style", {})
        return style if isinstance(style, dict) else {}
    return {}


def _paragraph_metrics(
    element: ParsedElement,
    raw_style: dict[str, Any],
) -> tuple[ParagraphMetrics, ...]:
    raw_text = raw_style.get("text", {})
    raw_paragraphs = raw_text.get("paragraphs", []) if isinstance(raw_text, dict) else []
    if not isinstance(raw_paragraphs, list):
        raw_paragraphs = []
    element_style = element.styles.text_style if element.styles else None
    element_paragraph_style = element.styles.paragraph_style if element.styles else None
    paragraphs: list[ParagraphMetrics] = []

    for index, paragraph_text in enumerate(element.paragraphs):
        raw_paragraph = (
            raw_paragraphs[index]
            if index < len(raw_paragraphs) and isinstance(raw_paragraphs[index], dict)
            else {}
        )
        paragraph_defaults = (
            element.paragraph_styles[index]
            if index < len(element.paragraph_styles)
            else None
        )
        paragraph_style = (
            paragraph_defaults.paragraph_style if paragraph_defaults else None
        )
        raw_para_style = raw_paragraph.get("style", {})
        if not isinstance(raw_para_style, dict):
            raw_para_style = {}

        def para_number(
            raw_key: str,
            authored_name: str,
            default: float | None = None,
        ) -> float | None:
            authored_values = (
                getattr(element_paragraph_style, authored_name, None),
                getattr(paragraph_style, authored_name, None),
            )
            for value in authored_values:
                parsed = _number(value)
                if parsed is not None:
                    return parsed
            return _number(raw_para_style.get(raw_key), default)

        runs = (
            element.runs[index]
            if index < len(element.runs) and element.runs[index]
            else [ParsedRun(text=paragraph_text)]
        )
        resolved_runs: list[TextRunMetrics] = []
        offset = 0
        for run in runs:
            resolved_runs.append(
                _resolved_run_metrics(
                    run.text,
                    _raw_run_style(raw_paragraph, offset),
                    element_style,
                    paragraph_defaults.text_style if paragraph_defaults else None,
                    run.text_style,
                )
            )
            offset += len(run.text)

        paragraphs.append(
            ParagraphMetrics(
                runs=tuple(resolved_runs),
                line_spacing_percent=para_number("lineSpacing", "line_spacing"),
                space_above_pt=para_number("spaceAbove", "space_above_pt", 0.0) or 0.0,
                space_below_pt=para_number("spaceBelow", "space_below_pt", 0.0) or 0.0,
                indent_start_pt=para_number("indentStart", "indent_start_pt", 0.0)
                or 0.0,
                indent_end_pt=para_number("indentEnd", "indent_end_pt", 0.0) or 0.0,
                indent_first_line_pt=para_number(
                    "indentFirstLine", "indent_first_line_pt", 0.0
                )
                or 0.0,
            )
        )
    return tuple(paragraphs)


def _autofit_adjustment(raw_style: dict[str, Any]) -> tuple[float, float | None]:
    autofit = raw_style.get("autofit", {})
    if not isinstance(autofit, dict):
        return 1.0, None
    autofit_type = str(autofit.get("type", autofit.get("autofitType", "NONE"))).upper()
    if autofit_type not in {"TEXT_AUTOFIT", "SHRINK_ON_OVERFLOW"}:
        # NONE is deliberately a no-op; SHAPE_AUTOFIT changes the shape frame,
        # not the text metrics, and is therefore also not a shrink adjustment.
        return 1.0, None
    font_scale = _number(autofit.get("fontScale"), 1.0) or 1.0
    reduction = _number(autofit.get("lineSpacingReduction"))
    return max(0.01, font_scale), reduction


def _measure_text_element(
    element: ParsedElement,
    raw_style: dict[str, Any],
    box: BoundingBox,
    measurer: TextMeasurer,
    *,
    autofit_deactivated: bool = False,
) -> _MeasuredTextBox:
    left, top, right, bottom = _text_insets(raw_style)
    available_width = box.w - left - right
    if available_width <= 0:
        return _MeasuredTextBox(
            TextLayoutMeasurement(float("inf"), 0.0, 0.0, 0, 0.0),
            left,
            top,
            right,
            bottom,
        )

    paragraphs = _paragraph_metrics(element, raw_style)
    font_scale, line_spacing_reduction = (
        (1.0, None)
        if autofit_deactivated
        else _autofit_adjustment(raw_style)
    )
    paragraph_measurer = getattr(measurer, "measure_paragraphs", None)
    if callable(paragraph_measurer):
        layout = paragraph_measurer(
            paragraphs,
            available_width,
            font_scale=font_scale,
            line_spacing_reduction=line_spacing_reduction,
        )
    else:
        # Keep custom TextMeasurer implementations source-compatible. Their
        # fallback is intentionally less precise, but the built-in QA path
        # always uses the paragraph-aware backend.
        family, size, weight = _text_metrics(element, raw_style)
        text = "\n".join(element.paragraphs)
        height = measurer.measure_wrapped_height(
            text,
            family,
            size,
            weight,
            available_width,
        )
        line_height = size * ApproximateTextMeasurer.LINE_HEIGHT_FACTOR
        line_count = max(1, round(height / max(line_height, 0.01)))
        layout = TextLayoutMeasurement(
            height,
            min(available_width, len(text) * size * 0.52),
            line_height,
            line_count,
            size,
        )
    return _MeasuredTextBox(layout, left, top, right, bottom)


def _is_text_element(element: ParsedElement) -> bool:
    return bool(element.paragraphs) or element.tag == "TextBox"


def _text_ink_box(
    element: ParsedElement,
    box: BoundingBox,
    raw_style: dict[str, Any],
    text_measurer: TextMeasurer | None,
    *,
    autofit_deactivated: bool = False,
) -> BoundingBox | None:
    if not _is_text_element(element) or not any(element.paragraphs):
        return None
    measurement = _measure_text_element(
        element,
        raw_style,
        box,
        text_measurer or ApproximateTextMeasurer(),
        autofit_deactivated=autofit_deactivated,
    )
    content_width = max(0.0, box.w - measurement.left_inset_pt - measurement.right_inset_pt)
    content_height = max(0.0, box.h - measurement.top_inset_pt - measurement.bottom_inset_pt)
    if (
        content_width <= 0
        or content_height <= 0
        or measurement.layout.max_line_width_pt <= 0
    ):
        return None

    paragraph_layouts = measurement.layout.paragraphs
    if not paragraph_layouts:
        paragraph_layouts = (
            ParagraphLayoutMeasurement(
                (measurement.layout.max_line_width_pt,),
                (measurement.layout.first_line_height_pt,),
            ),
        )

    content_alignment = _content_alignment(element, raw_style)
    flow_height = max(0.0, measurement.layout.height_pt)
    vertical_slack = max(0.0, content_height - flow_height)
    if content_alignment in {"MIDDLE", "CENTER"}:
        vertical_offset = vertical_slack / 2.0
    elif content_alignment in {"BOTTOM", "END"}:
        vertical_offset = vertical_slack
    else:
        vertical_offset = 0.0

    ink_left: float | None = None
    ink_top: float | None = None
    ink_right: float | None = None
    ink_bottom: float | None = None
    cursor_y = box.y + measurement.top_inset_pt + vertical_offset
    for paragraph_index, paragraph_layout in enumerate(paragraph_layouts):
        cursor_y += max(0.0, paragraph_layout.space_above_pt)
        paragraph_alignment = _paragraph_alignment(
            element,
            raw_style,
            paragraph_index,
        )
        for line_width, line_height in zip(
            paragraph_layout.line_widths_pt,
            paragraph_layout.line_heights_pt,
            strict=False,
        ):
            line_width = min(content_width, max(0.0, line_width))
            line_height = min(content_height, max(0.0, line_height))
            if line_width <= 0 or line_height <= 0:
                cursor_y += max(0.0, line_height)
                continue
            line_x = box.x + measurement.left_inset_pt
            if paragraph_alignment == "CENTER":
                line_x += max(0.0, content_width - line_width) / 2.0
            elif paragraph_alignment == "END":
                line_x += max(0.0, content_width - line_width)
            line_y = cursor_y
            ink_left = line_x if ink_left is None else min(ink_left, line_x)
            ink_top = line_y if ink_top is None else min(ink_top, line_y)
            ink_right = (
                line_x + line_width
                if ink_right is None
                else max(ink_right, line_x + line_width)
            )
            ink_bottom = (
                line_y + line_height
                if ink_bottom is None
                else max(ink_bottom, line_y + line_height)
            )
            cursor_y += line_height
        cursor_y += max(0.0, paragraph_layout.space_below_pt)

    if ink_left is None or ink_top is None or ink_right is None or ink_bottom is None:
        return None
    return BoundingBox(ink_left, ink_top, ink_right - ink_left, ink_bottom - ink_top)


def _content_alignment(element: ParsedElement, raw_style: dict[str, Any]) -> str:
    authored_alignment = element.styles.content_alignment if element.styles else None
    if authored_alignment is not None:
        return str(authored_alignment.value).upper()
    return str(raw_style.get("contentAlignment", "")).upper()


def _paragraph_alignment(
    element: ParsedElement,
    raw_style: dict[str, Any],
    paragraph_index: int,
) -> str:
    paragraph_style = (
        element.paragraph_styles[paragraph_index].paragraph_style
        if paragraph_index < len(element.paragraph_styles)
        and element.paragraph_styles[paragraph_index] is not None
        else None
    )
    if paragraph_style is None and element.styles:
        paragraph_style = element.styles.paragraph_style
    alignment = (
        str(paragraph_style.alignment.value).upper()
        if paragraph_style is not None and paragraph_style.alignment is not None
        else ""
    )
    raw_text = raw_style.get("text", {})
    paragraphs = raw_text.get("paragraphs", []) if isinstance(raw_text, dict) else []
    raw_paragraph = (
        paragraphs[paragraph_index]
        if isinstance(paragraphs, list)
        and paragraph_index < len(paragraphs)
        and isinstance(paragraphs[paragraph_index], dict)
        else {}
    )
    raw_para_style = raw_paragraph.get("style", {})
    if not alignment and isinstance(raw_para_style, dict):
        alignment = str(raw_para_style.get("alignment", "")).upper()
    if alignment in {"RIGHT", "LEFT"}:
        alignment = {"RIGHT": "END", "LEFT": "START"}[alignment]
    if alignment not in {"START", "CENTER", "END"}:
        alignment = "START"

    direction = (
        paragraph_style.direction
        if paragraph_style is not None
        else None
    )
    if not direction and element.styles and element.styles.paragraph_style:
        direction = element.styles.paragraph_style.direction
    if not direction and isinstance(raw_para_style, dict):
        direction = raw_para_style.get("direction")
    if str(direction or "").upper() == "RIGHT_TO_LEFT":
        if alignment == "START":
            return "END"
        if alignment == "END":
            return "START"
    return alignment


def _overlap_check_boxes(
    first: ParsedElement,
    first_box: BoundingBox,
    second: ParsedElement,
    second_box: BoundingBox,
    first_style: dict[str, Any],
    second_style: dict[str, Any],
    text_measurer: TextMeasurer | None,
    autofit_invalidated_instances: set[int],
) -> tuple[BoundingBox | None, BoundingBox | None]:
    first_is_text = _is_text_element(first)
    second_is_text = _is_text_element(second)
    if first_is_text and second_is_text:
        return first_box, second_box
    first_check = (
        _text_ink_box(
            first,
            first_box,
            first_style,
            text_measurer,
            autofit_deactivated=id(first) in autofit_invalidated_instances,
        )
        if first_is_text
        else first_box
    )
    second_check = (
        _text_ink_box(
            second,
            second_box,
            second_style,
            text_measurer,
            autofit_deactivated=id(second) in autofit_invalidated_instances,
        )
        if second_is_text
        else second_box
    )
    return first_check, second_check


def _find_overlaps(
    siblings: list[ParsedElement],
    slide_number: int,
    slide_id: str | None,
    workspace_root: Path,
    *,
    slide_area: float,
    styles: dict[str, Any] | None = None,
    text_measurer: TextMeasurer | None = None,
    autofit_invalidated_instances: set[int] | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    autofit_invalidated_instances = autofit_invalidated_instances or set()
    # Divider lines intentionally cross other content. Treating their bounding
    # boxes as ordinary shapes produces noisy overlap findings; filled
    # rectangles are deliberately left in the set so their actual ink overlap
    # is evaluated below.
    leaves = [
        element
        for element in siblings
        if not element.children
        and element.tag != "Line"
    ]
    for index, first in enumerate(leaves):
        first_box = _box(first, workspace_root)
        if first_box is None or first_box.area <= 0:
            continue
        for second in leaves[index + 1 :]:
            second_box = _box(second, workspace_root)
            if second_box is None or second_box.area <= 0:
                continue
            if _is_background(first_box, slide_area) or _is_background(
                second_box, slide_area
            ):
                continue
            # Text-vs-text remains raw-bounds conservative: two text frames
            # may be intentionally layered even when their glyphs are sparse.
            # Against a non-text sibling, use estimated ink so an empty half of
            # a large text frame cannot report a visual overlap.
            first_check_box, second_check_box = _overlap_check_boxes(
                first,
                first_box,
                second,
                second_box,
                _raw_element_style(first, styles),
                _raw_element_style(second, styles),
                text_measurer,
                autofit_invalidated_instances,
            )
            if first_check_box is None or second_check_box is None:
                continue
            if first_check_box.contains(
                second_check_box, threshold=OVERLAP_CONTAINMENT_THRESHOLD
            ) or second_check_box.contains(
                first_check_box, threshold=OVERLAP_CONTAINMENT_THRESHOLD
            ):
                continue
            intersection = _intersection_area(first_check_box, second_check_box)
            smaller_area = min(first_check_box.area, second_check_box.area)
            if smaller_area <= 0:
                continue
            if intersection / smaller_area <= OVERLAP_THRESHOLD:
                continue
            ids = (first.clean_id, second.clean_id)
            findings.append(
                Finding(
                    severity="WARNING",
                    rule="OVERLAP",
                    element_ids=ids,
                    slide_number=slide_number,
                    slide_id=slide_id,
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
        findings.extend(
            _find_overlaps(
                element.children,
                slide_number,
                slide_id,
                workspace_root,
                slide_area=slide_area,
                styles=styles,
                text_measurer=text_measurer,
                autofit_invalidated_instances=autofit_invalidated_instances,
            )
        )
    return findings


def _is_background(box: BoundingBox, slide_area: float) -> bool:
    """Return whether a leaf covers enough of the slide to be a background."""
    return box.area / slide_area >= OVERLAP_BACKGROUND_AREA_RATIO


def _text_overflow_limit(
    box_height: float,
    measured_height: float,
    font_size: float,
    *,
    first_line_height: float = 0.0,
    line_count: int = 0,
    inset_height: float = 0.0,
) -> float:
    """Return the height above which approximate text is actionable.

    Large display text and compact one-line labels are especially sensitive to
    one phantom wrapped line: the estimator's line-height and safety margin
    both scale with font size, while Google may auto-shrink a title at render
    time. Allow only the bounded short-estimate budget; multi-line body text
    keeps the flat tolerance and remains actionable.
    """
    baseline_limit = box_height * TEXT_OVERFLOW_TOLERANCE
    # Compact one-line scaffold labels have the same frame/inset uncertainty
    # as large titles. Keep the old guard for those labels as well, but never
    # extend it to multi-line body content.
    if font_size < TEXT_OVERFLOW_LARGE_FONT_SIZE_PT and line_count > 1:
        return baseline_limit

    true_line_height = first_line_height or (
        font_size * ApproximateTextMeasurer.LINE_HEIGHT_FACTOR
    )
    content_height = max(0.0, box_height - max(0.0, inset_height))
    box_can_hold_one_true_line = content_height >= true_line_height
    if not box_can_hold_one_true_line:
        return baseline_limit

    estimated_line_height = true_line_height
    estimated_lines = (
        line_count
        if line_count > 0
        else measured_height / max(estimated_line_height, 0.01)
    )
    if estimated_lines > TEXT_OVERFLOW_MAX_UNCERTAIN_LINES:
        return baseline_limit

    tolerance_slack = box_height * (TEXT_OVERFLOW_TOLERANCE - 1.0)
    return box_height + max(
        tolerance_slack,
        estimated_line_height,
    )


def _walk(elements: list[ParsedElement]) -> Iterator[ParsedElement]:
    for element in elements:
        yield element
        yield from _walk(element.children)


def _box(element: ParsedElement, workspace_root: Path) -> BoundingBox | None:
    position = get_effective_position(element, workspace_root=workspace_root)
    if position is None:
        return None
    return BoundingBox(
        position["x"],
        position["y"],
        position["w"],
        position["h"],
    )


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
