"""Shared guarded batch execution and resumable push helpers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from slidesmith.engine.conflicts import ConflictError, ensure_no_conflicts
from slidesmith.engine.diff_model import PushWarning, WarningSeverity
from slidesmith.engine.push_progress import (
    SlideBatch,
    clear_progress_ledger,
    partition_requests_by_slide,
)
from slidesmith.engine.transport import APIError, Transport
from slidesmith.engine.workspace_layout import (
    PRISTINE_BASE_FILE,
    PRISTINE_DIR,
    refresh_after_success,
)


def _missing_base_warning() -> PushWarning:
    return PushWarning(
        WarningSeverity.WARNING,
        "no pristine base snapshot found "
        f"({PRISTINE_DIR}/{PRISTINE_BASE_FILE}); this folder was "
        "pulled by an older slidesmith. Remote-change detection "
        "skipped for this push -- re-pull to re-enable the guard.",
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
    """Fetch, conflict-check, revision-lock, and execute one request batch."""
    required_revision: str | None = None
    if guard_conflicts or lock_revision:
        remote = await transport.get_presentation(presentation_id)
        if guard_conflicts and base_raw is not None:
            ensure_no_conflicts(base_raw, remote.data, requests, id_mapping)
        if lock_revision:
            required_revision = remote.revision_id

    try:
        return await transport.batch_update(
            presentation_id,
            requests,
            required_revision_id=required_revision,
        )
    except APIError as exc:
        if exc.status_code == 400 and "revision" in str(exc).lower():
            raise ConflictError(
                "push aborted: the deck changed mid-push (someone edited "
                "it between our conflict check and our write). "
                "Re-pull and retry."
            ) from exc
        raise


async def _finalize_push(
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
    refresh: Callable[..., Any],
) -> dict[str, Any]:
    """Merge warnings, refresh authoritative state, and verify persistence."""
    if diff_warnings:
        response.setdefault("warnings", []).extend(diff_warnings)
    for warning in (base_warning, force_warning):
        if warning is not None:
            response.setdefault("warnings", []).append(warning)

    refreshed = await refresh(
        transport, folder_path, presentation_id, response
    )
    if refreshed:
        verify_persistence(response)
        if clear_progress:
            clear_progress_ledger(folder_path)
    return response


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
    """Merge warnings, refresh authoritative state, and verify persistence."""
    return await _finalize_push(
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


class PerSlidePushError(RuntimeError):
    """A per-slide batch failed after earlier slide batches may have committed."""

    def __init__(self, slide_index: str, total_slides: int, cause: Exception) -> None:
        self.slide_index = slide_index
        self.total_slides = total_slides
        self.cause = cause
        super().__init__(
            f"slide {slide_index}/{total_slides:02d} failed: {cause}"
        )


class PerSlideConflictError(ConflictError):
    """A conflict tied to one resumable slide batch."""

    def __init__(
        self, slide_index: str, total_slides: int, cause: ConflictError
    ) -> None:
        self.slide_index = slide_index
        self.total_slides = total_slides
        self.cause = cause
        super().__init__(
            f"slide {slide_index}/{total_slides:02d} failed: {cause}",
            conflicts=cause.conflicts,
        )


def _progress_slide_total(
    intended_slides: dict[str, list[Any]], batches: list[SlideBatch]
) -> int:
    numeric_indices = [
        int(slide_index)
        for slide_index in intended_slides
        if slide_index.isdigit()
    ]
    numeric_indices.extend(
        int(batch.slide_index)
        for batch in batches
        if batch.slide_index.isdigit()
    )
    return max(numeric_indices, default=max(len(intended_slides), len(batches)))


def _report_progress(
    progress: Callable[[str, str], None] | None,
    event: str,
    batch: SlideBatch,
    total_slides: int,
    detail: str | None = None,
) -> None:
    if progress is None:
        return
    prefix = f"slide {batch.slide_index}/{total_slides:02d}"
    if event == "start":
        message = f"{prefix} …"
    elif event == "success":
        message = f"{prefix} ✓ ({len(batch.requests)} changes)"
    else:
        message = f"{prefix} ✓ ({detail})"
    progress(event, message)


__all__ = [
    "PerSlideConflictError",
    "PerSlidePushError",
    "SlideBatch",
    "_missing_base_warning",
    "_progress_slide_total",
    "_report_progress",
    "execute_guarded_batch",
    "finalize_push",
    "partition_requests_by_slide",
]
