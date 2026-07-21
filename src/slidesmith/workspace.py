"""Offline workspace materialization and post-push refresh orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
    materialize_workspace,
    prune_stale_slide_folders,
    pull_timestamp,
)

__all__ = [
    "ID_MAPPING_FILE", "PRESENTATION_FILE", "PRISTINE_BASE_FILE", "PRISTINE_DIR",
    "PRISTINE_ZIP", "RAW_DIR", "SLIDES_DIR", "STYLES_FILE",
    "create_pristine_zip", "prune_stale_slide_folders", "pull_timestamp", "materialize",
]


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

    revision_id = presentation_data.get("revisionId")
    materialize_workspace(
        presentation_data,
        presentation_dir,
        revision_id=revision_id if isinstance(revision_id, str) else None,
        save_raw=save_raw,
        record_qa_baseline=False,
    )

    return presentation_dir
