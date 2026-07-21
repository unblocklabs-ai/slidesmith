"""CLI workspace-staleness warnings."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from slidesmith.engine.conflicts import ConflictError
from slidesmith import cli
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
        "warning: workspace pulled 2026-07-18T11:59:59Z; deck may have changed — "
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


def test_staleness_warning_silently_ignores_corrupt_timestamp(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "deck"
    folder.mkdir()
    (folder / "presentation.json").write_text(
        json.dumps({"pulledAt": "not-a-timestamp"}), encoding="utf-8"
    )

    # Intentional: bad advisory metadata must never block a real CLI command.
    _warn_if_stale(folder)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_push_conflict_exits_two_and_lists_conflicting_elements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "deck"
    folder.mkdir()
    message = (
        "push aborted: conflicting elements:\n"
        "  - title: text changed remotely\n"
        "  - chart: geometry changed remotely"
    )

    def raise_conflict(coroutine: Any) -> None:
        coroutine.close()
        raise ConflictError(
            message,
            conflicts=[
                ("title", "text changed remotely"),
                ("chart", "geometry changed remotely"),
            ],
        )

    monkeypatch.setattr(cli, "_warn_if_stale", lambda _folder: None)
    monkeypatch.setattr(cli, "_token", lambda *_args: "token")
    monkeypatch.setattr(cli.asyncio, "run", raise_conflict)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["push", str(folder)])

    assert excinfo.value.code == 2
    assert capsys.readouterr().err == message + "\n"


def test_replace_image_conflict_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "deck"
    folder.mkdir()
    message = "replace-image aborted: the deck changed during replacement"

    def raise_conflict(coroutine: Any) -> None:
        coroutine.close()
        raise ConflictError(message, conflicts=[])

    monkeypatch.setattr(cli, "_warn_if_stale", lambda _folder: None)
    monkeypatch.setattr(cli, "_token", lambda *_args: "token")
    monkeypatch.setattr(cli.asyncio, "run", raise_conflict)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["replace-image", str(folder), "hero", "replacement.png"])

    assert excinfo.value.code == 2
    assert capsys.readouterr().err == message + "\n"


def test_unhandled_cli_error_exits_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_args: Any) -> None:
        raise RuntimeError("unexpected top-level failure")

    monkeypatch.setattr(cli, "cmd_diff", fail)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["diff", "deck"])

    assert excinfo.value.code == 1
    assert capsys.readouterr().err == "error: unexpected top-level failure\n"


def test_resume_without_per_slide_errors_before_authentication(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "_token",
        lambda *_args: pytest.fail("invalid flag combination must not authenticate"),
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["push", "deck", "--resume"])

    assert excinfo.value.code == 1
    assert capsys.readouterr().err == "error: --resume requires --per-slide\n"


def test_diff_stdout_stays_json_and_stderr_maps_object_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "deck"
    folder.mkdir()
    (folder / "id_mapping.json").write_text(
        json.dumps({"e59": "g87a21", "s1": "slide_google"}),
        encoding="utf-8",
    )
    requests = [
        {"updateShapeProperties": {"objectId": "g87a21", "fields": "contentAlignment"}},
        {"createShape": {"objectId": "new_card_ship"}},
    ]
    monkeypatch.setattr("slidesmith.engine.client.diff_folder", lambda _: requests)
    monkeypatch.setattr(cli, "_warn_if_stale", lambda _: None)

    cli.cmd_diff(SimpleNamespace(folder=folder))

    captured = capsys.readouterr()
    assert json.loads(captured.out) == requests
    assert captured.err == (
        "Object IDs: g87a21 = e59, new_card_ship = card_ship(new)\n"
    )
