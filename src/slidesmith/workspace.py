"""Offline workspace materialization and post-push refresh orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from slidesmith.engine.slide_processor import process_presentation, write_new_format
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
