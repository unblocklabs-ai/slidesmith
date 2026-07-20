"""Offline contracts for cross-deck theme extraction and application."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slidesmith import cli
from slidesmith.engine.content_parser import parse_slide_content
from slidesmith.engine.theme import apply_theme, extract_theme, load_theme


def test_theme_extract_inventory_tokens_and_role_map(tmp_path: Path) -> None:
    folder = _theme_workspace(tmp_path)

    theme = extract_theme(folder, from_slides="1")

    assert theme["version"] == 1
    assert theme["source"]["slides"] == [1]
    assert theme["tokens"] == {
        "palette": ["#f2ede2", "#112233", "#445566"],
        "themeColors": [],
        "primaryFontFamily": {
            "family": "Montserrat",
            "class": "font-family-montserrat",
        },
        "typeScale": [
            {
                "tier": "display",
                "pt": 53.0,
                "class": "text-size-53",
                "count": 1,
            },
            {
                "tier": "title",
                "pt": 18.0,
                "class": "text-size-18",
                "count": 1,
            },
        ],
        "typeScalePt": [53.0, 18.0],
    }
    assert theme["roles"]["title"]["classes"] == [
        "fill-#112233",
        "stroke-#445566",
        "font-family-montserrat",
        "text-size-53",
        "text-color-#f2ede2",
        "bold",
    ]
    assert theme["roles"]["title"]["elementIds"] == ["source_title"]
    assert theme["inventory"]["palette"][0] == {
        "color": "#f2ede2",
        "count": 2,
        "uses": {"text-color": 2},
    }
    assert theme["inventory"]["type"]["fontFamilies"][0]["count"] == 2


def test_theme_extract_cli_writes_readable_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = _theme_workspace(tmp_path)
    output = tmp_path / "theme.json"

    cli.main(
        [
            "theme",
            "extract",
            str(folder),
            "--from-slides",
            "1-1",
            "-o",
            str(output),
        ]
    )

    assert json.loads(output.read_text(encoding="utf-8"))["roles"]["title"]
    assert "1 role style(s)" in capsys.readouterr().out


def test_theme_apply_role_restyles_and_preserves_text_and_geometry(
    tmp_path: Path,
) -> None:
    folder = _theme_workspace(tmp_path)
    path = folder / "slides" / "02" / "content.sml"
    before = {
        element.clean_id: (
            element.x,
            element.y,
            element.w,
            element.h,
            tuple(element.paragraphs),
        )
        for element in parse_slide_content(path.read_text(encoding="utf-8"))
    }

    result = apply_theme(folder, extract_theme(folder, from_slides="1"), to_slides="2")

    content = path.read_text(encoding="utf-8")
    assert (
        'id="target_title" x="42" y="33" w="500" h="70" '
        'class="fill-#112233 stroke-#445566 font-family-montserrat '
        'text-size-53 text-color-#f2ede2 bold"'
    ) in content
    assert 'id="body" class="fill-#122334 font-family-montserrat"' in content
    after = {
        element.clean_id: (
            element.x,
            element.y,
            element.w,
            element.h,
            tuple(element.paragraphs),
        )
        for element in parse_slide_content(content)
    }
    assert after == before
    assert result.role_restyles == 1
    assert result.font_changes == 1
    assert result.color_changes == 0


def test_theme_apply_maps_near_color_and_leaves_far_color(tmp_path: Path) -> None:
    folder = _theme_workspace(tmp_path)
    path = folder / "slides" / "02" / "content.sml"

    result = apply_theme(
        folder,
        extract_theme(folder, from_slides="1"),
        to_slides="2",
        map_colors=True,
    )

    content = path.read_text(encoding="utf-8")
    assert 'id="body" class="fill-#112233 font-family-montserrat"' in content
    assert 'id="alert" class="fill-#ff0000"' in content
    assert result.color_changes == 1


def test_theme_apply_conflict_validation_is_atomic(tmp_path: Path) -> None:
    folder = _theme_workspace(tmp_path)
    theme = extract_theme(folder, from_slides="1")
    theme["roles"]["title"]["classes"].extend(["text-size-20"])
    before = _snapshot(folder)

    with pytest.raises(ValueError, match="Conflicting classes.*font size"):
        apply_theme(folder, theme, to_slides="2")

    assert _snapshot(folder) == before


def test_theme_apply_cli_dry_run_counts_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = _theme_workspace(tmp_path)
    theme_path = tmp_path / "theme.json"
    theme_path.write_text(
        json.dumps(extract_theme(folder, from_slides="1")),
        encoding="utf-8",
    )
    assert load_theme(theme_path)["version"] == 1
    before = _snapshot(folder)

    cli.main(
        [
            "theme",
            "apply",
            str(folder),
            str(theme_path),
            "--to-slides",
            "2",
            "--map-colors",
            "--dry-run",
        ]
    )

    assert _snapshot(folder) == before
    output = capsys.readouterr().out
    assert "Slide 02: 1 role restyle(s)" in output
    assert output.endswith("Dry run: no files written.\n")


def _theme_workspace(tmp_path: Path) -> Path:
    folder = tmp_path / "deck"
    slide_01 = folder / "slides" / "01"
    slide_02 = folder / "slides" / "02"
    slide_01.mkdir(parents=True)
    slide_02.mkdir(parents=True)
    (slide_01 / "content.sml").write_text(
        '<Slide id="s1">\n'
        '  <TextBox id="source_title" x="20" y="30" w="500" h="70" '
        'class="fill-#112233 stroke-#445566 font-family-montserrat '
        'text-size-53 text-color-#f2ede2 bold"><P>Source title</P></TextBox>\n'
        '  <TextBox id="source_label" x="20" y="120" w="200" h="30" '
        'class="font-family-montserrat text-size-18 text-color-#f2ede2">'
        '<P>Label</P></TextBox>\n'
        "</Slide>\n",
        encoding="utf-8",
    )
    (slide_02 / "content.sml").write_text(
        '<Slide id="s2">\n'
        '  <TextBox id="target_title" x="42" y="33" w="500" h="70" '
        'class="fill-#102132 font-family-arial text-size-20 '
        'text-color-#eeeeee italic"><P>Keep this exact title</P></TextBox>\n'
        '  <TextBox id="body" class="fill-#122334"><P>Body stays put</P></TextBox>\n'
        '  <Rect id="alert" class="fill-#ff0000" x="4" y="5" w="6" h="7" />\n'
        "</Slide>\n",
        encoding="utf-8",
    )
    (folder / "roles.json").write_text(
        json.dumps({"source_title": "title", "target_title": "title"}),
        encoding="utf-8",
    )
    return folder


def _snapshot(folder: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(folder): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }
