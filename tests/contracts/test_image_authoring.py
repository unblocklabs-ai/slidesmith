"""Offline contracts for authored Image elements."""

from __future__ import annotations

import pytest

from slidesmith.engine import content_diff
from slidesmith.engine.content_diff import ChangeType, diff_presentation
from slidesmith.engine.content_parser import parse_slide_content
from slidesmith.engine.content_requests import generate_batch_requests
from slidesmith.engine.units import pt_to_emu


def _diff(sml: str):
    return diff_presentation(
        {},
        {"01": parse_slide_content(sml)},
        {},
        {},
    )


def _visual_geometry(request: dict) -> tuple[int, int, int, int]:
    properties = request["createImage"]["elementProperties"]
    size = properties["size"]
    transform = properties["transform"]
    return (
        transform["translateX"],
        transform["translateY"],
        round(transform["scaleX"] * size["width"]["magnitude"]),
        round(transform["scaleY"] * size["height"]["magnitude"]),
    )


def test_parser_accepts_image_src_and_fit_without_changing_pulled_images() -> None:
    authored, defaulted, pulled = parse_slide_content(
        """<Slide>
          <Image id="contained" src="https://example.com/hero.png"
                 fit="contain" x="1" y="2" w="3" h="4"/>
          <Image id="stretched" src="http://example.com/photo.jpg"
                 x="5" y="6" w="7" h="8"/>
          <Image id="pulled" x="9" y="10" w="11" h="12"/>
        </Slide>"""
    )

    assert (authored.src, authored.fit) == (
        "https://example.com/hero.png",
        "contain",
    )
    assert (defaulted.src, defaulted.fit) == (
        "http://example.com/photo.jpg",
        "stretch",
    )
    assert (pulled.src, pulled.fit) == (None, None)


@pytest.mark.parametrize(
    "src",
    [
        "file:///tmp/hero.png",
        "data:image/png;base64,AAAA",
        "ftp://example.com/hero.png",
        "https:///missing-host.png",
    ],
)
def test_parser_rejects_non_http_image_src_loudly(src: str) -> None:
    with pytest.raises(
        ValueError,
        match=r"Invalid src on Image element 'hero'.*expected an http\(s\) URL",
    ):
        parse_slide_content(
            f'<Slide><Image id="hero" src="{src}" x="0" y="0" '
            'w="100" h="100"/></Slide>'
        )


def test_parser_rejects_unknown_image_fit_loudly() -> None:
    with pytest.raises(
        ValueError,
        match="Invalid fit 'cover' on Image element 'hero'.*stretch.*contain",
    ):
        parse_slide_content(
            '<Slide><Image id="hero" src="https://example.com/hero.png" '
            'fit="cover" x="0" y="0" w="100" h="100"/></Slide>'
        )


def test_create_diff_and_request_carry_url_fit_and_authored_emu_geometry() -> None:
    result = _diff(
        '<Slide><Image id="hero_image" src="https://example.com/hero.png" '
        'x="12.5" y="20" w="160" h="90"/></Slide>'
    )

    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.change_type == ChangeType.CREATE
    assert change.src == "https://example.com/hero.png"
    assert change.fit == "stretch"

    requests = generate_batch_requests(result, {}, {"01": "slide_1"})
    assert len(requests) == 1
    assert requests[0]["createImage"]["url"] == "https://example.com/hero.png"
    assert _visual_geometry(requests[0]) == (
        pt_to_emu(12.5),
        pt_to_emu(20),
        pt_to_emu(160),
        pt_to_emu(90),
    )


@pytest.mark.parametrize(
    ("pixels", "expected_frame"),
    [
        ((400, 200), (10, 20, 200, 100)),
        ((200, 400), (10, 20, 50, 100)),
    ],
)
def test_contain_uses_stubbed_dimensions_and_anchors_top_left(
    monkeypatch: pytest.MonkeyPatch,
    pixels: tuple[int, int],
    expected_frame: tuple[float, float, float, float],
) -> None:
    calls: list[str] = []

    def stub_dimensions(url: str) -> tuple[int, int]:
        calls.append(url)
        return pixels

    monkeypatch.setattr(content_diff, "_fetch_image_dimensions", stub_dimensions)
    result = _diff(
        '<Slide><Image id="hero_image" src="https://example.com/hero.png" '
        'fit="contain" x="10" y="20" w="200" h="100"/></Slide>'
    )

    change = result.changes[0]
    assert change.new_position == dict(
        zip(("x", "y", "w", "h"), expected_frame, strict=True)
    )
    assert change.fit == "contain"
    assert calls == ["https://example.com/hero.png"]

    request = generate_batch_requests(result, {}, {"01": "slide_1"})[0]
    assert _visual_geometry(request) == tuple(
        pt_to_emu(value) for value in expected_frame
    )


def test_image_inside_stack_uses_container_position_and_flex_size() -> None:
    image = parse_slide_content(
        """<Slide><Stack direction="row" x="10" y="20" w="160" h="60"
            gap="10" padding="5" align="stretch">
          <Rect id="fixed" w="40"/>
          <Image id="hero" src="https://example.com/hero.png" flex="1"/>
        </Stack></Slide>"""
    )[1]

    assert (image.x, image.y, image.w, image.h) == (65.0, 25.0, 100.0, 50.0)
    assert (image.src, image.fit) == (
        "https://example.com/hero.png",
        "stretch",
    )
