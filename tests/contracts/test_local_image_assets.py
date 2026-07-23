"""Offline M4 contracts for local assets and replace-image."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image, ImageOps

from slidesmith import cli
from slidesmith.engine.assets import (
    AssetCache,
    AssetUploadError,
    GoogleDriveAssetUploader,
    UploadedAsset,
)
from slidesmith.engine.client import SlidesClient
from slidesmith.engine.content_diff import ChangeType
from slidesmith.engine.content_parser import parse_slide_content
from slidesmith.engine.diff_model import WarningSeverity
from slidesmith.engine.transport import APIError, PresentationData, Transport
from slidesmith.engine.image_replace import CoverFitPushError
from slidesmith.engine.units import pt_to_emu

GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)
FAKE_URL = "https://drive.google.com/uc?export=download&id=fake-drive-file"


class FakeUploader:
    def __init__(self, url: str = FAKE_URL) -> None:
        self.calls: list[tuple[Path, str]] = []
        self.url = url

    async def upload(self, path: Path, *, mime_type: str) -> UploadedAsset:
        self.calls.append((path, mime_type))
        return UploadedAsset(file_id="fake-drive-file", url=self.url)

    async def close(self) -> None:
        pass


class ImageTransport(Transport):
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = copy.deepcopy(data)
        self.batch_calls: list[dict[str, Any]] = []
        self.fail_next_batch = False
        self.replacement_source_url: str | None = None
        self.omit_replacement_source_url = False
        self.replacement_geometry_offset_pt = 0.0
        self.created_source_url: str | None = None
        self.fail_cover_replace = False

    async def get_presentation(self, presentation_id: str) -> PresentationData:
        return PresentationData(presentation_id, copy.deepcopy(self.data))

    async def batch_update(
        self,
        presentation_id: str,
        requests: list[dict[str, Any]],
        required_revision_id: str | None = None,
    ) -> dict[str, Any]:
        self.batch_calls.append(
            {
                "presentation_id": presentation_id,
                "requests": copy.deepcopy(requests),
                "required_revision_id": required_revision_id,
            }
        )
        if self.fail_next_batch:
            self.fail_next_batch = False
            raise APIError("API error (503): retry", status_code=503)

        for request in requests:
            if create := request.get("createImage"):
                properties = create["elementProperties"]
                slide = next(
                    slide
                    for slide in self.data["slides"]
                    if slide["objectId"] == properties["pageObjectId"]
                )
                slide.setdefault("pageElements", []).append(
                    {
                        "objectId": create["objectId"],
                        "size": copy.deepcopy(properties["size"]),
                        "transform": copy.deepcopy(properties["transform"]),
                        "image": {
                            "contentUrl": create["url"],
                            "sourceUrl": self.created_source_url or create["url"],
                            "imageProperties": {},
                        },
                    }
                )
            if replace := request.get("replaceImage"):
                if (
                    self.fail_cover_replace
                    and replace.get("imageReplaceMethod") == "CENTER_CROP"
                ):
                    raise APIError(
                        "API error (400): Invalid requests[1].replaceImage: "
                        "CENTER_CROP not supported in this fixture",
                        status_code=400,
                    )
                element = _find_raw_element(self.data, replace["imageObjectId"])
                element["image"]["contentUrl"] = replace["url"]
                if self.omit_replacement_source_url:
                    element["image"].pop("sourceUrl", None)
                else:
                    element["image"]["sourceUrl"] = (
                        self.replacement_source_url or replace["url"]
                    )
                if self.replacement_geometry_offset_pt:
                    element["transform"]["translateX"] += pt_to_emu(
                        self.replacement_geometry_offset_pt
                    )

        self.data["revisionId"] = f"rev-{len(self.batch_calls)}"
        return {"replies": [{} for _ in requests]}

    async def close(self) -> None:
        pass


async def _workspace(tmp_path: Path) -> tuple[ImageTransport, SlidesClient, Path]:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    transport = ImageTransport(data)
    client = SlidesClient(transport)
    await client.pull(data["presentationId"], tmp_path, save_raw=False)
    return transport, client, tmp_path / data["presentationId"]


async def _workspace_with_grouped_image(
    tmp_path: Path,
    ancestor_transforms: list[dict[str, Any]],
) -> tuple[ImageTransport, SlidesClient, Path, str]:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    image: dict[str, Any] = {
        "objectId": "grouped_image",
        "size": {
            "width": {"magnitude": pt_to_emu(100), "unit": "EMU"},
            "height": {"magnitude": pt_to_emu(50), "unit": "EMU"},
        },
        "transform": {
            "scaleX": 1,
            "scaleY": 1,
            "translateX": pt_to_emu(10),
            "translateY": pt_to_emu(15),
            "unit": "EMU",
        },
        "image": {
            "contentUrl": "https://example.com/old.png",
            "sourceUrl": "https://example.com/old.png",
            "imageProperties": {},
        },
    }
    grouped: dict[str, Any] = image
    for index, transform in reversed(list(enumerate(ancestor_transforms))):
        grouped = {
            "objectId": f"grouped_image_parent_{index}",
            "transform": transform,
            "elementGroup": {"children": [grouped]},
        }
    data["slides"][0].setdefault("pageElements", []).append(grouped)

    transport = ImageTransport(data)
    client = SlidesClient(transport, FakeUploader())
    await client.pull(data["presentationId"], tmp_path, save_raw=False)
    folder = tmp_path / data["presentationId"]
    mapping = json.loads((folder / "id_mapping.json").read_text(encoding="utf-8"))
    image_id = next(
        clean_id for clean_id, google_id in mapping.items() if google_id == "grouped_image"
    )
    return transport, client, folder, image_id


def _write_png(
    folder: Path,
    relative: str = "assets/logo.png",
    *,
    size: tuple[int, int] = (80, 40),
) -> Path:
    path = folder / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "navy").save(path)
    return path


def _append_local_image(
    folder: Path,
    *,
    source: str = "./assets/logo.png",
    fit: str = "stretch",
    geometry: str = 'x="12" y="18" w="160" h="90"',
) -> None:
    content_path = folder / "slides" / "01" / "content.sml"
    content = content_path.read_text(encoding="utf-8")
    image = (
        f'<Image id="local_logo" src="{source}" fit="{fit}" '
        f"{geometry} />\n"
    )
    content_path.write_text(content.replace("</Slide>", image + "</Slide>"), encoding="utf-8")


def _author_existing_image(
    folder: Path,
    image_id: str,
    source: str,
    fit: str,
) -> None:
    content_path = folder / "slides" / "01" / "content.sml"
    content = content_path.read_text(encoding="utf-8")
    marker = f'<Image id="{image_id}"'
    assert marker in content
    content_path.write_text(
        content.replace(
            marker,
            f'<Image id="{image_id}" src="{source}" fit="{fit}"',
            1,
        ),
        encoding="utf-8",
    )


def _find_raw_element(data: dict[str, Any], object_id: str) -> dict[str, Any]:
    def walk(elements: list[dict[str, Any]]) -> dict[str, Any] | None:
        for element in elements:
            if element.get("objectId") == object_id:
                return element
            found = walk(element.get("elementGroup", {}).get("children", []))
            if found is not None:
                return found
        return None

    for slide in data.get("slides", []):
        found = walk(slide.get("pageElements", []))
        if found is not None:
            return found
    raise AssertionError(f"element {object_id!r} not found")


def _first_clean_id(folder: Path, data: dict[str, Any], kind: str) -> str:
    mapping = json.loads((folder / "id_mapping.json").read_text(encoding="utf-8"))
    reverse = {google_id: clean_id for clean_id, google_id in mapping.items()}

    def walk(elements: list[dict[str, Any]]) -> str | None:
        for element in elements:
            if kind in element and element.get("objectId") in reverse:
                return reverse[element["objectId"]]
            found = walk(element.get("elementGroup", {}).get("children", []))
            if found is not None:
                return found
        return None

    for slide in data["slides"]:
        found = walk(slide.get("pageElements", []))
        if found is not None:
            return found
    raise AssertionError(f"no mapped {kind} element found")


@pytest.mark.asyncio
async def test_local_image_insert_emits_create_image_with_fake_uploaded_url(
    tmp_path: Path,
) -> None:
    transport, _, folder = await _workspace(tmp_path)
    uploader = FakeUploader()
    client = SlidesClient(transport, uploader)
    _write_png(folder)
    _append_local_image(folder)

    preview = client.diff(folder)
    assert next(r for r in preview if "createImage" in r)["createImage"]["url"] == (
        "./assets/logo.png"
    )

    response = await client.push(folder)

    create = next(
        request["createImage"]
        for request in transport.batch_calls[-1]["requests"]
        if "createImage" in request
    )
    assert create["url"] == FAKE_URL
    assert uploader.calls == [(folder / "assets" / "logo.png", "image/png")]
    assert response.get("warnings", []) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_size", "target_aspect", "expected_box"),
    [
        ((4, 2), 1.0, (1, 0, 3, 2)),
        ((2, 4), 1.0, (0, 1, 2, 3)),
    ],
)
async def test_local_cover_asset_center_crops_wide_and_tall_sources_deterministically(
    tmp_path: Path,
    source_size: tuple[int, int],
    target_aspect: float,
    expected_box: tuple[int, int, int, int],
) -> None:
    workspace = tmp_path / "deck"
    source = workspace / "assets" / "source.png"
    source.parent.mkdir(parents=True)
    pixels = Image.new("RGB", source_size)
    for y in range(source_size[1]):
        for x in range(source_size[0]):
            pixels.putpixel((x, y), (x * 30, y * 30, 1))
    pixels.save(source)

    uploader = FakeUploader()
    cache = AssetCache(workspace)
    first_url = await cache.resolve_cover("./assets/source.png", target_aspect, uploader)
    derived_path = uploader.calls[0][0]
    first_mtime = derived_path.stat().st_mtime_ns
    with Image.open(derived_path) as derived:
        expected = pixels.crop(expected_box)
        assert derived.size == expected.size
        assert derived.tobytes() == expected.tobytes()

    second_url = await cache.resolve_cover("./assets/source.png", target_aspect, uploader)

    assert second_url == first_url
    assert len(uploader.calls) == 1
    assert derived_path.stat().st_mtime_ns == first_mtime
    assert json.loads((workspace / ".assets.json").read_text())["assets"][0][
        "kind"
    ] == "cover"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_size", "target_aspect"),
    [((5, 3), 3 / 4), ((3, 5), 4 / 3)],
    ids=("wide-into-tall", "tall-into-wide"),
)
async def test_local_cover_asset_resamples_odd_dimensions_to_exact_target_aspect(
    tmp_path: Path,
    source_size: tuple[int, int],
    target_aspect: float,
) -> None:
    workspace = tmp_path / "deck"
    source = workspace / "assets" / "odd.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", source_size, "navy").save(source)

    uploader = FakeUploader()
    await AssetCache(workspace).resolve_cover("./assets/odd.png", target_aspect, uploader)

    with Image.open(uploader.calls[0][0]) as derived:
        assert derived.width / derived.height == pytest.approx(target_aspect)


@pytest.mark.asyncio
async def test_local_cover_asset_bounds_irrational_aspect_raster_and_validates_same_ratio(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "deck"
    source = workspace / "assets" / "irrational.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (4, 2), "navy").save(source)

    uploader = FakeUploader()
    await AssetCache(workspace).resolve_cover(
        "./assets/irrational.png", math.pi, uploader, element_id="hero"
    )

    with Image.open(uploader.calls[0][0]) as derived:
        assert derived.size == (355, 113)
        assert max(derived.size) <= 4096
        assert derived.width * 113 == derived.height * 355


@pytest.mark.asyncio
async def test_local_cover_asset_golden_ratio_walks_down_to_in_bounds_rational(
    tmp_path: Path,
) -> None:
    """limit_denominator alone picks 4181/2584, whose numerator exceeds the
    4096px cap; the walk-down must land on the safe 2584/1597 rational
    instead of rejecting a realistic aspect."""
    workspace = tmp_path / "deck"
    source = workspace / "assets" / "golden.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (8, 4), "navy").save(source)

    uploader = FakeUploader()
    await AssetCache(workspace).resolve_cover(
        "./assets/golden.png", (1 + 5**0.5) / 2, uploader, element_id="hero"
    )

    with Image.open(uploader.calls[0][0]) as derived:
        assert max(derived.size) <= 4096
        assert derived.width * 1597 == derived.height * 2584


@pytest.mark.asyncio
async def test_local_cover_asset_rejects_unbounded_aspect_with_element_name(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "deck"
    source = workspace / "assets" / "extreme.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (4, 4), "navy").save(source)

    with pytest.raises(
        ValueError,
        match=r"Image element 'hero'.*no safe rational raster.*4096",
    ):
        await AssetCache(workspace).resolve_cover(
            "./assets/extreme.png", 1e9, FakeUploader(), element_id="hero"
        )


@pytest.mark.asyncio
async def test_local_cover_asset_ignores_pre_exif_derivation_cache_key(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "deck"
    source = workspace / "assets" / "oriented.jpg"
    source.parent.mkdir(parents=True)
    pixels = Image.new("RGB", (4, 2), "navy")
    exif = pixels.getexif()
    exif[274] = 6
    pixels.save(source, format="JPEG", quality=100, exif=exif)

    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    old_key = hashlib.sha256(
        f"{source_hash}\0{(1.0).hex()}".encode("ascii")
    ).hexdigest()
    stale_path = workspace / ".slidesmith-cover" / f"{old_key}.png"
    stale_path.parent.mkdir(parents=True)
    Image.new("RGB", (2, 2), "red").save(stale_path)

    uploader = FakeUploader()
    await AssetCache(workspace).resolve_cover(
        "./assets/oriented.jpg", 1.0, uploader, element_id="hero"
    )

    assert uploader.calls[0][0] != stale_path
    assert uploader.calls[0][0].exists()


@pytest.mark.asyncio
async def test_local_cover_asset_applies_exif_orientation_before_portrait_crop(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "deck"
    source = workspace / "assets" / "portrait.jpg"
    source.parent.mkdir(parents=True)
    pixels = Image.new("RGB", (4, 2))
    for y in range(2):
        for x in range(4):
            pixels.putpixel((x, y), (x * 50, y * 100, 20))
    exif = pixels.getexif()
    exif[274] = 6
    pixels.save(source, format="JPEG", quality=100, exif=exif)

    uploader = FakeUploader()
    await AssetCache(workspace).resolve_cover(
        "./assets/portrait.jpg", 1.0, uploader, element_id="hero"
    )

    with Image.open(source) as opened, Image.open(uploader.calls[0][0]) as derived:
        oriented = ImageOps.exif_transpose(opened)
        expected = oriented.crop((0, 1, 2, 3))
        assert derived.size == (2, 2)
        assert derived.getpixel((0, 0))[0] == pytest.approx(
            expected.getpixel((0, 0))[0], abs=10
        )


@pytest.mark.asyncio
async def test_local_cover_asset_rejects_animated_source_with_element_name(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "deck"
    source = workspace / "assets" / "animated.gif"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (4, 4), "red").save(
        source,
        save_all=True,
        append_images=[Image.new("RGB", (4, 4), "blue")],
        duration=100,
        loop=0,
    )

    with pytest.raises(
        ValueError,
        match=r"Image element 'hero'.*animated.*static source",
    ):
        await AssetCache(workspace).resolve_cover(
            "./assets/animated.gif", 1.0, FakeUploader(), element_id="hero"
        )


@pytest.mark.asyncio
async def test_new_local_cover_uses_derived_asset_through_normal_upload_path(
    tmp_path: Path,
) -> None:
    transport, _, folder = await _workspace(tmp_path)
    uploader = FakeUploader()
    client = SlidesClient(transport, uploader)
    _write_png(folder, size=(400, 200))
    _append_local_image(folder, fit="cover", geometry='x="12" y="18" w="100" h="100"')

    await client.push(folder)

    create = next(
        request["createImage"]
        for request in transport.batch_calls[-1]["requests"]
        if "createImage" in request
    )
    assert create["url"] == FAKE_URL
    assert uploader.calls[0][0].parent.name == ".slidesmith-cover"
    assert uploader.calls[0][0].suffix == ".png"
    assert [next(iter(request)) for request in transport.batch_calls[-1]["requests"]] == [
        "createImage"
    ]


@pytest.mark.asyncio
async def test_new_remote_cover_rejection_names_cover_fit_and_element(
    tmp_path: Path,
) -> None:
    transport, _, folder = await _workspace(tmp_path)
    client = SlidesClient(transport)
    _append_local_image(
        folder,
        source="https://example.com/remote.png",
        fit="cover",
    )
    transport.fail_cover_replace = True

    with pytest.raises(
        CoverFitPushError,
        match=r"cover fit replaceImage was rejected.*local_logo",
    ):
        await client.push(folder)

    sent = transport.batch_calls[-1]["requests"]
    assert [next(iter(request)) for request in sent] == [
        "createImage",
        "replaceImage",
        "updatePageElementTransform",
    ]


@pytest.mark.asyncio
async def test_local_image_create_warns_when_refreshed_source_differs(
    tmp_path: Path,
) -> None:
    transport, _, folder = await _workspace(tmp_path)
    uploader = FakeUploader()
    client = SlidesClient(transport, uploader)
    _write_png(folder)
    _append_local_image(folder)
    transport.created_source_url = "https://drive.google.com/other-file"

    response = await client.push(folder)

    assert any(
        "image replacement did not persist" in warning.message
        for warning in response["warnings"]
    )
    assert any(
        "https://drive.google.com/other-file" in warning.message
        for warning in response["warnings"]
    )


@pytest.mark.asyncio
async def test_asset_cache_uploads_same_local_path_and_hash_only_once_across_retries(
    tmp_path: Path,
) -> None:
    transport, _, folder = await _workspace(tmp_path)
    uploader = FakeUploader()
    client = SlidesClient(transport, uploader)
    _write_png(folder)
    _append_local_image(folder)
    transport.fail_next_batch = True

    with pytest.raises(APIError, match="503"):
        await client.push(folder)
    await client.push(folder)

    assert len(uploader.calls) == 1
    cache = json.loads((folder / ".assets.json").read_text(encoding="utf-8"))
    assert cache == {
        "assets": [
            {
                "fileId": "fake-drive-file",
                "path": "assets/logo.png",
                "sha256": cache["assets"][0]["sha256"],
                "url": FAKE_URL,
            }
        ],
        "version": 1,
    }
    assert len(cache["assets"][0]["sha256"]) == 64
    assert all(
        next(r for r in call["requests"] if "createImage" in r)["createImage"][
            "url"
        ]
        == FAKE_URL
        for call in transport.batch_calls
    )


@pytest.mark.asyncio
async def test_existing_pulled_image_src_edit_replaces_uploads_and_round_trips(
    tmp_path: Path,
) -> None:
    transport, _, folder = await _workspace(tmp_path)
    uploader = FakeUploader()
    client = SlidesClient(transport, uploader)
    image_id = _first_clean_id(folder, transport.data, "image")
    _write_png(folder, relative="assets/replacement.png", size=(900, 600))
    _author_existing_image(folder, image_id, "./assets/replacement.png", "stretch")

    diff_result, requests = client.diff_with_result(folder)
    assert [change.change_type for change in diff_result.changes] == [
        ChangeType.IMAGE_UPDATE
    ]
    assert [next(iter(request)) for request in requests] == [
        "replaceImage",
        "updatePageElementTransform",
    ]
    assert requests[0]["replaceImage"]["url"] == "./assets/replacement.png"

    response = await client.push(folder)

    assert response.get("warnings", []) == []
    assert transport.batch_calls[-1]["requests"][0]["replaceImage"]["url"] == FAKE_URL
    assert uploader.calls == [(folder / "assets" / "replacement.png", "image/png")]
    assert client.diff(folder) == []


@pytest.mark.asyncio
async def test_existing_image_replace_warns_when_remote_geometry_differs(
    tmp_path: Path,
) -> None:
    transport, _, folder = await _workspace(tmp_path)
    uploader = FakeUploader()
    client = SlidesClient(transport, uploader)
    image_id = _first_clean_id(folder, transport.data, "image")
    _write_png(folder, relative="assets/replacement.png", size=(900, 600))
    _author_existing_image(folder, image_id, "./assets/replacement.png", "stretch")
    transport.replacement_geometry_offset_pt = 0.1

    response = await client.push(folder)

    assert any(
        "geometry on" in warning.message and "did not persist" in warning.message
        for warning in response["warnings"]
    )


@pytest.mark.asyncio
async def test_existing_image_replace_accepts_geometry_within_tolerance(
    tmp_path: Path,
) -> None:
    transport, _, folder = await _workspace(tmp_path)
    uploader = FakeUploader()
    client = SlidesClient(transport, uploader)
    image_id = _first_clean_id(folder, transport.data, "image")
    _write_png(folder, relative="assets/replacement.png", size=(900, 600))
    _author_existing_image(folder, image_id, "./assets/replacement.png", "stretch")
    transport.replacement_geometry_offset_pt = 0.01

    response = await client.push(folder)

    assert not any(
        "did not persist"
        in warning.message
        for warning in response.get("warnings", [])
    )


@pytest.mark.asyncio
async def test_existing_image_replace_warns_when_remote_source_differs(
    tmp_path: Path,
) -> None:
    transport, _, folder = await _workspace(tmp_path)
    uploader = FakeUploader()
    client = SlidesClient(transport, uploader)
    image_id = _first_clean_id(folder, transport.data, "image")
    _write_png(folder, relative="assets/replacement.png", size=(900, 600))
    _author_existing_image(folder, image_id, "./assets/replacement.png", "stretch")
    transport.replacement_source_url = "https://drive.google.com/other-file"

    response = await client.push(folder)

    assert any(
        "image replacement did not persist" in warning.message
        for warning in response["warnings"]
    )
    assert any(
        "https://drive.google.com/uc" in warning.message
        for warning in response["warnings"]
    )


@pytest.mark.asyncio
async def test_existing_image_replace_redacts_signed_source_in_persistence_warning(
    tmp_path: Path,
) -> None:
    transport, _, folder = await _workspace(tmp_path)
    signed_url = "https://drive.google.com/uc?X-Goog-Signature=SECRET"
    uploader = FakeUploader(signed_url)
    client = SlidesClient(transport, uploader)
    image_id = _first_clean_id(folder, transport.data, "image")
    _write_png(folder, relative="assets/replacement.png", size=(900, 600))
    _author_existing_image(folder, image_id, "./assets/replacement.png", "stretch")
    transport.replacement_source_url = "https://drive.google.com/other-file"

    response = await client.push(folder)

    messages = [warning.message for warning in response["warnings"]]
    assert any("image replacement did not persist" in message for message in messages)
    assert all("SECRET" not in message for message in messages)
    assert any("https://drive.google.com/uc" in message for message in messages)


@pytest.mark.asyncio
async def test_existing_image_replace_accepts_omitted_remote_source_url(
    tmp_path: Path,
) -> None:
    transport, _, folder = await _workspace(tmp_path)
    uploader = FakeUploader()
    client = SlidesClient(transport, uploader)
    image_id = _first_clean_id(folder, transport.data, "image")
    _write_png(folder, relative="assets/replacement.png", size=(900, 600))
    _author_existing_image(folder, image_id, "./assets/replacement.png", "stretch")
    transport.omit_replacement_source_url = True

    response = await client.push(folder)

    assert not any(
        "image replacement did not persist" in warning.message
        for warning in response.get("warnings", [])
    )


@pytest.mark.asyncio
async def test_remote_stretch_dimension_fetch_failure_falls_back_with_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, client, folder = await _workspace(tmp_path)
    _append_local_image(
        folder,
        source="https://example.com/oversize.png?X-Goog-Signature=SECRET",
        fit="stretch",
    )

    def fail_dimensions(_url: str) -> tuple[int, int]:
        raise ValueError(f"image download failed for {_url}")

    monkeypatch.setattr(
        "slidesmith.engine.content_diff.fetch_image_dimensions", fail_dimensions
    )

    response = await client.push(folder)

    assert any(
        warning.severity is WarningSeverity.NOTICE
        and "follow-up resize" in warning.message
        for warning in response["warnings"]
    )
    messages = [warning.message for warning in response["warnings"]]
    assert all("SECRET" not in message for message in messages)
    assert any("https://example.com/oversize.png" in message for message in messages)
    create = next(
        request["createImage"]
        for request in transport.batch_calls[-1]["requests"]
        if "createImage" in request
    )
    properties = create["elementProperties"]
    assert properties["size"]["width"]["magnitude"] == pt_to_emu(160)
    assert properties["size"]["height"]["magnitude"] == pt_to_emu(90)
@pytest.mark.asyncio
async def test_drive_permission_failure_deletes_uploaded_file_and_stays_typed(
    tmp_path: Path,
) -> None:
    image_path = _write_png(tmp_path)
    requests: list[tuple[str, str]] = []
    permission_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/upload/drive/v3/files":
            return httpx.Response(200, json={"id": "created-file"})
        if request.url.path == "/drive/v3/files/created-file/permissions":
            permission_requests.append(request)
            return httpx.Response(403, text="permission denied")
        if (
            request.method == "DELETE"
            and request.url.path == "/drive/v3/files/created-file"
        ):
            return httpx.Response(204)
        raise AssertionError(f"unexpected Drive request: {request.method} {request.url}")

    uploader = GoogleDriveAssetUploader("token")
    await uploader._client.aclose()
    uploader._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(AssetUploadError, match="permission denied"):
            await uploader.upload(image_path, mime_type="image/png")
    finally:
        await uploader.close()

    assert requests == [
        ("POST", "/upload/drive/v3/files"),
        ("POST", "/drive/v3/files/created-file/permissions"),
        ("DELETE", "/drive/v3/files/created-file"),
    ]
    assert dict(permission_requests[0].url.params) == {"fields": "id"}
    assert json.loads(permission_requests[0].content) == {
        "type": "anyone",
        "role": "reader",
    }


@pytest.mark.asyncio
async def test_drive_malformed_json_raises_typed_asset_error() -> None:
    uploader = GoogleDriveAssetUploader("token")
    await uploader._client.aclose()
    uploader._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"not-json")
        )
    )
    try:
        with pytest.raises(AssetUploadError, match="invalid JSON"):
            await uploader._request("GET", "https://www.googleapis.com/drive/v3/files/1")
    finally:
        await uploader.close()


@pytest.mark.asyncio
async def test_replace_image_contain_pins_top_left_and_new_aspect_geometry(
    tmp_path: Path,
) -> None:
    transport, _, folder = await _workspace(tmp_path)
    uploader = FakeUploader()
    client = SlidesClient(transport, uploader)
    image_id = _first_clean_id(folder, transport.data, "image")
    google_id = json.loads((folder / "id_mapping.json").read_text())[image_id]
    target = _find_raw_element(transport.data, google_id)
    target["size"] = {
        "width": {"magnitude": pt_to_emu(220), "unit": "EMU"},
        "height": {"magnitude": pt_to_emu(124), "unit": "EMU"},
    }
    target["transform"] = {
        "scaleX": 1,
        "scaleY": 1,
        "translateX": pt_to_emu(40),
        "translateY": pt_to_emu(30),
        "unit": "EMU",
    }
    _write_png(folder, size=(900, 600))

    await client.replace_image(folder, image_id, "./assets/logo.png")

    assert transport.batch_calls[-1]["requests"] == [
        {
            "replaceImage": {
                "imageObjectId": google_id,
                "url": FAKE_URL,
                "imageReplaceMethod": "CENTER_INSIDE",
            }
        },
        {
            "updatePageElementTransform": {
                "objectId": google_id,
                "transform": {
                    "scaleX": 1,
                    "scaleY": 1,
                    "translateX": pt_to_emu(-17),
                    "translateY": 0,
                    "unit": "EMU",
                },
                "applyMode": "RELATIVE",
            }
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ancestor_transforms", "expected_geometry"),
    [
        (
            [
                {
                    "scaleX": 1,
                    "scaleY": 1,
                    "translateX": pt_to_emu(40),
                    "translateY": pt_to_emu(30),
                    "unit": "EMU",
                }
            ],
            {"x": 50, "y": 45, "w": 50, "h": 50},
        ),
        (
            [
                {
                    "scaleX": 2,
                    "scaleY": 3,
                    "translateX": pt_to_emu(40),
                    "translateY": pt_to_emu(30),
                    "unit": "EMU",
                }
            ],
            {"x": 60, "y": 75, "w": 150, "h": 150},
        ),
        (
            [
                {
                    "scaleX": 2,
                    "scaleY": 3,
                    "translateX": pt_to_emu(40),
                    "translateY": pt_to_emu(30),
                    "unit": "EMU",
                },
                {
                    "scaleX": 0.5,
                    "scaleY": 2,
                    "translateX": pt_to_emu(10),
                    "translateY": pt_to_emu(20),
                    "unit": "EMU",
                },
            ],
            {"x": 70, "y": 180, "w": 100, "h": 100},
        ),
    ],
    ids=("translated-group", "scaled-group", "nested-groups"),
)
async def test_replace_image_uses_slide_geometry_for_grouped_images(
    tmp_path: Path,
    ancestor_transforms: list[dict[str, Any]],
    expected_geometry: dict[str, float],
) -> None:
    _, client, folder, image_id = await _workspace_with_grouped_image(
        tmp_path, ancestor_transforms
    )
    _write_png(folder, size=(100, 100))

    preview = await client.replace_image(
        folder, image_id, "./assets/logo.png", dry_run=True
    )

    assert preview["geometry"] == {
        "fit": "contain",
        **expected_geometry,
        "unit": "PT",
    }


@pytest.mark.asyncio
async def test_replace_image_fetches_remote_dimensions_with_guarded_fetcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, _, folder = await _workspace(tmp_path)
    client = SlidesClient(transport)
    image_id = _first_clean_id(folder, transport.data, "image")
    google_id = json.loads((folder / "id_mapping.json").read_text())[image_id]

    calls: list[str] = []

    def fake_dimensions(url: str) -> tuple[int, int]:
        calls.append(url)
        return (900, 600)

    monkeypatch.setattr("slidesmith.engine.client.fetch_image_dimensions", fake_dimensions)

    await client.replace_image(folder, image_id, "https://example.com/new.png")

    assert calls == ["https://example.com/new.png"]
    assert transport.batch_calls[-1]["requests"][0] == {
        "replaceImage": {
            "imageObjectId": google_id,
            "url": "https://example.com/new.png",
            "imageReplaceMethod": "CENTER_INSIDE",
        }
    }


@pytest.mark.asyncio
async def test_replace_image_stretch_keeps_exact_old_box(tmp_path: Path) -> None:
    transport, _, folder = await _workspace(tmp_path)
    uploader = FakeUploader()
    client = SlidesClient(transport, uploader)
    image_id = _first_clean_id(folder, transport.data, "image")
    google_id = json.loads((folder / "id_mapping.json").read_text())[image_id]
    target = _find_raw_element(transport.data, google_id)
    target["size"] = {
        "width": {"magnitude": pt_to_emu(220), "unit": "EMU"},
        "height": {"magnitude": pt_to_emu(124), "unit": "EMU"},
    }
    target["transform"] = {
        "scaleX": 1,
        "scaleY": 1,
        "translateX": pt_to_emu(40),
        "translateY": pt_to_emu(30),
        "unit": "EMU",
    }
    _write_png(folder, size=(900, 600))

    await client.replace_image(folder, image_id, "./assets/logo.png", fit="stretch")

    transform = transport.batch_calls[-1]["requests"][1][
        "updatePageElementTransform"
    ]
    assert transform["objectId"] == google_id
    assert transform["applyMode"] == "RELATIVE"
    assert transform["transform"] == {
        "scaleX": pytest.approx(220 / 186),
        "scaleY": 1,
        "translateX": pytest.approx(pt_to_emu(40 - (220 / 186) * 57)),
        "translateY": 0,
        "unit": "EMU",
    }


@pytest.mark.asyncio
async def test_replace_image_dry_run_shows_geometry_and_requests_without_write(
    tmp_path: Path,
) -> None:
    transport, _, folder = await _workspace(tmp_path)
    uploader = FakeUploader()
    client = SlidesClient(transport, uploader)
    image_id = _first_clean_id(folder, transport.data, "image")
    google_id = json.loads((folder / "id_mapping.json").read_text())[image_id]
    target = _find_raw_element(transport.data, google_id)
    target["size"] = {
        "width": {"magnitude": pt_to_emu(220), "unit": "EMU"},
        "height": {"magnitude": pt_to_emu(124), "unit": "EMU"},
    }
    target["transform"] = {
        "scaleX": 1,
        "scaleY": 1,
        "translateX": pt_to_emu(40),
        "translateY": pt_to_emu(30),
        "unit": "EMU",
    }
    _write_png(folder, size=(900, 600))

    preview = await client.replace_image(
        folder, image_id, "./assets/logo.png", dry_run=True
    )

    assert transport.batch_calls == []
    assert preview["dryRun"] is True
    assert preview["geometry"] == {
        "fit": "contain",
        "x": 40,
        "y": 30,
        "w": 186,
        "h": 124,
        "unit": "PT",
    }
    assert [next(iter(request)) for request in preview["requests"]] == [
        "replaceImage",
        "updatePageElementTransform",
    ]
    assert preview["requests"][0]["replaceImage"]["imageObjectId"] == google_id


@pytest.mark.asyncio
async def test_replace_image_rejects_non_image_element_clearly(tmp_path: Path) -> None:
    transport, _, folder = await _workspace(tmp_path)
    uploader = FakeUploader()
    client = SlidesClient(transport, uploader)
    shape_id = _first_clean_id(folder, transport.data, "shape")

    with pytest.raises(ValueError, match=rf"Element '{shape_id}' is not an image"):
        await client.replace_image(folder, shape_id, "./missing.png")

    assert transport.batch_calls == []
    assert uploader.calls == []


@pytest.mark.asyncio
async def test_replace_image_refuses_pending_workspace_edits(tmp_path: Path) -> None:
    transport, client, folder = await _workspace(tmp_path)
    image_id = _first_clean_id(folder, transport.data, "image")
    content_path = folder / "slides" / "01" / "content.sml"
    original = content_path.read_text(encoding="utf-8")
    content_path.write_text(
        original.replace(
            "</Slide>",
            '<Rect id="pending_shape" x="1" y="1" w="10" h="10" />\n</Slide>',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requires a clean workspace"):
        await client.replace_image(folder, image_id, "https://example.com/new.png")

    assert transport.batch_calls == []


@pytest.mark.asyncio
async def test_missing_local_image_file_errors_loudly(tmp_path: Path) -> None:
    _, client, folder = await _workspace(tmp_path)
    _append_local_image(folder, source="./assets/missing.png")

    with pytest.raises(
        FileNotFoundError,
        match=r"Local image './assets/missing.png' was not found",
    ):
        client.diff(folder)


def test_local_image_fit_cover_is_accepted_and_unknown_fit_is_rejected() -> None:
    image = parse_slide_content(
        '<Slide><Image id="local_logo" src="./assets/logo.png" fit="cover" '
        'x="1" y="2" w="100" h="100" /></Slide>'
    )[0]
    assert image.fit == "cover"
    with pytest.raises(
        ValueError,
        match=r"Invalid fit 'tile'.*stretch.*contain.*cover",
    ):
        parse_slide_content(
            '<Slide><Image id="local_logo" src="./assets/logo.png" fit="tile" '
            'x="1" y="2" w="100" h="100" /></Slide>'
        )


def test_local_image_geometry_allows_zero_origin() -> None:
    image = parse_slide_content(
        '<Slide><Image id="local_logo" src="./assets/logo.png" '
        'x="0" y="2" w="100" h="100" /></Slide>'
    )[0]

    assert image.x == 0
    assert image.y == 2


def test_replace_image_cli_accepts_folder_element_and_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_command(args: Any) -> None:
        captured.update(
            folder=args.folder,
            element_id=args.element_id,
            new_src=args.new_src,
            fit=args.fit,
            dry_run=args.dry_run,
        )

    monkeypatch.setattr(cli, "cmd_replace_image", fake_command)
    cli.main(
        [
            "replace-image",
            "deck-folder",
            "hero_image",
            "./hero.png",
            "--fit",
            "stretch",
            "--dry-run",
        ]
    )

    assert captured == {
        "folder": "deck-folder",
        "element_id": "hero_image",
        "new_src": "./hero.png",
        "fit": "stretch",
        "dry_run": True,
    }


@pytest.mark.asyncio
async def test_local_contain_reads_dimensions_with_pillow_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, client, folder = await _workspace(tmp_path)
    _write_png(folder)
    _append_local_image(folder, fit="contain", geometry='x="1" y="2" w="100" h="100"')
    monkeypatch.setattr(
        "slidesmith.engine.content_diff.fetch_image_dimensions",
        lambda _url: pytest.fail("local contain must not fetch over the network"),
    )

    request = next(r for r in client.diff(folder) if "createImage" in r)
    transform = request["createImage"]["elementProperties"]["transform"]
    size = request["createImage"]["elementProperties"]["size"]
    visual_width = round(transform["scaleX"] * size["width"]["magnitude"])
    visual_height = round(transform["scaleY"] * size["height"]["magnitude"])
    assert visual_width == 1_270_000
    assert visual_height == 635_000
