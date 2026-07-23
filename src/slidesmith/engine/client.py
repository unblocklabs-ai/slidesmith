"""SlidesClient - Main API for slidesmith.engine.

Provides the `pull`, `diff`, and `push` methods for the presentation workflow:
- id_mapping.json: clean_id -> google_object_id
- styles.json: clean_id -> styles (relative positions for children)
- slides/NN/content.sml: minimal XML with IDs, positions, and text
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import re
from typing import Any

from slidesmith.engine.assets import (
    AssetCache,
    AssetUploader,
    image_source_kind,
)
from slidesmith.engine.bounds import get_bounds
from slidesmith.engine.conflicts import ConflictError
from slidesmith.engine.content_diff import (
    ChangeType,
    DiffResult,
    diff_presentation,
)
from slidesmith.engine.diff_model import PushWarning, WarningSeverity
from slidesmith.engine.content_requests import generate_batch_requests
from slidesmith.engine.json_utils import read_json
from slidesmith.engine.image_fetch import fetch_image_dimensions
from slidesmith.engine.push_progress import (
    clear_progress_ledger,
    load_progress_ledger,
    write_progress_ledger,
)
from slidesmith.engine.transport import APIError, PresentationData, Transport
from slidesmith.engine import push_executor as _push_executor
from slidesmith.engine.image_replace import (
    CoverFitPushError,
    _find_element_with_parent_transform,
    _replacement_geometry_requests,
    _replacement_image_dimensions,
    resolve_asset_source,
)
from slidesmith.engine.id_manager import authored_clean_id
from slidesmith.engine.persistence import append_persistence_warning
from slidesmith.engine.push_executor import (
    PerSlideConflictError,
    PerSlidePushError,
    _missing_base_warning,
    _progress_slide_total,
    _report_progress,
    partition_requests_by_slide,
)
from slidesmith.engine.workspace_layout import (
    ID_MAPPING_FILE,
    PRESENTATION_FILE,
    materialize_workspace,
    refresh_after_success,
)
from slidesmith.engine.workspace_reader import (
    _build_slide_id_mapping,
    _enrich_pristine_geometry,
    _pristine_element_metadata,
    _read_base_raw,
    _read_current_slides,
    _read_current_slide_metadata,
    _read_pristine,
)
from slidesmith.engine.z_order import (
    build_group_requests,
    build_reorder_requests,
    validate_live_group_targets,
    validate_live_reorder_targets,
)


async def execute_guarded_batch(
    *,
    transport: Transport,
    presentation_id: str,
    requests: list[dict[str, Any]],
    base_raw: dict[str, Any] | None,
    id_mapping: dict[str, str],
    guard_conflicts: bool,
    lock_revision: bool,
) -> dict[str, Any]:
    """Compatibility bridge for the shared guarded batch executor."""
    return await _push_executor.execute_guarded_batch(
        transport=transport,
        presentation_id=presentation_id,
        requests=requests,
        base_raw=base_raw,
        id_mapping=id_mapping,
        guard_conflicts=guard_conflicts,
        lock_revision=lock_revision,
    )


async def finalize_push(
    *,
    transport: Transport,
    folder_path: Path,
    presentation_id: str,
    response: dict[str, Any],
    diff_warnings: list[PushWarning],
    base_warning: PushWarning | None,
    force_warning: PushWarning | None,
    verify_persistence: Callable[[dict[str, Any]], None],
    clear_progress: bool,
) -> dict[str, Any]:
    """Compatibility bridge preserving the client refresh patch point."""
    return await _push_executor._finalize_push(
        transport=transport,
        folder_path=folder_path,
        presentation_id=presentation_id,
        response=response,
        diff_warnings=diff_warnings,
        base_warning=base_warning,
        force_warning=force_warning,
        verify_persistence=verify_persistence,
        clear_progress=clear_progress,
        refresh=refresh_after_success,
    )


def validate_create_output_parent(output_path: str | Path) -> Path:
    """Validate the existing, writable parent used by ``create``.

    This check is intentionally local and must run before authentication or a
    transport request.  The presentation ID is assigned remotely, so the
    ID-specific child collision is checked again after creation.
    """
    parent = Path(output_path)
    if not parent.exists():
        raise FileNotFoundError(f"Output parent does not exist: {parent}")
    if not parent.is_dir():
        raise NotADirectoryError(f"Output parent is not a directory: {parent}")
    mode = parent.stat().st_mode
    if not mode & 0o222 or not os.access(parent, os.W_OK | os.X_OK):
        raise PermissionError(f"Output parent is not writable: {parent}")
    if (parent / PRESENTATION_FILE).exists():
        raise FileExistsError(
            f"Workspace already exists: {parent}. "
            "Pass its parent directory with --dir, or choose a different path."
        )
    return parent


def _presentation_url(presentation_id: str) -> str:
    return f"https://docs.google.com/presentation/d/{presentation_id}/edit"


class PresentationCreateError(RuntimeError):
    """A remote deck exists but its local workspace could not be materialized."""

    def __init__(
        self,
        presentation_id: str,
        output_path: Path,
        cause: Exception,
    ) -> None:
        self.presentation_id = presentation_id
        self.presentation_url = _presentation_url(presentation_id)
        super().__init__(
            f"Local workspace creation failed: {cause}\n"
            f"Presentation ID: {presentation_id}\n"
            f"URL: {self.presentation_url}\n"
            "The remote deck exists and can be pulled normally with: "
            f"slidesmith pull {presentation_id} -o {output_path}"
        )


def _cover_element_ids(
    diff_result: DiffResult,
    id_mapping: dict[str, str],
    requests: list[dict[str, Any]],
    failed_request_index: int | None,
) -> list[str]:
    """Map one failed CENTER_CROP request or its paired pin to a clean ID."""
    if failed_request_index is None or not 0 <= failed_request_index < len(requests):
        return []

    cover_ids_by_object = {
        id_mapping.get(change.target_id)
        or diff_result.generated_image_ids.get(change.target_id): change.target_id
        for change in diff_result.changes
        if change.fit == "cover"
    }

    request = requests[failed_request_index]
    image_request = request.get("replaceImage")
    if isinstance(image_request, dict) and image_request.get(
        "imageReplaceMethod"
    ) == "CENTER_CROP":
        element_id = cover_ids_by_object.get(image_request.get("imageObjectId"))
        return [element_id] if element_id else []

    # The geometry pin immediately follows its CENTER_CROP replacement. Keep
    # unrelated style/text failures on their original API error path.
    if failed_request_index == 0:
        return []
    previous = requests[failed_request_index - 1].get("replaceImage")
    if not isinstance(previous, dict) or previous.get("imageReplaceMethod") != "CENTER_CROP":
        return []
    element_id = cover_ids_by_object.get(previous.get("imageObjectId"))
    return [element_id] if element_id else []


def _failed_request_index(error: APIError) -> int | None:
    """Extract Google's failed batch request index from an API error message."""
    explicit = getattr(error, "failed_request_index", None)
    if isinstance(explicit, int) and explicit >= 0:
        return explicit
    match = re.search(r"\brequests\[(\d+)\]", str(error))
    return int(match.group(1)) if match else None


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

    def __init__(
        self,
        transport: Transport | None = None,
        asset_uploader: AssetUploader | None = None,
    ) -> None:
        """Initialize the client.

        Args:
            transport: Transport for network operations; local diffing needs none.
            asset_uploader: Drive upload seam for local image sources. Tests can
                inject an offline fake; URL-only workflows do not need one.
        """
        self._transport = transport
        self._asset_uploader = asset_uploader

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

        output_path = Path(output_path)
        presentation_dir = output_path / presentation_id
        return materialize_workspace(
            presentation_data.data,
            presentation_dir,
            revision_id=presentation_data.revision_id,
            save_raw=save_raw,
            record_qa_baseline=True,
        )

    async def create(
        self,
        title: str,
        output_path: str | Path,
        *,
        save_raw: bool = True,
        on_created: Callable[[PresentationData], None] | None = None,
    ) -> PresentationData:
        """Create a deck and materialize it through the normal pull projection."""
        output_path = validate_create_output_parent(output_path)
        presentation_data = await self._require_transport().create_presentation(title)
        try:
            if on_created is not None:
                on_created(presentation_data)
            presentation_dir = output_path / presentation_data.presentation_id
            if presentation_dir.exists():
                raise FileExistsError(
                    f"Workspace already exists: {presentation_dir}. "
                    "Choose a different --dir or remove the existing workspace."
                )
            materialize_workspace(
                presentation_data.data,
                presentation_dir,
                revision_id=presentation_data.revision_id,
                save_raw=save_raw,
                record_qa_baseline=True,
            )
        except Exception as exc:
            raise PresentationCreateError(
                presentation_data.presentation_id,
                output_path,
                exc,
            ) from exc
        return presentation_data

    def diff(
        self,
        folder_path: Path,
        *,
        slide: int | None = None,
    ) -> list[dict[str, Any]]:
        """Compare current content against pristine copy and generate update requests.

        This is a local-only operation that does not call any APIs.

        Args:
            folder_path: Path to the presentation folder

        Returns:
            List of Google Slides API batchUpdate request objects
        """
        _, requests = self.diff_with_result(folder_path, slide=slide)
        return requests

    def diff_with_result(
        self,
        folder_path: Path,
        *,
        slide: int | None = None,
        allow_remote_image_fetch: bool = False,
        fetch_remote_stretch_dimensions: bool = False,
    ) -> tuple[DiffResult, list[dict[str, Any]]]:
        """Return semantic changes and requests, offline unless fetch is allowed."""
        folder_path = Path(folder_path)
        if slide is not None and slide < 1:
            raise ValueError("diff --slide must be at least 1")

        # Read current state
        current_slides = self._read_current_slides(folder_path)
        current_slide_metadata = _read_current_slide_metadata(folder_path)
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
            slide_metadata=current_slide_metadata,
            workspace_root=folder_path,
            allow_remote_image_fetch=allow_remote_image_fetch,
            fetch_remote_stretch_dimensions=fetch_remote_stretch_dimensions,
        )
        if slide is not None:
            slide_index = f"{slide:02d}"
            diff_result.changes = [
                change
                for change in diff_result.changes
                if change.slide_index == slide_index
            ]

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

    async def push(
        self,
        folder_path: Path,
        *,
        force: bool = False,
        per_slide: bool = False,
        resume: bool = False,
        progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
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

        if resume and not per_slide:
            raise ValueError("--resume requires --per-slide")

        # 1. Load the presentation identity and pending local diff.
        metadata = read_json(folder_path / PRESENTATION_FILE, missing_ok=True)
        presentation_id = metadata.get("presentationId")
        if not presentation_id:
            raise ValueError("Presentation ID not found in presentation.json")

        diff_result, requests = self.diff_with_result(
            folder_path,
            allow_remote_image_fetch=True,
            fetch_remote_stretch_dimensions=True,
        )

        if not requests:
            if per_slide:
                clear_progress_ledger(folder_path)
            return {"replies": [], "message": "No changes detected."}

        # Diff remains local and leaves authored local paths in preview JSON.
        # Resolve only the outgoing request URLs at push time.
        id_mapping = read_json(folder_path / ID_MAPPING_FILE, missing_ok=True)
        resolved_image_sources = await self._resolve_local_asset_requests(
            folder_path, requests, diff_result
        )
        expected_image_sources = self._expected_image_sources(
            diff_result,
            requests,
            id_mapping,
            resolved_image_sources,
        )

        intended_slides = self._read_current_slides(folder_path)
        self._mark_remote_cover_creates_as_local(
            folder_path, intended_slides, diff_result
        )
        intended_change_keys = {
            (change.target_id, change.change_type)
            for change in diff_result.changes
        }
        create_copy_targets = {
            (change.slide_index or "", change.target_id)
            for change in diff_result.changes
            if change.change_type in {ChangeType.CREATE, ChangeType.COPY}
        }
        transport = self._require_transport()

        if per_slide:
            return await self._push_per_slide(
                folder_path=folder_path,
                presentation_id=presentation_id,
                diff_result=diff_result,
                requests=requests,
                intended_slides=intended_slides,
                intended_change_keys=intended_change_keys,
                create_copy_targets=create_copy_targets,
                expected_image_sources=expected_image_sources,
                force=force,
                resume=resume,
                progress=progress,
            )

        force_warning: PushWarning | None = None
        if force:
            force_warning = PushWarning(
                WarningSeverity.WARNING,
                "push --force: conflict guard and revision lock bypassed; "
                "concurrent human edits to the touched properties will be "
                "overwritten",
            )

        base_raw = self._read_base_raw(folder_path)
        base_warning = (
            _missing_base_warning() if base_raw is None and not force else None
        )
        await transport.refresh_if_expiring()
        try:
            response = await execute_guarded_batch(
                transport=transport,
                presentation_id=presentation_id,
                requests=requests,
                base_raw=base_raw,
                id_mapping=id_mapping,
                guard_conflicts=not force,
                lock_revision=not force,
            )
        except APIError as exc:
            cover_ids = _cover_element_ids(
                diff_result, id_mapping, requests, _failed_request_index(exc)
            )
            if cover_ids:
                raise CoverFitPushError(cover_ids, exc) from exc
            raise
        return await finalize_push(
            transport=transport,
            folder_path=folder_path,
            presentation_id=presentation_id,
            response=response,
            diff_warnings=diff_result.warnings,
            base_warning=base_warning,
            force_warning=force_warning,
            verify_persistence=lambda finalized_response: (
                self._append_persistence_warning(
                    folder_path,
                    intended_slides,
                    intended_change_keys,
                    create_copy_targets,
                    finalized_response,
                    author_changes=diff_result.changes,
                    expected_image_sources=expected_image_sources,
                )
            ),
            clear_progress=False,
        )

    async def replace_image(
        self,
        folder_path: Path,
        element_id: str,
        new_source: str,
        *,
        fit: str = "contain",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Replace an image and explicitly pin its requested page geometry."""
        folder_path = Path(folder_path)
        if fit not in {"contain", "stretch", "cover"}:
            raise ValueError(
                "replace-image --fit must be 'contain', 'stretch', or 'cover'"
            )
        metadata = read_json(folder_path / PRESENTATION_FILE, missing_ok=False)
        presentation_id = metadata.get("presentationId")
        if not isinstance(presentation_id, str) or not presentation_id:
            raise ValueError("Presentation ID not found in presentation.json")

        if self.diff(folder_path):
            raise ValueError(
                "replace-image requires a clean workspace because its post-write "
                "refresh would replace pending SML edits; push or revert them first"
            )

        id_mapping = read_json(folder_path / ID_MAPPING_FILE, missing_ok=False)
        google_id = id_mapping.get(element_id)
        if not isinstance(google_id, str) or not google_id:
            raise ValueError(f"Element {element_id!r} was not found in id_mapping.json")

        transport = self._require_transport()
        remote = await transport.get_presentation(presentation_id)
        target, parent_transform = _find_element_with_parent_transform(
            remote.data, google_id
        )
        if target is None:
            raise ValueError(
                f"Element {element_id!r} ({google_id}) no longer exists in the deck"
            )
        if "image" not in target:
            raise ValueError(f"Element {element_id!r} is not an image")

        if fit == "cover":
            pixel_width = pixel_height = None
        else:
            pixel_width, pixel_height = self._replacement_image_dimensions(
                folder_path, new_source
            )
        old_geometry = get_bounds(target, parent_transform)
        geometry, pin_request = _replacement_geometry_requests(
            google_id,
            old_geometry,
            pixel_width=pixel_width,
            pixel_height=pixel_height,
            fit=fit,
        )
        replace_request = {
            "replaceImage": {
                "imageObjectId": google_id,
                "url": new_source,
                "imageReplaceMethod": (
                    "CENTER_CROP" if fit == "cover" else "CENTER_INSIDE"
                ),
            }
        }
        requests = [replace_request, pin_request]
        if dry_run:
            return {
                "dryRun": True,
                "geometry": {
                    "fit": fit,
                    "x": geometry.x,
                    "y": geometry.y,
                    "w": geometry.w,
                    "h": geometry.h,
                    "unit": "PT",
                },
                "requests": requests,
            }

        replace_request["replaceImage"]["url"] = await self._resolve_asset_source(
            folder_path, new_source
        )
        try:
            response = await transport.batch_update(
                presentation_id,
                requests,
                required_revision_id=remote.revision_id,
            )
        except APIError as exc:
            if exc.status_code == 400 and "revision" in str(exc).lower():
                raise ConflictError(
                    "replace-image aborted: the deck changed between validation "
                    "and the write; re-pull and retry"
                ) from exc
            if fit == "cover":
                raise CoverFitPushError([element_id], exc) from exc
            raise

        await refresh_after_success(transport, folder_path, presentation_id, response)
        return response

    async def reorder(
        self,
        folder_path: Path,
        selector: str,
        operation: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Reorder selected top-level page elements in the live deck."""
        folder_path = Path(folder_path)
        if self.diff(folder_path):
            raise ValueError(
                "reorder requires a clean workspace because its post-write refresh "
                "would replace pending SML edits; push or revert them first"
            )

        requests = build_reorder_requests(folder_path, selector, operation)
        if dry_run:
            return {"dryRun": True, "requests": requests}

        metadata = read_json(folder_path / PRESENTATION_FILE, missing_ok=False)
        presentation_id = metadata.get("presentationId")
        if not isinstance(presentation_id, str) or not presentation_id:
            raise ValueError("Presentation ID not found in presentation.json")

        mapping = read_json(folder_path / ID_MAPPING_FILE, missing_ok=False)
        transport = self._require_transport()
        remote = await transport.get_presentation(presentation_id)
        validate_live_reorder_targets(remote.data, requests, mapping)
        try:
            response = await transport.batch_update(
                presentation_id,
                requests,
                required_revision_id=remote.revision_id,
            )
        except APIError as exc:
            if exc.status_code == 400 and "revision" in str(exc).lower():
                raise ConflictError(
                    "reorder aborted: the deck changed between validation and "
                    "the write; re-pull and retry"
                ) from exc
            raise

        await refresh_after_success(transport, folder_path, presentation_id, response)
        return response

    async def group(
        self,
        folder_path: Path,
        selector: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Group selected top-level siblings in the live deck."""
        folder_path = Path(folder_path)
        if self.diff(folder_path):
            raise ValueError(
                "group requires a clean workspace because its post-write refresh "
                "would replace pending SML edits; push or revert them first"
            )

        requests = build_group_requests(folder_path, selector)
        if dry_run:
            return {"dryRun": True, "requests": requests}

        metadata = read_json(folder_path / PRESENTATION_FILE, missing_ok=False)
        presentation_id = metadata.get("presentationId")
        if not isinstance(presentation_id, str) or not presentation_id:
            raise ValueError("Presentation ID not found in presentation.json")

        mapping = read_json(folder_path / ID_MAPPING_FILE, missing_ok=False)
        transport = self._require_transport()
        remote = await transport.get_presentation(presentation_id)
        validate_live_group_targets(remote.data, requests, mapping)
        try:
            response = await transport.batch_update(
                presentation_id,
                requests,
                required_revision_id=remote.revision_id,
            )
        except APIError as exc:
            if exc.status_code == 400 and "revision" in str(exc).lower():
                raise ConflictError(
                    "group aborted: the deck changed between validation and the "
                    "write; re-pull and retry"
                ) from exc
            raise

        await refresh_after_success(transport, folder_path, presentation_id, response)
        return response

    @staticmethod
    def _replacement_image_dimensions(
        folder_path: Path, source: str
    ) -> tuple[int, int]:
        """Read replacement pixels through the same bounded source paths as create."""
        return _replacement_image_dimensions(
            folder_path,
            source,
            fetch_image_dimensions,
        )

    @staticmethod
    def _mark_remote_cover_creates_as_local(
        folder_path: Path,
        intended_slides: dict[str, list[Any]],
        diff_result: DiffResult,
    ) -> None:
        """Model derived remote creates as local rasters for persistence QA."""
        cache = AssetCache(folder_path)

        def replace_source(elements: list[Any], target_id: str, source: str) -> bool:
            for element in elements:
                if getattr(element, "clean_id", None) == target_id:
                    element.src = source
                    return True
                if replace_source(getattr(element, "children", []), target_id, source):
                    return True
            return False

        for change in diff_result.changes:
            if (
                change.change_type != ChangeType.CREATE
                or change.fit != "cover"
                or not change.src
                or image_source_kind(change.src) != "remote"
                or change.new_position is None
            ):
                continue
            source = cache.remote_cover_local_source(
                change.src,
                change.new_position["w"] / change.new_position["h"],
            )
            if source is not None:
                replace_source(
                    intended_slides.get(change.slide_index or "", []),
                    change.target_id,
                    source,
                )

    async def _resolve_local_asset_requests(
        self,
        folder_path: Path,
        requests: list[dict[str, Any]],
        diff_result: DiffResult,
    ) -> dict[str, str]:
        resolved_image_sources: dict[str, str] = {}
        cover_changes = {
            change.target_id: change
            for change in diff_result.changes
            if change.change_type == ChangeType.CREATE
            and change.fit == "cover"
            and change.src is not None
        }
        cover_creates = {
            object_id: cover_changes[target_id]
            for target_id, object_id in diff_result.generated_image_ids.items()
            if target_id in cover_changes
        }
        for request in requests:
            image_request = request.get("createImage") or request.get("replaceImage")
            if not isinstance(image_request, dict):
                continue
            source = image_request.get("url")
            if not isinstance(source, str):
                continue
            cover_change = cover_creates.get(image_request.get("objectId"))
            if cover_change is not None and cover_change.new_position is not None:
                try:
                    image_request["url"] = await self._resolve_asset_source(
                        folder_path,
                        source,
                        cover_aspect=(
                            cover_change.new_position["w"]
                            / cover_change.new_position["h"]
                        ),
                        element_id=cover_change.target_id,
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"Image element '{cover_change.target_id}' cover fit "
                        f"could not derive a static asset: {exc}"
                    ) from exc
            elif image_source_kind(source) == "local":
                image_request["url"] = await self._resolve_asset_source(
                    folder_path, source
                )
            object_id_key = (
                "imageObjectId" if "replaceImage" in request else "objectId"
            )
            object_id = image_request.get(object_id_key)
            if isinstance(object_id, str):
                resolved_image_sources[object_id] = image_request["url"]
        return resolved_image_sources

    @staticmethod
    def _expected_image_sources(
        diff_result: DiffResult,
        requests: list[dict[str, Any]],
        id_mapping: dict[str, str],
        resolved_image_sources: dict[str, str],
    ) -> dict[tuple[str, str], str]:
        """Map the URLs actually sent to the clean IDs seen after refresh."""
        reverse_mapping = {
            google_id: clean_id for clean_id, google_id in id_mapping.items()
        }
        request_clean_ids: dict[str, str] = {}
        for request in requests:
            image_request = request.get("createImage") or request.get("replaceImage")
            if not isinstance(image_request, dict):
                continue
            object_id_key = (
                "imageObjectId" if "replaceImage" in request else "objectId"
            )
            object_id = image_request.get(object_id_key)
            if not isinstance(object_id, str):
                continue
            clean_id = reverse_mapping.get(object_id) or authored_clean_id(object_id)
            if clean_id is not None:
                request_clean_ids[object_id] = clean_id

        expected: dict[tuple[str, str], str] = {}
        for change in diff_result.changes:
            if not change.src:
                continue
            object_id = id_mapping.get(change.target_id)
            if object_id is None:
                object_id = next(
                    (
                        candidate
                        for candidate, clean_id in request_clean_ids.items()
                        if clean_id == change.target_id
                    ),
                    None,
                )
            if object_id in resolved_image_sources:
                expected[(change.slide_index or "", change.target_id)] = (
                    resolved_image_sources[object_id]
                )
        return expected

    async def _resolve_asset_source(
        self,
        folder_path: Path,
        source: str,
        *,
        cover_aspect: float | None = None,
        element_id: str | None = None,
    ) -> str:
        return await resolve_asset_source(
            folder_path,
            source,
            self._asset_uploader,
            cover_aspect=cover_aspect,
            element_id=element_id,
        )

    async def _push_per_slide(
        self,
        *,
        folder_path: Path,
        presentation_id: str,
        diff_result: DiffResult,
        requests: list[dict[str, Any]],
        intended_slides: dict[str, list[Any]],
        intended_change_keys: set[tuple[str, ChangeType]],
        create_copy_targets: set[tuple[str, str]],
        expected_image_sources: dict[tuple[str, str], str],
        force: bool,
        resume: bool,
        progress: Callable[[str, str], None] | None,
    ) -> dict[str, Any]:
        """Apply the generated deck diff as ordered, resumable slide batches."""
        metadata = read_json(folder_path / PRESENTATION_FILE, missing_ok=True)
        id_mapping = read_json(folder_path / ID_MAPPING_FILE, missing_ok=True)
        slide_id_mapping = self._build_slide_id_mapping(
            id_mapping, metadata.get("slideOrder")
        )
        base_raw = self._read_base_raw(folder_path)
        batches = partition_requests_by_slide(
            requests,
            diff_result,
            id_mapping,
            slide_id_mapping,
            base_raw or {},
            folder_path,
            generated_slide_ids=diff_result.generated_slide_ids,
        )
        total_slides = _progress_slide_total(intended_slides, batches)
        recorded = (
            load_progress_ledger(folder_path, presentation_id) if resume else {}
        )
        succeeded: dict[str, str] = {}
        if not resume:
            write_progress_ledger(folder_path, presentation_id, succeeded)
        skipping_prefix = resume
        response: dict[str, Any] = {"replies": []}
        transport = self._require_transport()

        warning: PushWarning | None = None
        if base_raw is None:
            warning = _missing_base_warning()

        for batch in batches:
            if (
                skipping_prefix
                and recorded.get(batch.slide_index) == batch.content_hash
            ):
                succeeded[batch.slide_index] = batch.content_hash
                write_progress_ledger(folder_path, presentation_id, succeeded)
                _report_progress(
                    progress,
                    "skipped",
                    batch,
                    total_slides,
                    "already pushed",
                )
                continue
            skipping_prefix = False
            _report_progress(progress, "start", batch, total_slides)

            try:
                await transport.refresh_if_expiring()
                batch_response = await execute_guarded_batch(
                    transport=transport,
                    presentation_id=presentation_id,
                    requests=batch.requests,
                    base_raw=base_raw,
                    id_mapping=id_mapping,
                    guard_conflicts=not force,
                    lock_revision=True,
                )
            except APIError as exc:
                write_progress_ledger(folder_path, presentation_id, succeeded)
                cover_ids = _cover_element_ids(
                    diff_result,
                    id_mapping,
                    batch.requests,
                    _failed_request_index(exc),
                )
                cause: Exception = (
                    CoverFitPushError(cover_ids, exc) if cover_ids else exc
                )
                raise PerSlidePushError(
                    batch.slide_index, total_slides, cause
                ) from cause
            except ConflictError as exc:
                write_progress_ledger(folder_path, presentation_id, succeeded)
                raise PerSlideConflictError(
                    batch.slide_index, total_slides, exc
                ) from exc
            except Exception as exc:
                write_progress_ledger(folder_path, presentation_id, succeeded)
                raise PerSlidePushError(
                    batch.slide_index, total_slides, exc
                ) from exc

            response["replies"].extend(batch_response.get("replies", []))
            if batch_response.get("warnings"):
                response.setdefault("warnings", []).extend(
                    batch_response["warnings"]
                )
            succeeded[batch.slide_index] = batch.content_hash
            write_progress_ledger(folder_path, presentation_id, succeeded)
            _report_progress(progress, "success", batch, total_slides)

        force_warning: PushWarning | None = None
        if force:
            force_warning = PushWarning(
                WarningSeverity.WARNING,
                "push --force --per-slide: conflict guard bypassed; per-slide "
                "revision locks remain enabled, but concurrent human edits "
                "already present on touched properties will be overwritten",
            )

        return await finalize_push(
            transport=transport,
            folder_path=folder_path,
            presentation_id=presentation_id,
            response=response,
            diff_warnings=diff_result.warnings,
            base_warning=warning,
            force_warning=force_warning,
            verify_persistence=lambda finalized_response: (
                self._append_persistence_warning(
                    folder_path,
                    intended_slides,
                    intended_change_keys,
                    create_copy_targets,
                    finalized_response,
                    author_changes=diff_result.changes,
                    expected_image_sources=expected_image_sources,
                )
            ),
            clear_progress=True,
        )

    def _append_persistence_warning(
        self,
        folder_path: Path,
        intended_slides: dict[str, list[Any]],
        intended_change_keys: set[tuple[str, ChangeType]],
        create_copy_targets: set[tuple[str, str]],
        response: dict[str, Any],
        *,
        author_changes: list[Any] | None = None,
        expected_image_sources: dict[tuple[str, str], str] | None = None,
    ) -> None:
        """Warn when pushed semantic changes differ from refreshed truth."""
        append_persistence_warning(
            folder_path,
            intended_slides,
            intended_change_keys,
            create_copy_targets,
            response,
            author_changes=author_changes,
            read_pristine=self._read_pristine,
            expected_image_sources=expected_image_sources,
        )

    def _read_base_raw(self, folder_path: Path) -> dict[str, Any] | None:
        """Read the pristine base raw API tree, if this folder has one.

        Prefers .pristine/base.json (always written by current pulls); falls
        back to .raw/presentation.json (older pulls with save_raw=True).
        Returns None when neither exists (folder pulled by old code).
        """
        return _read_base_raw(folder_path)

    def _read_current_slides(self, folder_path: Path) -> dict[str, list[Any]]:
        """Read current slide content files."""
        return _read_current_slides(folder_path)

    def _read_pristine(
        self,
        folder_path: Path,
    ) -> tuple[dict[str, list[Any]], dict[str, dict[str, Any]]]:
        """Read pristine slides and styles from zip."""
        return _read_pristine(folder_path)

    def _build_slide_id_mapping(
        self,
        id_mapping: dict[str, str],
        slide_order: list[str] | None = None,
    ) -> dict[str, str]:
        """Build mapping from slide index to Google slide ID.

        Slide clean IDs are like "s1", "s2", etc.
        Slide indices are like "01", "02", etc.
        """
        return _build_slide_id_mapping(id_mapping, slide_order)

def diff_folder(
    folder_path: str | Path,
    *,
    slide: int | None = None,
) -> list[dict[str, Any]]:
    """Convenience function to diff a presentation folder.

    Args:
        folder_path: Path to the presentation folder

    Returns:
        List of batchUpdate request objects
    """

    return diff_folder_with_result(folder_path, slide=slide)[1]


def diff_folder_with_result(
    folder_path: str | Path,
    *,
    slide: int | None = None,
) -> tuple[DiffResult, list[dict[str, Any]]]:
    """Return semantic changes and batchUpdate requests for a local folder."""
    client = SlidesClient()
    return client.diff_with_result(Path(folder_path), slide=slide)
