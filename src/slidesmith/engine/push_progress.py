"""Per-slide request partitioning and resumable-push ledger helpers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from slidesmith.engine.content_diff import DiffResult


PUSH_PROGRESS_FILE = ".push-progress.json"
_LEDGER_VERSION = 1


@dataclass(frozen=True)
class SlideBatch:
    """One slide's requests and the local-content identity they represent."""

    slide_index: str
    requests: list[dict[str, Any]]
    content_hash: str


def partition_requests_by_slide(
    requests: list[dict[str, Any]],
    diff_result: DiffResult,
    id_mapping: dict[str, str],
    slide_id_mapping: dict[str, str],
    base_raw: dict[str, Any],
    folder_path: Path,
) -> list[SlideBatch]:
    """Partition an already-generated request stream by its target slide.

    Request generation stays deck-wide so object-ID allocation and ordering
    remain identical to the default atomic push. This pass only assigns each
    request to a slide and preserves its relative order inside that slide.
    """
    slide_by_page_id = {
        google_id: slide_index
        for slide_index, google_id in slide_id_mapping.items()
    }
    slide_by_object_id: dict[str, str] = {}

    for position, slide in enumerate(base_raw.get("slides", []) or [], 1):
        page_id = slide.get("objectId")
        slide_index = slide_by_page_id.get(page_id, f"{position:02d}")
        if isinstance(page_id, str) and page_id:
            slide_by_page_id.setdefault(page_id, slide_index)
            slide_by_object_id[page_id] = slide_index
        _index_page_elements(
            slide.get("pageElements", []) or [], slide_index, slide_by_object_id
        )

    for change in diff_result.changes:
        if change.slide_index:
            target_google_id = id_mapping.get(change.target_id)
            if target_google_id:
                slide_by_object_id.setdefault(target_google_id, change.slide_index)
        if change.source_slide_index and change.source_id:
            source_google_id = id_mapping.get(change.source_id)
            if source_google_id:
                slide_by_object_id.setdefault(
                    source_google_id, change.source_slide_index
                )

    missing_slide_indices = {
        change.slide_index
        for change in diff_result.changes
        if change.slide_index and change.slide_index not in slide_id_mapping
    }
    grouped: dict[str, list[dict[str, Any]]] = {}

    for request in requests:
        if len(request) != 1:
            raise ValueError(f"Cannot partition malformed request: {request!r}")
        operation, body = next(iter(request.items()))
        if not isinstance(body, dict):
            raise ValueError(f"Cannot partition malformed {operation} request")

        if operation == "createSlide":
            page_id = body.get("objectId")
            slide_index = _new_slide_index_from_object_id(page_id)
            if slide_index is None or slide_index not in missing_slide_indices:
                raise ValueError(
                    "Cannot map createSlide request to a local slide"
                )
            missing_slide_indices.remove(slide_index)
            if isinstance(page_id, str) and page_id:
                slide_by_page_id[page_id] = slide_index
                slide_by_object_id[page_id] = slide_index
        else:
            slide_index = _request_slide_index(
                body, slide_by_page_id, slide_by_object_id
            )
            if slide_index is None:
                object_id = body.get("objectId") or body.get("groupObjectId")
                detail = f" for object {object_id!r}" if object_id else ""
                raise ValueError(
                    f"Cannot determine target slide for {operation}{detail}"
                )

        grouped.setdefault(slide_index, []).append(request)
        _record_created_objects(body, slide_index, slide_by_object_id)

    return [
        SlideBatch(
            slide_index=slide_index,
            requests=grouped[slide_index],
            content_hash=slide_content_hash(
                folder_path, slide_index, grouped[slide_index]
            ),
        )
        for slide_index in sorted(grouped, key=_slide_sort_key)
    ]


def _new_slide_index_from_object_id(object_id: Any) -> str | None:
    if not isinstance(object_id, str):
        return None
    match = re.fullmatch(r"new_slide_(.+)_([0-9]+)", object_id)
    return match.group(1) if match else None


def _index_page_elements(
    elements: list[dict[str, Any]],
    slide_index: str,
    slide_by_object_id: dict[str, str],
) -> None:
    for element in elements:
        object_id = element.get("objectId")
        if isinstance(object_id, str) and object_id:
            slide_by_object_id[object_id] = slide_index
        _index_page_elements(
            element.get("elementGroup", {}).get("children", []) or [],
            slide_index,
            slide_by_object_id,
        )


def _request_slide_index(
    body: dict[str, Any],
    slide_by_page_id: dict[str, str],
    slide_by_object_id: dict[str, str],
) -> str | None:
    element_properties = body.get("elementProperties")
    if isinstance(element_properties, dict):
        page_id = element_properties.get("pageObjectId")
        if isinstance(page_id, str) and page_id in slide_by_page_id:
            return slide_by_page_id[page_id]

    for key in ("objectId", "groupObjectId"):
        object_id = body.get(key)
        if isinstance(object_id, str) and object_id in slide_by_object_id:
            return slide_by_object_id[object_id]

    for key in ("childrenObjectIds", "slideObjectIds"):
        object_ids = body.get(key)
        if isinstance(object_ids, list):
            for object_id in object_ids:
                if isinstance(object_id, str) and object_id in slide_by_object_id:
                    return slide_by_object_id[object_id]

    page_id = body.get("pageObjectId")
    if isinstance(page_id, str):
        return slide_by_page_id.get(page_id)
    return None


def _record_created_objects(
    body: dict[str, Any],
    slide_index: str,
    slide_by_object_id: dict[str, str],
) -> None:
    object_id = body.get("objectId")
    if isinstance(object_id, str) and object_id:
        slide_by_object_id.setdefault(object_id, slide_index)

    group_id = body.get("groupObjectId")
    if isinstance(group_id, str) and group_id:
        slide_by_object_id.setdefault(group_id, slide_index)

    object_ids = body.get("objectIds")
    if isinstance(object_ids, dict):
        for new_object_id in object_ids.values():
            if isinstance(new_object_id, str) and new_object_id:
                slide_by_object_id.setdefault(new_object_id, slide_index)


def slide_content_hash(
    folder_path: Path,
    slide_index: str,
    requests: list[dict[str, Any]],
) -> str:
    """Hash local source content plus the exact generated slide request batch."""
    digest = hashlib.sha256(b"slidesmith-per-slide-v1\0")
    for relative_path in (
        Path("components.sml"),
        Path("slides") / slide_index / "content.sml",
    ):
        digest.update(str(relative_path).encode("utf-8"))
        digest.update(b"\0")
        path = folder_path / relative_path
        digest.update(path.read_bytes() if path.exists() else b"<missing>")
        digest.update(b"\0")
    digest.update(
        json.dumps(
            requests,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return digest.hexdigest()


def load_progress_ledger(folder_path: Path, presentation_id: str) -> dict[str, str]:
    """Return recorded successful slide hashes, validating ledger ownership."""
    path = folder_path / PUSH_PROGRESS_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Cannot resume: {PUSH_PROGRESS_FILE} is unreadable; "
            "run without --resume to start over"
        ) from exc
    if (
        not isinstance(data, dict)
        or data.get("version") != _LEDGER_VERSION
        or data.get("presentationId") != presentation_id
        or not isinstance(data.get("succeeded"), list)
    ):
        raise ValueError(
            f"Cannot resume: {PUSH_PROGRESS_FILE} does not match this presentation; "
            "run without --resume to start over"
        )

    succeeded: dict[str, str] = {}
    for entry in data["succeeded"]:
        if not isinstance(entry, dict):
            continue
        slide_index = entry.get("slideIndex")
        content_hash = entry.get("contentHash")
        if isinstance(slide_index, str) and isinstance(content_hash, str):
            succeeded[slide_index] = content_hash
    return succeeded


def write_progress_ledger(
    folder_path: Path,
    presentation_id: str,
    succeeded: dict[str, str],
) -> None:
    """Atomically persist the successful prefix of a per-slide push."""
    path = folder_path / PUSH_PROGRESS_FILE
    temporary = folder_path / f"{PUSH_PROGRESS_FILE}.tmp"
    data = {
        "version": _LEDGER_VERSION,
        "presentationId": presentation_id,
        "succeeded": [
            {"slideIndex": slide_index, "contentHash": succeeded[slide_index]}
            for slide_index in sorted(succeeded, key=_slide_sort_key)
        ],
    }
    temporary.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def clear_progress_ledger(folder_path: Path) -> None:
    """Remove per-slide progress after a clean push/refresh completion."""
    path = folder_path / PUSH_PROGRESS_FILE
    if path.exists():
        path.unlink()


def _slide_sort_key(slide_index: str) -> tuple[int, int | str]:
    if slide_index.isdigit():
        return (0, int(slide_index))
    return (1, slide_index)
