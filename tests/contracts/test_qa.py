"""Visual QA contracts: thumbnail transport, offline lint, and CLI wiring."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
import pytest

from extraslide.qa import check_folder, lint_folder, record_qa_baseline
from extraslide.transport import GoogleSlidesTransport
from slidesmith import cli
from slidesmith.workspace import materialize

GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)


@pytest.fixture
def qa_folder(tmp_path: Path) -> Path:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    folder = materialize(data, tmp_path)
    _replace_slides(folder, "")
    return folder


def _replace_slides(folder: Path, first_slide_body: str) -> None:
    for index, content_path in enumerate(
        sorted((folder / "slides").glob("*/content.sml"))
    ):
        slide_id = ET.fromstring(content_path.read_text(encoding="utf-8")).get("id")
        body = first_slide_body if index == 0 else ""
        content_path.write_text(
            f'<Slide id="{slide_id}">{body}</Slide>',
            encoding="utf-8",
        )


def _rules(folder: Path) -> list[str]:
    return [finding.rule for finding in lint_folder(folder)]


async def test_get_page_thumbnail_fetches_metadata_then_png() -> None:
    requests: list[httpx.Request] = []
    png = b"\x89PNG\r\n\x1a\nthumbnail"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "slides.googleapis.com":
            return httpx.Response(
                200,
                json={"contentUrl": "https://thumbnail.example/rendered.png"},
            )
        assert request.url == httpx.URL("https://thumbnail.example/rendered.png")
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    transport = GoogleSlidesTransport("fake-token")
    await transport._client.aclose()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await transport.get_page_thumbnail("presentation-1", "page-2")
    finally:
        await transport.close()

    assert result == png
    assert len(requests) == 2
    assert requests[0].url.path == (
        "/v1/presentations/presentation-1/pages/page-2/thumbnail"
    )
    assert requests[0].url.params["thumbnailProperties.thumbnailSize"] == "LARGE"
    assert requests[0].url.params["thumbnailProperties.mimeType"] == "PNG"


def test_overlap_flags_sibling_leaves_over_threshold(qa_folder: Path) -> None:
    _replace_slides(
        qa_folder,
        '<Rect id="left" x="10" y="10" w="100" h="100" />'
        '<Rect id="right" x="90" y="10" w="100" h="100" />',
    )

    findings = [
        finding for finding in lint_folder(qa_folder) if finding.rule == "OVERLAP"
    ]

    assert len(findings) == 1
    assert findings[0].element_ids == ("left", "right")
    assert findings[0].slide_number == 1
    assert findings[0].severity == "WARNING"
    assert findings[0].suggested_fix


def test_overlap_allows_exactly_fifteen_percent(qa_folder: Path) -> None:
    _replace_slides(
        qa_folder,
        '<Rect id="left" x="10" y="10" w="100" h="100" />'
        '<Rect id="right" x="95" y="10" w="100" h="100" />',
    )

    assert "OVERLAP" not in _rules(qa_folder)


def test_out_of_bounds_flags_element_beyond_page(qa_folder: Path) -> None:
    _replace_slides(
        qa_folder,
        '<Rect id="outside" x="700" y="10" w="30" h="30" />',
    )

    findings = [
        finding for finding in lint_folder(qa_folder) if finding.rule == "OUT_OF_BOUNDS"
    ]

    assert len(findings) == 1
    assert findings[0].element_ids == ("outside",)
    assert "720 x 405" in findings[0].description


def test_out_of_bounds_allows_element_on_page_edge(qa_folder: Path) -> None:
    _replace_slides(
        qa_folder,
        '<Rect id="inside" x="690" y="375" w="30" h="30" />',
    )

    assert "OUT_OF_BOUNDS" not in _rules(qa_folder)


def test_text_overflow_flags_likely_overflow(qa_folder: Path) -> None:
    _replace_slides(
        qa_folder,
        '<TextBox id="copy" x="10" y="10" w="60" h="12" '
        'class="text-size-20"><P>This sentence wraps onto many lines.</P></TextBox>',
    )

    findings = [
        finding for finding in lint_folder(qa_folder) if finding.rule == "TEXT_OVERFLOW"
    ]

    assert len(findings) == 1
    assert findings[0].element_ids == ("copy",)
    assert "likely overflow (approximate measurement)" in findings[0].description


def test_text_overflow_allows_text_that_fits(qa_folder: Path) -> None:
    _replace_slides(
        qa_folder,
        '<TextBox id="copy" x="10" y="10" w="200" h="40" '
        'class="text-size-12"><P>Short text.</P></TextBox>',
    )

    assert "TEXT_OVERFLOW" not in _rules(qa_folder)


def test_check_folder_strict_exit_and_clean_bill(
    qa_folder: Path,
) -> None:
    output: list[str] = []
    assert check_folder(qa_folder, strict=True, output=output.append) == 0
    assert output == ["QA clean: no issues found."]

    _replace_slides(
        qa_folder,
        '<Rect id="outside" x="700" y="10" w="30" h="30" />',
    )
    assert check_folder(qa_folder, strict=False, output=lambda _: None) == 0
    assert check_folder(qa_folder, strict=True, output=lambda _: None) == 1


def test_check_labels_new_pre_existing_and_resolved_findings(
    qa_folder: Path,
) -> None:
    _replace_slides(
        qa_folder,
        '<Rect id="old" x="700" y="10" w="30" h="30" />',
    )
    baseline_path = record_qa_baseline(qa_folder)
    assert baseline_path == qa_folder / ".pristine" / "qa-baseline.json"

    output: list[str] = []
    check_folder(qa_folder, output=output.append)
    assert output[0] == "1 findings (0 new, 1 pre-existing, 0 resolved)"
    assert output[1].startswith("[PRE-EXISTING] [WARNING] OUT_OF_BOUNDS")

    _replace_slides(
        qa_folder,
        '<Rect id="new" x="10" y="10" w="100" h="100" />'
        '<Rect id="other" x="90" y="10" w="100" h="100" />',
    )
    output = []
    check_folder(qa_folder, output=output.append)
    assert output[0] == "1 findings (1 new, 0 pre-existing, 1 resolved)"
    assert any(line.startswith("[NEW] [WARNING] OVERLAP") for line in output)
    assert any(line.startswith("[RESOLVED] [WARNING] OUT_OF_BOUNDS") for line in output)


def test_cli_no_thumbnails_uses_no_auth_or_transport(
    qa_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("lint-only check must not use auth or transport")

    monkeypatch.setattr(cli, "_token", forbidden)
    monkeypatch.setattr("extraslide.transport.GoogleSlidesTransport", forbidden)

    cli.main(["check", str(qa_folder), "--no-thumbnails"])

    assert "no issues found" in capsys.readouterr().out
    assert not (qa_folder / ".qa").exists()


def test_cli_no_thumbnails_strict_exits_one_on_findings(
    qa_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _replace_slides(
        qa_folder,
        '<Rect id="outside" x="700" y="10" w="30" h="30" />',
    )

    def forbidden_auth(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("strict lint-only check must not authenticate")

    monkeypatch.setattr(cli, "_token", forbidden_auth)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["check", str(qa_folder), "--no-thumbnails", "--strict"])

    assert excinfo.value.code == 1


def test_cli_downloads_thumbnails_sequentially_and_prints_paths(
    qa_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    class FakeTransport:
        def __init__(self, token: str) -> None:
            assert token == "fake-token"

        async def get_page_thumbnail(
            self,
            presentation_id: str,
            page_object_id: str,
            size: str = "LARGE",
        ) -> bytes:
            assert presentation_id
            calls.append(page_object_id)
            return f"png:{page_object_id}".encode()

        async def close(self) -> None:
            pass

    def fake_token(command: str, target: str) -> str:
        assert command == "slide.pull"
        assert target
        return "fake-token"

    monkeypatch.setattr(cli, "_token", fake_token)
    monkeypatch.setattr("extraslide.transport.GoogleSlidesTransport", FakeTransport)

    cli.main(["check", str(qa_folder)])

    mapping = json.loads((qa_folder / "id_mapping.json").read_text(encoding="utf-8"))
    expected_page_ids = []
    for content_path in sorted((qa_folder / "slides").glob("*/content.sml")):
        expected_page_ids.append(
            mapping[ET.fromstring(content_path.read_text(encoding="utf-8")).get("id")]
        )
    assert calls == expected_page_ids

    output_lines = capsys.readouterr().out.splitlines()
    for content_path in sorted((qa_folder / "slides").glob("*/content.sml")):
        thumbnail = qa_folder / ".qa" / f"slide-{content_path.parent.name}.png"
        assert thumbnail.exists()
        assert str(thumbnail) in output_lines
