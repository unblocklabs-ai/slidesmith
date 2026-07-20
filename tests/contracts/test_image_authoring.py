"""Offline contracts for authored Image elements."""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from slidesmith.engine import content_diff
from slidesmith.engine import image_fetch
from slidesmith.engine.content_diff import Change, ChangeType, DiffResult, diff_presentation
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


@contextmanager
def _loopback_redirect_probe():
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            paths.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/probe")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
                b"\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xcf\xc0\x00\x00"
                b"\x03\x01\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
            )

        def log_message(self, format_string: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/redirect", paths
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


_ONE_PIXEL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    b"\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xcf\xc0\x00\x00"
    b"\x03\x01\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _FakeConnection:
    def close(self) -> None:
        pass


class _FakeResponse:
    status = 200

    def __init__(
        self,
        *,
        content_length: int | None = None,
        body: bytes = _ONE_PIXEL_PNG,
        streamed_bytes: int | None = None,
    ) -> None:
        self.headers = (
            {} if content_length is None else {"Content-Length": str(content_length)}
        )
        self.body = body
        self.streamed_bytes = streamed_bytes
        self.read_sizes: list[int | None] = []
        self.body_returned = False

    def getheader(self, name: str) -> str | None:
        return self.headers.get(name)

    def read(self, amount: int | None = None) -> bytes:
        self.read_sizes.append(amount)
        if self.streamed_bytes is None:
            if self.body_returned:
                return b""
            self.body_returned = True
            return self.body
        if self.streamed_bytes <= 0:
            return b""
        chunk_size = min(amount, self.streamed_bytes)
        self.streamed_bytes -= chunk_size
        return b"x" * chunk_size

    def close(self) -> None:
        pass


def _stub_fetch_response(
    monkeypatch: pytest.MonkeyPatch, response: _FakeResponse
) -> None:
    monkeypatch.setattr(
        image_fetch,
        "validate_public_image_url",
        lambda url, *, resolve_host: object(),
    )
    monkeypatch.setattr(
        image_fetch,
        "_open_response",
        lambda resolved: (_FakeConnection(), response),
    )


def test_contain_fetch_rejects_loopback_redirect_probe() -> None:
    with _loopback_redirect_probe() as (url, paths):
        with pytest.raises(ValueError, match="non-public"):
            content_diff._fetch_image_dimensions(url)

    assert paths == []


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "10.0.0.8", "169.254.169.254", "240.0.0.1"],
)
def test_parser_rejects_non_public_image_host(host: str) -> None:
    with pytest.raises(ValueError, match="non-public"):
        parse_slide_content(
            f'<Slide><Image id="hero" src="http://{host}/image.png" '
            'x="0" y="0" w="100" h="100"/></Slide>'
        )


def test_contain_fetch_rejects_oversized_content_length_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(content_length=25 * 1024 * 1024 + 1)
    _stub_fetch_response(monkeypatch, response)

    with pytest.raises(ValueError, match="25 MB"):
        image_fetch.fetch_image_dimensions("https://example.com/image.png")

    assert response.read_sizes == []


def test_contain_fetch_enforces_stream_ceiling_without_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(streamed_bytes=25 * 1024 * 1024 + 1)
    _stub_fetch_response(monkeypatch, response)

    with pytest.raises(ValueError, match="25 MB"):
        image_fetch.fetch_image_dimensions("https://example.com/image.png")


def test_contain_fetch_rejects_pixel_bomb_before_pillow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    width, height = 100_001, 1_001
    png_header = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )
    response = _FakeResponse(body=png_header)
    _stub_fetch_response(monkeypatch, response)
    monkeypatch.setattr(
        image_fetch.Image,
        "open",
        lambda data: pytest.fail("Pillow must not inspect over-limit dimensions"),
    )

    with pytest.raises(ValueError, match="100,000,000 pixels"):
        image_fetch.fetch_image_dimensions("https://example.com/image.png")


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
        "data:image/png;base64,AAAA",
        "ftp://example.com/hero.png",
        "https:///missing-host.png",
    ],
)
def test_parser_rejects_non_http_image_src_loudly(src: str) -> None:
    with pytest.raises(
        ValueError,
        match=r"Invalid src on Image element 'hero'",
    ):
        parse_slide_content(
            f'<Slide><Image id="hero" src="{src}" x="0" y="0" '
            'w="100" h="100"/></Slide>'
        )


@pytest.mark.parametrize("src", ["./assets/hero.png", "file:///tmp/hero.png"])
def test_parser_accepts_local_image_sources(src: str) -> None:
    image = parse_slide_content(
        f'<Slide><Image id="hero" src="{src}" x="1" y="2" '
        'w="100" h="100"/></Slide>'
    )[0]

    assert image.src == src
    assert image.fit == "stretch"


def test_parser_rejects_unknown_image_fit_loudly() -> None:
    with pytest.raises(
        ValueError,
        match="Invalid fit 'cover' on Image element 'hero'.*stretch.*contain",
    ):
        parse_slide_content(
            '<Slide><Image id="hero" src="https://example.com/hero.png" '
            'fit="cover" x="0" y="0" w="100" h="100"/></Slide>'
        )


@pytest.mark.parametrize(
    ("fit", "geometry"),
    [
        ("stretch", 'y="2" w="3" h="4"'),
        ("contain", 'x="1" y="0" w="3" h="4"'),
        ("stretch", 'x="1" y="2" w="-3" h="4"'),
        ("contain", 'x="1" y="2" w="3" h="nan"'),
        ("stretch", 'x="inf" y="2" w="3" h="4"'),
    ],
)
def test_authored_image_requires_finite_strictly_positive_geometry(
    fit: str,
    geometry: str,
) -> None:
    with pytest.raises(
        ValueError,
        match=r"Image element 'hero'.*finite, strictly-positive x/y/w/h",
    ):
        parse_slide_content(
            '<Slide><Image id="hero" src="https://example.com/image.png" '
            f'fit="{fit}" {geometry}/></Slide>'
        )


def test_image_request_factory_rejects_missing_authored_geometry() -> None:
    result = DiffResult(
        changes=[
            Change(
                change_type=ChangeType.CREATE,
                target_id="hero_image",
                slide_index="01",
                tag="Image",
                src="https://example.com/image.png",
            )
        ]
    )

    with pytest.raises(
        ValueError,
        match=r"Image element 'hero_image'.*finite, strictly-positive x/y/w/h",
    ):
        generate_batch_requests(result, {}, {"01": "slide_1"})


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
    properties = request["createImage"]["elementProperties"]
    assert properties["size"] == {
        "width": {"magnitude": pt_to_emu(expected_frame[2]), "unit": "EMU"},
        "height": {"magnitude": pt_to_emu(expected_frame[3]), "unit": "EMU"},
    }
    assert properties["transform"]["scaleX"] == 1
    assert properties["transform"]["scaleY"] == 1
    # An aspect-matched createImage box already pins the intended visual frame;
    # Google may refactor size/transform values but does not re-fit the geometry.
    assert list(request) == ["createImage"]


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
