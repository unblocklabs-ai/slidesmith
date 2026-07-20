"""Shared JSON-file loading with explicit missing-file semantics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_json(path: Path, *, missing_ok: bool) -> dict[str, Any]:
    """Read one JSON object, optionally returning an empty object if absent."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        if missing_ok:
            return {}
        raise ValueError(f"Missing Slidesmith workspace file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return data
