"""Human-readable diff summary rendering."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from slidesmith.engine.classes import ContentAlignment
from slidesmith.engine.content_diff import (
    Change,
    ChangeType,
    DiffResult,
    ParagraphClassUpdate,
    format_diff_summary,
)
from slidesmith.engine.content_parser import ElementStyles
from slidesmith import cli
from slidesmith.cli import _request_id_legend


def _summary_result() -> DiffResult:
    return DiffResult(
        changes=[
            Change(ChangeType.DELETE, "e59", slide_index="01"),
            Change(ChangeType.DELETE, "e60", slide_index="01"),
            Change(
                ChangeType.CREATE,
                "mission_ship",
                slide_index="01",
                new_position={"x": 153.4, "y": 395, "w": 154.4, "h": 80},
                new_text=["First", "Second"],
                new_styles=ElementStyles(fill=object(), stroke=object()),
                tag="TextBox",
            ),
            Change(
                ChangeType.MOVE,
                "e10",
                slide_index="01",
                new_position={"x": 20, "y": 30, "w": 40, "h": 50},
            ),
            Change(
                ChangeType.COPY,
                "e12_copy0",
                source_id="e12",
                slide_index="01",
                new_position={"x": 60, "y": 70, "w": 80, "h": 90},
            ),
            Change(
                ChangeType.STYLE_UPDATE,
                "e121",
                slide_index="01",
                new_styles=ElementStyles(
                    content_alignment=ContentAlignment.MIDDLE
                ),
            ),
            Change(
                ChangeType.PARAGRAPH_STYLE_UPDATE,
                "e17",
                slide_index="01",
                paragraph_style_updates=[ParagraphClassUpdate(1, None, None)],
            ),
            Change(ChangeType.TEXT_UPDATE, "e18", slide_index="01"),
            Change(
                ChangeType.IMAGE_UPDATE,
                "hero",
                slide_index="01",
                src="./assets/new.png",
                fit="stretch",
            ),
        ]
    )


def test_format_diff_summary_renders_each_change_type_and_count() -> None:
    assert format_diff_summary(_summary_result(), 39) == """Slide 01
  DELETE e59, e60
  CREATE mission_ship (TextBox 154.4x80 @153.4,395) +fill +stroke +2 paragraphs
  MOVE e10 40x50 @20,30
  IMAGE hero: replace src='./assets/new.png' fit=stretch
  COPY e12 -> e12_copy0 80x90 @60,70
  STYLE e121: contentAlignment MIDDLE
  STYLE e17: 1 paragraph range edit
  TEXT e18: 1 range edit

39 requests total"""


def test_diff_summary_redacts_remote_query_and_preserves_plain_and_local_sources() -> None:
    result = DiffResult(
        changes=[
            Change(
                ChangeType.IMAGE_UPDATE,
                "signed",
                slide_index="01",
                src="https://cdn.example/hero.png?X-Goog-Signature=SECRET",
                fit="stretch",
            ),
            Change(
                ChangeType.IMAGE_UPDATE,
                "plain",
                slide_index="01",
                src="https://cdn.example/plain.png",
                fit="stretch",
            ),
            Change(
                ChangeType.IMAGE_UPDATE,
                "local",
                slide_index="01",
                src="./assets/logo.png",
                fit="stretch",
            ),
        ]
    )

    summary = format_diff_summary(result, 3)

    assert "SECRET" not in summary
    assert "https://cdn.example/hero.png" in summary
    assert "?[redacted]" not in summary
    assert "https://cdn.example/plain.png" in summary
    assert "src='./assets/logo.png'" in summary


def test_diff_summary_names_cover_fit_for_new_image() -> None:
    result = DiffResult(
        changes=[
            Change(
                ChangeType.CREATE,
                "hero",
                slide_index="01",
                tag="Image",
                fit="cover",
                new_position={"x": 1, "y": 2, "w": 3, "h": 4},
            )
        ]
    )

    assert "CREATE hero (Image fit=cover 3x4 @1,2)" in format_diff_summary(
        result, 2
    )


@pytest.mark.parametrize("fit", ["contain", "stretch"])
def test_diff_summary_keeps_non_cover_image_create_rendering(fit: str) -> None:
    result = DiffResult(
        changes=[
            Change(
                ChangeType.CREATE,
                "hero",
                slide_index="01",
                tag="Image",
                fit=fit,
                new_position={"x": 1, "y": 2, "w": 3, "h": 4},
            )
        ]
    )

    assert format_diff_summary(result, 1) == (
        "Slide 01\n  CREATE hero (Image 3x4 @1,2)\n\n1 requests total"
    )


def test_diff_summary_cli_uses_compact_stdout_without_legend(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "slidesmith.engine.client.diff_folder_with_result",
        lambda _: (_summary_result(), [{"request": {}}] * 39),
    )
    monkeypatch.setattr(cli, "_warn_if_stale", lambda _: None)

    cli.cmd_diff(SimpleNamespace(folder=tmp_path, summary=True))

    captured = capsys.readouterr()
    assert "CREATE mission_ship" in captured.out
    assert captured.out.endswith("39 requests total\n")
    assert captured.err == ""


def test_empty_plain_diff_emits_json_array(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("slidesmith.engine.client.diff_folder", lambda _: [])
    monkeypatch.setattr(cli, "_warn_if_stale", lambda _: None)

    cli.cmd_diff(SimpleNamespace(folder=tmp_path, summary=False))

    captured = capsys.readouterr()
    assert captured.out == "[]\n"
    assert json.loads(captured.out) == []
    assert captured.err == ""


def test_empty_diff_summary_retains_human_readable_message(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "slidesmith.engine.client.diff_folder_with_result",
        lambda _: (DiffResult(changes=[]), []),
    )
    monkeypatch.setattr(cli, "_warn_if_stale", lambda _: None)

    cli.cmd_diff(SimpleNamespace(folder=tmp_path, summary=True))

    captured = capsys.readouterr()
    assert captured.out == "No changes detected.\n"
    assert captured.err == ""


def test_default_diff_legend_labels_direct_authored_create_id() -> None:
    assert _request_id_legend(
        [{"createShape": {"objectId": "mission_ship"}}], {}
    ) == "mission_ship = mission_ship(new)"
