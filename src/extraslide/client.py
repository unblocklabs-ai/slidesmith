"""SlidesClient - Main API for extraslide.

Provides the `pull`, `diff`, and `push` methods for the presentation workflow:
- id_mapping.json: clean_id -> google_object_id
- styles.json: clean_id -> styles (relative positions for children)
- slides/NN/content.sml: minimal XML with IDs, positions, text, pattern hints
"""

from __future__ import annotations

import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from extraslide.content_diff import diff_presentation
from extraslide.content_parser import parse_slide_content
from extraslide.content_requests import generate_batch_requests
from extraslide.slide_processor import process_presentation, write_new_format
from extraslide.transport import APIError, Transport

# File and directory names
PRESENTATION_FILE = "presentation.json"
ID_MAPPING_FILE = "id_mapping.json"
STYLES_FILE = "styles.json"
SLIDES_DIR = "slides"
RAW_DIR = ".raw"
PRISTINE_DIR = ".pristine"
PRISTINE_ZIP = "presentation.zip"
PRISTINE_BASE_FILE = "base.json"


def _pull_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ConflictError(Exception):
    """A push would collide with edits made in Google Slides since the pull.

    Attributes:
        conflicts: List of (clean_id, description) pairs, one per element this
            push would touch that also changed (or was deleted) remotely.
            Empty when the conflict was detected by the API's revision guard
            rather than the pre-push comparison.
    """

    def __init__(
        self, message: str, conflicts: list[tuple[str, str]] | None = None
    ) -> None:
        super().__init__(message)
        self.conflicts: list[tuple[str, str]] = conflicts or []


def _index_presentation(
    data: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Index a raw presentation JSON tree by objectId.

    Returns:
        (elements, page_ids) where elements maps every page element's
        objectId to its raw JSON subtree (recursing into groups) across
        slides, layouts, and masters, and page_ids is the set of page
        objectIds (slides/layouts/masters).
    """
    elements: dict[str, dict[str, Any]] = {}
    page_ids: set[str] = set()

    def walk(element: dict[str, Any]) -> None:
        object_id = element.get("objectId")
        if object_id:
            elements[object_id] = element
        for child in element.get("elementGroup", {}).get("children", []):
            walk(child)

    for page_kind in ("slides", "layouts", "masters"):
        for page in data.get(page_kind, []) or []:
            page_id = page.get("objectId")
            if page_id:
                page_ids.add(page_id)
            for element in page.get("pageElements", []) or []:
                walk(element)

    return elements, page_ids


def _collect_request_object_ids(
    requests: list[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    """Collect the Google objectIds a batch of requests will touch.

    Returns:
        (object_ids, page_ids): element-level ids referenced by the requests,
        and page ids referenced as creation targets (elementProperties.
        pageObjectId). Ids of objects created by the same batch are included
        too; callers filter to ids that exist in the pristine base.
    """
    object_ids: set[str] = set()
    page_ids: set[str] = set()

    for request in requests:
        for body in request.values():
            if not isinstance(body, dict):
                continue
            for key in ("objectId", "groupObjectId"):
                if body.get(key):
                    object_ids.add(body[key])
            for child_id in body.get("childrenObjectIds", []) or []:
                object_ids.add(child_id)
            element_properties = body.get("elementProperties")
            if isinstance(element_properties, dict) and element_properties.get(
                "pageObjectId"
            ):
                page_ids.add(element_properties["pageObjectId"])

    return object_ids, page_ids


def _classify_element_change(
    base_element: dict[str, Any], remote_element: dict[str, Any]
) -> str | None:
    """Describe how an element changed remotely, or None if it did not.

    Compares the raw JSON subtrees and buckets differences into geometry
    (transform/size), text (shape.text), and properties (everything else).
    """
    if base_element == remote_element:
        return None

    kinds: list[str] = []
    if base_element.get("transform") != remote_element.get(
        "transform"
    ) or base_element.get("size") != remote_element.get("size"):
        kinds.append("geometry")

    if base_element.get("shape", {}).get("text") != remote_element.get(
        "shape", {}
    ).get("text"):
        kinds.append("text")

    def strip(element: dict[str, Any]) -> dict[str, Any]:
        stripped = {
            k: v for k, v in element.items() if k not in ("transform", "size")
        }
        shape = stripped.get("shape")
        if isinstance(shape, dict):
            stripped["shape"] = {k: v for k, v in shape.items() if k != "text"}
        return stripped

    if strip(base_element) != strip(remote_element):
        kinds.append("properties")

    return "/".join(kinds) if kinds else "properties"


class SlidesClient:
    """Client for transforming Google Slides to/from SML format.

    This client uses a folder-based workflow:
    1. pull() - Fetch presentation and save as SML files
    2. diff() - Compare current content against pristine copy
    3. push() - Apply changes to Google Slides

    Example:
        >>> from extraslide.transport import GoogleSlidesTransport
        >>> transport = GoogleSlidesTransport(access_token="ya29...")
        >>> client = SlidesClient(transport)
        >>> await client.pull("1abc...", "./output")
        >>> # Edit slides/01/content.sml, slides/02/content.sml, etc.
        >>> changes = client.diff(Path("./output/1abc..."))
        >>> await client.push(Path("./output/1abc..."))
    """

    def __init__(self, transport: Transport) -> None:
        """Initialize the client.

        Args:
            transport: Transport implementation for fetching/updating presentations
        """
        self._transport = transport

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
        presentation_data = await self._transport.get_presentation(presentation_id)

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
        result["presentation_info"]["pulledAt"] = _pull_timestamp()

        # Write the new format files
        written_files.extend(write_new_format(result, presentation_dir))

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
        pristine_path = self._create_pristine_copy(presentation_dir, written_files)
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

        return written_files

    def diff(self, folder_path: Path) -> list[dict[str, Any]]:
        """Compare current content against pristine copy and generate update requests.

        This is a local-only operation that does not call any APIs.

        Args:
            folder_path: Path to the presentation folder

        Returns:
            List of Google Slides API batchUpdate request objects
        """
        folder_path = Path(folder_path)

        # Read current state
        current_slides = self._read_current_slides(folder_path)
        id_mapping = self._read_json(folder_path / ID_MAPPING_FILE)

        # Read pristine state
        pristine_slides, pristine_styles = self._read_pristine(folder_path)

        # Generate diff
        diff_result = diff_presentation(
            pristine_slides,
            current_slides,
            pristine_styles,
            id_mapping,
        )

        # Build slide ID mapping (slide_index -> google_slide_id)
        slide_id_mapping = self._build_slide_id_mapping(id_mapping)

        # Generate API requests
        return generate_batch_requests(diff_result, id_mapping, slide_id_mapping)

    async def push(self, folder_path: Path, *, force: bool = False) -> dict[str, Any]:
        """Apply content changes to the presentation, guarded against
        concurrent human edits (contract C5).

        Flow:
        1. Re-fetch the remote presentation (capturing its revisionId).
        2. Determine the Google objectIds the pending diff will touch.
        3. Compare remote vs the pristine base for those objects only; if any
           touched object changed or was deleted remotely, raise ConflictError
           without writing anything.
        4. Otherwise batchUpdate with writeControl.requiredRevisionId set to
           the just-fetched revision; a revision-mismatch 400 (human edited
           between fetch and write) also surfaces as ConflictError.

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

        # Get presentation ID from metadata
        metadata = self._read_json(folder_path / PRESENTATION_FILE)
        presentation_id = metadata.get("presentationId")
        if not presentation_id:
            raise ValueError("Presentation ID not found in presentation.json")

        # Generate diff
        requests = self.diff(folder_path)

        if not requests:
            return {"replies": [], "message": "No changes detected"}

        if force:
            print(
                "warning: push --force: conflict guard and revision lock "
                "bypassed; concurrent human edits to the touched properties "
                "will be overwritten",
                file=sys.stderr,
            )
            response = await self._transport.batch_update(presentation_id, requests)
            await self._refresh_after_push(folder_path, presentation_id)
            return response

        # (a) Re-fetch the remote presentation and capture its revision.
        remote = await self._transport.get_presentation(presentation_id)
        required_revision = remote.revision_id

        # (b)+(c) Compare remote vs pristine base for the touched objects.
        base_raw = self._read_base_raw(folder_path)
        if base_raw is None:
            print(
                "warning: no pristine base snapshot found "
                f"({PRISTINE_DIR}/{PRISTINE_BASE_FILE}); this folder was "
                "pulled by an older slidesmith. Remote-change detection "
                "skipped for this push -- re-pull to re-enable the guard.",
                file=sys.stderr,
            )
        else:
            conflicts = self._detect_conflicts(
                base_raw,
                remote.data,
                requests,
                self._read_json(folder_path / ID_MAPPING_FILE),
            )
            if conflicts:
                lines = [
                    f"push aborted: {len(conflicts)} element(s) this push "
                    "would modify changed in Google Slides since the pull:"
                ]
                lines += [f"  - {clean_id}: {kind}" for clean_id, kind in conflicts]
                lines.append(
                    "Re-pull the deck, re-apply your edits, then push again "
                    "(or push --force to overwrite the remote edits)."
                )
                raise ConflictError("\n".join(lines), conflicts=conflicts)

        # (d) Guarded write: fail if the deck is revised between (a) and now.
        try:
            response = await self._transport.batch_update(
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

        await self._refresh_after_push(folder_path, presentation_id)
        return response

    async def _refresh_after_push(
        self, folder_path: Path, presentation_id: str
    ) -> None:
        """Replace the pristine base with the authoritative post-push deck.

        Re-fetch instead of treating the local pre-push SML as authoritative:
        Google Slides may normalize values while applying a batch. Generate the
        same files as pull() in a staging directory, then promote them into the
        workspace. An SML file that already byte-matches the generated version
        is deliberately left untouched; a mismatch is replaced by the remote-
        derived version so the working tree and pristine snapshot agree.
        """
        refreshed = await self._transport.get_presentation(presentation_id)
        result = process_presentation(refreshed.data)
        if refreshed.revision_id:
            result["presentation_info"]["revisionId"] = refreshed.revision_id
        result["presentation_info"]["pulledAt"] = _pull_timestamp()

        with TemporaryDirectory(prefix="slidesmith-push-refresh-") as temp_dir:
            staging_dir = Path(temp_dir)
            staged_files = write_new_format(result, staging_dir)
            refreshed_files: list[Path] = []

            for staged_path in staged_files:
                relative_path = staged_path.relative_to(staging_dir)
                destination = folder_path / relative_path
                generated = staged_path.read_bytes()
                is_sml = (
                    relative_path.parts[0] == SLIDES_DIR
                    and relative_path.name == "content.sml"
                )

                destination.parent.mkdir(parents=True, exist_ok=True)
                if not (
                    is_sml
                    and destination.exists()
                    and destination.read_bytes() == generated
                ):
                    destination.write_bytes(generated)
                refreshed_files.append(destination)

            self._create_pristine_copy(folder_path, refreshed_files)

        base_path = folder_path / PRISTINE_DIR / PRISTINE_BASE_FILE
        base_path.write_text(
            json.dumps(refreshed.data, indent=2, ensure_ascii=False),
            encoding="utf-8",
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
                data: dict[str, Any] = json.loads(
                    candidate.read_text(encoding="utf-8")
                )
                return data
        return None

    def _detect_conflicts(
        self,
        base_raw: dict[str, Any],
        remote_raw: dict[str, Any],
        requests: list[dict[str, Any]],
        id_mapping: dict[str, str],
    ) -> list[tuple[str, str]]:
        """Find touched objects that changed remotely since the pristine base.

        Only objects the requests reference AND that exist in the base are
        checked (ids created by this very batch don't exist remotely yet).
        Returns (clean_id, description) pairs; empty list means safe to push.
        """
        base_elements, base_page_ids = _index_presentation(base_raw)
        remote_elements, remote_page_ids = _index_presentation(remote_raw)
        reverse_mapping = {google: clean for clean, google in id_mapping.items()}

        object_ids, page_ids = _collect_request_object_ids(requests)
        conflicts: list[tuple[str, str]] = []

        for object_id in sorted(object_ids):
            base_element = base_elements.get(object_id)
            if base_element is None:
                continue  # created by this push; nothing to conflict with
            clean_id = reverse_mapping.get(object_id, object_id)
            remote_element = remote_elements.get(object_id)
            if remote_element is None:
                conflicts.append((clean_id, "deleted remotely"))
                continue
            kind = _classify_element_change(base_element, remote_element)
            if kind:
                conflicts.append((clean_id, f"{kind} changed remotely"))

        # Pages referenced as creation targets only need to still exist;
        # other edits on those pages must not block the push.
        for page_id in sorted(page_ids):
            if page_id in base_page_ids and page_id not in remote_page_ids:
                clean_id = reverse_mapping.get(page_id, page_id)
                conflicts.append((clean_id, "target slide deleted remotely"))

        return conflicts

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

    def _read_json(self, path: Path) -> dict[str, Any]:
        """Read a JSON file."""
        if not path.exists():
            return {}
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def _build_slide_id_mapping(self, id_mapping: dict[str, str]) -> dict[str, str]:
        """Build mapping from slide index to Google slide ID.

        Slide clean IDs are like "s1", "s2", etc.
        Slide indices are like "01", "02", etc.
        """
        result: dict[str, str] = {}

        for clean_id, google_id in id_mapping.items():
            if clean_id.startswith("s"):
                try:
                    # Extract number from "s1", "s2", etc.
                    num = int(clean_id[1:])
                    # Convert to zero-padded index
                    slide_index = f"{num:02d}"
                    result[slide_index] = google_id
                except ValueError:
                    continue

        return result

    def _create_pristine_copy(
        self,
        presentation_dir: Path,
        written_files: list[Path],
    ) -> Path:
        """Create a pristine copy of the pulled files for diff/push workflow."""
        pristine_dir = presentation_dir / PRISTINE_DIR
        pristine_dir.mkdir(parents=True, exist_ok=True)

        zip_path = pristine_dir / PRISTINE_ZIP

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in written_files:
                # Skip raw and pristine directories
                if any(d in file_path.parts for d in [RAW_DIR, PRISTINE_DIR]):
                    continue

                # Store with path relative to presentation directory
                arcname = file_path.relative_to(presentation_dir)
                zf.write(file_path, arcname)

        return zip_path


async def pull_presentation(
    transport: Transport,
    presentation_id: str,
    output_path: str | Path,
    *,
    save_raw: bool = True,
) -> list[Path]:
    """Convenience function to pull a presentation.

    Args:
        transport: Transport implementation
        presentation_id: The ID of the presentation
        output_path: Directory to write files to
        save_raw: If True, saves raw API response

    Returns:
        List of paths to written files
    """
    client = SlidesClient(transport)
    return await client.pull(presentation_id, output_path, save_raw=save_raw)


def diff_folder(folder_path: str | Path) -> list[dict[str, Any]]:
    """Convenience function to diff a presentation folder.

    Note: This creates a client with a dummy transport since diff doesn't need it.

    Args:
        folder_path: Path to the presentation folder

    Returns:
        List of batchUpdate request objects
    """

    # Create a minimal transport for diff (not used)
    class DummyTransport(Transport):
        async def get_presentation(self, _: str) -> Any:
            raise NotImplementedError("Diff doesn't need transport")

        async def batch_update(
            self, _id: str, _reqs: list[Any], _required_revision_id: str | None = None
        ) -> Any:
            raise NotImplementedError("Diff doesn't need transport")

        async def close(self) -> None:
            pass

    client = SlidesClient(DummyTransport())
    return client.diff(Path(folder_path))
