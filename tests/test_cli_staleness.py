"""CLI workspace-staleness warnings."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from slidesmith import cli
from slidesmith.cli import _warn_if_stale
from slidesmith.engine.conflicts import ConflictError


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


def _mark_workspace(folder: Path) -> None:
    (folder / ".pristine").mkdir(exist_ok=True)
    (folder / ".pristine" / "presentation.zip").write_bytes(b"zip")
    if not (folder / "presentation.json").exists():
        (folder / "presentation.json").write_text("{}", encoding="utf-8")


def test_cli_version_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out == f"slidesmith {version('slidesmith')}\n"


def test_pull_dir_alias_matches_output_dir_and_is_documented(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = cli.build_parser()
    output_dir = parser.parse_args(["pull", "deck-id", "-o", "one"]).output_dir
    dir_alias = parser.parse_args(["pull", "deck-id", "--dir", "one"]).output_dir
    assert dir_alias == output_dir == "one"

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["pull", "--help"])
    assert excinfo.value.code == 0
    assert "--dir" in capsys.readouterr().out


def test_replace_image_help_declares_immediate_remote_mutation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["replace-image", "--help"])

    assert excinfo.value.code == 0
    help_text = capsys.readouterr().out
    assert "pushes immediately with a revision" in help_text
    assert "lock; it is not staged" in help_text
    assert "not staged by diff/push" in help_text


def test_non_workspace_error_names_path_missing_files_and_recovery_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["diff", str(tmp_path)])

    assert excinfo.value.code == 1
    error = capsys.readouterr().err
    assert str(tmp_path) in error
    assert "presentation.json" in error
    assert ".pristine/presentation.zip" in error
    assert "workspace directory created by slidesmith pull or slidesmith create" in error


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
    _mark_workspace(folder)
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
    _mark_workspace(folder)
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
    _mark_workspace(folder)
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


def test_per_slide_progress_uses_stderr_and_stdout_is_final_result_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "deck"
    folder.mkdir()
    _mark_workspace(folder)

    class FakeResource:
        def __init__(self, token: str) -> None:
            assert token == "token"

        async def close(self) -> None:
            pass

    class FakeClient:
        def __init__(self, _transport: Any, _uploader: Any) -> None:
            pass

        async def push(self, _folder: Path, **kwargs: Any) -> dict[str, Any]:
            progress = kwargs["progress"]
            progress("start", "slide 01/02 ...")
            progress("success", "slide 01/02 ✓")
            return {"replies": [{}, {}]}

    monkeypatch.setattr(cli, "_warn_if_stale", lambda _folder: None)
    monkeypatch.setattr(cli, "_token", lambda *_args: "token")
    monkeypatch.setattr("slidesmith.engine.client.SlidesClient", FakeClient)
    monkeypatch.setattr(
        "slidesmith.engine.transport.GoogleSlidesTransport", FakeResource
    )
    monkeypatch.setattr(
        "slidesmith.engine.assets.GoogleDriveAssetUploader", FakeResource
    )

    cli.main(["push", str(folder), "--per-slide"])

    captured = capsys.readouterr()
    assert captured.out == "Push applied 2 change(s).\n"
    assert captured.err == "slide 01/02 ...\rslide 01/02 ✓\n"


def test_push_json_prints_one_machine_receipt_to_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "deck"
    folder.mkdir()
    _mark_workspace(folder)
    receipt = {
        "presentation_id": "pid",
        "revision_before": "rev-before",
        "revision_after": "rev-after",
        "requests_sent": 2,
        "changes_applied": 2,
        "persistence": {"verified": True, "warnings": []},
        "duration_s": 0.001,
    }

    class FakeResource:
        def __init__(self, _token: str) -> None:
            pass

        async def close(self) -> None:
            pass

    class FakeClient:
        def __init__(self, _transport: Any, _uploader: Any) -> None:
            pass

        async def push(self, _folder: Path, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["receipt"] is True
            return {"replies": [{}, {}], "receipt": receipt}

    monkeypatch.setattr(cli, "_warn_if_stale", lambda _folder: None)
    monkeypatch.setattr(cli, "_token", lambda *_args: "token")
    monkeypatch.setattr("slidesmith.engine.client.SlidesClient", FakeClient)
    monkeypatch.setattr(
        "slidesmith.engine.transport.GoogleSlidesTransport", FakeResource
    )
    monkeypatch.setattr(
        "slidesmith.engine.assets.GoogleDriveAssetUploader", FakeResource
    )

    cli.main(["push", str(folder), "--json"])

    captured = capsys.readouterr()
    assert json.loads(captured.out) == receipt
    assert captured.out.count("\n") == 1
    assert captured.err == ""


def test_push_json_prints_partial_receipt_before_nonzero_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "deck"
    folder.mkdir()
    _mark_workspace(folder)
    partial_receipt = {
        "status": "partial_failure",
        "slides": [
            {"slide": "01", "status": "applied"},
            {"slide": "02", "status": "failed"},
            {"slide": "03", "status": "not-attempted"},
        ],
    }

    class FakeResource:
        def __init__(self, _token: str) -> None:
            pass

        async def close(self) -> None:
            pass

    class FakeClient:
        def __init__(self, _transport: Any, _uploader: Any) -> None:
            pass

        async def push(self, _folder: Path, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["receipt"] is True
            error = RuntimeError("slide 02 failed")
            error.receipt = partial_receipt
            error.response = {"warnings": []}
            raise error

    monkeypatch.setattr(cli, "_warn_if_stale", lambda _folder: None)
    monkeypatch.setattr(cli, "_token", lambda *_args: "token")
    monkeypatch.setattr("slidesmith.engine.client.SlidesClient", FakeClient)
    monkeypatch.setattr(
        "slidesmith.engine.transport.GoogleSlidesTransport", FakeResource
    )
    monkeypatch.setattr(
        "slidesmith.engine.assets.GoogleDriveAssetUploader", FakeResource
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["push", str(folder), "--per-slide", "--json"])

    captured = capsys.readouterr()
    assert excinfo.value.code == 1
    assert json.loads(captured.out) == partial_receipt
    assert captured.out.count("\n") == 1
    assert captured.err == "error: slide 02 failed\n"


def test_diff_stdout_stays_json_and_stderr_maps_object_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "deck"
    folder.mkdir()
    _mark_workspace(folder)
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
