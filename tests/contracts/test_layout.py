"""Offline contracts for the SML authoring layout compiler."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from extraslide.client import diff_folder
from extraslide.content_parser import ParsedElement, parse_slide_content
from extraslide.layout import ApproximateTextMeasurer, compile_layout
from extraslide.units import pt_to_emu
from slidesmith.workspace import materialize


GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)


def _elements(sml: str) -> dict[str, ParsedElement]:
    return {element.clean_id: element for element in parse_slide_content(sml)}


def _geometry(element: ParsedElement) -> tuple[float | None, ...]:
    return element.x, element.y, element.w, element.h


def test_row_stack_applies_gap_and_padding_exactly() -> None:
    sml = """<Slide id="s1">
  <Stack id="row" direction="row" x="10" y="20" w="300" h="100"
         gap="10" padding="5">
    <Rect id="first" w="40" h="20" class="fill-#ffffff"/>
    <Rect id="second" w="60" h="30"/>
  </Stack>
</Slide>"""

    elements = _elements(sml)

    assert _geometry(elements["first"]) == (15.0, 25.0, 40.0, 20.0)
    assert _geometry(elements["second"]) == (65.0, 25.0, 60.0, 30.0)
    assert elements["first"].styles is not None
    assert "<Stack" not in compile_layout(sml)


def test_column_stack_flex_splits_remaining_height() -> None:
    sml = """<Slide id="s1"><Stack direction="column" x="0" y="0"
        w="100" h="120" gap="10" padding="10">
      <Rect id="fixed" w="20" h="20"/>
      <Rect id="flex_a" w="20" flex="1"/>
      <Rect id="flex_b" w="20" flex="1"/>
    </Stack></Slide>"""

    elements = _elements(sml)

    assert _geometry(elements["fixed"]) == (10.0, 10.0, 20.0, 20.0)
    assert _geometry(elements["flex_a"]) == (10.0, 40.0, 20.0, 30.0)
    assert _geometry(elements["flex_b"]) == (10.0, 80.0, 20.0, 30.0)


def test_stack_align_center_and_stretch() -> None:
    sml = """<Slide id="s1">
      <Stack direction="row" x="0" y="0" w="100" h="100"
             padding="10" align="center">
        <Rect id="centered" w="20" h="20"/>
      </Stack>
      <Stack direction="row" x="120" y="0" w="100" h="100"
             padding="10" align="stretch">
        <Rect id="stretched" w="20"/>
      </Stack>
    </Slide>"""

    elements = _elements(sml)

    assert _geometry(elements["centered"]) == (10.0, 40.0, 20.0, 20.0)
    assert _geometry(elements["stretched"]) == (130.0, 10.0, 20.0, 80.0)


def test_grid_places_children_in_three_columns() -> None:
    sml = """<Slide id="s1"><Grid id="grid" x="10" y="20" w="320"
        h="100" columns="3" gap="10" row-h="30">
      <Rect id="a"/><Rect id="b"/><Rect id="c"/><Rect id="d"/>
    </Grid></Slide>"""

    elements = _elements(sml)

    assert _geometry(elements["a"]) == (10.0, 20.0, 100.0, 30.0)
    assert _geometry(elements["b"]) == (120.0, 20.0, 100.0, 30.0)
    assert _geometry(elements["c"]) == (230.0, 20.0, 100.0, 30.0)
    assert _geometry(elements["d"]) == (10.0, 60.0, 100.0, 30.0)


def test_nested_stack_fills_grid_cell_and_is_flattened() -> None:
    sml = """<Slide id="s1"><Grid x="0" y="0" w="210" h="100"
        columns="2" gap="10" row-h="100">
      <Stack direction="column" gap="10" align="stretch">
        <Rect id="nested_fixed" h="20"/>
        <Rect id="nested_flex" flex="1"/>
      </Stack>
      <Rect id="neighbor"/>
    </Grid></Slide>"""

    compiled = compile_layout(sml)
    elements = _elements(compiled)

    assert "<Grid" not in compiled and "<Stack" not in compiled
    assert _geometry(elements["nested_fixed"]) == (0.0, 0.0, 100.0, 20.0)
    assert _geometry(elements["nested_flex"]) == (0.0, 30.0, 100.0, 70.0)
    assert _geometry(elements["neighbor"]) == (110.0, 0.0, 100.0, 100.0)


def test_auto_text_height_is_exact_and_monotonic() -> None:
    short = """<Slide id="s1"><TextBox id="short" x="0" y="0" w="50"
        h="auto" class="font-family-arial text-size-10 font-weight-400">
      <P>1234567890</P></TextBox></Slide>"""
    long = short.replace('id="short"', 'id="long"').replace(
        "1234567890", "12345678901234567890"
    )

    short_height = _elements(short)["short"].h
    long_height = _elements(long)["long"].h

    # Arial 10pt averages 5.2pt/character. At 50pt wide these strings use
    # 2 and 3 lines; 10 * 1.2 line-height * 1.08 safety margin per line.
    assert short_height == 25.92
    assert long_height == 38.88
    assert long_height > short_height
    assert ApproximateTextMeasurer().measure_wrapped_height(
        "1234567890", "Arial", 10, 400, 50
    ) == pytest.approx(25.92)


def test_auto_text_height_uses_injected_measurer() -> None:
    class FixedMeasurer:
        def measure_wrapped_height(
            self,
            text: str,
            font_family: str,
            font_size_pt: float,
            font_weight: int,
            available_width: float,
        ) -> float:
            assert (text, font_family, font_size_pt, font_weight, available_width) == (
                "Measured",
                "Roboto",
                14.0,
                700,
                90.0,
            )
            return 47.25

    sml = """<Slide><TextBox id="custom" x="0" y="0" w="90" h="auto"
        class="font-family-roboto text-size-14 bold"><P>Measured</P>
      </TextBox></Slide>"""

    compiled = compile_layout(sml, FixedMeasurer())

    assert 'h="47.25"' in compiled


def test_explicit_child_position_fails_with_element_id() -> None:
    sml = """<Slide><Stack direction="row" x="0" y="0" w="100" h="100">
      <Rect id="bad_child" x="5" w="20" h="20"/>
    </Stack></Slide>"""

    with pytest.raises(ValueError, match="bad_child.*cannot declare x or y"):
        parse_slide_content(sml)


def test_plain_sml_passes_through_byte_identical() -> None:
    plain = """<Slide id="s1">
  <TextBox id="plain" x="1" y="2" w="3" h="4"><P>Hello</P></TextBox>
</Slide>
"""

    assert compile_layout(plain) == plain

    comment_only = plain.replace(
        '<Slide id="s1">', '<Slide id="s1"><!-- <Stack is documentation only> -->'
    )
    assert compile_layout(comment_only) == comment_only


def test_diff_compiles_authoring_layout_to_create_shape_transforms(
    tmp_path: Path,
) -> None:
    folder = materialize(json.loads(GOLDEN.read_text(encoding="utf-8")), tmp_path)
    sml_path = folder / "slides" / "01" / "content.sml"
    authoring = """
<Stack direction="row" x="10" y="20" w="230" h="50" gap="10">
  <Rect id="layout_a" w="100" h="30"/>
  <Rect id="layout_b" flex="1" h="30"/>
</Stack>
<Grid x="10" y="90" w="230" h="40" columns="2" gap="10" row-h="40">
  <Rect id="layout_c"/><Rect id="layout_d"/>
</Grid>
<TextBox id="layout_auto" x="260" y="20" w="50" h="auto"
         class="font-family-arial text-size-10"><P>1234567890</P></TextBox>
"""
    sml_path.write_text(
        sml_path.read_text(encoding="utf-8").replace(
            "</Slide>", authoring + "</Slide>"
        ),
        encoding="utf-8",
    )

    requests = diff_folder(folder)
    creates = {
        request["createShape"]["objectId"]: request["createShape"]
        for request in requests
        if "createShape" in request
    }

    def assert_transform(object_id: str, x: float, y: float, w: float, h: float) -> None:
        properties = creates[object_id]["elementProperties"]
        transform = properties["transform"]
        base_width = properties["size"]["width"]["magnitude"]
        base_height = properties["size"]["height"]["magnitude"]
        assert transform["translateX"] == pt_to_emu(x)
        assert transform["translateY"] == pt_to_emu(y)
        assert transform["scaleX"] * base_width == pytest.approx(pt_to_emu(w))
        assert transform["scaleY"] * base_height == pytest.approx(pt_to_emu(h))

    assert_transform("layout_a", 10, 20, 100, 30)
    assert_transform("layout_b", 120, 20, 120, 30)
    assert_transform("layout_c", 10, 90, 110, 40)
    assert_transform("layout_d", 130, 90, 110, 40)
    assert_transform("layout_auto", 260, 20, 50, 25.92)
