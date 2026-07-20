"""Offline workspace materialization: presentation JSON -> SML folder, no network.

Produces the same folder layout as SlidesClient.pull() so diff/push work on it,
and so contract tests can run against golden fixtures without API access.
"""

from __future__ import annotations

import json
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from extraslide.slide_processor import process_presentation, write_new_format
from extraslide.transport import Transport

PRESENTATION_FILE = "presentation.json"
ID_MAPPING_FILE = "id_mapping.json"
STYLES_FILE = "styles.json"
SLIDES_DIR = "slides"
RAW_DIR = ".raw"
PRISTINE_DIR = ".pristine"
PRISTINE_ZIP = "presentation.zip"
PRISTINE_BASE_FILE = "base.json"


def pull_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def create_pristine_zip(
    presentation_dir: Path, written_files: list[Path]
) -> Path:
    """Archive generated workspace files for local diff/push comparison."""
    pristine_dir = presentation_dir / PRISTINE_DIR
    pristine_dir.mkdir(parents=True, exist_ok=True)
    zip_path = pristine_dir / PRISTINE_ZIP
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in written_files:
            if any(part in file_path.parts for part in (RAW_DIR, PRISTINE_DIR)):
                continue
            archive.write(file_path, file_path.relative_to(presentation_dir))
    return zip_path


def prune_stale_slide_folders(
    presentation_dir: Path, valid_indices: set[str]
) -> None:
    """Remove stale generated slides, preserving edited ones as orphans."""
    slides_dir = presentation_dir / SLIDES_DIR
    if not slides_dir.exists():
        return
    pristine_content: dict[str, bytes] = {}
    pristine_zip = presentation_dir / PRISTINE_DIR / PRISTINE_ZIP
    if pristine_zip.exists():
        with zipfile.ZipFile(pristine_zip, "r") as archive:
            for name in archive.namelist():
                if name.startswith(f"{SLIDES_DIR}/") and name.endswith(
                    "/content.sml"
                ):
                    pristine_content[name.split("/")[1]] = archive.read(name)

    for slide_folder in sorted(slides_dir.iterdir()):
        if not slide_folder.is_dir() or slide_folder.name in valid_indices:
            continue
        content_file = slide_folder / "content.sml"
        current = content_file.read_bytes() if content_file.exists() else None
        folder_entries = list(slide_folder.iterdir())
        generated_only = folder_entries == [content_file]
        if (
            generated_only
            and current is not None
            and current == pristine_content.get(slide_folder.name)
        ):
            shutil.rmtree(slide_folder)
            continue
        orphan_root = presentation_dir / ".orphaned-slides"
        orphan_root.mkdir(parents=True, exist_ok=True)
        destination = orphan_root / slide_folder.name
        suffix = 2
        while destination.exists():
            destination = orphan_root / f"{slide_folder.name}-{suffix}"
            suffix += 1
        shutil.move(str(slide_folder), destination)


async def refresh_after_success(
    transport: Transport,
    folder_path: Path,
    presentation_id: str,
    response: dict[str, Any],
) -> None:
    """Refresh after a committed write, preserving a clear stale state on error."""
    try:
        await refresh_after_push(transport, folder_path, presentation_id)
    except Exception as exc:
        warning = (
            "push applied; workspace stale; re-pull required "
            f"(post-push refresh failed: {exc})"
        )
        print(f"warning: {warning}", file=sys.stderr)
        response.setdefault("warnings", []).append(warning)


async def refresh_after_push(
    transport: Transport, folder_path: Path, presentation_id: str
) -> None:
    """Replace the pristine base with the authoritative post-push deck."""
    refreshed = await transport.get_presentation(presentation_id)
    result = process_presentation(refreshed.data)
    if refreshed.revision_id:
        result["presentation_info"]["revisionId"] = refreshed.revision_id
    result["presentation_info"]["pulledAt"] = pull_timestamp()

    with TemporaryDirectory(prefix="slidesmith-push-refresh-") as temp_dir:
        temp_root = Path(temp_dir)
        staging_dir = temp_root / "generated"
        backup_dir = temp_root / "backup"
        staged_files = write_new_format(result, staging_dir)
        refreshed_files: list[Path] = []
        tracked_paths = (
            Path(PRESENTATION_FILE),
            Path(ID_MAPPING_FILE),
            Path(STYLES_FILE),
            Path(SLIDES_DIR),
            Path(".orphaned-slides"),
            Path(PRISTINE_DIR) / PRISTINE_ZIP,
            Path(PRISTINE_DIR) / PRISTINE_BASE_FILE,
        )
        for relative_path in tracked_paths:
            source = folder_path / relative_path
            backup = backup_dir / relative_path
            if source.is_dir():
                shutil.copytree(source, backup)
            elif source.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, backup)

        try:
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

            prune_stale_slide_folders(
                folder_path,
                {slide["slide_index"] for slide in result["slides"]},
            )
            create_pristine_zip(folder_path, refreshed_files)
            base_path = folder_path / PRISTINE_DIR / PRISTINE_BASE_FILE
            base_path.write_text(
                json.dumps(refreshed.data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            for relative_path in tracked_paths:
                destination = folder_path / relative_path
                if destination.is_dir():
                    shutil.rmtree(destination)
                elif destination.exists():
                    destination.unlink()
                backup = backup_dir / relative_path
                if backup.is_dir():
                    shutil.copytree(backup, destination)
                elif backup.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, destination)
            raise


def materialize(
    presentation_data: dict[str, Any],
    output_path: str | Path,
    *,
    save_raw: bool = False,
) -> Path:
    """Write a presentation's raw API JSON to the SML folder format.

    Returns the presentation directory (output_path/<presentationId>).
    """
    presentation_id = presentation_data["presentationId"]
    presentation_dir = Path(output_path) / presentation_id
    presentation_dir.mkdir(parents=True, exist_ok=True)

    result = process_presentation(presentation_data)
    written = write_new_format(result, presentation_dir)

    if save_raw:
        raw_dir = presentation_dir / RAW_DIR
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "presentation.json").write_text(
            json.dumps(presentation_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    create_pristine_zip(presentation_dir, written)

    return presentation_dir
