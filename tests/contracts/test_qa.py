"""Visual QA contracts: thumbnail transport, offline lint, and CLI wiring."""

from __future__ import annotations

import copy
import json
import re
import shutil
from types import SimpleNamespace
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from slidesmith import cli
from slidesmith.engine import content_diff
from slidesmith.engine import qa as qa_engine
from slidesmith.engine.client import (
    SlidesClient,
    diff_folder,
    diff_folder_with_result,
)
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


@pytest.fixture
def clean_qa_folder(tmp_path: Path) -> Path:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return materialize(data, tmp_path)


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


def _write_slide_body(folder: Path, slide_number: int, body: str) -> None:
    content_path = folder / "slides" / f"{slide_number:02d}" / "content.sml"
    slide_id = ET.fromstring(content_path.read_text(encoding="utf-8")).get("id")
    content_path.write_text(
        f'<Slide id="{slide_id}">{body}</Slide>', encoding="utf-8"
    )


def _write_idless_slide(folder: Path, slide_number: int, body: str) -> None:
    content_path = folder / "slides" / f"{slide_number:02d}" / "content.sml"
    content_path.write_text(f"<Slide>{body}</Slide>", encoding="utf-8")


def _reindex_slides(folder: Path, contents: list[str]) -> None:
    slides_dir = folder / "slides"
    shutil.rmtree(slides_dir)
    for index, content in enumerate(contents, 1):
        slide_dir = slides_dir / f"{index:02d}"
        slide_dir.mkdir(parents=True)
        (slide_dir / "content.sml").write_text(content, encoding="utf-8")


def _slide_contents(folder: Path) -> list[str]:
    return [
        path.read_text(encoding="utf-8")
        for path in sorted((folder / "slides").glob("*/content.sml"))
    ]


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
    assert "thumbnails always reflect the remote deck" in help_text


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


def test_overlap_uses_paragraph_and_content_alignment_for_text_ink(
    qa_folder: Path,
) -> None:
    _replace_slides(
        qa_folder,
        '<TextBox id="aligned" x="100" y="100" w="180" h="100" '
        'class="content-align-bottom text-size-12">'
        '<P class="text-align-right">Hi</P></TextBox>'
        '<Image id="image" x="265" y="180" w="20" h="10" />',
    )
    styles_path = qa_folder / "styles.json"
    styles = json.loads(styles_path.read_text(encoding="utf-8"))
    styles["aligned"] = {
        "contentAlignment": "TOP",
        "text": {"paragraphs": [{"style": {"alignment": "START"}}]},
    }
    styles_path.write_text(json.dumps(styles, indent=2) + "\n", encoding="utf-8")

    findings = [finding for finding in lint_folder(qa_folder) if finding.rule == "OVERLAP"]
    assert [finding.element_ids for finding in findings] == [("aligned", "image")]

    _replace_slides(
        qa_folder,
        '<TextBox id="aligned" x="100" y="100" w="180" h="100" '
        'class="text-size-12"><P>Hi</P></TextBox>'
        '<Image id="image" x="265" y="180" w="20" h="10" />',
    )
    assert "OVERLAP" not in _rules(qa_folder)


def test_overlap_maps_rtl_end_to_physical_start(qa_folder: Path) -> None:
    _replace_slides(
        qa_folder,
        '<TextBox id="rtl" x="100" y="100" w="180" h="100" '
        'class="content-align-bottom text-size-12">'
        '<P class="text-align-right dir-rtl">Hi</P></TextBox>'
        '<Image id="image" x="105" y="180" w="20" h="10" />',
    )

    findings = [finding for finding in lint_folder(qa_folder) if finding.rule == "OVERLAP"]
    assert [finding.element_ids for finding in findings] == [("rtl", "image")]


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


def test_text_measurement_calibration_corpus(tmp_path: Path) -> None:
    """Pin the geometry estimator against a small visual-ground-truth table."""
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = [
        (
            "mixed_paragraph_card",
            '<TextBox id="card" x="20" y="20" w="300" h="100" '
            'class="text-size-12">'
            '<P class="leading-100 space-below-4"><T class="text-size-24 bold">TITLE</T></P>'
            '<P class="leading-90"><T class="text-size-10">This body copy is intentionally long enough to wrap once.</T></P>'
            "</TextBox>",
            {},
            (),
        ),
        (
            "genuine_overflow",
            '<TextBox id="overflow" x="20" y="20" w="100" h="50" '
            'class="text-size-12"><P>This sentence is deliberately long and must wrap across many lines in this narrow box.</P></TextBox>',
            {},
            ("TEXT_OVERFLOW",),
        ),
        (
            "word_wrap_21pt_something",
            '<TextBox id="wrap21" x="20" y="20" w="21" h="7" '
            'class="text-size-2.5"><P>something something something</P></TextBox>',
            {
                "textInsets": {"left": 0, "top": 0, "right": 0, "bottom": 0}
            },
            ("TEXT_OVERFLOW",),
        ),
        (
            "text_ink_avoids_empty_half_image",
            '<TextBox id="copy" x="20" y="20" w="300" h="100" '
            'class="text-size-12"><P>Hi</P></TextBox>'
            '<Image id="image" x="200" y="20" w="100" h="120" />',
            {},
            (),
        ),
        (
            "thin_filled_rect_crosses_ink",
            '<TextBox id="copy" x="50" y="20" w="100" h="60" '
            'class="text-size-12"><P>Hi</P></TextBox>'
            '<Rect id="rule" x="55" y="30" w="30" h="2" class="fill-#222222" />',
            {},
            ("OVERLAP",),
        ),
        (
            "thin_filled_rect_only_empty_raw_bounds",
            '<TextBox id="copy" x="50" y="20" w="100" h="60" '
            'class="text-size-12"><P>Hi</P></TextBox>'
            '<Rect id="rule" x="0" y="50" w="200" h="1" class="fill-#222222" />',
            {},
            (),
        ),
        (
            "autofit_text_pending",
            '<TextBox id="auto" x="20" y="20" w="220" h="70">'
            "<P>Autofit shrinks this large heading to fit.</P></TextBox>",
            {
                "autofit": {
                    "type": "TEXT_AUTOFIT",
                    "fontScale": 0.5,
                    "lineSpacingReduction": 0.2,
                },
                "text": {
                    "paragraphs": [
                        {
                            "style": {"lineSpacing": 100},
                            "runs": [
                                {
                                    "content": "Autofit shrinks this large heading to fit.",
                                    "style": {
                                        "fontFamily": "Arial",
                                        "fontSize": 40,
                                    },
                                }
                            ],
                        }
                    ]
                },
            },
            ("TEXT_OVERFLOW",),
        ),
        (
            "autofit_none",
            '<TextBox id="none" x="20" y="20" w="220" h="70">'
            "<P>Autofit shrinks this large heading to fit.</P></TextBox>",
            {
                "autofit": {"type": "NONE", "fontScale": 1},
                "text": {
                    "paragraphs": [
                        {
                            "style": {"lineSpacing": 100},
                            "runs": [
                                {
                                    "content": "Autofit shrinks this large heading to fit.",
                                    "style": {
                                        "fontFamily": "Arial",
                                        "fontSize": 40,
                                    },
                                }
                            ],
                        }
                    ]
                },
            },
            ("TEXT_OVERFLOW",),
        ),
    ]

    verdict_vector: list[tuple[str, ...]] = []
    for name, body, style, expected in cases:
        case_root = tmp_path / name
        case_root.mkdir()
        folder = materialize(data, case_root)
        _replace_slides(folder, body)
        if style:
            styles_path = folder / "styles.json"
            styles = json.loads(styles_path.read_text(encoding="utf-8"))
            element_id = "auto" if name == "autofit_text_pending" else "none"
            if name == "word_wrap_21pt_something":
                element_id = "wrap21"
            styles[element_id] = style
            styles_path.write_text(
                json.dumps(styles, indent=2) + "\n", encoding="utf-8"
            )

        verdict = tuple(sorted(finding.rule for finding in lint_folder(folder)))
        verdict_vector.append(verdict)
        assert verdict == expected, name

    assert verdict_vector == [
        (),
        ("TEXT_OVERFLOW",),
        ("TEXT_OVERFLOW",),
        (),
        ("OVERLAP",),
        (),
        ("TEXT_OVERFLOW",),
        ("TEXT_OVERFLOW",),
    ]


def test_pending_text_edit_deactivates_pull_time_autofit(qa_folder: Path) -> None:
    payload_style = {
        "autofit": {
            "type": "TEXT_AUTOFIT",
            "fontScale": 0.5,
            "lineSpacingReduction": 0.2,
        },
        "text": {
            "paragraphs": [
                {
                    "style": {"lineSpacing": 100},
                    "runs": [
                        {
                            "content": "Autofit shrinks this large heading to fit.",
                            "style": {"fontFamily": "Arial", "fontSize": 40},
                        }
                    ],
                }
            ]
        },
    }
    styles_path = qa_folder / "styles.json"
    styles = json.loads(styles_path.read_text(encoding="utf-8"))
    styles["e121"] = payload_style
    styles_path.write_text(json.dumps(styles, indent=2) + "\n", encoding="utf-8")

    _replace_slides(
        qa_folder,
        '<TextBox id="e121" x="20" y="20" w="220" h="70">'
        '<P><T class="text-size-40">Autofit shrinks this large heading to fit.</T></P>'
        "</TextBox>",
    )
    assert "TEXT_OVERFLOW" in _rules(qa_folder)


def _write_e121_autofit_style(folder: Path) -> None:
    styles_path = folder / "styles.json"
    styles = json.loads(styles_path.read_text(encoding="utf-8"))
    styles["e121"] = {
        "autofit": {
            "type": "TEXT_AUTOFIT",
            "fontScale": 0.5,
            "lineSpacingReduction": 0.2,
        },
        "text": {
            "paragraphs": [
                {
                    "style": {"lineSpacing": 100},
                    "runs": [
                        {
                            "content": "Driving GenAI Transformations",
                            "style": {"fontFamily": "Montserrat", "fontSize": 40},
                        }
                    ],
                }
            ]
        },
    }
    styles_path.write_text(json.dumps(styles, indent=2) + "\n", encoding="utf-8")


def _write_e121_run_edit(folder: Path, run_class: str) -> None:
    _write_e121_autofit_style(folder)
    _replace_slides(
        folder,
        '<TextBox id="e121" x="20" y="20" w="220" h="70" '
        'class="text-align-left leading-90">'
        '<P class="font-family-montserrat text-size-43 font-weight-400">'
        f'<T class="{run_class}">Driving GenAI Transformations</T></P>'
        "</TextBox>",
    )


def test_autofit_invalidation_ignores_run_decorations_but_flags_size_edit(
    qa_folder: Path,
) -> None:
    _write_e121_run_edit(qa_folder, "text-color-#ff0000")
    color_findings = [
        finding
        for finding in lint_folder(qa_folder)
        if finding.element_ids == ("e121",) and finding.rule == "TEXT_OVERFLOW"
    ]
    assert color_findings == []

    _write_e121_run_edit(qa_folder, "text-size-40")
    size_findings = [
        finding
        for finding in lint_folder(qa_folder)
        if finding.element_ids == ("e121",) and finding.rule == "TEXT_OVERFLOW"
    ]
    assert size_findings


def test_autofit_copy_invalidation_is_instance_scoped(qa_folder: Path) -> None:
    _write_e121_autofit_style(qa_folder)
    _replace_slides(
        qa_folder,
        '<TextBox id="e121" x="40.55" y="118.8" w="551.74" h="93.8" '
        'class="text-align-left leading-90">'
        '<P class="font-family-montserrat text-size-43 font-weight-400">'
        "Driving GenAI Transformations</P></TextBox>"
        '<TextBox id="e121" x="20" y="20" w="220" h="70">'
        '<P><T class="text-size-40">Autofit shrinks this large heading to fit.</T></P>'
        "</TextBox>",
    )

    slides = qa_engine.parse_all_slides(str(qa_folder / "slides"))
    invalidated = qa_engine._pending_autofit_invalidations(qa_folder, slides)
    instances = [
        element
        for element in qa_engine._walk(slides["01"])
        if element.clean_id == "e121"
    ]
    assert [id(element) in invalidated for element in instances] == [False, True]
    assert [
        finding
        for finding in lint_folder(qa_folder)
        if finding.element_ids == ("e121",) and finding.rule == "TEXT_OVERFLOW"
    ]


def test_empty_paragraph_uses_inherited_size_for_overflow(qa_folder: Path) -> None:
    _replace_slides(
        qa_folder,
        '<TextBox id="empty" x="20" y="20" w="220" h="60" '
        'class="font-family-arial text-size-24"><P></P><P>Visible</P></TextBox>',
    )
    assert "TEXT_OVERFLOW" in {
        finding.rule
        for finding in lint_folder(qa_folder)
        if finding.element_ids == ("empty",)
    }

    _replace_slides(
        qa_folder,
        '<TextBox id="empty" x="20" y="20" w="220" h="60" '
        'class="font-family-arial text-size-12"><P></P><P>Visible</P></TextBox>',
    )
    assert "TEXT_OVERFLOW" not in {
        finding.rule
        for finding in lint_folder(qa_folder)
        if finding.element_ids == ("empty",)
    }


def test_text_overflow_flags_large_title_shorter_than_true_line(qa_folder: Path) -> None:
    _replace_slides(
        qa_folder,
        '<TextBox id="title" x="40" y="40" w="350" h="20.736" '
        'class="font-family-arial text-size-40"><P>Launch night</P></TextBox>',
    )

    assert "TEXT_OVERFLOW" in _rules(qa_folder)


def test_compact_scaffold_guard_uses_inset_subtracted_content_box() -> None:
    limit = qa_engine._text_overflow_limit(
        21.3,
        25.8,
        9.3,
        first_line_height=11.4,
        line_count=1,
        inset_height=14.4,
    )

    assert 25.8 > limit


def test_text_overflow_allows_single_line_large_title_with_metric_margin(
    qa_folder: Path,
) -> None:
    _replace_slides(
        qa_folder,
        '<TextBox id="title" x="40" y="40" w="350" h="66" '
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
        '<TextBox id="display_title" x="40" y="40" w="200" h="66" '
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


def test_lint_folder_accepts_findings_from_an_idless_authored_slide(
    qa_folder: Path,
) -> None:
    _write_idless_slide(
        qa_folder,
        3,
        '<Rect id="new" x="700" y="10" w="30" h="30" />',
    )

    findings = lint_folder(qa_folder)

    assert len(findings) == 1
    assert findings[0].element_ids == ("new",)
    assert findings[0].slide_id is None


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


def test_qa_baseline_survives_inserting_an_earlier_slide(
    qa_folder: Path,
) -> None:
    _write_slide_body(
        qa_folder,
        3,
        '<Rect id="untouched" x="700" y="10" w="30" h="30" />',
    )
    record_qa_baseline(qa_folder)
    original_contents = _slide_contents(qa_folder)
    _reindex_slides(
        qa_folder,
        [
            *original_contents[:2],
            '<Slide id="s-new"><Rect id="new" x="700" y="10" '
            'w="30" h="30" /></Slide>',
            *original_contents[2:],
        ],
    )

    findings = lint_folder(qa_folder)
    untouched = next(item for item in findings if "untouched" in item.element_ids)
    new_finding = next(item for item in findings if "new" in item.element_ids)
    assert untouched.slide_number == 4
    assert untouched.slide_id == "focus_areas_slide"
    assert new_finding.slide_id == "s-new"
    assert qa_engine._finding_key(untouched) != qa_engine._finding_key(new_finding)

    output: list[str] = []
    check_folder(qa_folder, output=output.append)
    assert output[0] == (
        "2 findings (1 new, 1 pre-existing, 0 resolved; NEW = since last pull)"
    )
    assert any(
        line.startswith("[PRE-EXISTING] [WARNING] OUT_OF_BOUNDS slide 04 (untouched)")
        for line in output
    )
    assert any(
        line.startswith("[NEW] [WARNING] OUT_OF_BOUNDS slide 03 (new)")
        for line in output
    )


def test_qa_baseline_survives_deleting_an_earlier_slide(
    qa_folder: Path,
) -> None:
    _write_slide_body(
        qa_folder,
        3,
        '<Rect id="untouched" x="700" y="10" w="30" h="30" />',
    )
    record_qa_baseline(qa_folder)
    original_contents = _slide_contents(qa_folder)
    _reindex_slides(qa_folder, [*original_contents[:1], *original_contents[2:]])

    current = next(
        item for item in lint_folder(qa_folder) if "untouched" in item.element_ids
    )
    assert current.slide_number == 2
    assert current.slide_id == "focus_areas_slide"

    output: list[str] = []
    check_folder(qa_folder, output=output.append)
    assert output[0] == (
        "1 findings (0 new, 1 pre-existing, 0 resolved; NEW = since last pull)"
    )
    assert output[1].startswith(
        "[PRE-EXISTING] [WARNING] OUT_OF_BOUNDS slide 02 (untouched)"
    )


def test_idless_finding_does_not_match_identified_baseline_at_same_position(
    qa_folder: Path,
) -> None:
    _write_slide_body(
        qa_folder,
        2,
        '<Rect id="same_position" x="700" y="10" w="30" h="30" />',
    )
    record_qa_baseline(qa_folder)
    _write_idless_slide(
        qa_folder,
        2,
        '<Rect id="same_position" x="700" y="10" w="30" h="30" />',
    )

    output: list[str] = []
    check_folder(qa_folder, output=output.append)

    assert output[0] == (
        "1 findings (1 new, 0 pre-existing, 1 resolved; NEW = since last pull)"
    )
    assert any(line.startswith("[NEW] [WARNING] OUT_OF_BOUNDS") for line in output)
    assert any(
        line.startswith("[RESOLVED] [WARNING] OUT_OF_BOUNDS") for line in output
    )


def test_idless_acceptance_does_not_migrate_onto_identified_slide(
    qa_folder: Path,
) -> None:
    _write_idless_slide(
        qa_folder,
        2,
        '<Rect id="accepted_idless" x="700" y="10" w="30" h="30" />',
    )
    finding = next(item for item in lint_folder(qa_folder))
    identity = finding_id(finding)
    assert identity == "OUT_OF_BOUNDS:2:accepted_idless"
    assert check_folder(qa_folder, accept=[identity]) == 0

    accepted_path = qa_folder / ".qa" / "accepted.json"
    accepted = json.loads(accepted_path.read_text(encoding="utf-8"))
    assert accepted["accepted"][identity]["slideId"] is None

    _write_slide_body(
        qa_folder,
        2,
        '<Rect id="accepted_idless" x="700" y="10" w="30" h="30" />',
    )
    output: list[str] = []
    assert check_folder(qa_folder, strict=True, output=output.append) == 1
    assert not any(line.startswith("[ACCEPTED]") for line in output)

    persisted = json.loads(accepted_path.read_text(encoding="utf-8"))
    assert persisted["accepted"][identity]["slideId"] is None
    assert "OUT_OF_BOUNDS:s2:accepted_idless" not in persisted["accepted"]


def test_two_idless_findings_at_same_position_match(
    qa_folder: Path,
) -> None:
    _write_idless_slide(
        qa_folder,
        2,
        '<Rect id="same_idless" x="700" y="10" w="30" h="30" />',
    )
    record_qa_baseline(qa_folder)

    output: list[str] = []
    check_folder(qa_folder, output=output.append)

    assert output[0] == (
        "1 findings (0 new, 1 pre-existing, 0 resolved; NEW = since last pull)"
    )


def test_accepted_finding_survives_slide_renumbering(
    qa_folder: Path,
) -> None:
    _write_slide_body(
        qa_folder,
        3,
        '<Rect id="accepted" x="700" y="10" w="30" h="30" />',
    )
    finding = next(
        item for item in lint_folder(qa_folder) if "accepted" in item.element_ids
    )
    assert finding_id(finding) == "OUT_OF_BOUNDS:focus_areas_slide:accepted"
    record_qa_baseline(qa_folder)
    assert check_folder(qa_folder, accept=[finding_id(finding)]) == 0

    accepted_path = qa_folder / ".qa" / "accepted.json"
    accepted = json.loads(accepted_path.read_text(encoding="utf-8"))
    assert accepted["accepted"] == {
        "OUT_OF_BOUNDS:focus_areas_slide:accepted": {
            "rule": "OUT_OF_BOUNDS",
            "slide": 3,
            "slideId": "focus_areas_slide",
            "elementIds": ["accepted"],
        }
    }

    contents = _slide_contents(qa_folder)
    _reindex_slides(
        qa_folder,
        [*contents[:2], '<Slide id="s-new" />', *contents[2:]],
    )
    output: list[str] = []
    assert check_folder(qa_folder, strict=True, output=output.append) == 0
    assert any(
        line.startswith(
            "[ACCEPTED] [PRE-EXISTING] [WARNING] OUT_OF_BOUNDS slide 04 (accepted)"
        )
        for line in output
    )


def test_old_baseline_without_slide_id_uses_legacy_slide_number_fallback(
    qa_folder: Path,
) -> None:
    _write_slide_body(
        qa_folder,
        3,
        '<Rect id="legacy" x="700" y="10" w="30" h="30" />',
    )
    baseline_path = record_qa_baseline(qa_folder)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    for finding in baseline["findings"]:
        finding.pop("slideId", None)
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    output: list[str] = []
    assert check_folder(qa_folder, output=output.append) == 0
    assert output[0] == (
        "1 findings (0 new, 1 pre-existing, 0 resolved; NEW = since last pull)"
    )


def test_old_accepted_file_without_slide_id_is_migrated(
    qa_folder: Path,
) -> None:
    _write_slide_body(
        qa_folder,
        3,
        '<Rect id="legacy" x="700" y="10" w="30" h="30" />',
    )
    accepted_path = qa_folder / ".qa" / "accepted.json"
    accepted_path.parent.mkdir(parents=True)
    accepted_path.write_text(
        json.dumps(
            {
                "version": 1,
                "accepted": {
                    "OUT_OF_BOUNDS:3:legacy": {
                        "rule": "OUT_OF_BOUNDS",
                        "slide": 3,
                        "elementIds": ["legacy"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    output: list[str] = []
    assert check_folder(qa_folder, strict=True, output=output.append) == 0
    assert any(line.startswith("[ACCEPTED]") for line in output)
    migrated = json.loads(accepted_path.read_text(encoding="utf-8"))
    assert "OUT_OF_BOUNDS:focus_areas_slide:legacy" in migrated["accepted"]
    assert (
        migrated["accepted"]["OUT_OF_BOUNDS:focus_areas_slide:legacy"]["slideId"]
        == "focus_areas_slide"
    )


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
    identity = "OUT_OF_BOUNDS:s1:outside"

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
                "slideId": "s1",
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
    assert finding_id(finding) == "OVERLAP:s1:left,right"
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

    identity = "OUT_OF_BOUNDS:s1:outside"
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
    monkeypatch.setattr(
        "slidesmith.engine.client.diff_folder_with_result", forbidden
    )

    cli.main(["check", str(qa_folder), "--no-thumbnails"])

    assert "no issues found" in capsys.readouterr().out
    assert not (qa_folder / ".qa").exists()


def test_cli_thumbnail_check_validates_metadata_before_dirty_diff(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tmp_path / "deck"
    folder.mkdir()

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["check", str(folder)])

    assert excinfo.value.code == 1
    error = capsys.readouterr().err
    assert f"Missing Slidesmith workspace file: {folder / 'presentation.json'}" in error
    assert "Pristine zip not found" not in error


def test_cli_thumbnail_check_validates_presentation_id_before_dirty_diff(
    clean_qa_folder: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    metadata_path = clean_qa_folder / "presentation.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    del metadata["presentationId"]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["check", str(clean_qa_folder)])

    assert excinfo.value.code == 1
    assert capsys.readouterr().err == "error: 'presentationId'\n"


def test_cli_thumbnail_check_propagates_unexpected_diff_errors(
    clean_qa_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_unexpected(_folder: Path) -> Any:
        raise AssertionError("unexpected diff programming error")

    monkeypatch.setattr(
        "slidesmith.engine.client.diff_folder_with_result", raise_unexpected
    )

    with pytest.raises(AssertionError, match="unexpected diff programming error"):
        cli.cmd_check(
            SimpleNamespace(
                folder=clean_qa_folder,
                contact_sheet=False,
                no_thumbnails=False,
                strict=False,
                accept=[],
                unaccept=[],
            )
        )


def test_cli_thumbnail_check_clean_worktree_has_no_remote_state_warning(
    clean_qa_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    class FakeTransport:
        def __init__(self, token: str) -> None:
            assert token == "fake-token"

        async def get_page_thumbnail(
            self,
            _presentation_id: str,
            page_object_id: str,
            _size: str = "LARGE",
        ) -> bytes:
            calls.append(page_object_id)
            return b"png"

        async def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "_token", lambda *_args: "fake-token")
    monkeypatch.setattr(
        "slidesmith.engine.transport.GoogleSlidesTransport", FakeTransport
    )

    cli.main(["check", str(clean_qa_folder)])

    assert calls
    assert "contact sheet and thumbnails reflect the REMOTE deck" not in (
        capsys.readouterr().err
    )


def test_cli_contact_sheet_warns_when_local_edits_are_pending(
    clean_qa_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_slide_body(
        clean_qa_folder,
        1,
        '<Rect id="local" x="10" y="10" w="20" h="20" />',
    )

    image = Image.new("RGB", (20, 20), "white")
    image_buffer = BytesIO()
    image.save(image_buffer, format="PNG")
    image.close()
    thumbnail = image_buffer.getvalue()

    events: list[str] = []

    def record_diff(folder: Path) -> Any:
        events.append("diff")
        return diff_folder_with_result(folder)

    class FakeTransport:
        def __init__(self, token: str) -> None:
            assert token == "fake-token"

        async def get_page_thumbnail(
            self,
            _presentation_id: str,
            _page_object_id: str,
            _size: str = "LARGE",
        ) -> bytes:
            events.append("thumbnail")
            return thumbnail

        async def close(self) -> None:
            pass

    monkeypatch.setattr(
        "slidesmith.engine.client.diff_folder_with_result", record_diff
    )
    monkeypatch.setattr(cli, "_token", lambda *_args: "fake-token")
    monkeypatch.setattr(
        "slidesmith.engine.transport.GoogleSlidesTransport", FakeTransport
    )

    cli.main(["check", str(clean_qa_folder), "--contact-sheet"])

    captured = capsys.readouterr()
    assert events[0] == "diff"
    assert "warning: contact sheet and thumbnails reflect the REMOTE deck" in (
        captured.err
    )
    assert "do NOT include pending local edits" in captured.err
    assert "`slidesmith push`" in captured.err
    assert (clean_qa_folder / ".qa" / "contact-sheet.png").exists()


def _corrupt_first_deflate_entry(zip_path: Path) -> None:
    """Rewrite a zip so its first entry decompresses to a zlib error.

    Overwrites the first byte of the entry's deflate stream with an invalid
    block type (BTYPE=3), which zlib rejects deterministically.
    """
    import zipfile as _zipfile

    payload = ("x" * 4096).encode("utf-8")
    with _zipfile.ZipFile(zip_path, "w", _zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("styles.json", payload)
    raw = bytearray(zip_path.read_bytes())
    name_len = int.from_bytes(raw[26:28], "little")
    extra_len = int.from_bytes(raw[28:30], "little")
    data_offset = 30 + name_len + extra_len
    raw[data_offset] = 0x06
    zip_path.write_bytes(bytes(raw))


@pytest.mark.parametrize(
    "pristine_state", ["missing", "corrupt", "corrupt-entry"]
)
def test_cli_thumbnail_check_skips_warning_when_pristine_diff_fails(
    clean_qa_folder: Path,
    pristine_state: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pristine_zip = clean_qa_folder / ".pristine" / "presentation.zip"
    if pristine_state == "missing":
        pristine_zip.unlink()
    elif pristine_state == "corrupt-entry":
        _corrupt_first_deflate_entry(pristine_zip)
    else:
        pristine_zip.write_bytes(b"not a zip archive")

    class FakeTransport:
        def __init__(self, token: str) -> None:
            assert token == "fake-token"

        async def get_page_thumbnail(
            self,
            _presentation_id: str,
            _page_object_id: str,
            _size: str = "LARGE",
        ) -> bytes:
            return b"png"

        async def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "_token", lambda *_args: "fake-token")
    monkeypatch.setattr(
        "slidesmith.engine.transport.GoogleSlidesTransport", FakeTransport
    )

    cli.main(["check", str(clean_qa_folder)])

    captured = capsys.readouterr()
    assert "contact sheet and thumbnails reflect the REMOTE deck" not in (
        captured.err
    )


def test_read_pristine_normalizes_deflate_corruption_to_value_error(
    clean_qa_folder: Path,
) -> None:
    from slidesmith.engine.workspace_reader import _read_pristine

    _corrupt_first_deflate_entry(
        clean_qa_folder / ".pristine" / "presentation.zip"
    )

    with pytest.raises(ValueError, match="Pristine zip is corrupt"):
        _read_pristine(clean_qa_folder)


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
