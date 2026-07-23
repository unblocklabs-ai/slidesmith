"""Pure, local pattern suggestions for pulled Slidesmith workspaces.

The advisor is deliberately separate from geometry QA.  Its rules describe
patterns an author may want to act on; they do not assert that a deck is
invalid and never participate in push or QA acceptance.
"""

from __future__ import annotations

import math
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from slidesmith.engine.bounds import BoundingBox
from slidesmith.engine.json_utils import read_json
from slidesmith.engine.layout import ApproximateTextMeasurer

if TYPE_CHECKING:
    from slidesmith.engine.content_parser import ParsedElement


# These are intentionally explicit and stable.  A near-miss at the boundary
# should remain easy to understand when a future rule revision changes one.
EDGE_TOLERANCE_PT = 2.0
COMPACT_UNION_AREA_RATIO = 1.5
BURIED_COVERAGE_RATIO = 0.90
OPAQUE_ALPHA_THRESHOLD = 0.99
STACK_SIZE_TOLERANCE_PT = 2.0
STACK_GAP_TOLERANCE_PT = 2.0
NEAR_OVERFLOW_LOWER_RATIO = 0.90
NEAR_OVERFLOW_UPPER_RATIO = 1.00


@dataclass(frozen=True)
class Suggestion:
    """One advisory pattern found in a local workspace."""

    rule: str
    slide_number: int
    element_ids: tuple[str, ...]
    message: str
    command_hint: str | None = None

    @property
    def rule_id(self) -> str:
        """Compatibility spelling for callers that call the field a rule ID."""
        return self.rule

    @property
    def slide(self) -> int:
        """Short spelling used by the stable JSON representation."""
        return self.slide_number

    def to_dict(self) -> dict[str, Any]:
        """Serialize the stable machine-readable advisor schema."""
        return {
            "rule": self.rule,
            "slide": self.slide_number,
            "element_ids": list(self.element_ids),
            "message": self.message,
            "command_hint": self.command_hint,
        }


@dataclass(frozen=True)
class AdvisorElement:
    """The local geometry and source metadata needed by advisor rules."""

    clean_id: str
    tag: str
    element_type: str
    bounds: BoundingBox
    source_order: int
    raw_element: dict[str, Any]
    parsed: ParsedElement | None
    style: dict[str, Any]
    parent_id: str | None = None
    native_group: bool = False


@dataclass(frozen=True)
class AdvisorSlide:
    """One slide's z-order roots and parsed local SML projection."""

    index: str
    number: int
    roots: tuple[AdvisorElement, ...]
    elements: tuple[AdvisorElement, ...]
    parsed_by_id: dict[str, ParsedElement]


@dataclass(frozen=True)
class WorkspaceContext:
    """Read-only context shared by every advisor rule."""

    folder: Path
    slides: tuple[AdvisorSlide, ...]
    styles: dict[str, dict[str, Any]]
    api_parent_by_id: dict[str, str | None]


Rule = Callable[[WorkspaceContext], list[Suggestion]]


def _measure_text_element(*args: Any, **kwargs: Any) -> Any:
    """Load the QA measurement helper only when the text rule runs."""
    from slidesmith.engine.qa import _measure_text_element as measure

    return measure(*args, **kwargs)


def _selector_for_ids(element_ids: Iterable[str]) -> str:
    return " OR ".join(f"id={element_id}" for element_id in element_ids)


def _command(*parts: str) -> str:
    return shlex.join(("slidesmith", *parts))


def _element_message_ids(elements: Iterable[AdvisorElement]) -> str:
    return ", ".join(element.clean_id for element in elements)


def _same_geometry(first: AdvisorElement, second: AdvisorElement) -> bool:
    return (
        first.element_type == second.element_type
        and abs(first.bounds.w - second.bounds.w) <= STACK_SIZE_TOLERANCE_PT
        and abs(first.bounds.h - second.bounds.h) <= STACK_SIZE_TOLERANCE_PT
    )


def _cluster_members(
    root: AdvisorElement,
    by_id: dict[str, AdvisorElement],
) -> tuple[AdvisorElement, ...]:
    """Return one current-SML visual container and its descendants."""
    members: list[AdvisorElement] = []

    def walk(parsed: ParsedElement) -> None:
        current = by_id.get(parsed.clean_id)
        if current is None:
            return
        members.append(current)
        for child in parsed.children:
            walk(child)

    if root.parsed is not None:
        walk(root.parsed)
    return tuple(members)


def _cluster_types(cluster: tuple[AdvisorElement, ...]) -> tuple[str, ...]:
    return tuple(element.element_type for element in cluster)


def _similar_cluster(
    first: tuple[AdvisorElement, ...],
    second: tuple[AdvisorElement, ...],
) -> bool:
    """Compare clusters, tolerating a single extra member on one side.

    Two cards that differ by one optional element (a badge, a caption) are
    still the same repeated visual unit; requiring exact member counts made
    both cards go silent.
    """
    if abs(len(first) - len(second)) > 1:
        return False
    if len(first) == len(second):
        return _aligned_cluster(first, second)
    small, large = (first, second) if len(first) < len(second) else (second, first)
    return any(
        _aligned_cluster(small, large[:skip] + large[skip + 1 :])
        for skip in range(len(large))
    )


def _aligned_cluster(
    first: tuple[AdvisorElement, ...],
    second: tuple[AdvisorElement, ...],
) -> bool:
    """Compare equal-length member geometry relative to each visual origin."""
    if _cluster_types(first) != _cluster_types(second):
        return False
    first_origin = first[0].bounds
    second_origin = second[0].bounds
    for first_element, second_element in zip(first, second, strict=True):
        if not _same_geometry(first_element, second_element):
            return False
        if abs(
            (first_element.bounds.x - first_origin.x)
            - (second_element.bounds.x - second_origin.x)
        ) > EDGE_TOLERANCE_PT:
            return False
        if abs(
            (first_element.bounds.y - first_origin.y)
            - (second_element.bounds.y - second_origin.y)
        ) > EDGE_TOLERANCE_PT:
            return False
    return True


def _top_level_api_ids(
    cluster: tuple[AdvisorElement, ...],
    api_parent_by_id: dict[str, str | None],
) -> tuple[str, ...]:
    """Expand a visual cluster to its selectable, top-level API objects."""
    return tuple(
        element.clean_id
        for element in cluster
        if element.clean_id and element.clean_id in api_parent_by_id
        and api_parent_by_id[element.clean_id] is None
    )


def pseudo_group_rule(context: WorkspaceContext) -> list[Suggestion]:
    """Suggest repeated current-SML visual containers, one card at a time.

    A cluster is an inferred visual container from the current SML tree, not an
    arbitrary contiguous row.  At least two clusters must share member count,
    member types, and relative layout within ``EDGE_TOLERANCE_PT``.  This
    deliberately avoids title/body pairs and row-shaped near misses.
    """
    suggestions: list[Suggestion] = []
    for slide in context.slides:
        by_id = {element.clean_id: element for element in slide.elements}
        clusters: list[tuple[AdvisorElement, ...]] = []
        for root in slide.roots:
            if (
                not root.clean_id
                or root.bounds.area <= 0
                or root.native_group
                or root.parsed is None
                or not root.parsed.children
            ):
                continue
            cluster = _cluster_members(root, by_id)
            if len(cluster) < 2 or any(element.native_group for element in cluster):
                continue
            if len(cluster) == 2 and all(
                element.element_type == "TextBox" for element in cluster
            ):
                continue
            if not all(
                element.clean_id in context.api_parent_by_id
                and context.api_parent_by_id[element.clean_id] is None
                for element in cluster
            ):
                continue
            clusters.append(cluster)

        for index, cluster in enumerate(clusters):
            if not any(
                other_index != index and _similar_cluster(cluster, other)
                for other_index, other in enumerate(clusters)
            ):
                continue
            ids = _top_level_api_ids(cluster, context.api_parent_by_id)
            if len(ids) < 2:
                continue
            suggestions.append(
                Suggestion(
                    "pseudo-group",
                    slide.number,
                    ids,
                    f"Elements {_element_message_ids(cluster)} form a repeated "
                    "visual cluster; consider grouping them for maintainability.",
                    _command(
                        "group",
                        str(context.folder),
                        _selector_for_ids(ids),
                    ),
                )
            )
    return suggestions


def _opaque_fill(element: AdvisorElement) -> bool:
    """Return whether local data proves an opaque-capable cover element.

    Only explicit solid fills with alpha at least 0.99 and images qualify.
    Inherited/default fills are intentionally not resolved because that
    information is not reliably represented in the local advisor projection.
    """
    if element.element_type == "Image" or "image" in element.raw_element:
        return True

    # A current SML class is an authored local override and therefore takes
    # precedence over the immutable pristine API style metadata.
    parsed_styles = element.parsed.styles if element.parsed is not None else None
    parsed_fill = parsed_styles.fill if parsed_styles is not None else None
    if parsed_fill is not None:
        color = getattr(parsed_fill, "color", None)
        alpha = getattr(color, "alpha", None)
        return (
            isinstance(alpha, (int, float))
            and not isinstance(alpha, bool)
            and math.isfinite(float(alpha))
            and float(alpha) >= OPAQUE_ALPHA_THRESHOLD
        )

    shape = element.raw_element.get("shape")
    if isinstance(shape, dict):
        properties = shape.get("shapeProperties", {})
        if isinstance(properties, dict):
            fill = properties.get("shapeBackgroundFill")
            if isinstance(fill, dict):
                if fill.get("propertyState") in {"NOT_RENDERED", "INHERIT"}:
                    return False
                solid = fill.get("solidFill")
                if isinstance(solid, dict):
                    alpha = solid.get("alpha", 1.0)
                    if isinstance(alpha, (int, float)) and not isinstance(alpha, bool):
                        return (
                            math.isfinite(float(alpha))
                            and float(alpha) >= OPAQUE_ALPHA_THRESHOLD
                        )

    fill = element.style.get("fill")
    if not isinstance(fill, dict) or fill.get("type") != "solid":
        return False
    alpha = fill.get("alpha", 1.0)
    return (
        isinstance(alpha, (int, float))
        and not isinstance(alpha, bool)
        and math.isfinite(float(alpha))
        and float(alpha) >= OPAQUE_ALPHA_THRESHOLD
    )


def _intersection_area(first: BoundingBox, second: BoundingBox) -> float:
    return max(0.0, min(first.x2, second.x2) - max(first.x, second.x)) * max(
        0.0, min(first.y2, second.y2) - max(first.y, second.y)
    )


def buried_element_rule(context: WorkspaceContext) -> list[Suggestion]:
    suggestions: list[Suggestion] = []
    for slide in context.slides:
        # Current SML depth-first order is the local paint-order projection.
        # Scan it rather than pristine render roots so a contained image or
        # other cover element remains visible to the rule.
        elements = tuple(element for element in slide.elements if element.bounds.area > 0)
        for back_index, buried in enumerate(elements[:-1]):
            if (
                not buried.clean_id
                or context.api_parent_by_id.get(buried.clean_id) is not None
            ):
                continue
            for front in elements[back_index + 1 :]:
                if (
                    context.api_parent_by_id.get(front.clean_id) is not None
                    or not _opaque_fill(front)
                ):
                    continue
                coverage = _intersection_area(front.bounds, buried.bounds) / buried.bounds.area
                if coverage + 1e-9 < BURIED_COVERAGE_RATIO:
                    continue
                suggestions.append(
                    Suggestion(
                        "buried-element",
                        slide.number,
                        (buried.clean_id, front.clean_id),
                        f"Element {buried.clean_id} is beneath {front.clean_id}, "
                        f"which covers {coverage:.0%} of its area; consider "
                        "bringing it forward or deleting it.",
                        _command(
                            "reorder",
                            str(context.folder),
                            f"id={buried.clean_id}",
                            "--op",
                            "bring-forward",
                        ),
                    )
                )
                break
    return suggestions


def _stack_axis(
    elements: tuple[AdvisorElement, ...],
) -> str | None:
    if len(elements) < 3:
        return None
    first = elements[0]
    horizontal = all(
        min(
            abs(element.bounds.y - first.bounds.y),
            abs(element.bounds.y2 - first.bounds.y2),
            abs(element.bounds.y + element.bounds.h / 2 - (first.bounds.y + first.bounds.h / 2)),
        )
        <= EDGE_TOLERANCE_PT
        for element in elements[1:]
    )
    vertical = all(
        min(
            abs(element.bounds.x - first.bounds.x),
            abs(element.bounds.x2 - first.bounds.x2),
            abs(element.bounds.x + element.bounds.w / 2 - (first.bounds.x + first.bounds.w / 2)),
        )
        <= EDGE_TOLERANCE_PT
        for element in elements[1:]
    )
    if horizontal:
        return "horizontal"
    if vertical:
        return "vertical"
    return None


def _stack_candidate(elements: tuple[AdvisorElement, ...]) -> str | None:
    if len(elements) < 3 or not all(_same_geometry(elements[0], item) for item in elements[1:]):
        return None
    axis = _stack_axis(elements)
    if axis is None:
        return None
    gaps = (
        [second.bounds.x - first.bounds.x2 for first, second in zip(elements, elements[1:])]
        if axis == "horizontal"
        else [second.bounds.y - first.bounds.y2 for first, second in zip(elements, elements[1:])]
    )
    if any(gap < 0 for gap in gaps):
        return None
    return axis if max(gaps) - min(gaps) <= STACK_GAP_TOLERANCE_PT + 1e-9 else None


def stack_candidate_rule(context: WorkspaceContext) -> list[Suggestion]:
    suggestions: list[Suggestion] = []
    for slide in context.slides:
        candidates = [element for element in slide.roots if element.clean_id and element.bounds.area > 0]
        by_type: dict[str, list[AdvisorElement]] = {}
        for element in candidates:
            by_type.setdefault(element.element_type, []).append(element)

        for element_type, typed in by_type.items():
            for axis in ("horizontal", "vertical"):
                ordered = sorted(
                    typed,
                    key=lambda element: element.bounds.x if axis == "horizontal" else element.bounds.y,
                )
                start = 0
                while start < len(ordered) - 2:
                    best_end: int | None = None
                    for end in range(start + 2, len(ordered)):
                        candidate = tuple(ordered[start : end + 1])
                        if _stack_candidate(candidate) == axis:
                            best_end = end
                    if best_end is None:
                        start += 1
                        continue
                    candidate = tuple(ordered[start : best_end + 1])
                    ids = tuple(element.clean_id for element in candidate)
                    suggestions.append(
                        Suggestion(
                            "stack-candidate",
                            slide.number,
                            ids,
                            f"Elements {_element_message_ids(candidate)} are {len(candidate)} "
                            f"equal-size {element_type} siblings on a {axis} axis; "
                            "consider a Stack container for maintainability.",
                        )
                    )
                    start = best_end + 1
    return _dedupe_suggestions(suggestions)


def near_overflow_rule(context: WorkspaceContext) -> list[Suggestion]:
    suggestions: list[Suggestion] = []
    measurer = ApproximateTextMeasurer()
    for slide in context.slides:
        for element in slide.elements:
            parsed = element.parsed
            if parsed is None or parsed.tag != "TextBox" or not any(parsed.paragraphs):
                continue
            raw_style = element.style
            measurement = _measure_text_element(
                parsed,
                raw_style,
                element.bounds,
                measurer,
            )
            content_height = (
                element.bounds.h
                - measurement.top_inset_pt
                - measurement.bottom_inset_pt
            )
            measured_height = measurement.layout.height_pt
            if (
                content_height <= 0
                or not math.isfinite(measured_height)
                or not NEAR_OVERFLOW_LOWER_RATIO * content_height
                <= measured_height
                < NEAR_OVERFLOW_UPPER_RATIO * content_height
            ):
                continue
            ratio = measured_height / content_height
            suggestions.append(
                Suggestion(
                    "near-overflow",
                    slide.number,
                    (element.clean_id,),
                    f"Text box {element.clean_id} uses about {ratio:.0%} of its "
                    "content height; enlarge the box or shorten the text before "
                    "the next edit.",
                )
            )
    return suggestions


RULE_TABLE: tuple[tuple[str, Rule], ...] = (
    ("pseudo-group", pseudo_group_rule),
    ("buried-element", buried_element_rule),
    ("stack-candidate", stack_candidate_rule),
    ("near-overflow", near_overflow_rule),
)

# A mapping is convenient for integrations while RULE_TABLE preserves output
# order and makes registration visibly cheap for future rules.
RULES: dict[str, Rule] = dict(RULE_TABLE)


def _dedupe_suggestions(suggestions: Iterable[Suggestion]) -> list[Suggestion]:
    seen: set[tuple[str, int, tuple[str, ...]]] = set()
    result: list[Suggestion] = []
    for suggestion in suggestions:
        key = (suggestion.rule, suggestion.slide_number, suggestion.element_ids)
        if key not in seen:
            seen.add(key)
            result.append(suggestion)
    return result


def _parsed_elements_for_slide(roots: list[ParsedElement]) -> dict[str, ParsedElement]:
    from slidesmith.engine.content_parser import flatten_elements

    return flatten_elements(roots)


def _tag_for_type(element_type: str) -> str:
    return {
        "RECTANGLE": "Rect",
        "TEXT_BOX": "TextBox",
        "IMAGE": "Image",
        "LINE": "Line",
        "GROUP": "Group",
    }.get(element_type, element_type)


def _from_parsed_element(
    element: ParsedElement,
    styles: dict[str, dict[str, Any]],
    raw_by_clean: dict[str, dict[str, Any]],
    source_order: int,
    parent_id: str | None = None,
) -> tuple[AdvisorElement, ...]:
    if None in {element.x, element.y, element.w, element.h}:
        return ()
    style = styles.get(element.clean_id, {})
    if not isinstance(style, dict):
        style = {}
    current = AdvisorElement(
        clean_id=element.clean_id,
        tag=element.tag,
        element_type=_tag_for_type(element.tag),
        bounds=BoundingBox(element.x or 0, element.y or 0, element.w or 0, element.h or 0),
        source_order=source_order,
        raw_element=raw_by_clean.get(element.clean_id, {}),
        parsed=element,
        style=style,
        parent_id=parent_id,
        native_group=(
            element.tag == "Group"
            or "elementGroup" in raw_by_clean.get(element.clean_id, {})
        ),
    )
    descendants: list[AdvisorElement] = []
    next_order = source_order + 1
    for child in element.children:
        child_infos = _from_parsed_element(
            child,
            styles,
            raw_by_clean,
            next_order,
            element.clean_id,
        )
        descendants.extend(child_infos)
        next_order += len(child_infos)
    return (current, *descendants)


def _read_base_raw(folder_path: Path) -> dict[str, Any] | None:
    """Read immutable API metadata without importing the transport package."""
    for relative_path in (Path(".pristine") / "base.json", Path(".raw") / "presentation.json"):
        candidate = folder_path / relative_path
        if candidate.exists():
            return read_json(candidate, missing_ok=False)
    return None


def _raw_metadata(
    base: dict[str, Any] | None,
    mapping: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, str | None]]:
    """Index raw elements by clean ID and retain native API parentage."""
    if not isinstance(base, dict):
        return {}, {}
    reverse_mapping = {
        google_id: clean_id
        for clean_id, google_id in mapping.items()
        if isinstance(clean_id, str) and isinstance(google_id, str)
    }
    raw_by_clean: dict[str, dict[str, Any]] = {}
    api_parent_by_id: dict[str, str | None] = {}

    def walk(element: dict[str, Any], parent_google_id: str | None) -> None:
        google_id = element.get("objectId")
        clean_id = reverse_mapping.get(google_id)
        if clean_id is not None:
            raw_by_clean[clean_id] = element
            api_parent_by_id[clean_id] = reverse_mapping.get(parent_google_id)
        child_parent = google_id if isinstance(google_id, str) else parent_google_id
        for child in element.get("elementGroup", {}).get("children", []) or []:
            if isinstance(child, dict):
                walk(child, child_parent)

    for page_kind in ("slides", "layouts", "masters"):
        for page in base.get(page_kind, []) or []:
            if not isinstance(page, dict):
                continue
            for element in page.get("pageElements", []) or []:
                if isinstance(element, dict):
                    walk(element, None)
    return raw_by_clean, api_parent_by_id


def load_workspace(folder: str | Path) -> WorkspaceContext:
    """Build an advisor context from local files only."""
    from slidesmith.engine.content_parser import parse_all_slides

    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise ValueError(f"Presentation folder does not exist: {folder_path}")

    # Match the check command's minimum workspace contract before reading any
    # optional projection.  An empty suggestion set is valid only for a valid
    # workspace.
    read_json(folder_path / "presentation.json", missing_ok=False)
    mapping = read_json(folder_path / "id_mapping.json", missing_ok=False)
    styles = read_json(folder_path / "styles.json", missing_ok=True)
    styles = styles if isinstance(styles, dict) else {}
    parsed_slides = parse_all_slides(str(folder_path / "slides"))
    base = _read_base_raw(folder_path)
    raw_by_clean, api_parent_by_id = _raw_metadata(base, mapping)
    slides: list[AdvisorSlide] = []
    for index, parsed_roots in sorted(parsed_slides.items()):
        try:
            number = int(index)
        except ValueError as exc:
            raise ValueError(f"Slide folder name must be numeric: {index}") from exc
        parsed_by_id = _parsed_elements_for_slide(parsed_roots)
        root_infos: list[AdvisorElement] = []
        all_infos: list[AdvisorElement] = []
        next_order = 0
        for root in parsed_roots:
            infos = _from_parsed_element(
                root,
                styles,
                raw_by_clean,
                next_order,
            )
            all_infos.extend(infos)
            if infos:
                root_infos.append(infos[0])
            next_order += len(infos)
        slides.append(
            AdvisorSlide(index, number, tuple(root_infos), tuple(all_infos), parsed_by_id)
        )
    return WorkspaceContext(folder_path, tuple(slides), styles, api_parent_by_id)


def advise_folder(
    folder: str | Path,
    *,
    rule: str | None = None,
) -> list[Suggestion]:
    """Run the registered pure rules against a pulled workspace."""
    context = load_workspace(folder)
    selected = RULES.get(rule) if rule else None
    if rule and selected is None:
        return []
    rules = ((rule, selected),) if selected is not None else RULE_TABLE
    suggestions: list[Suggestion] = []
    for _, rule_function in rules:
        suggestions.extend(rule_function(context))
    return sorted(
        _dedupe_suggestions(suggestions),
        key=lambda item: (item.slide_number, item.rule, item.element_ids),
    )


def format_suggestions(suggestions: Iterable[Suggestion]) -> list[str]:
    """Format suggestions in the slide-grouped text CLI shape."""
    items = list(suggestions)
    if not items:
        return ["No suggestions."]
    lines = [f"Advisor found {len(items)} suggestion(s):"]
    current_slide: int | None = None
    for suggestion in items:
        if suggestion.slide_number != current_slide:
            current_slide = suggestion.slide_number
            lines.append(f"Slide {current_slide:02d}")
        ids = ", ".join(suggestion.element_ids)
        lines.append(f"  [{suggestion.rule_id}] ({ids}): {suggestion.message}")
        if suggestion.command_hint:
            lines.append(f"    Command: {suggestion.command_hint}")
    return lines


__all__ = [
    "AdvisorElement",
    "AdvisorSlide",
    "BURIED_COVERAGE_RATIO",
    "COMPACT_UNION_AREA_RATIO",
    "EDGE_TOLERANCE_PT",
    "NEAR_OVERFLOW_LOWER_RATIO",
    "NEAR_OVERFLOW_UPPER_RATIO",
    "RULES",
    "RULE_TABLE",
    "STACK_GAP_TOLERANCE_PT",
    "STACK_SIZE_TOLERANCE_PT",
    "Suggestion",
    "WorkspaceContext",
    "advise_folder",
    "buried_element_rule",
    "format_suggestions",
    "load_workspace",
    "near_overflow_rule",
    "pseudo_group_rule",
    "stack_candidate_rule",
]
