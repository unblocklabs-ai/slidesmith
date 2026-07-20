"""Whitespace-safe SML formatting contracts."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from slidesmith.cli import main
from slidesmith.engine.client import diff_folder
from slidesmith.engine.content_parser import parse_slide_content
from slidesmith.engine.formatting import format_folder, format_slide_content
from slidesmith.workspace import materialize


GOLDEN = (
    Path(__file__).parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)


def test_pretty_printed_mixed_content_diffs_to_zero_requests(tmp_path: Path) -> None:
    folder = materialize(json.loads(GOLDEN.read_text(encoding="utf-8")), tmp_path)

    for content_path in sorted((folder / "slides").glob("*/content.sml")):
        root = ET.fromstring(content_path.read_text(encoding="utf-8"))
        ET.indent(root, space="    ")
        content_path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")

    assert diff_folder(folder) == []


def test_intentional_leading_and_trailing_run_spaces_round_trip() -> None:
    content = """<Slide id="s1">
    <TextBox id="label" x="0" y="0" w="100" h="20">
        <P>
            <T class="bold">  keep both sides  </T>
        </P>
    </TextBox>
</Slide>"""

    formatted = format_slide_content(content)

    assert "<T class=\"bold\">  keep both sides  </T>" in formatted
    assert parse_slide_content(formatted)[0].paragraphs == ["  keep both sides  "]
    assert parse_slide_content(formatted) == parse_slide_content(content)


def test_fmt_is_noop_on_generator_output_and_check_reports_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = materialize(json.loads(GOLDEN.read_text(encoding="utf-8")), tmp_path)
    before = {
        path: path.read_bytes()
        for path in sorted((folder / "slides").glob("*/content.sml"))
    }

    result = format_folder(folder)
    main(["fmt", str(folder), "--check"])

    assert result.changed_paths == ()
    assert {path: path.read_bytes() for path in before} == before
    assert capsys.readouterr().out == "All content.sml files are canonically formatted.\n"


def test_fmt_check_detects_changes_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = materialize(json.loads(GOLDEN.read_text(encoding="utf-8")), tmp_path)
    content_path = folder / "slides" / "01" / "content.sml"
    root = ET.fromstring(content_path.read_text(encoding="utf-8"))
    ET.indent(root, space="    ")
    pretty = ET.tostring(root, encoding="unicode")
    content_path.write_text(pretty, encoding="utf-8")

    with pytest.raises(SystemExit, match="1"):
        main(["fmt", str(folder), "--check"])

    assert content_path.read_text(encoding="utf-8") == pretty
    assert capsys.readouterr().out == "1 content.sml file(s) would be reformatted.\n"

    main(["fmt", str(folder)])

    assert content_path.read_text(encoding="utf-8") != pretty
    assert diff_folder(folder) == []
    assert capsys.readouterr().out == "Formatted 1 content.sml file(s).\n"
