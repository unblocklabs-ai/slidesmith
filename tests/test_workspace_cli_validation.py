"""Every workspace-folder CLI entry point fails early on arbitrary paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from slidesmith import cli


@pytest.mark.parametrize(
    "argv",
    [
        ("add-slide", "{folder}"),
        ("check", "{folder}", "--no-thumbnails"),
        ("components", "{folder}"),
        ("diff", "{folder}"),
        ("fmt", "{folder}"),
        ("group", "{folder}", "tag=Rect", "--dry-run"),
        ("push", "{folder}"),
        ("replace-class", "{folder}", "bold", "italic"),
        ("replace-image", "{folder}", "image", "new.png", "--dry-run"),
        ("reorder", "{folder}", "tag=Rect", "--op", "bring-to-front", "--dry-run"),
        ("select", "{folder}", "tag=Rect"),
        ("advise", "{folder}"),
        ("theme", "extract", "{folder}"),
        ("theme", "apply", "{folder}", "theme.json"),
        ("snippet", "copy", "{folder}", "tag=Rect", "-o", "snippet.sml"),
        (
            "snippet",
            "paste",
            "{folder}",
            "snippet.sml",
            "--slide",
            "1",
        ),
        ("apply", "{folder}", "tag=Rect", "--dry-run"),
    ],
)
def test_workspace_commands_reject_non_workspace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...],
) -> None:
    folder = tmp_path / "not-a-workspace"
    rendered = [str(folder) if value == "{folder}" else value for value in argv]

    with pytest.raises(SystemExit) as excinfo:
        cli.main(rendered)

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: Not a Slidesmith workspace:")
    assert str(folder) in captured.err
    assert "workspace directory created by slidesmith pull or slidesmith create" in captured.err
