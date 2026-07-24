"""Offline contracts for text edits combined with cross-scope styling."""

from __future__ import annotations

from typing import Any

import pytest

from slidesmith.engine.classes import TextStyle
from slidesmith.engine.content_diff import diff_presentation
from slidesmith.engine.content_parser import (
    ElementStyles,
    ParsedElement,
    ParsedRun,
    parse_slide_content,
)
from slidesmith.engine.content_requests import (
    _assert_safe_text_request_order,
    generate_batch_requests,
)
from slidesmith.engine.diff_model import Change, ChangeType, DiffResult
from slidesmith.engine.persistence_styles import effective_text_style_ranges
from slidesmith.engine.text_requests import _create_effective_style_requests


def _requests(pristine: str, edited: str) -> list[dict[str, Any]]:
    diff = diff_presentation(
        {"01": parse_slide_content(pristine)},
        {"01": parse_slide_content(edited)},
        {},
    )
    return generate_batch_requests(diff, {"box": "box"}, {"01": "slide"})


def _style_requests(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        request
        for request in requests
        if "updateTextStyle" in request or "updateParagraphStyle" in request
    ]


def test_text_edit_scope_move_repro_has_no_unsafe_broad_style_sequence() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="box" x="0" y="0" w="200" h="80">'
        '<P class="text-color-#00ff00">Heading</P>'
        '<P class="text-color-#ffffff">Body</P></TextBox></Slide>'
    )
    edited = (
        '<Slide id="s1"><TextBox id="box" x="0" y="0" w="200" h="80" '
        'class="text-color-#00ff00"><P>Heading!</P>'
        '<P class="text-color-#ffffff">Body</P></TextBox></Slide>'
    )

    assert _requests(pristine, edited) == [
        {
            "insertText": {
                "objectId": "box",
                "insertionIndex": 7,
                "text": "!",
            }
        }
    ]


@pytest.mark.parametrize(
    ("old_text", "new_text", "index", "inserted"),
    [
        ("ABC", "XABC", 0, "X"),
        ("ABC", "AXBC", 1, "X"),
        ("ABC", "ABCX", 3, "X"),
    ],
)
def test_insertion_start_middle_end_uses_post_edit_style_space(
    old_text: str,
    new_text: str,
    index: int,
    inserted: str,
) -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="box">'
        f'<P class="text-color-#00ff00">{old_text}</P>'
        '<P class="text-color-#ffffff">Body</P></TextBox></Slide>'
    )
    edited = (
        '<Slide id="s1"><TextBox id="box" class="text-color-#00ff00">'
        f"<P>{new_text}</P>"
        '<P class="text-color-#ffffff">Body</P></TextBox></Slide>'
    )

    requests = _requests(pristine, edited)
    assert [
        request["insertText"]
        for request in requests
        if "insertText" in request
    ] == [{"objectId": "box", "insertionIndex": index, "text": inserted}]
    if index == 0:
        assert _style_requests(requests) == [
            {
                "updateTextStyle": {
                    "objectId": "box",
                    "textRange": {
                        "type": "FIXED_RANGE",
                        "startIndex": 0,
                        "endIndex": 1,
                    },
                    "style": {
                        "foregroundColor": {
                            "opaqueColor": {
                                "rgbColor": {
                                    "red": 0.0,
                                    "green": 1.0,
                                    "blue": 0.0,
                                }
                            }
                        }
                    },
                    "fields": "foregroundColor",
                }
            }
        ]
    else:
        assert _style_requests(requests) == []


def test_deletion_shrinking_styled_span_keeps_retained_style() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="box">'
        '<P class="text-color-#00ff00">ABC</P></TextBox></Slide>'
    )
    edited = (
        '<Slide id="s1"><TextBox id="box" class="text-color-#00ff00">'
        "<P>AC</P></TextBox></Slide>"
    )

    assert _requests(pristine, edited) == [
        {
            "deleteText": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 1,
                    "endIndex": 2,
                },
            }
        }
    ]


def test_newline_insertion_styles_new_paragraph_in_post_edit_space() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="box">'
        '<P class="text-color-#00ff00">one</P></TextBox></Slide>'
    )
    edited = (
        '<Slide id="s1"><TextBox id="box" class="text-color-#00ff00">'
        '<P>one</P><P class="text-color-#ffffff">two</P></TextBox></Slide>'
    )

    requests = _requests(pristine, edited)
    assert requests[0] == {
        "insertText": {
            "objectId": "box",
            "insertionIndex": 3,
            "text": "\ntwo",
        }
    }
    style_updates = [
        request["updateTextStyle"]
        for request in requests
        if "updateTextStyle" in request
    ]
    assert style_updates == [
        {
            "objectId": "box",
            "textRange": {
                "type": "FIXED_RANGE",
                "startIndex": 4,
                "endIndex": 7,
            },
            "style": {
                "foregroundColor": {
                    "opaqueColor": {
                        "rgbColor": {
                            "red": 1.0,
                            "green": 1.0,
                            "blue": 1.0,
                        }
                    }
                }
            },
            "fields": "foregroundColor",
        }
    ]
    assert all(update["textRange"]["type"] != "ALL" for update in style_updates)


def test_inserted_span_inside_merged_effective_range_gets_own_reset() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="box" x="0" y="0" w="200" h="80"><P>'
        '<T class="text-color-#00ff00">A</T><T>BC</T></P></TextBox></Slide>'
    )
    edited = (
        '<Slide id="s1"><TextBox id="box" x="0" y="0" w="200" h="80"><P>'
        'AXC</P></TextBox></Slide>'
    )

    assert _requests(pristine, edited) == [
        {
            "deleteText": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 1,
                    "endIndex": 2,
                },
            }
        },
        {
            "insertText": {
                "objectId": "box",
                "insertionIndex": 1,
                "text": "X",
            }
        },
        {
            "updateTextStyle": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 0,
                    "endIndex": 1,
                },
                "style": {},
                "fields": "foregroundColor",
            }
        },
        {
            "updateTextStyle": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 1,
                    "endIndex": 2,
                },
                "style": {},
                "fields": "foregroundColor",
            }
        },
    ]


def test_split_paragraph_marks_surviving_right_half_as_created() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="box" x="0" y="0" w="200" h="80">'
        '<P class="leading-150">AB</P></TextBox></Slide>'
    )
    edited = (
        '<Slide id="s1"><TextBox id="box" x="0" y="0" w="200" h="80">'
        '<P class="leading-150">A</P><P class="leading-150">X</P>'
        '<P class="leading-150">B</P></TextBox></Slide>'
    )

    assert _requests(pristine, edited) == [
        {
            "insertText": {
                "objectId": "box",
                "insertionIndex": 1,
                "text": "\nX\n",
            }
        },
        {
            "updateParagraphStyle": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 2,
                    "endIndex": 3,
                },
                "style": {"lineSpacing": 150.0},
                "fields": "lineSpacing",
            }
        },
        {
            "updateParagraphStyle": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 4,
                    "endIndex": 5,
                },
                "style": {"lineSpacing": 150.0},
                "fields": "lineSpacing",
            }
        },
    ]


def test_emoji_edit_crossing_run_boundary_styles_each_replacement_span() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="box"><P>'
        '<T class="text-color-#00ff00">A</T>'
        '<T class="text-color-#0000ff">B</T></P></TextBox></Slide>'
    )
    edited = (
        '<Slide id="s1"><TextBox id="box" class="text-color-#00ff00"><P>'
        "<T>A😀</T><T class=\"text-color-#0000ff\">B</T>"
        "</P></TextBox></Slide>"
    )

    requests = _requests(pristine, edited)
    assert requests[:2] == [
        {
            "deleteText": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 0,
                    "endIndex": 2,
                },
            }
        },
        {
            "insertText": {
                "objectId": "box",
                "insertionIndex": 0,
                "text": "A😀B",
            }
        },
    ]
    assert requests[2:] == [
        {
            "updateTextStyle": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 0,
                    "endIndex": 3,
                },
                "style": {
                    "foregroundColor": {
                        "opaqueColor": {
                            "rgbColor": {
                                "red": 0.0,
                                "green": 1.0,
                                "blue": 0.0,
                            }
                        }
                    }
                },
                "fields": "foregroundColor",
            }
        },
        {
            "updateTextStyle": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 3,
                    "endIndex": 4,
                },
                "style": {
                    "foregroundColor": {
                        "opaqueColor": {
                            "rgbColor": {
                                "red": 0.0,
                                "green": 0.0,
                                "blue": 1.0,
                            }
                        }
                    }
                },
                "fields": "foregroundColor",
            }
        }
    ]


def test_inserted_paragraph_gets_explicit_paragraph_style() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="box" x="0" y="0" w="200" h="80">'
        '<P class="leading-150">A</P><P class="leading-100">B</P>'
        "</TextBox></Slide>"
    )
    edited = (
        '<Slide id="s1"><TextBox id="box" x="0" y="0" w="200" h="80" '
        'class="leading-150"><P>A</P><P>X</P>'
        '<P class="leading-100">B</P></TextBox></Slide>'
    )

    assert _requests(pristine, edited) == [
        {
            "insertText": {
                "objectId": "box",
                "insertionIndex": 2,
                "text": "X\n",
            }
        },
        {
            "updateParagraphStyle": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 2,
                    "endIndex": 3,
                },
                "style": {"lineSpacing": 150.0},
                "fields": "lineSpacing",
            }
        },
    ]


def test_inserted_paragraphs_never_use_following_text_style_anchor() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="box">'
        '<P class="text-color-#00ff00">A</P>'
        '<P class="text-color-#0000ff">B</P></TextBox></Slide>'
    )
    edited = (
        '<Slide id="s1"><TextBox id="box">'
        '<P class="text-color-#00ff00">A</P>'
        '<P class="text-color-#00ff00">X</P>'
        '<P class="text-color-#00ff00">Y</P>'
        '<P class="text-color-#0000ff">B</P></TextBox></Slide>'
    )

    assert _requests(pristine, edited) == [
        {
            "insertText": {
                "objectId": "box",
                "insertionIndex": 2,
                "text": "X\nY\n",
            }
        },
        {
            "updateTextStyle": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 2,
                    "endIndex": 3,
                },
                "style": {
                    "foregroundColor": {
                        "opaqueColor": {
                            "rgbColor": {
                                "red": 0.0,
                                "green": 1.0,
                                "blue": 0.0,
                            }
                        }
                    }
                },
                "fields": "foregroundColor",
            }
        },
        {
            "updateTextStyle": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 4,
                    "endIndex": 5,
                },
                "style": {
                    "foregroundColor": {
                        "opaqueColor": {
                            "rgbColor": {
                                "red": 0.0,
                                "green": 1.0,
                                "blue": 0.0,
                            }
                        }
                    }
                },
                "fields": "foregroundColor",
            }
        },
    ]


def test_empty_inserted_paragraph_gets_explicit_paragraph_style() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="box">'
        '<P class="leading-150">A</P><P class="leading-100">B</P>'
        "</TextBox></Slide>"
    )
    edited = (
        '<Slide id="s1"><TextBox id="box">'
        '<P class="leading-150">A</P><P class="leading-150"></P>'
        '<P class="leading-100">B</P></TextBox></Slide>'
    )

    assert _requests(pristine, edited) == [
        {
            "insertText": {
                "objectId": "box",
                "insertionIndex": 2,
                "text": "\n",
            }
        },
        {
            "updateParagraphStyle": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 2,
                    "endIndex": 3,
                },
                "style": {"lineSpacing": 150.0},
                "fields": "lineSpacing",
            }
        },
    ]


def test_mixed_run_paragraph_element_scope_moves_share_one_edit_plan() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="box"><P class="leading-150">'
        '<T class="text-color-#00ff00">A</T>'
        '<T class="italic">B</T></P></TextBox></Slide>'
    )
    edited = (
        '<Slide id="s1"><TextBox id="box" class="leading-150"><P '
        'class="text-color-#00ff00"><T>A</T><T class="italic">B!</T>'
        "</P></TextBox></Slide>"
    )

    requests = _requests(pristine, edited)
    assert not any(
        request.get("updateTextStyle", {}).get("textRange") == {"type": "ALL"}
        for request in requests
    )
    assert not any(
        request.get("updateParagraphStyle", {}).get("textRange")
        == {"type": "ALL"}
        for request in requests
    )
    assert requests == [
        {
            "deleteText": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 0,
                    "endIndex": 2,
                },
            }
        },
        {
            "insertText": {
                "objectId": "box",
                "insertionIndex": 0,
                "text": "AB!",
            }
        },
        {
            "updateTextStyle": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 0,
                    "endIndex": 1,
                },
                "style": {
                    "foregroundColor": {
                        "opaqueColor": {
                            "rgbColor": {
                                "red": 0.0,
                                "green": 1.0,
                                "blue": 0.0,
                            }
                        }
                    }
                },
                "fields": "foregroundColor",
            }
        },
        {
            "updateTextStyle": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 0,
                    "endIndex": 1,
                },
                "style": {},
                "fields": "italic",
            }
        },
        {
            "updateTextStyle": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 1,
                    "endIndex": 3,
                },
                "style": {
                    "italic": True,
                    "foregroundColor": {
                        "opaqueColor": {
                            "rgbColor": {
                                "red": 0.0,
                                "green": 1.0,
                                "blue": 0.0,
                            }
                        }
                    }
                },
                "fields": "italic,foregroundColor",
            }
        },
    ]


def test_legitimate_reset_remains_fixed_range_and_clears_property() -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="box" class="text-color-#00ff00">'
        "<P>ABC</P></TextBox></Slide>"
    )
    edited = (
        '<Slide id="s1"><TextBox id="box"><P>ABX</P></TextBox></Slide>'
    )

    requests = _requests(pristine, edited)
    reset = [
        request["updateTextStyle"]
        for request in requests
        if "updateTextStyle" in request
        and request["updateTextStyle"]["style"] == {}
    ]
    assert reset == [
        {
            "objectId": "box",
            "textRange": {
                "type": "FIXED_RANGE",
                "startIndex": 0,
                "endIndex": 2,
            },
            "style": {},
            "fields": "foregroundColor",
        },
        {
            "objectId": "box",
            "textRange": {
                "type": "FIXED_RANGE",
                "startIndex": 2,
                "endIndex": 3,
            },
            "style": {},
            "fields": "foregroundColor",
        },
    ]
    assert all(update["textRange"]["type"] == "FIXED_RANGE" for update in reset)


@pytest.mark.parametrize(
    "class_name",
    [
        "text-size-24",
        "font-family-roboto",
        "font-weight-700",
        "bold font-weight-700",
        "italic",
        "underline",
        "line-through",
        "small-caps",
        "superscript",
        "bg-#ffff00",
        "leading-150",
    ],
)
def test_every_authored_text_property_class_uses_fixed_post_edit_updates(
    class_name: str,
) -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="box"><P class="'
        f'{class_name}">Heading</P><P>Body</P></TextBox></Slide>'
    )
    edited = (
        '<Slide id="s1"><TextBox id="box" class="'
        f'{class_name}"><P>Heading!</P><P>Body</P></TextBox></Slide>'
    )

    requests = _requests(pristine, edited)
    assert requests[0]["insertText"]["insertionIndex"] == 7
    style_updates = _style_requests(requests)
    assert style_updates
    assert all(
        request[operation]["textRange"]["type"] == "FIXED_RANGE"
        for request in style_updates
        for operation in ("updateTextStyle", "updateParagraphStyle")
        if operation in request
    )
    assert all(
        request[operation]["textRange"]["startIndex"] == 9
        for request in style_updates
        for operation in ("updateTextStyle", "updateParagraphStyle")
        if operation in request
    )


def test_fail_closed_guard_rejects_unmappable_mixed_scope_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pristine = (
        '<Slide id="s1"><TextBox id="box"><P class="text-color-#00ff00">'
        "Heading</P></TextBox></Slide>"
    )
    edited = (
        '<Slide id="s1"><TextBox id="box" class="text-color-#00ff00">'
        "<P>Heading!</P></TextBox></Slide>"
    )
    diff = diff_presentation(
        {"01": parse_slide_content(pristine)},
        {"01": parse_slide_content(edited)},
        {},
    )

    monkeypatch.setattr(
        "slidesmith.engine.content_requests._create_effective_style_requests",
        lambda *_args: None,
    )
    with pytest.raises(ValueError, match="Cannot safely plan.*fixed UTF-16"):
        generate_batch_requests(diff, {"box": "box"}, {"01": "slide"})


def test_request_plan_invariant_rejects_unsafe_broad_then_fixed_reset() -> None:
    edited = parse_slide_content(
        '<Slide><TextBox id="box" class="text-color-#00ff00"><P>AB</P>'
        "</TextBox></Slide>"
    )[0]
    diff = DiffResult(edited_elements={"box": edited})
    requests = [
        {
            "updateTextStyle": {
                "objectId": "box",
                "textRange": {"type": "ALL"},
                "style": {
                    "foregroundColor": {
                        "opaqueColor": {
                            "rgbColor": {
                                "red": 0.0,
                                "green": 1.0,
                                "blue": 0.0,
                            }
                        }
                    }
                },
                "fields": "foregroundColor",
            }
        },
        {
            "updateTextStyle": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 0,
                    "endIndex": 2,
                },
                "style": {},
                "fields": "foregroundColor",
            }
        },
    ]

    with pytest.raises(ValueError, match="Unsafe text request plan"):
        _assert_safe_text_request_order(requests, diff, {"box": "box"})


def test_fail_closed_guard_rejects_missing_style_snapshots() -> None:
    diff = DiffResult(
        changes=[
            Change(
                ChangeType.TEXT_UPDATE,
                "box",
                old_text=["old"],
                new_text=["new"],
            ),
            Change(
                ChangeType.STYLE_UPDATE,
                "box",
                new_styles=ElementStyles(text_style=TextStyle(bold=True)),
            ),
        ]
    )

    with pytest.raises(ValueError, match="pristine and edited text-style snapshots"):
        generate_batch_requests(diff, {"box": "box"}, {"01": "slide"})


def test_effective_ranges_are_in_new_utf16_space_after_insert() -> None:
    pristine = parse_slide_content(
        '<Slide><TextBox id="box"><P class="text-color-#00ff00">A😀B</P>'
        "</TextBox></Slide>"
    )[0]
    edited = parse_slide_content(
        '<Slide><TextBox id="box" class="text-color-#00ff00"><P>'
        "A😀XB</P></TextBox></Slide>"
    )[0]

    ranges = effective_text_style_ranges(pristine, edited)
    assert ranges is not None
    assert [(item.start, item.end) for item in ranges] == [
        (0, 3),
        (3, 4),
        (4, 5),
    ]


def test_link_property_is_planned_as_a_fixed_post_edit_update() -> None:
    pristine = ParsedElement(
        clean_id="box",
        tag="TextBox",
        paragraphs=["AB"],
        runs=[[ParsedRun("AB", TextStyle(link="https://old.example"))]],
    )
    edited = ParsedElement(
        clean_id="box",
        tag="TextBox",
        paragraphs=["AB!"],
        runs=[[ParsedRun("AB!", TextStyle(link="https://new.example"))]],
    )

    plan = _create_effective_style_requests("box", pristine, edited)
    assert plan is not None
    assert plan.requests == [
        {
            "updateTextStyle": {
                "objectId": "box",
                "textRange": {
                    "type": "FIXED_RANGE",
                    "startIndex": 0,
                    "endIndex": 3,
                },
                "style": {"link": {"url": "https://new.example"}},
                "fields": "link",
            }
        }
    ]
