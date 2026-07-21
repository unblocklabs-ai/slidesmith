"""Visual QA contracts: thumbnail transport, offline lint, and CLI wiring."""

from __future__ import annotations

import copy
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from slidesmith import cli
from slidesmith.engine import content_diff
from slidesmith.engine import qa as qa_engine
from slidesmith.engine.client import SlidesClient, diff_folder
from slidesmith.engine.qa import (
    CONTACT_SHEET_GAP,
    CONTACT_SHEET_LABEL_HEIGHT,
    CONTACT_SHEET_PADDING,
    check_folder,
    create_contact_sheet,
    finding_id,
    lint_folder,
    push_preflight,
    record_qa_baseline,
)
from slidesmith.engine.transport import (
    GoogleSlidesTransport,
    PresentationData,
    Transport,
)
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


def test_check_help_documents_overlap_suppression(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["check", "--help"])

    help_text = capsys.readouterr().out
    assert "qa-accept-overlap" in help_text
    assert "90%" in help_text
    assert "backgrounds" in help_text


async def test_download_thumbnails_routes_paths_through_output_callback(
    qa_folder: Path,
) -> None:
    class FakeTransport:
        async def get_page_thumbnail(
            self,
            _presentation_id: str,
            page_object_id: str,
        ) -> bytes:
            return f"png:{page_object_id}".encode()

    qa_dir = qa_folder / ".qa"
    qa_dir.mkdir()
    messages: list[str] = []

    paths = await qa_engine.download_thumbnails(
        FakeTransport(), qa_folder, qa_dir, output=messages.append
    )

    assert messages == [str(path) for path in paths]


async def test_get_page_thumbnail_fetches_metadata_then_png() -> None:
    requests: list[httpx.Request] = []
    png = b"\x89PNG\r\n\x1a\nthumbnail"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "slides.googleapis.com":
            return httpx.Response(
                200,
                json={
                    "contentUrl": "https://lh3.googleusercontent.com/rendered.png"
                },
            )
        assert request.url == httpx.URL(
            "https://lh3.googleusercontent.com/rendered.png"
        )
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    transport = GoogleSlidesTransport("fake-token")
    await transport._client.aclose()
    await transport._thumbnail_client.aclose()
    transport._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer fake-token"},
    )
    transport._thumbnail_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
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


def test_preflight_uses_contain_image_effective_box_for_overlap(
    qa_folder: Path,
) -> None:
    assets = qa_folder / "assets"
    assets.mkdir()
    Image.new("RGB", (300, 100)).save(assets / "wide.png")
    _replace_slides(
        qa_folder,
        '<Image id="hero" src="./assets/wide.png" fit="contain" '
        'x="10" y="10" w="120" h="90" />'
        '<Rect id="neighbor" x="100" y="65" w="60" h="20" />',
    )
    output: list[str] = []

    active = push_preflight(qa_folder, output=output.append)

    assert active == 0
    assert not any("OVERLAP" in line for line in output)


def test_preflight_warns_for_genuine_contain_effective_box_overlap(
    qa_folder: Path,
) -> None:
    assets = qa_folder / "assets"
    assets.mkdir()
    Image.new("RGB", (300, 100)).save(assets / "wide.png")
    _replace_slides(
        qa_folder,
        '<Image id="hero" src="./assets/wide.png" fit="contain" '
        'x="10" y="10" w="120" h="90" />'
        '<Rect id="neighbor" x="100" y="40" w="60" h="30" />',
    )
    output: list[str] = []

    active = push_preflight(qa_folder, output=output.append)

    assert active == 1
    assert any("OVERLAP" in line for line in output)


def test_overlap_allows_exactly_fifteen_percent(qa_folder: Path) -> None:
    _replace_slides(
        qa_folder,
        '<Rect id="left" x="10" y="10" w="100" h="100" />'
        '<Rect id="right" x="95" y="10" w="100" h="100" />',
    )

    assert "OVERLAP" not in _rules(qa_folder)


def test_overlap_ignores_partially_overlapping_background_card(
    qa_folder: Path,
) -> None:
    _replace_slides(
        qa_folder,
        '<Rect id="background" x="0" y="0" w="710" h="405" />'
        '<Rect id="card" x="705" y="100" w="15" h="100" />',
    )

    assert "OVERLAP" not in _rules(qa_folder)


def test_overlap_ignores_ninety_six_percent_contained_sibling(
    qa_folder: Path,
) -> None:
    _replace_slides(
        qa_folder,
        '<Rect id="outer" x="100" y="100" w="100" h="100" />'
        '<Rect id="inner" x="96" y="100" w="100" h="100" />',
    )

    assert "OVERLAP" not in _rules(qa_folder)


def test_overlap_still_flags_genuine_forty_percent_overlap(qa_folder: Path) -> None:
    _replace_slides(
        qa_folder,
        '<Rect id="left" x="10" y="10" w="100" h="100" />'
        '<Rect id="right" x="70" y="10" w="100" h="100" />',
    )

    assert [finding.rule for finding in lint_folder(qa_folder)] == ["OVERLAP"]


def test_overlap_ignores_partially_overlapping_scrim_over_photo(
    qa_folder: Path,
) -> None:
    _replace_slides(
        qa_folder,
        '<Image id="photo" x="0" y="0" w="710" h="405" />'
        '<Rect id="scrim" x="705" y="100" w="15" h="100" />',
    )

    assert "OVERLAP" not in _rules(qa_folder)


def test_overlap_ignores_line_crossing_content_box(qa_folder: Path) -> None:
    _replace_slides(
        qa_folder,
        '<Rect id="content" x="50" y="20" w="100" h="60" />'
        '<Line id="divider" x="0" y="50" w="200" h="1" />',
    )

    assert "OVERLAP" not in _rules(qa_folder)


def test_overlap_recurses_into_group_children(qa_folder: Path) -> None:
    _replace_slides(
        qa_folder,
        '<Group id="cards">'
        '<Rect id="left" x="10" y="10" w="100" h="100" />'
        '<Rect id="right" x="90" y="10" w="100" h="100" />'
        "</Group>",
    )

    overlaps = [
        finding for finding in lint_folder(qa_folder) if finding.rule == "OVERLAP"
    ]

    assert [finding.element_ids for finding in overlaps] == [("left", "right")]


@pytest.mark.parametrize("zero_dimension", ['w="0" h="100"', 'w="100" h="0"'])
def test_overlap_skips_zero_area_elements(
    qa_folder: Path, zero_dimension: str
) -> None:
    _replace_slides(
        qa_folder,
        f'<Rect id="zero" x="20" y="20" {zero_dimension} />'
        '<Rect id="content" x="10" y="10" w="100" h="100" />',
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


def test_text_overflow_flags_large_title_shorter_than_true_line(qa_folder: Path) -> None:
    _replace_slides(
        qa_folder,
        '<TextBox id="title" x="40" y="40" w="350" h="20.736" '
        'class="font-family-arial text-size-40"><P>Launch night</P></TextBox>',
    )

    assert "TEXT_OVERFLOW" in _rules(qa_folder)


def test_text_overflow_allows_single_line_large_title_with_metric_margin(
    qa_folder: Path,
) -> None:
    _replace_slides(
        qa_folder,
        '<TextBox id="title" x="40" y="40" w="350" h="52" '
        'class="font-family-arial text-size-40"><P>Launch night</P></TextBox>',
    )

    assert "TEXT_OVERFLOW" not in _rules(qa_folder)


def test_text_overflow_still_flags_clearly_overflowing_multiline_paragraph(
    qa_folder: Path,
) -> None:
    _replace_slides(
        qa_folder,
        '<TextBox id="body" x="40" y="40" w="140" h="30" '
        'class="font-family-arial text-size-24"><P>This paragraph has enough '
        'words to wrap across several lines and clearly overflow its short box.</P></TextBox>',
    )

    assert "TEXT_OVERFLOW" in _rules(qa_folder)


def test_text_overflow_allows_marginal_unknown_display_font_title(
    qa_folder: Path,
) -> None:
    _replace_slides(
        qa_folder,
        '<TextBox id="display_title" x="40" y="40" w="200" h="52" '
        'class="font-family-display-unknown text-size-40"><P>Launch night</P></TextBox>',
    )

    assert "TEXT_OVERFLOW" not in _rules(qa_folder)


def test_text_overflow_with_nonpositive_width_is_unbounded(
    qa_folder: Path,
) -> None:
    class UnexpectedMeasurer:
        def measure_wrapped_height(self, *_args: Any, **_kwargs: Any) -> float:
            raise AssertionError("zero-width text must not be measured")

    _replace_slides(
        qa_folder,
        '<TextBox id="copy" x="10" y="10" w="0" h="40">'
        "<P>Visible text.</P></TextBox>",
    )

    findings = [
        finding
        for finding in lint_folder(qa_folder, text_measurer=UnexpectedMeasurer())
        if finding.rule == "TEXT_OVERFLOW"
    ]

    assert len(findings) == 1
    assert findings[0].element_ids == ("copy",)
    assert "unbounded amount" in findings[0].description


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


def test_check_folder_rejects_nonlist_qa_baseline(qa_folder: Path) -> None:
    baseline_path = qa_folder / ".pristine" / "qa-baseline.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text('{"findings": {}}', encoding="utf-8")

    with pytest.raises(ValueError, match="Expected a findings list"):
        check_folder(qa_folder)


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
    assert output[0] == (
        "1 findings (0 new, 1 pre-existing, 0 resolved; NEW = since last pull)"
    )
    assert output[1].startswith("[PRE-EXISTING] [WARNING] OUT_OF_BOUNDS")

    _replace_slides(
        qa_folder,
        '<Rect id="new" x="10" y="10" w="100" h="100" />'
        '<Rect id="other" x="90" y="10" w="100" h="100" />',
    )
    output = []
    check_folder(qa_folder, output=output.append)
    assert output[0] == (
        "1 findings (1 new, 0 pre-existing, 1 resolved; NEW = since last pull)"
    )
    assert any(line.startswith("[NEW] [WARNING] OVERLAP") for line in output)
    assert any(line.startswith("[RESOLVED] [WARNING] OUT_OF_BOUNDS") for line in output)


def test_push_preflight_block_aborts_before_auth_on_new_overflow(
    qa_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record_qa_baseline(qa_folder)
    _replace_slides(
        qa_folder,
        '<Rect id="new_overflow" x="700" y="10" w="30" h="30" />',
    )
    monkeypatch.setattr(
        cli,
        "_token",
        lambda *_args: pytest.fail("blocked preflight must not authenticate"),
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["push", str(qa_folder), "--preflight=block"])

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "[NEW] [WARNING] OUT_OF_BOUNDS" in captured.err
    assert "push preflight blocked: 1 new finding(s)" in captured.err


def test_push_preflight_warn_reports_and_proceeds_with_per_slide(
    qa_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record_qa_baseline(qa_folder)
    _replace_slides(
        qa_folder,
        '<Rect id="new_overflow" x="700" y="10" w="30" h="30" />',
    )
    runs: list[None] = []

    def record_run(coroutine: Any) -> None:
        coroutine.close()
        runs.append(None)

    monkeypatch.setattr(cli, "_warn_if_stale", lambda _folder: None)
    monkeypatch.setattr(cli, "_token", lambda *_args: "token")
    monkeypatch.setattr(cli.asyncio, "run", record_run)

    cli.main(
        ["push", str(qa_folder), "--per-slide", "--preflight=warn"]
    )

    assert runs == [None]
    captured = capsys.readouterr()
    assert "[NEW] [WARNING] OUT_OF_BOUNDS" in captured.err
    assert "warning: push preflight: 1 new finding(s); proceeding" in captured.err


def test_cli_accept_and_unaccept_round_trip_through_workspace_sidecar(
    qa_folder: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _replace_slides(
        qa_folder,
        '<Rect id="outside" x="700" y="10" w="30" h="30" />',
    )
    identity = "OUT_OF_BOUNDS:1:outside"

    cli.main(
        ["check", str(qa_folder), "--no-thumbnails", "--accept", identity]
    )

    accepted_path = qa_folder / ".qa" / "accepted.json"
    assert json.loads(accepted_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "accepted": {
            identity: {
                "rule": "OUT_OF_BOUNDS",
                "slide": 1,
                "elementIds": ["outside"],
            }
        },
    }
    assert "[ACCEPTED]" in capsys.readouterr().out

    cli.main(
        ["check", str(qa_folder), "--no-thumbnails", "--unaccept", identity]
    )

    assert json.loads(accepted_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "accepted": {},
    }
    output = capsys.readouterr().out
    assert "[ACCEPTED]" not in output
    assert "QA found 1 issue(s)" in output


def test_accepted_identity_survives_finding_and_element_order_changes(
    qa_folder: Path,
) -> None:
    first = '<Rect id="left" x="10" y="10" w="100" h="100" />'
    second = '<Rect id="right" x="90" y="10" w="100" h="100" />'
    _replace_slides(qa_folder, first + second)
    finding = lint_folder(qa_folder)[0]
    assert finding_id(finding) == "OVERLAP:1:left,right"
    assert check_folder(qa_folder, accept=[finding_id(finding)]) == 0

    _replace_slides(qa_folder, second + first)
    reordered = lint_folder(qa_folder)[0]
    assert reordered.element_ids == ("right", "left")
    assert finding_id(reordered) == finding_id(finding)

    output: list[str] = []
    assert check_folder(qa_folder, strict=True, output=output.append) == 0
    assert any(line.startswith("[ACCEPTED]") for line in output)
    assert output[-1] == "QA accepted: 1 finding(s)."


def test_qa_accept_class_creates_sidecar_and_never_reaches_api_requests(
    qa_folder: Path,
) -> None:
    _replace_slides(
        qa_folder,
        '<Rect id="outside" x="700" y="10" w="30" h="30" '
        'class="qa-accept-out-of-bounds" />',
    )

    output: list[str] = []
    assert check_folder(qa_folder, strict=True, output=output.append) == 0

    identity = "OUT_OF_BOUNDS:1:outside"
    accepted = json.loads(
        (qa_folder / ".qa" / "accepted.json").read_text(encoding="utf-8")
    )
    assert identity in accepted["accepted"]
    assert any(line.startswith("[ACCEPTED]") for line in output)

    requests = SlidesClient().diff(qa_folder)
    assert requests
    assert "qa-accept-" not in json.dumps(requests)


def _qa_cycle5_presentation() -> dict[str, Any]:
    def rect(object_id: str, x: float, y: float, w: float, h: float) -> dict[str, Any]:
        return {
            "objectId": object_id,
            "size": {
                "width": {"magnitude": w * 12700, "unit": "EMU"},
                "height": {"magnitude": h * 12700, "unit": "EMU"},
            },
            "transform": {
                "scaleX": 1,
                "scaleY": 1,
                "translateX": x * 12700,
                "translateY": y * 12700,
                "unit": "EMU",
            },
            "shape": {"shapeType": "RECTANGLE"},
        }

    return {
        "presentationId": "qa-cycle5",
        "revisionId": "rev-pull",
        "title": "QA cycle 5",
        "pageSize": {
            "width": {"magnitude": 720 * 12700, "unit": "EMU"},
            "height": {"magnitude": 405 * 12700, "unit": "EMU"},
        },
        "slides": [
            {
                "objectId": "google_slide",
                "pageElements": [
                    rect("shape_a", 10, 20, 100, 100),
                    rect("shape_b", 200, 20, 100, 100),
                    rect("old_oob", 700, 300, 40, 40),
                ],
            }
        ],
    }


class _QaCycle5Transport(Transport):
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.batch_calls = 0

    async def get_presentation(self, presentation_id: str) -> PresentationData:
        return PresentationData(presentation_id, copy.deepcopy(self.data))

    async def batch_update(
        self,
        presentation_id: str,
        requests: list[dict[str, Any]],
        required_revision_id: str | None = None,
    ) -> dict[str, Any]:
        self.batch_calls += 1
        for request in requests:
            update = request.get("updatePageElementTransform")
            if update is None:
                continue
            for element in self.data["slides"][0]["pageElements"]:
                if element["objectId"] == update["objectId"]:
                    element["transform"] = copy.deepcopy(update["transform"])
                    break
        self.data["revisionId"] = f"rev-after-push-{self.batch_calls}"
        return {"replies": [{}] * len(requests)}

    async def close(self) -> None:
        pass


async def test_push_refresh_keeps_new_qa_findings_new_until_next_pull(
    tmp_path: Path,
) -> None:
    data = _qa_cycle5_presentation()
    transport = _QaCycle5Transport(data)
    client = SlidesClient(transport)
    await client.pull(data["presentationId"], tmp_path, save_raw=False)
    folder = tmp_path / data["presentationId"]

    baseline_path = folder / ".pristine" / "qa-baseline.json"
    baseline_before_push = baseline_path.read_bytes()
    baseline = json.loads(baseline_before_push)["findings"]
    assert len(baseline) == 1
    assert baseline[0]["rule"] == "OUT_OF_BOUNDS"

    sml = folder / "slides" / "01" / "content.sml"
    content = sml.read_text(encoding="utf-8")
    content = re.sub(
        r'(id="shape_a" x=")10(?:\.0)?(")',
        r'\g<1>170\2',
        content,
        count=1,
    )
    sml.write_text(content, encoding="utf-8")

    before_push: list[str] = []
    check_folder(folder, output=before_push.append)
    assert any(
        line.startswith("[NEW] [WARNING] OVERLAP") for line in before_push
    )

    await client.push(folder)

    assert baseline_path.read_bytes() == baseline_before_push
    after_push: list[str] = []
    check_folder(folder, output=after_push.append)
    assert any(
        line.startswith("[NEW] [WARNING] OVERLAP") for line in after_push
    )

    await client.pull(data["presentationId"], tmp_path, save_raw=False)
    after_pull: list[str] = []
    check_folder(folder, output=after_pull.append)
    assert not any(line.startswith("[NEW]") for line in after_pull)
    assert any(
        line.startswith("[PRE-EXISTING] [WARNING] OVERLAP")
        for line in after_pull
    )


def test_cli_no_thumbnails_uses_no_auth_or_transport(
    qa_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("lint-only check must not use auth or transport")

    monkeypatch.setattr(cli, "_token", forbidden)
    monkeypatch.setattr("slidesmith.engine.transport.GoogleSlidesTransport", forbidden)

    cli.main(["check", str(qa_folder), "--no-thumbnails"])

    assert "no issues found" in capsys.readouterr().out
    assert not (qa_folder / ".qa").exists()


def test_cli_no_thumbnails_does_not_fetch_remote_contain_dimensions(
    qa_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _replace_slides(
        qa_folder,
        '<Image id="hero" src="https://example.com/hero.png" fit="contain" '
        'x="10" y="20" w="120" h="90" />',
    )
    calls: list[str] = []

    def record_fetch(url: str) -> tuple[int, int]:
        calls.append(url)
        return (400, 200)

    monkeypatch.setattr(content_diff, "fetch_image_dimensions", record_fetch)

    cli.main(["check", str(qa_folder), "--no-thumbnails"])

    assert calls == []


def test_diff_does_not_fetch_remote_contain_dimensions(
    qa_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _replace_slides(
        qa_folder,
        '<Image id="hero" src="https://example.com/hero.png" fit="contain" '
        'x="10" y="20" w="120" h="90" />',
    )
    calls: list[str] = []

    def record_fetch(url: str) -> tuple[int, int]:
        calls.append(url)
        return (400, 200)

    monkeypatch.setattr(content_diff, "fetch_image_dimensions", record_fetch)

    requests = diff_folder(qa_folder)

    assert calls == []
    create = next(request["createImage"] for request in requests if "createImage" in request)
    assert create["elementProperties"]["size"] == {
        "width": {"magnitude": 1_524_000, "unit": "EMU"},
        "height": {"magnitude": 1_143_000, "unit": "EMU"},
    }


def test_push_preflight_does_not_fetch_remote_contain_dimensions(
    qa_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _replace_slides(
        qa_folder,
        '<Image id="hero" src="https://example.com/hero.png" fit="contain" '
        'x="10" y="20" w="120" h="90" />',
    )
    calls: list[str] = []

    def record_fetch(url: str) -> tuple[int, int]:
        calls.append(url)
        return (400, 200)

    monkeypatch.setattr(content_diff, "fetch_image_dimensions", record_fetch)

    push_preflight(qa_folder, output=lambda _message: None)

    assert calls == []


def test_contact_sheet_dimensions_and_slide_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qa_dir = tmp_path / ".qa"
    qa_dir.mkdir()
    for slide_number, size, color in (
        (1, (100, 50), "red"),
        (2, (80, 60), "green"),
        (3, (120, 40), "blue"),
    ):
        Image.new("RGB", size, color).save(qa_dir / f"slide-{slide_number:02}.png")

    labels: list[str] = []
    real_draw = qa_engine.ImageDraw.Draw

    def recording_draw(image: Image.Image) -> Any:
        delegate = real_draw(image)

        class DrawRecorder:
            def text(self, position: tuple[int, int], label: str, **kwargs: Any) -> Any:
                labels.append(label)
                return delegate.text(position, label, **kwargs)

        return DrawRecorder()

    monkeypatch.setattr(qa_engine.ImageDraw, "Draw", recording_draw)

    output_path = create_contact_sheet(qa_dir)

    cell_width = 120 + 2 * CONTACT_SHEET_PADDING
    cell_height = 60 + CONTACT_SHEET_LABEL_HEIGHT + 2 * CONTACT_SHEET_PADDING
    with Image.open(output_path) as sheet:
        assert sheet.size == (
            2 * cell_width + CONTACT_SHEET_GAP,
            2 * cell_height + CONTACT_SHEET_GAP,
        )
    assert output_path == qa_dir / "contact-sheet.png"
    assert labels == ["Slide 1", "Slide 2", "Slide 3"]


def test_cli_contact_sheet_with_no_thumbnails_fails_gracefully(
    qa_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("invalid flag combination must fail before auth")

    monkeypatch.setattr(cli, "_token", forbidden)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            ["check", str(qa_folder), "--no-thumbnails", "--contact-sheet"]
        )

    assert excinfo.value.code == 1
    assert capsys.readouterr().err == (
        "error: --contact-sheet requires thumbnail downloads; "
        "remove --no-thumbnails\n"
    )
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

    original_download_thumbnails = qa_engine.download_thumbnails

    async def record_output_callback(
        transport: Any,
        folder: Path,
        qa_dir: Path,
        *,
        output: Any,
    ) -> list[Path]:
        assert output is print
        return await original_download_thumbnails(
            transport, folder, qa_dir, output=output
        )

    monkeypatch.setattr(cli, "_token", fake_token)
    monkeypatch.setattr("slidesmith.engine.transport.GoogleSlidesTransport", FakeTransport)
    monkeypatch.setattr(qa_engine, "download_thumbnails", record_output_callback)

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
