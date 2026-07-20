"""Bulk local class replacement contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from slidesmith import cli
from slidesmith.engine.class_replacement import replace_class


def _workspace(tmp_path: Path) -> Path:
    folder = tmp_path / "deck"
    slide_01 = folder / "slides" / "01"
    slide_02 = folder / "slides" / "02"
    slide_01.mkdir(parents=True)
    slide_02.mkdir(parents=True)
    (slide_01 / "content.sml").write_text(
        '<Slide id="s1"><TextBox id="title" class="font-family-arial">'
        '<P class="font-family-arial text-align-left">'
        '<T class="font-family-arial bold">Title</T></P></TextBox></Slide>',
        encoding="utf-8",
    )
    (slide_02 / "content.sml").write_text(
        '<Slide id="s2"><TextBox id="body" class="font-family-roboto">'
        '<P><T class="font-family-arial">Body</T></P></TextBox></Slide>',
        encoding="utf-8",
    )
    return folder


def test_replace_class_counts_all_supported_scopes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = _workspace(tmp_path)

    cli.main(
        ["replace-class", str(folder), "font-family-arial", "font-family-inter"]
    )

    captured = capsys.readouterr()
    assert captured.out == (
        "Slide 01: 3 replacement(s)\n"
        "Slide 02: 1 replacement(s)\n"
        "Total: 4 replacement(s)\n"
    )
    assert captured.err == ""
    assert "font-family-arial" not in (
        folder / "slides" / "01" / "content.sml"
    ).read_text(encoding="utf-8")


def test_replace_class_dry_run_counts_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = _workspace(tmp_path)
    paths = sorted((folder / "slides").glob("*/content.sml"))
    before = {path: path.read_bytes() for path in paths}

    cli.main(
        [
            "replace-class",
            str(folder),
            "font-family-arial",
            "font-family-inter",
            "--dry-run",
        ]
    )

    assert {path: path.read_bytes() for path in paths} == before
    assert capsys.readouterr().out.endswith(
        "Total: 4 replacement(s)\nDry run: no files written.\n"
    )


def test_replace_class_rejects_invalid_new_class_before_writing(
    tmp_path: Path,
) -> None:
    folder = _workspace(tmp_path)
    path = folder / "slides" / "01" / "content.sml"
    before = path.read_bytes()

    with pytest.raises(ValueError, match="Unrecognized class 'fontish-inter'"):
        replace_class(folder, "font-family-arial", "fontish-inter")

    assert path.read_bytes() == before


def test_replace_class_reports_conflict_with_element_name(
    tmp_path: Path,
) -> None:
    folder = _workspace(tmp_path)
    path = folder / "slides" / "01" / "content.sml"
    content = path.read_text(encoding="utf-8").replace(
        'id="title" class="font-family-arial"',
        'id="title" class="font-family-arial text-size-24"',
    )
    path.write_text(content, encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(ValueError) as excinfo:
        replace_class(folder, "font-family-arial", "text-size-18")

    message = str(excinfo.value)
    assert "title" in message
    assert "text-size-18" in message
    assert "text-size-24" in message
    assert "remove one" in message
    assert path.read_bytes() == before
