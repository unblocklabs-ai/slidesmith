"""R2 track-2 regressions from the 2026-07-23 dogfood deck."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from slidesmith.engine.client import SlidesClient
from slidesmith.engine.content_diff import ChangeType, diff_presentation
from slidesmith.engine.content_parser import parse_slide_content
from slidesmith.engine.content_requests import generate_batch_requests
from slidesmith.engine.persistence_styles import effective_text_style_spans
from slidesmith.engine.qa import _text_insets, _text_overflow_limit, lint_folder
from slidesmith.engine.advisor import advise_folder


def _style_requests(pristine: str, edited: str) -> list[dict[str, Any]]:
    diff = diff_presentation(
        {"01": parse_slide_content(pristine)},
        {"01": parse_slide_content(edited)},
        {},
    )
    return generate_batch_requests(diff, {"agenda_card_1": "agenda_card_1"}, {"01": "slide"})


def _foreground_ranges(content: str) -> list[tuple[int, int, str | None]]:
    spans = effective_text_style_spans(parse_slide_content(content)[0])
    assert spans is not None
    return [
        (
            span.start
            + sum(
                len(paragraph) + 1
                for paragraph in parse_slide_content(content)[0].paragraphs[
                    : span.paragraph_index
                ]
            ),
            span.end
            + sum(
                len(paragraph) + 1
                for paragraph in parse_slide_content(content)[0].paragraphs[
                    : span.paragraph_index
                ]
            ),
            getattr(span.properties.get("text.foreground_color"), "hex", None),
        )
        for span in spans
        if span.start < span.end
    ]


def test_scope_move_color_is_effectively_a_noop_and_true_change_is_fixed_range() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="agenda_card_1" x="0" y="0" w="200" h="80">'
        '<P class="text-color-#5df2b2">01  Create + share</P>'
        '<P class="text-color-#ffffff">Start with a live deck and a reviewable local workspace.</P>'
        "</TextBox></Slide>"
    )
    moved = pristine.replace(
        '<TextBox id="agenda_card_1" x="0" y="0" w="200" h="80">',
        '<TextBox id="agenda_card_1" x="0" y="0" w="200" h="80" class="text-color-#5df2b2">',
    ).replace('<P class="text-color-#5df2b2">', "<P>", 1)

    assert _style_requests(pristine, moved) == []
    assert _foreground_ranges(moved) == [
        (0, 18, "#5df2b2"),
        (19, 75, "#ffffff"),
    ]

    changed = moved.replace('class="text-color-#5df2b2"', 'class="text-color-#ff0000"', 1)
    requests = _style_requests(pristine, changed)
    text_updates = [request["updateTextStyle"] for request in requests if "updateTextStyle" in request]
    assert text_updates == [
        {
            "objectId": "agenda_card_1",
            "textRange": {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 18},
            "style": {
                "foregroundColor": {
                    "opaqueColor": {
                        "rgbColor": {"red": 1.0, "green": 0.0, "blue": 0.0}
                    }
                }
            },
            "fields": "foregroundColor",
        }
    ]
    assert all(update["style"] for update in text_updates)


@pytest.mark.parametrize(
    "class_name",
    [
        "text-size-24",
        "bold",
        "font-family-roboto font-weight-700",
        "leading-150",
    ],
)
def test_scope_move_all_text_and_paragraph_properties_is_a_noop(
    class_name: str,
) -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="agenda_card_1" x="0" y="0" w="200" h="80">'
        f'<P class="{class_name}">A😀B</P></TextBox></Slide>'
    )
    moved = pristine.replace(
        '<TextBox id="agenda_card_1" x="0" y="0" w="200" h="80">',
        f'<TextBox id="agenda_card_1" x="0" y="0" w="200" h="80" class="{class_name}">',
    ).replace(f'<P class="{class_name}">', "<P>", 1)

    assert _style_requests(pristine, moved) == []


def test_run_to_paragraph_color_move_uses_utf16_effective_spans() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="agenda_card_1" x="0" y="0" w="200" h="80">'
        '<P><T class="text-color-#5df2b2">A😀B</T></P></TextBox></Slide>'
    )
    moved = pristine.replace(
        '<P><T class="text-color-#5df2b2">A😀B</T></P>',
        '<P class="text-color-#5df2b2"><T>A😀B</T></P>',
    )

    assert _style_requests(pristine, moved) == []


def test_paragraph_color_move_with_bold_addition_has_one_fixed_range_plan() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="agenda_card_1" x="0" y="0" w="200" h="80">'
        '<P class="text-color-#5df2b2">A😀B</P></TextBox></Slide>'
    )
    edited = pristine.replace(
        '<TextBox id="agenda_card_1" x="0" y="0" w="200" h="80">',
        '<TextBox id="agenda_card_1" x="0" y="0" w="200" h="80" '
        'class="text-color-#5df2b2 bold">',
    ).replace('<P class="text-color-#5df2b2">', "<P>", 1)
    requests = _style_requests(pristine, edited)

    assert requests == [
        {
            "updateTextStyle": {
                "objectId": "agenda_card_1",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 0,
                    "endIndex": 4,
                },
                "style": {"bold": True},
                "fields": "bold",
            }
        }
    ]


def test_mixed_color_removal_and_addition_is_planned_per_paragraph_range() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="agenda_card_1" x="0" y="0" w="200" h="80" '
        'class="text-color-#ff0000"><P>one</P><P>two</P></TextBox></Slide>'
    )
    edited = (
        '<Slide id="s1"><TextBox id="agenda_card_1" x="0" y="0" w="200" h="80">'
        '<P class="text-color-#0000ff">one</P><P>two</P></TextBox></Slide>'
    )

    assert _style_requests(pristine, pristine) == []
    assert _style_requests(pristine, edited) == [
        {
            "updateTextStyle": {
                "objectId": "agenda_card_1",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 0,
                    "endIndex": 3,
                },
                "style": {
                    "foregroundColor": {
                        "opaqueColor": {
                            "rgbColor": {"red": 0.0, "green": 0.0, "blue": 1.0}
                        }
                    }
                },
                "fields": "foregroundColor",
            }
        },
        {
            "updateTextStyle": {
                "objectId": "agenda_card_1",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 4,
                    "endIndex": 7,
                },
                "style": {},
                "fields": "foregroundColor",
            }
        },
    ]


def test_mixed_paragraph_property_removal_and_addition_is_range_scoped() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="agenda_card_1" x="0" y="0" w="200" h="80" '
        'class="spacing-collapse-lists indent-start-8"><P>one</P><P>two</P>'
        "</TextBox></Slide>"
    )
    edited = (
        '<Slide id="s1"><TextBox id="agenda_card_1" x="0" y="0" w="200" h="80">'
        '<P class="spacing-never-collapse indent-start-12">one</P><P>two</P>'
        "</TextBox></Slide>"
    )

    assert _style_requests(pristine, edited) == [
        {
            "updateParagraphStyle": {
                "objectId": "agenda_card_1",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 0,
                    "endIndex": 3,
                },
                "style": {
                    "indentStart": {"magnitude": 12.0, "unit": "PT"},
                    "spacingMode": "NEVER_COLLAPSE",
                },
                "fields": "indentStart,spacingMode",
            }
        },
        {
            "updateParagraphStyle": {
                "objectId": "agenda_card_1",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 4,
                    "endIndex": 7,
                },
                "style": {},
                "fields": "indentStart,spacingMode",
            }
        },
    ]


def test_three_replace_class_color_changes_are_the_only_real_restyle_requests() -> None:
    pristine = (
        '<Slide id="s1">'
        '<TextBox id="one" x="0" y="0" w="100" h="20" class="text-color-#bdc5d4"><P>one</P></TextBox>'
        '<TextBox id="two" x="0" y="30" w="100" h="20" class="text-color-#bdc5d4"><P>two</P></TextBox>'
        '<TextBox id="three" x="0" y="60" w="100" h="20" class="text-color-#bdc5d4"><P>three</P></TextBox>'
        "</Slide>"
    )
    edited = pristine.replace("text-color-#bdc5d4", "text-color-#ffffff")
    diff = diff_presentation(
        {"01": parse_slide_content(pristine)},
        {"01": parse_slide_content(edited)},
        {},
    )
    requests = generate_batch_requests(
        diff,
        {"one": "one", "two": "two", "three": "three"},
        {"01": "slide"},
    )
    assert len(requests) == 3
    assert all(
        request["updateTextStyle"]["fields"] == "foregroundColor"
        and request["updateTextStyle"]["textRange"]["type"] == "FIXED_RANGE"
        and request["updateTextStyle"]["style"]
        for request in requests
    )


_GOOGLE_DEFAULT_PAIRS = (
    (
        "decimal_probe",
        "leading-88.421",
        "fill-none stroke-none text-align-left leading-88.421 space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists content-align-top",
    ),
    (
        "subtitle",
        None,
        "fill-none stroke-none text-align-left leading-130 space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists content-align-top",
    ),
    (
        "title",
        None,
        "fill-none stroke-none text-align-left leading-105 space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists content-align-top",
    ),
    (
        "title_kicker",
        None,
        "fill-none stroke-none text-align-left leading-100 space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists content-align-top",
    ),
    (
        "stack_footer",
        None,
        "fill-none stroke-none text-align-center leading-100 space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists content-align-top",
    ),
    (
        "stack_heading",
        None,
        "fill-none stroke-none text-align-left leading-105 space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists content-align-top",
    ),
    (
        "remote_heading",
        None,
        "fill-none stroke-none text-align-left leading-100 space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists content-align-top",
    ),
    (
        "agenda_card_1",
        "stroke-#5df2b2/60 stroke-solid",
        "stroke-#5df2b2/60 stroke-w-0.75 stroke-solid text-align-left space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists content-align-top",
    ),
    (
        "agenda_card_2",
        "stroke-#ffffff/35 stroke-solid",
        "stroke-#ffffff/35 stroke-w-0.75 stroke-solid text-align-left space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists content-align-top",
    ),
    (
        "agenda_card_3",
        "stroke-#ffffff/35 stroke-solid",
        "stroke-#ffffff/35 stroke-w-0.75 stroke-solid text-align-left space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists content-align-top",
    ),
    (
        "agenda_card_4",
        "stroke-#5df2b2/60 stroke-solid",
        "stroke-#5df2b2/60 stroke-w-0.75 stroke-solid text-align-left space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists content-align-top",
    ),
    (
        "content_card",
        "stroke-#d9e1ec stroke-solid",
        "stroke-#d9e1ec stroke-w-0.75 stroke-solid text-align-left space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists content-align-top",
    ),
    (
        "signal_one",
        "stroke-#5df2b2/55 stroke-solid",
        "stroke-#5df2b2/55 stroke-w-0.75 stroke-solid text-align-center space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists content-align-middle",
    ),
    (
        "signal_two",
        "stroke-#5df2b2/55 stroke-solid",
        "stroke-#5df2b2/55 stroke-w-0.75 stroke-solid text-align-center space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists content-align-middle",
    ),
    (
        "signal_three",
        "stroke-#5df2b2/55 stroke-solid",
        "stroke-#5df2b2/55 stroke-w-0.75 stroke-solid text-align-center space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists content-align-middle",
    ),
)


def _persistence_response(
    tmp_path: Path,
    intended_sml: str,
    remote_sml: str,
    *,
    newly_created: bool,
    target_id: str = "field",
    author_changes: list[Any] | None = None,
) -> dict[str, Any]:
    folder = tmp_path / "deck"
    folder.mkdir()
    (folder / "id_mapping.json").write_text("{}", encoding="utf-8")
    client = SlidesClient()
    client._read_pristine = lambda _folder: (
        {"01": parse_slide_content(remote_sml)},
        {},
    )
    response: dict[str, Any] = {}
    client._append_persistence_warning(
        folder,
        {"01": parse_slide_content(intended_sml)},
        {(target_id, ChangeType.CREATE if newly_created else ChangeType.STYLE_UPDATE)},
        {("01", target_id)} if newly_created else set(),
        response,
        author_changes=author_changes,
    )
    return response


def _field_sml(
    class_string: str | None,
    paragraph_class: str | None = None,
    tag: str = "TextBox",
    element_id: str = "field",
) -> str:
    class_attr = f' class="{class_string}"' if class_string else ""
    paragraph_attr = f' class="{paragraph_class}"' if paragraph_class else ""
    return f'<Slide id="s1"><{tag} id="{element_id}" x="0" y="0" w="100" h="30"{class_attr}><P{paragraph_attr}>Authored</P></{tag}></Slide>'


def _matching_paragraph_defaults(remote: str) -> str:
    return " ".join(
        class_name
        for class_name in remote.split()
        if not class_name.startswith(("fill-", "stroke-", "content-align-"))
    )


def _dogfood_element_tag(element_id: str) -> str:
    return "Rect" if element_id.startswith("signal_") else "TextBox"


@pytest.mark.parametrize("element_id, sent, remote", _GOOGLE_DEFAULT_PAIRS)
def test_new_element_google_paint_and_stroke_defaults_are_not_warnings(
    tmp_path: Path,
    element_id: str,
    sent: str | None,
    remote: str,
) -> None:
    tag = _dogfood_element_tag(element_id)
    response = _persistence_response(
        tmp_path,
        _field_sml(sent, _matching_paragraph_defaults(remote), tag, element_id),
        _field_sml(remote, _matching_paragraph_defaults(remote), tag, element_id),
        newly_created=True,
        target_id=element_id,
    )
    assert "warnings" not in response


@pytest.mark.parametrize("element_id, sent, remote", _GOOGLE_DEFAULT_PAIRS)
def test_existing_element_google_paint_and_stroke_defaults_still_warn(
    tmp_path: Path,
    element_id: str,
    sent: str | None,
    remote: str,
) -> None:
    tag = _dogfood_element_tag(element_id)
    response = _persistence_response(
        tmp_path,
        _field_sml(sent, _matching_paragraph_defaults(remote), tag, element_id),
        _field_sml(remote, _matching_paragraph_defaults(remote), tag, element_id),
        newly_created=False,
        target_id=element_id,
    )
    assert response["warnings"]


def test_authored_stroke_none_restored_by_google_still_warns(tmp_path: Path) -> None:
    pristine = _field_sml("stroke-none")
    intended = _field_sml(None)
    author_change = diff_presentation(
        {"01": parse_slide_content(pristine)},
        {"01": parse_slide_content(intended)},
        {},
    ).changes[0]
    response = _persistence_response(
        tmp_path,
        intended,
        _field_sml("stroke-none"),
        newly_created=True,
        author_changes=[author_change],
    )
    assert response["warnings"]


def test_remote_cover_geometry_warning_remains_one_warning(tmp_path: Path) -> None:
    intended = '<Slide id="s1"><Image id="remote_cover" x="90" y="78" w="540" h="270" /></Slide>'
    remote = '<Slide id="s1"><Image id="remote_cover" x="158.23" y="78" w="403.53" h="270" /></Slide>'
    folder = tmp_path / "deck"
    folder.mkdir()
    (folder / "id_mapping.json").write_text("{}", encoding="utf-8")
    client = SlidesClient()
    client._read_pristine = lambda _folder: (
        {"01": parse_slide_content(remote)},
        {},
    )
    response: dict[str, Any] = {}
    client._append_persistence_warning(
        folder,
        {"01": parse_slide_content(intended)},
        {("remote_cover", ChangeType.MOVE)},
        set(),
        response,
    )
    assert len(response["warnings"]) == 1
    assert "x=90, y=78, w=540, h=270" in response["warnings"][0].message
    assert "x=158.23, y=78, w=403.53, h=270" in response["warnings"][0].message


def _write_qa_fixture(folder: Path) -> None:
    folder.mkdir()
    (folder / "presentation.json").write_text(
        json.dumps({"pageSize": {"width": 720, "height": 405}}),
        encoding="utf-8",
    )
    (folder / "styles.json").write_text("{}", encoding="utf-8")
    (folder / "id_mapping.json").write_text("{}", encoding="utf-8")
    (folder / "slides").mkdir()
    elements = (
        (
            "subtitle",
            'x="56" y="238" w="519.99" h="57.99"',
            "content-align-top fill-none stroke-none text-align-left leading-130 space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists",
            "font-family-arial text-size-18 font-weight-400 text-color-#ffffff",
            "A real Google Slides deck built, restyled, pushed, pulled, and checked by an agent.",
        ),
        (
            "stack_footer",
            'x="56" y="328" w="608.01" h="24"',
            "content-align-top fill-none stroke-none text-align-center leading-100 space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists",
            "font-family-arial text-size-12 font-weight-400 text-color-#9fb0c5",
            "Stack owns the spacing; the author owns the intent.",
        ),
        (
            "remote_heading",
            'x="48" y="28" w="624" h="33.99"',
            "content-align-top fill-none stroke-none text-align-left leading-100 space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists",
            "bold font-family-arial text-size-22 font-weight-700 text-color-#0b1324",
            "A live cover crop",
        ),
        (
            "remote_caption",
            'x="90" y="358" w="540" h="21.99"',
            "content-align-top fill-#0b1324/88 stroke-none text-align-center leading-100 space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists",
            "font-family-arial text-size-10 font-weight-400 text-color-#ffffff",
            'Remote URL • fit="cover" • center crop validation',
        ),
        (
            "decimal_probe",
            'x="56" y="344" w="608.01" h="24"',
            "content-align-top fill-none stroke-none text-align-left leading-88.421 space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists",
            "font-family-arial text-size-10 font-weight-400 text-color-#71809b",
            "Fractional leading probe: 88.421%",
        ),
        (
            "stack_heading",
            'x="56" y="54" w="608.01" h="44.01"',
            "content-align-top fill-none stroke-none text-align-left leading-105 space-above-0 space-below-0 indent-start-0 indent-first-0 spacing-collapse-lists",
            "bold font-family-arial text-size-26 font-weight-700 text-color-#ffffff",
            "Three equal signals",
        ),
    )
    for index, (element_id, geometry, element_classes, paragraph_classes, text) in enumerate(elements, 1):
        slide = folder / "slides" / f"{index:02d}"
        slide.mkdir()
        (slide / "content.sml").write_text(
            f'<Slide id="slide{index}"><TextBox id="{element_id}" {geometry} class="{element_classes}"><P class="{paragraph_classes}">{text}</P></TextBox></Slide>',
            encoding="utf-8",
        )


def test_field_calibrated_vertical_inset_clears_all_six_false_results(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "qa"
    _write_qa_fixture(folder)
    findings = lint_folder(folder)
    assert [finding for finding in findings if finding.rule == "TEXT_OVERFLOW"] == []
    assert [
        suggestion.element_ids
        for suggestion in advise_folder(folder, rule="near-overflow")
        if suggestion.element_ids in {("decimal_probe",), ("stack_heading",)}
    ] == []
    assert _text_insets({"textInsets": {"left": 1, "top": 2, "right": 3, "bottom": 4}}) == (1, 2, 3, 4)


def test_deliberate_short_text_box_still_flags_overflow(tmp_path: Path) -> None:
    folder = tmp_path / "qa"
    _write_qa_fixture(folder)
    slide = folder / "slides" / "07"
    slide.mkdir()
    (slide / "content.sml").write_text(
        '<Slide id="probe"><TextBox id="overflow_probe" x="20" y="20" w="100" h="8" class="text-size-26"><P>Deliberate overflow probe</P></TextBox></Slide>',
        encoding="utf-8",
    )
    assert [
        finding
        for finding in lint_folder(folder)
        if finding.element_ids == ("overflow_probe",)
        and finding.rule == "TEXT_OVERFLOW"
    ]


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [(0.90, False), (1.00, False), (1.049, False), (1.05, False), (1.051, True)],
)
def test_qa_overflow_boundary_starts_strictly_above_105_percent(
    ratio: float,
    expected: bool,
) -> None:
    limit = _text_overflow_limit(
        60.0,
        60.0 * ratio,
        18.0,
        first_line_height=18.0,
        line_count=2,
        inset_height=0.0,
    )

    assert (60.0 * ratio > limit) is expected


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [(0.90, True), (1.00, True), (1.049, True), (1.05, True), (1.051, False)],
)
def test_advisor_near_overflow_boundary_tiles_qa_tolerance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ratio: float,
    expected: bool,
) -> None:
    folder = tmp_path / f"advisor-{str(ratio).replace('.', '_')}"
    _write_qa_fixture(folder)
    slide = folder / "slides" / "08"
    slide.mkdir()
    (slide / "content.sml").write_text(
        '<Slide id="advisor-probe"><TextBox id="advisor_probe" '
        'x="20" y="20" w="100" h="60" class="text-size-18">'
        "<P>Probe</P></TextBox></Slide>",
        encoding="utf-8",
    )

    from types import SimpleNamespace

    monkeypatch.setattr(
        "slidesmith.engine.advisor._measure_text_element",
        lambda _element, _style, box, _measurer: SimpleNamespace(
            top_inset_pt=0.0,
            bottom_inset_pt=0.0,
            layout=SimpleNamespace(height_pt=box.h * ratio),
        ),
    )
    suggestions = advise_folder(folder, rule="near-overflow")

    assert bool([item for item in suggestions if item.element_ids == ("advisor_probe",)]) is expected
