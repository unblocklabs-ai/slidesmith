"""Offline contracts for reusable cross-deck layout snippets."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from slidesmith import cli
from slidesmith.engine.content_parser import parse_slide_content
from slidesmith.engine.snippet import copy_snippet, paste_snippet


def test_snippet_copy_captures_subtree_relative_to_origin(tmp_path: Path) -> None:
    source, _ = _snippet_workspaces(tmp_path)
    output = tmp_path / "card.sml"

    result = copy_snippet(source, "slide=1 AND id~=card", output)

    root = ET.fromstring(output.read_text(encoding="utf-8"))
    assert root.attrib == {
        "version": "1",
        "width": "200",
        "height": "100",
        "sourceSlide": "1",
    }
    assert root.find("./Group").attrib["x"] == "0"
    assert root.find(".//Rect").attrib["x"] == "0"
    title = root.find(".//TextBox")
    assert title.attrib["x"] == "10"
    assert title.attrib["y"] == "10"
    assert title.attrib["role"] == "title"
    assert title.find("P").text == "Competitive title"
    assert result.elements == 3


def test_snippet_paste_inserts_at_frame_remaps_ids_and_content(
    tmp_path: Path,
) -> None:
    source, destination = _snippet_workspaces(tmp_path)
    snippet = tmp_path / "card.sml"
    copy_snippet(source, "id~=card", snippet)
    path = destination / "slides" / "01" / "content.sml"
    before = parse_slide_content(path.read_text(encoding="utf-8"))[0]

    result = paste_snippet(
        destination,
        1,
        snippet,
        role_maps=[("title", "headline")],
        frame=(300, 100, 400, 200),
    )

    content = path.read_text(encoding="utf-8")
    elements = {
        element.clean_id: element for element in parse_slide_content(content)
    }
    assert elements["destination_title"].paragraphs == ["Destination headline"]
    assert (
        elements["destination_title"].x,
        elements["destination_title"].y,
        elements["destination_title"].w,
        elements["destination_title"].h,
    ) == (before.x, before.y, before.w, before.h)
    group = elements["snippet_1__card"]
    assert (group.x, group.y, group.w, group.h) == (300, 100, 400, 200)
    nested = {element.clean_id: element for element in group.children}
    assert (nested["snippet_1__card_title"].x, nested["snippet_1__card_title"].y) == (
        320,
        120,
    )
    assert nested["snippet_1__card_title"].paragraphs == ["Destination headline"]
    assert nested["snippet_1__card_title"].styles is not None
    roles = json.loads((destination / "roles.json").read_text(encoding="utf-8"))
    assert roles["snippet_1__card_title"] == "headline"
    assert roles["snippet_1__card_bg"] == "panel"
    assert result.id_prefix == "snippet_1"
    assert result.inserted_elements == 3


def test_snippet_paste_allocates_next_noncolliding_prefix(tmp_path: Path) -> None:
    source, destination = _snippet_workspaces(tmp_path)
    snippet = tmp_path / "card.sml"
    copy_snippet(source, "id~=card", snippet)

    first = paste_snippet(destination, 1, snippet)
    second = paste_snippet(destination, 1, snippet)

    assert first.id_prefix == "snippet_1"
    assert second.id_prefix == "snippet_2"


def test_snippet_paste_dry_run_performs_zero_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, destination = _snippet_workspaces(tmp_path)
    snippet = tmp_path / "card.sml"
    cli.main(
        ["snippet", "copy", str(source), "id~=card", "-o", str(snippet)]
    )
    capsys.readouterr()
    before = _snapshot(destination)

    cli.main(
        [
            "snippet",
            "paste",
            str(destination),
            "--slide",
            "1",
            str(snippet),
            "--frame",
            "300,100,400,200",
            "--map",
            "title:headline",
            "--dry-run",
        ]
    )

    assert _snapshot(destination) == before
    assert capsys.readouterr().out.endswith("Dry run: no files written.\n")


def test_snippet_copy_rejects_matches_across_slides(tmp_path: Path) -> None:
    source, _ = _snippet_workspaces(tmp_path)
    slide_02 = source / "slides" / "02"
    slide_02.mkdir()
    (slide_02 / "content.sml").write_text(
        '<Slide id="s2"><Rect id="card_other" x="0" y="0" w="10" h="10" /></Slide>',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exactly one source slide"):
        copy_snippet(source, "id~=card", tmp_path / "bad.sml")


def test_snippet_copy_materializes_component_shapes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    slide = source / "slides" / "01"
    slide.mkdir(parents=True)
    (source / "components.sml").write_text(
        '<Components><Component name="badge"><Rect id="body" x="0" y="0" '
        'w="80" h="24" class="fill-#112233" /></Component></Components>',
        encoding="utf-8",
    )
    (slide / "content.sml").write_text(
        '<Slide><Use id="status" component="badge" x="50" y="60" '
        'w="80" h="24" /></Slide>',
        encoding="utf-8",
    )
    output = tmp_path / "badge.sml"

    copy_snippet(source, "id~=status__body", output)

    root = ET.fromstring(output.read_text(encoding="utf-8"))
    assert root.find("Rect").attrib == {
        "id": "status__body",
        "x": "0",
        "y": "0",
        "w": "80",
        "h": "24",
        "class": "fill-#112233",
    }


def _snippet_workspaces(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source_slide = source / "slides" / "01"
    source_slide.mkdir(parents=True)
    (source_slide / "content.sml").write_text(
        '<Slide id="source">\n'
        '  <Group id="card" x="100" y="50" w="200" h="100">\n'
        '    <Rect id="card_bg" x="100" y="50" w="200" h="100" '
        'class="fill-#112233 stroke-none" />\n'
        '    <TextBox id="card_title" x="110" y="60" w="180" h="30" '
        'class="font-family-montserrat text-size-24 text-color-#f2ede2 bold">'
        '<P>Competitive title</P></TextBox>\n'
        "  </Group>\n"
        "</Slide>\n",
        encoding="utf-8",
    )
    (source / "roles.json").write_text(
        json.dumps({"card_bg": "panel", "card_title": "title"}),
        encoding="utf-8",
    )

    destination = tmp_path / "destination"
    destination_slide = destination / "slides" / "01"
    destination_slide.mkdir(parents=True)
    (destination_slide / "content.sml").write_text(
        '<Slide id="destination">\n'
        '  <TextBox id="destination_title" x="40" y="30" w="500" h="50" '
        'class="font-family-arial text-size-20"><P>Destination headline</P></TextBox>\n'
        "</Slide>\n",
        encoding="utf-8",
    )
    (destination / "roles.json").write_text(
        json.dumps({"destination_title": "headline"}),
        encoding="utf-8",
    )
    return source, destination


def _snapshot(folder: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(folder): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }
