"""Offline workspace materialization: presentation JSON -> SML folder, no network.

Produces the same folder layout as SlidesClient.pull() so diff/push work on it,
and so contract tests can run against golden fixtures without API access.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from extraslide.client import PRISTINE_DIR, PRISTINE_ZIP, RAW_DIR
from extraslide.slide_processor import process_presentation, write_new_format


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

    pristine_dir = presentation_dir / PRISTINE_DIR
    pristine_dir.mkdir(parents=True, exist_ok=True)
    zip_path = pristine_dir / PRISTINE_ZIP
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in written:
            if any(d in fp.parts for d in (RAW_DIR, PRISTINE_DIR)):
                continue
            zf.write(fp, fp.relative_to(presentation_dir))

    return presentation_dir
