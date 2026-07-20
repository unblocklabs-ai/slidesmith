"""SlidesClient - Main API for slidesmith.engine.

Provides the `pull`, `diff`, and `push` methods for the presentation workflow:
- id_mapping.json: clean_id -> google_object_id
- styles.json: clean_id -> styles (relative positions for children)
- slides/NN/content.sml: minimal XML with IDs, positions, and text
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

from slidesmith.engine.bounds import Transform
from slidesmith.engine.conflicts import (
    ConflictError,
    ensure_no_conflicts,
    index_presentation,
)
from slidesmith.engine.content_diff import (
    Change,
    ChangeType,
    DiffResult,
    diff_presentation,
)
from slidesmith.engine.content_parser import (
    ParsedElement,
    ParsedRun,
    flatten_elements,
    parse_slide_content,
)
from slidesmith.engine.content_requests import generate_batch_requests
from slidesmith.engine.json_utils import read_json
from slidesmith.engine.slide_processor import process_presentation, write_new_format
from slidesmith.engine.transport import APIError, Transport
from slidesmith.engine.workspace_layout import (
    ID_MAPPING_FILE,
    PRESENTATION_FILE,
    PRISTINE_BASE_FILE,
    PRISTINE_DIR,
    PRISTINE_ZIP,
    RAW_DIR,
    SLIDES_DIR,
    STYLES_FILE,
    create_pristine_zip,
    prune_stale_slide_folders,
    pull_timestamp,
    refresh_after_success,
)


def _pristine_element_metadata(
    data: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str | None]]:
    """Extract element types and group parentage from a pristine API tree."""
    types: dict[str, str] = {}
    parents: dict[str, str | None] = {}

    def walk(element: dict[str, Any], parent_id: str | None = None) -> None:
        object_id = element.get("objectId")
        if not isinstance(object_id, str) or not object_id:
            return
        if "elementGroup" in element:
            element_type = "GROUP"
        elif "shape" in element:
            element_type = element.get("shape", {}).get("shapeType", "SHAPE")
        elif "line" in element:
            element_type = "LINE"
        elif "image" in element:
            element_type = "IMAGE"
        elif "table" in element:
            element_type = "TABLE"
        elif "video" in element:
            element_type = "VIDEO"
        elif "sheetsChart" in element:
            element_type = "SHEETS_CHART"
        else:
            element_type = "UNKNOWN"
        types[object_id] = element_type
        parents[object_id] = parent_id
        for child in element.get("elementGroup", {}).get("children", []):
            walk(child, object_id)

    for page_kind in ("slides", "layouts", "masters"):
        for page in data.get(page_kind, []) or []:
            for element in page.get("pageElements", []) or []:
                walk(element)
    return types, parents


def _index_parsed_elements(
    slides: dict[str, list[Any]],
) -> dict[tuple[str, str], ParsedElement]:
    """Index refreshed or intended SML elements by slide and clean ID."""
    return {
        (slide_index, clean_id): element
        for slide_index, roots in slides.items()
        for clean_id, element in flatten_elements(roots).items()
    }


def _format_geometry(position: dict[str, float] | None) -> str:
    if position is None:
        return ""
    return ", ".join(
        f"{field}={position[field]:g}"
        for field in ("x", "y", "w", "h")
        if field in position
    )


def _format_run_style_classes(runs: list[list[ParsedRun]] | None) -> str:
    if not runs:
        return "(none)"
    values = [
        " ".join(run.text_style.to_classes()) if run.text_style is not None else ""
        for paragraph in runs
        for run in paragraph
    ]
    return " | ".join(values) if any(values) else "(none)"


def _format_paragraph_style_classes(change: Change, *, remote: bool) -> str:
    values: list[str] = []
    for update in change.paragraph_style_updates or []:
        styles = update.old_styles if remote else update.new_styles
        classes: list[str] = []
        if styles is not None:
            if styles.text_style is not None:
                classes.extend(styles.text_style.to_classes())
            if styles.paragraph_style is not None:
                classes.extend(styles.paragraph_style.to_classes())
        values.append(f"P{update.paragraph_index + 1}={' '.join(classes) or '(none)'}")
    return "; ".join(values)


def _format_changed_element_style_classes(
    change: Change,
    element: ParsedElement,
) -> str:
    styles = element.styles
    if styles is None:
        return "(none)"
    changed = change.new_styles
    classes: list[str] = []
    if changed is not None and changed.fill is not None and styles.fill is not None:
        fill_class = styles.fill.to_class()
        if fill_class:
            classes.append(fill_class)
    if (
        changed is not None and changed.stroke is not None
    ) or change.stroke_reset_fields:
        if styles.stroke is not None:
            classes.extend(styles.stroke.to_classes())
    if (
        changed is not None and changed.text_style is not None
    ) or change.text_style_reset_fields:
        if styles.text_style is not None:
            classes.extend(styles.text_style.to_classes())
    if (
        changed is not None and changed.paragraph_style is not None
    ) or change.paragraph_style_reset_fields:
        if styles.paragraph_style is not None:
            classes.extend(styles.paragraph_style.to_classes())
    if (
        (changed is not None and changed.content_alignment is not None)
        or change.reset_content_alignment
    ) and styles.content_alignment is not None:
        classes.append(styles.content_alignment.to_class())
    return " ".join(classes) or "(none)"


def _normalized_persistence_detail(
    change: Change,
    remote_elements: dict[tuple[str, str], ParsedElement],
    intended_elements: dict[tuple[str, str], ParsedElement],
) -> str | None:
    """Describe sent and refreshed values when both are cheaply available."""
    if change.change_type == ChangeType.MOVE:
        sent = _format_geometry(change.new_position)
        remote = _format_geometry(change.old_position)
        if sent and remote:
            return (
                f"geometry on {change.target_id} did not persist "
                f"(sent {sent!r}, remote now {remote!r})"
            )

    if change.change_type == ChangeType.TEXT_UPDATE:
        sent_text = "\n".join(change.new_text or [])
        remote_text = "\n".join(change.old_text or [])
        if change.new_text != change.old_text:
            return (
                f"text on {change.target_id} did not persist "
                f"(sent {sent_text!r}, remote now {remote_text!r})"
            )
        sent_styles = _format_run_style_classes(change.new_runs)
        remote_styles = _format_run_style_classes(change.old_runs)
        return (
            f"text run style classes on {change.target_id} did not persist "
            f"(sent {sent_styles!r}, remote now {remote_styles!r})"
        )

    if change.change_type == ChangeType.PARAGRAPH_STYLE_UPDATE:
        sent = _format_paragraph_style_classes(change, remote=False)
        remote = _format_paragraph_style_classes(change, remote=True)
        if sent and remote:
            return (
                f"paragraph style classes on {change.target_id} did not persist "
                f"(sent {sent!r}, remote now {remote!r})"
            )

    if change.change_type == ChangeType.STYLE_UPDATE:
        key = (change.slide_index or "", change.target_id)
        remote_element = remote_elements.get(key)
        intended_element = intended_elements.get(key)
        if remote_element is not None and intended_element is not None:
            sent = _format_changed_element_style_classes(change, intended_element)
            remote = _format_changed_element_style_classes(change, remote_element)
            return (
                f"style classes on {change.target_id} did not persist "
                f"(sent {sent!r}, remote now {remote!r})"
            )

    return None


def _enrich_pristine_geometry(
    styles: dict[str, dict[str, Any]],
    id_mapping: dict[str, str],
    base_raw: dict[str, Any],
) -> None:
    """Backfill native geometry for workspaces pulled by older versions."""
    elements, _ = index_presentation(base_raw)
    for clean_id, google_id in id_mapping.items():
        element = elements.get(google_id)
        if element is None:
            continue
        size = element.get("size")
        transform = element.get("transform")
        style = styles.setdefault(clean_id, {})
        if isinstance(size, dict):
            style.setdefault(
                "nativeSize",
                {
                    "w": size.get("width", {}).get("magnitude", 0),
                    "h": size.get("height", {}).get("magnitude", 0),
                },
            )
        if isinstance(transform, dict):
            style.setdefault(
                "nativeTransform",
                {
                    "scaleX": transform.get("scaleX", 1),
                    "scaleY": transform.get("scaleY", 1),
                    "shearX": transform.get("shearX", 0),
                    "shearY": transform.get("shearY", 0),
                    "translateX": transform.get("translateX", 0),
                    "translateY": transform.get("translateY", 0),
                },
            )

    clean_id_by_google_id = {
        google_id: clean_id for clean_id, google_id in id_mapping.items()
    }

    def walk(
        element: dict[str, Any],
        parent_group_transform: Transform | None = None,
    ) -> None:
        google_id = element.get("objectId")
        clean_id = clean_id_by_google_id.get(google_id)
        if clean_id is not None and parent_group_transform is not None:
            styles.setdefault(clean_id, {}).setdefault(
                "parentTransform",
                {
                    "scaleX": parent_group_transform.scale_x,
                    "scaleY": parent_group_transform.scale_y,
                    "shearX": parent_group_transform.shear_x,
                    "shearY": parent_group_transform.shear_y,
                    "translateX": parent_group_transform.translate_x,
                    "translateY": parent_group_transform.translate_y,
                },
            )

        child_group_transform = parent_group_transform
        if "elementGroup" in element:
            group_transform = Transform.from_element(element)
            child_group_transform = (
                parent_group_transform.compose(group_transform)
                if parent_group_transform is not None
                else group_transform
            )
        for child in element.get("elementGroup", {}).get("children", []):
            walk(child, child_group_transform)

    for page_kind in ("slides", "layouts", "masters"):
        for page in base_raw.get(page_kind, []) or []:
            for element in page.get("pageElements", []) or []:
                walk(element)


class SlidesClient:
    """Client for transforming Google Slides to/from SML format.

    This client uses a folder-based workflow:
    1. pull() - Fetch presentation and save as SML files
    2. diff() - Compare current content against pristine copy
    3. push() - Apply changes to Google Slides

    Example:
        >>> from slidesmith.engine.transport import GoogleSlidesTransport
        >>> transport = GoogleSlidesTransport(access_token="ya29...")
        >>> client = SlidesClient(transport)
        >>> await client.pull("1abc...", "./output")
        >>> # Edit slides/01/content.sml, slides/02/content.sml, etc.
        >>> changes = client.diff(Path("./output/1abc..."))
        >>> await client.push(Path("./output/1abc..."))
    """

    def __init__(self, transport: Transport | None = None) -> None:
        """Initialize the client.

        Args:
            transport: Transport for network operations; local diffing needs none.
        """
        self._transport = transport

    def _require_transport(self) -> Transport:
        if self._transport is None:
            raise RuntimeError("A transport is required for pull and push operations")
        return self._transport

    async def pull(
        self,
        presentation_id: str,
        output_path: str | Path,
        *,
        save_raw: bool = True,
    ) -> list[Path]:
        """Pull a presentation and write to SML format.

        Creates a folder with:
        - presentation.json: Metadata (title, page size, slide count)
        - id_mapping.json: clean_id -> google_object_id
        - styles.json: clean_id -> styles (with relative positions)
        - slides/01/content.sml, slides/02/content.sml, ...
        - .raw/presentation.json: Raw API response (optional)
        - .pristine/presentation.zip: Zip for diff comparison

        Args:
            presentation_id: The ID of the presentation (from the URL)
            output_path: Directory to write files to
            save_raw: If True, saves raw API response to .raw/ folder

        Returns:
            List of paths to written files
        """
        # Fetch presentation data
        presentation_data = await self._require_transport().get_presentation(
            presentation_id
        )

        # Create output directory
        output_path = Path(output_path)
        presentation_dir = output_path / presentation_id
        presentation_dir.mkdir(parents=True, exist_ok=True)

        written_files: list[Path] = []

        # Process the presentation into the new format
        result = process_presentation(presentation_data.data)

        # Record the pull-time revisionId in the folder metadata so tooling
        # can tell how stale a workspace is (see DESIGN.md: revisionId is a
        # write guard, not a change detector).
        revision_id = presentation_data.revision_id
        if revision_id:
            result["presentation_info"]["revisionId"] = revision_id
        result["presentation_info"]["pulledAt"] = pull_timestamp()

        # Write the new format files
        written_files.extend(write_new_format(result, presentation_dir))
        prune_stale_slide_folders(
            presentation_dir,
            {slide["slide_index"] for slide in result["slides"]},
        )

        # Save raw API response
        if save_raw:
            raw_dir = presentation_dir / RAW_DIR
            raw_dir.mkdir(parents=True, exist_ok=True)
            raw_path = raw_dir / "presentation.json"
            raw_path.write_text(
                json.dumps(presentation_data.data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            written_files.append(raw_path)

        # Create pristine copy
        pristine_path = create_pristine_zip(presentation_dir, written_files)
        written_files.append(pristine_path)

        # Always persist the raw API tree as the pristine base snapshot:
        # push compares remote vs this base to detect concurrent human edits
        # on the objects a push would touch (independent of save_raw).
        base_path = presentation_dir / PRISTINE_DIR / PRISTINE_BASE_FILE
        base_path.write_text(
            json.dumps(presentation_data.data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        written_files.append(base_path)

        from slidesmith.engine.qa import record_qa_baseline

        written_files.append(record_qa_baseline(presentation_dir))

        return written_files

    def diff(self, folder_path: Path) -> list[dict[str, Any]]:
        """Compare current content against pristine copy and generate update requests.

        This is a local-only operation that does not call any APIs.

        Args:
            folder_path: Path to the presentation folder

        Returns:
            List of Google Slides API batchUpdate request objects
        """
        _, requests = self.diff_with_result(folder_path)
        return requests

    def diff_with_result(
        self, folder_path: Path
    ) -> tuple[DiffResult, list[dict[str, Any]]]:
        """Return both semantic changes and their generated API requests."""
        folder_path = Path(folder_path)

        # Read current state
        current_slides = self._read_current_slides(folder_path)
        id_mapping = read_json(folder_path / ID_MAPPING_FILE, missing_ok=True)

        # Read pristine state
        pristine_slides, pristine_styles = self._read_pristine(folder_path)
        base_raw = self._read_base_raw(folder_path) or {}
        _enrich_pristine_geometry(pristine_styles, id_mapping, base_raw)

        # Generate diff
        diff_result = diff_presentation(
            pristine_slides,
            current_slides,
            pristine_styles,
            id_mapping,
        )

        # Build slide ID mapping (slide_index -> google_slide_id)
        metadata = read_json(folder_path / PRESENTATION_FILE, missing_ok=True)
        slide_id_mapping = self._build_slide_id_mapping(
            id_mapping, metadata.get("slideOrder")
        )

        pristine_types, pristine_parents = _pristine_element_metadata(base_raw)

        # Generate API requests
        requests = generate_batch_requests(
            diff_result,
            id_mapping,
            slide_id_mapping,
            pristine_types,
            pristine_parents,
        )
        return diff_result, requests

    async def push(self, folder_path: Path, *, force: bool = False) -> dict[str, Any]:
        """Apply content changes to the presentation, guarded against
        concurrent human edits (contract C5).

        Flow:
        1. Load the presentation identity and pending local diff.
        2. Re-fetch the remote presentation and guard the touched objects.
        3. Apply the batch with the just-fetched revision lock.
        4. Refresh the workspace from the authoritative post-push deck.

        Remote changes to objects this push does NOT touch never block the
        push: field-masked requests leave them alone.

        Args:
            folder_path: Path to the presentation folder
            force: Skip the conflict guard and the revision lock (logs a
                warning; last writer wins on the touched properties).

        Returns:
            API response from batchUpdate

        Raises:
            ConflictError: A touched object changed remotely, or the deck was
                revised between our fetch and our write.
        """
        folder_path = Path(folder_path)

        # 1. Load the presentation identity and pending local diff.
        metadata = read_json(folder_path / PRESENTATION_FILE, missing_ok=True)
        presentation_id = metadata.get("presentationId")
        if not presentation_id:
            raise ValueError("Presentation ID not found in presentation.json")

        diff_result, requests = self.diff_with_result(folder_path)

        if not requests:
            return {"replies": [], "message": "No changes detected."}

        intended_slides = self._read_current_slides(folder_path)
        intended_change_keys = {
            (change.target_id, change.change_type)
            for change in diff_result.changes
        }
        transport = self._require_transport()

        if force:
            warning = (
                "push --force: conflict guard and revision lock "
                "bypassed; concurrent human edits to the touched properties "
                "will be overwritten"
            )
            response = await transport.batch_update(presentation_id, requests)
            if diff_result.warnings:
                response.setdefault("warnings", []).extend(diff_result.warnings)
            response.setdefault("warnings", []).append(warning)
            refreshed = await refresh_after_success(
                transport, folder_path, presentation_id, response
            )
            if refreshed:
                self._append_persistence_warning(
                    folder_path,
                    intended_slides,
                    intended_change_keys,
                    response,
                )
            return response

        # 2. Re-fetch the remote presentation and guard the touched objects.
        remote = await transport.get_presentation(presentation_id)
        required_revision = remote.revision_id

        base_raw = self._read_base_raw(folder_path)
        if base_raw is None:
            warning = (
                "no pristine base snapshot found "
                f"({PRISTINE_DIR}/{PRISTINE_BASE_FILE}); this folder was "
                "pulled by an older slidesmith. Remote-change detection "
                "skipped for this push -- re-pull to re-enable the guard."
            )
        else:
            ensure_no_conflicts(
                base_raw,
                remote.data,
                requests,
                read_json(folder_path / ID_MAPPING_FILE, missing_ok=True),
            )

        # 3. Apply the batch with the just-fetched revision lock.
        try:
            response = await transport.batch_update(
                presentation_id, requests, required_revision_id=required_revision
            )
        except APIError as e:
            if e.status_code == 400 and "revision" in str(e).lower():
                raise ConflictError(
                    "push aborted: the deck changed mid-push (someone edited "
                    "it between our conflict check and our write). "
                    "Re-pull and retry."
                ) from e
            raise

        if diff_result.warnings:
            response.setdefault("warnings", []).extend(diff_result.warnings)

        if base_raw is None:
            response.setdefault("warnings", []).append(warning)

        # 4. Refresh from the authoritative post-push deck.
        refreshed = await refresh_after_success(
            transport, folder_path, presentation_id, response
        )
        if refreshed:
            self._append_persistence_warning(
                folder_path,
                intended_slides,
                intended_change_keys,
                response,
            )
        return response

    def _append_persistence_warning(
        self,
        folder_path: Path,
        intended_slides: dict[str, list[Any]],
        intended_change_keys: set[tuple[str, ChangeType]],
        response: dict[str, Any],
    ) -> None:
        """Warn when pushed semantic changes differ from refreshed truth."""
        refreshed_slides, refreshed_styles = self._read_pristine(folder_path)
        divergence = diff_presentation(
            refreshed_slides,
            intended_slides,
            refreshed_styles,
            read_json(folder_path / ID_MAPPING_FILE, missing_ok=True),
        )
        unpersisted = [
            change
            for change in divergence.changes
            if (change.target_id, change.change_type) in intended_change_keys
        ]
        if not unpersisted:
            return

        changes = sorted(
            unpersisted,
            key=lambda change: (
                change.slide_index or "",
                change.target_id,
                change.change_type.value,
            ),
        )
        remote_elements = _index_parsed_elements(refreshed_slides)
        intended_elements = _index_parsed_elements(intended_slides)
        details = ", ".join(
            _normalized_persistence_detail(
                change,
                remote_elements,
                intended_elements,
            )
            or f"{change.target_id} ({change.change_type.value.replace('_', ' ')})"
            for change in changes
        )
        response.setdefault("warnings", []).append(
            f"{len(changes)} change(s) did not persist remotely: {details} "
            "— the API may not support these values"
        )

    def _read_base_raw(self, folder_path: Path) -> dict[str, Any] | None:
        """Read the pristine base raw API tree, if this folder has one.

        Prefers .pristine/base.json (always written by current pulls); falls
        back to .raw/presentation.json (older pulls with save_raw=True).
        Returns None when neither exists (folder pulled by old code).
        """
        candidates = (
            folder_path / PRISTINE_DIR / PRISTINE_BASE_FILE,
            folder_path / RAW_DIR / "presentation.json",
        )
        for candidate in candidates:
            if candidate.exists():
                return read_json(candidate, missing_ok=False)
        return None


    def _read_current_slides(self, folder_path: Path) -> dict[str, list[Any]]:
        """Read current slide content files."""
        slides_dir = folder_path / SLIDES_DIR
        result: dict[str, list[Any]] = {}

        if not slides_dir.exists():
            return result

        for slide_folder in sorted(slides_dir.iterdir()):
            if slide_folder.is_dir():
                content_file = slide_folder / "content.sml"
                if content_file.exists():
                    content = content_file.read_text(encoding="utf-8")
                    result[slide_folder.name] = parse_slide_content(content)

        return result

    def _read_pristine(
        self,
        folder_path: Path,
    ) -> tuple[dict[str, list[Any]], dict[str, dict[str, Any]]]:
        """Read pristine slides and styles from zip."""
        zip_path = folder_path / PRISTINE_DIR / PRISTINE_ZIP
        if not zip_path.exists():
            raise FileNotFoundError(f"Pristine zip not found: {zip_path}")

        slides: dict[str, list[Any]] = {}
        styles: dict[str, dict[str, Any]] = {}

        with zipfile.ZipFile(zip_path, "r") as zf:
            # Read styles.json
            if STYLES_FILE in zf.namelist():
                styles = json.loads(zf.read(STYLES_FILE).decode("utf-8"))

            # Read slide content files
            for name in zf.namelist():
                if name.startswith(f"{SLIDES_DIR}/") and name.endswith("/content.sml"):
                    # Extract slide index from path like "slides/01/content.sml"
                    parts = name.split("/")
                    if len(parts) >= 2:
                        slide_index = parts[1]
                        content = zf.read(name).decode("utf-8")
                        slides[slide_index] = parse_slide_content(content)

        return slides, styles

    def _build_slide_id_mapping(
        self,
        id_mapping: dict[str, str],
        slide_order: list[str] | None = None,
    ) -> dict[str, str]:
        """Build mapping from slide index to Google slide ID.

        Slide clean IDs are like "s1", "s2", etc.
        Slide indices are like "01", "02", etc.
        """
        result: dict[str, str] = {}

        ordered_ids = slide_order
        if not isinstance(ordered_ids, list):
            ordered_ids = sorted(
                (
                    clean_id
                    for clean_id in id_mapping
                    if re.fullmatch(r"s\d+", clean_id)
                ),
                key=lambda value: int(value[1:]),
            )
        for index, clean_id in enumerate(ordered_ids, 1):
            google_id = id_mapping.get(clean_id)
            if google_id:
                result[f"{index:02d}"] = google_id

        return result

def diff_folder(folder_path: str | Path) -> list[dict[str, Any]]:
    """Convenience function to diff a presentation folder.

    Args:
        folder_path: Path to the presentation folder

    Returns:
        List of batchUpdate request objects
    """

    return diff_folder_with_result(folder_path)[1]


def diff_folder_with_result(
    folder_path: str | Path,
) -> tuple[DiffResult, list[dict[str, Any]]]:
    """Return semantic changes and batchUpdate requests for a local folder."""
    client = SlidesClient()
    return client.diff_with_result(Path(folder_path))
