"""Offline contracts for reusable SML layout components."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slidesmith.cli import main
from slidesmith.engine.client import diff_folder
from slidesmith.engine.components import load_components
from slidesmith.engine.content_parser import ParsedElement, parse_slide_content
from slidesmith.engine.layout import compile_layout
from slidesmith.workspace import materialize


GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)


def _write_components(folder: Path, body: str) -> None:
    (folder / "components.sml").write_text(
        f"<Components>{body}</Components>\n",
        encoding="utf-8",
    )


def _elements(sml: str, folder: Path) -> dict[str, ParsedElement]:
    components = load_components(folder)
    return {
        element.clean_id: element
        for element in parse_slide_content(sml, components=components)
    }


def _geometry(element: ParsedElement) -> tuple[float | None, ...]:
    return element.x, element.y, element.w, element.h


def test_component_interpolates_two_slots_and_translates_exactly(
    tmp_path: Path,
) -> None:
    _write_components(
        tmp_path,
        """
  <Component name="stat-card">
    <Rect id="card" x="0" y="0" w="200" h="120"
          class="fill-{{accent}} stroke-none" />
    <TextBox id="title" x="12" y="10" w="176" h="30">
      <P>{{title}}</P>
    </TextBox>
  </Component>""",
    )
    sml = """<Slide id="s1">
  <Use id="revenue" component="stat-card" title="Revenue" accent="#5df2b2"
       x="60" y="200" w="200" h="120" />
</Slide>"""

    elements = _elements(sml, tmp_path)

    assert _geometry(elements["revenue__card"]) == (60.0, 200.0, 200.0, 120.0)
    assert _geometry(elements["revenue__title"]) == (72.0, 210.0, 176.0, 30.0)
    assert elements["revenue__title"].paragraphs == ["Revenue"]
    assert elements["revenue__card"].styles is not None
    assert elements["revenue__card"].styles.fill is not None
    assert elements["revenue__card"].styles.fill.color is not None
    assert elements["revenue__card"].styles.fill.color.hex == "#5df2b2"


def test_component_inline_slot_default_is_optional(tmp_path: Path) -> None:
    _write_components(
        tmp_path,
        """
  <Component name="badge">
    <Rect id="pill" x="0" y="0" w="80" h="24"
          class="fill-{{accent|#112233}} stroke-none" />
  </Component>""",
    )

    elements = _elements(
        '<Slide><Use id="defaulted" component="badge" x="5" y="7" '
        'w="80" h="24" /></Slide>',
        tmp_path,
    )

    assert _geometry(elements["defaulted__pill"]) == (5.0, 7.0, 80.0, 24.0)
    assert elements["defaulted__pill"].styles is not None
    assert elements["defaulted__pill"].styles.fill is not None
    assert elements["defaulted__pill"].styles.fill.color is not None
    assert elements["defaulted__pill"].styles.fill.color.hex == "#112233"


def test_multiple_component_instances_have_noncolliding_prefixed_ids(
    tmp_path: Path,
) -> None:
    _write_components(
        tmp_path,
        '<Component name="tile"><Rect id="body" x="0" y="0" w="10" h="10" />'
        '<TextBox id="label" x="0" y="0" w="10" h="10"><P>{{text}}</P>'
        "</TextBox></Component>",
    )
    sml = """<Slide>
  <Use id="first" component="tile" text="A" x="0" y="0" w="10" h="10" />
  <Use id="second" component="tile" text="B" x="20" y="0" w="10" h="10" />
</Slide>"""

    compiled = compile_layout(sml, components=load_components(tmp_path))

    assert "<Use" not in compiled
    assert {"first__body", "first__label", "second__body", "second__label"} <= set(
        _elements(compiled, tmp_path)
    )


def test_use_inside_stack_gets_stack_frame_before_component_expands(
    tmp_path: Path,
) -> None:
    _write_components(
        tmp_path,
        """
  <Component name="inset-card">
    <Rect id="body" x="2" y="3" w="40" h="20" />
  </Component>""",
    )
    sml = """<Slide><Stack x="10" y="10" w="100" h="40" gap="10" padding="5">
  <Rect id="lead" w="20" h="10" />
  <Use id="nested" component="inset-card" w="40" h="20" />
</Stack></Slide>"""

    elements = _elements(sml, tmp_path)

    assert _geometry(elements["lead"]) == (15.0, 15.0, 20.0, 10.0)
    # Stack assigns the Use origin (45, 15); body-relative (2, 3) is then added.
    assert _geometry(elements["nested__body"]) == (47.0, 18.0, 40.0, 20.0)


def test_unknown_component_error_names_use_and_component(tmp_path: Path) -> None:
    _write_components(tmp_path, '<Component name="known"><Rect /></Component>')

    with pytest.raises(ValueError, match="Use 'bad'.*unknown component 'missing'"):
        _elements(
            '<Slide><Use id="bad" component="missing" x="0" y="0" '
            'w="10" h="10" /></Slide>',
            tmp_path,
        )


def test_missing_required_slot_error_names_use_component_and_slot(
    tmp_path: Path,
) -> None:
    _write_components(
        tmp_path,
        '<Component name="label"><TextBox id="text" x="0" y="0" w="40" '
        'h="20"><P>{{value}}</P></TextBox></Component>',
    )

    with pytest.raises(
        ValueError,
        match="Use 'missing-value'.*component 'label'.*missing required slot 'value'",
    ):
        _elements(
            '<Slide><Use id="missing-value" component="label" x="0" y="0" '
            'w="40" h="20" /></Slide>',
            tmp_path,
        )


def test_plain_sml_without_use_or_components_is_byte_identical() -> None:
    plain = """<Slide id="s1">
  <Rect id="plain" x="1" y="2" w="3" h="4" />
</Slide>
"""

    assert compile_layout(plain) == plain


def test_component_qa_accept_class_and_role_never_reach_requests(
    tmp_path: Path,
) -> None:
    folder = materialize(json.loads(GOLDEN.read_text(encoding="utf-8")), tmp_path)
    _write_components(
        folder,
        """
  <Component name="accepted-card">
    <Rect id="body" role="metric" x="0" y="0" w="30" h="30"
          class="qa-accept-out-of-bounds" />
  </Component>""",
    )
    slide_path = folder / "slides" / "01" / "content.sml"
    slide_path.write_text(
        slide_path.read_text(encoding="utf-8").replace(
            "</Slide>",
            '<Use id="accepted" component="accepted-card" '
            'x="650" y="10" w="30" h="30" /></Slide>',
        ),
        encoding="utf-8",
    )

    requests_json = json.dumps(diff_folder(folder))

    assert "accepted__body" in requests_json
    assert "qa-accept-" not in requests_json
    assert '"role"' not in requests_json


def test_components_cli_lists_names_and_derived_slots(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_components(
        tmp_path,
        """
  <Component name="stat-card">
    <Rect id="body" x="0" y="0" w="100" h="40" class="fill-{{accent|#000000}}" />
    <TextBox id="text" x="0" y="0" w="100" h="20"><P>{{title}}: {{value}}</P></TextBox>
  </Component>
  <Component name="spacer"><Rect id="space" x="0" y="0" w="10" h="10" /></Component>""",
    )

    main(["components", str(tmp_path)])

    assert capsys.readouterr().out.splitlines() == [
        "spacer: (no slots)",
        "stat-card: accent, title, value",
    ]


def test_malformed_components_file_is_loud_and_names_path(tmp_path: Path) -> None:
    path = tmp_path / "components.sml"
    path.write_text('<Components><Component name="broken">', encoding="utf-8")

    with pytest.raises(ValueError, match=r"Malformed components\.sml.*components\.sml"):
        load_components(tmp_path)
