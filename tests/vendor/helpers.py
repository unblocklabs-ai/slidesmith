"""Test-only compatibility helpers for the untouched donor vendor suite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from extraslide.transport import NotFoundError, PresentationData, Transport


class LocalFileTransport(Transport):
    """Golden-fixture transport retained outside the shipped package."""

    def __init__(self, golden_dir: Path) -> None:
        self._golden_dir = golden_dir
        self._batch_updates: list[dict[str, Any]] = []

    async def get_presentation(self, presentation_id: str) -> PresentationData:
        path = self._golden_dir / presentation_id / "presentation.json"
        if not path.exists():
            raise NotFoundError(f"Golden file not found: {path}")
        response = json.loads(path.read_text(encoding="utf-8"))
        return PresentationData(
            presentation_id=response.get("presentationId", presentation_id),
            data=response,
        )

    async def batch_update(
        self,
        presentation_id: str,
        requests: list[dict[str, Any]],
        required_revision_id: str | None = None,
    ) -> dict[str, Any]:
        self._batch_updates.append(
            {
                "presentation_id": presentation_id,
                "requests": requests,
                "required_revision_id": required_revision_id,
            }
        )
        return {"replies": [{}] * len(requests)}

    async def close(self) -> None:
        pass

    @property
    def batch_updates(self) -> list[dict[str, Any]]:
        return self._batch_updates
