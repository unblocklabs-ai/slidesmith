"""Bulk local class replacement contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from slidesmith import cli
from slidesmith.engine import atomic_files
from slidesmith.engine.class_replacement import replace_class, replace_classes


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


def test_replace_class_ignores_attribute_names_ending_in_class(
    tmp_path: Path,
) -> None:
    folder = _workspace(tmp_path)
    path = folder / "slides" / "01" / "content.sml"
    path.write_text(
        '<Slide id="s1"><TextBox id="title" '
        'data-class="font-family-arial" class="font-family-roboto"/>'
        "</Slide>",
        encoding="utf-8",
    )
    other = folder / "slides" / "02" / "content.sml"
    other.write_text('<Slide id="s2"/>', encoding="utf-8")

    result = replace_class(
        folder,
        "font-family-arial",
        "font-family-inter",
    )

    assert result.total == 0
    assert path.read_text(encoding="utf-8") == (
        '<Slide id="s1"><TextBox id="title" '
        'data-class="font-family-arial" class="font-family-roboto"/>'
        "</Slide>"
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


def test_replace_class_multi_swap_reports_per_swap_and_slide_counts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = _workspace(tmp_path)

    cli.main(
        [
            "replace-class",
            str(folder),
            "--swap",
            "font-family-arial=font-family-inter",
            "--swap",
            "bold=italic",
        ]
    )

    assert capsys.readouterr().out == (
        "Swap font-family-arial=font-family-inter: 4 replacement(s)\n"
        "Swap bold=italic: 1 replacement(s)\n"
        "Slide 01: 4 replacement(s)\n"
        "Slide 02: 1 replacement(s)\n"
        "Total: 5 replacement(s)\n"
    )


def test_replace_class_bad_swap_among_good_swaps_writes_nothing(
    tmp_path: Path,
) -> None:
    folder = _workspace(tmp_path)
    paths = sorted((folder / "slides").glob("*/content.sml"))
    before = {path: path.read_bytes() for path in paths}

    with pytest.raises(ValueError, match="Unrecognized class 'fontish-inter'"):
        replace_classes(
            folder,
            [
                ("font-family-arial", "font-family-inter"),
                ("bold", "fontish-inter"),
                ("italic", "underline"),
            ],
        )

    assert {path: path.read_bytes() for path in paths} == before


def test_replace_class_combined_swaps_detect_cross_swap_conflict(
    tmp_path: Path,
) -> None:
    folder = _workspace(tmp_path)
    path = folder / "slides" / "01" / "content.sml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(" bold", " bold italic"),
        encoding="utf-8",
    )
    before = path.read_bytes()
    swaps = [("bold", "text-size-18"), ("italic", "text-size-24")]

    for swap in swaps:
        replace_classes(folder, [swap], dry_run=True)

    with pytest.raises(ValueError) as excinfo:
        replace_classes(folder, swaps)

    message = str(excinfo.value)
    assert "text-size-18" in message
    assert "text-size-24" in message
    assert "remove one" in message
    assert path.read_bytes() == before


def test_replace_class_combines_positional_and_flag_swaps(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = _workspace(tmp_path)

    cli.main(
        [
            "replace-class",
            str(folder),
            "font-family-arial",
            "font-family-inter",
            "--swap",
            "bold=italic",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert "Swap font-family-arial=font-family-inter: 4 replacement(s)" in output
    assert "Swap bold=italic: 1 replacement(s)" in output
    assert output.endswith("Total: 5 replacement(s)\nDry run: no files written.\n")


def test_replace_class_rolls_back_a_mid_commit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = _workspace(tmp_path)
    paths = sorted((folder / "slides").glob("*/content.sml"))
    before = {path: path.read_bytes() for path in paths}
    real_replace = atomic_files.replace_file
    calls = 0

    def fail_second_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated commit failure")
        real_replace(source, destination)

    monkeypatch.setattr(atomic_files, "replace_file", fail_second_replace)

    with pytest.raises(OSError, match="simulated commit failure"):
        replace_classes(
            folder,
            [("font-family-arial", "font-family-inter")],
        )

    assert {path: path.read_bytes() for path in paths} == before
    assert not list((folder / "slides").rglob("*.tmp"))
