"""CLI workspace-staleness warnings."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from slidesmith.cli import _warn_if_stale


def _workspace(tmp_path: Path, pulled_at: datetime) -> Path:
    folder = tmp_path / "deck"
    folder.mkdir()
    (folder / "presentation.json").write_text(
        json.dumps(
            {"pulledAt": pulled_at.isoformat().replace("+00:00", "Z")}
        ),
        encoding="utf-8",
    )
    return folder


def test_staleness_warning_fires_after_24_hours(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime(2026, 7, 19, 12, tzinfo=timezone.utc)
    folder = _workspace(tmp_path, now - timedelta(hours=24, seconds=1))

    _warn_if_stale(folder, now=now)

    assert capsys.readouterr().err == (
        "workspace pulled 2026-07-18T11:59:59Z; deck may have changed — "
        "re-pull recommended\n"
    )


def test_staleness_warning_does_not_fire_at_or_before_24_hours(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime(2026, 7, 19, 12, tzinfo=timezone.utc)
    folder = _workspace(tmp_path, now - timedelta(hours=24))

    _warn_if_stale(folder, now=now)

    assert capsys.readouterr().err == ""
