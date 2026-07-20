"""Offline M4 contracts for local assets and replace-image."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from slidesmith import cli
from slidesmith.engine.assets import UploadedAsset
from slidesmith.engine.client import SlidesClient
from slidesmith.engine.content_parser import parse_slide_content
from slidesmith.engine.transport import APIError, PresentationData, Transport

GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)
FAKE_URL = "https://drive.google.com/uc?export=download&id=fake-drive-file"


class FakeUploader:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    async def upload(self, path: Path, *, mime_type: str) -> UploadedAsset:
        self.calls.append((path, mime_type))
        return UploadedAsset(file_id="fake-drive-file", url=FAKE_URL)

    async def close(self) -> None:
        pass


class ImageTransport(Transport):
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = copy.deepcopy(data)
        self.batch_calls: list[dict[str, Any]] = []
        self.fail_next_batch = False

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
                            "sourceUrl": create["url"],
                            "imageProperties": {},
                        },
                    }
                )
            if replace := request.get("replaceImage"):
                element = _find_raw_element(self.data, replace["imageObjectId"])
                element["image"]["contentUrl"] = replace["url"]
                element["image"]["sourceUrl"] = replace["url"]

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


def _write_png(folder: Path, relative: str = "assets/logo.png") -> Path:
    path = folder / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 40), "navy").save(path)
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

    await client.push(folder)

    create = next(
        request["createImage"]
        for request in transport.batch_calls[-1]["requests"]
        if "createImage" in request
    )
    assert create["url"] == FAKE_URL
    assert uploader.calls == [(folder / "assets" / "logo.png", "image/png")]


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
async def test_replace_image_targets_image_and_preserves_position_and_size(
    tmp_path: Path,
) -> None:
    transport, _, folder = await _workspace(tmp_path)
    uploader = FakeUploader()
    client = SlidesClient(transport, uploader)
    image_id = _first_clean_id(folder, transport.data, "image")
    google_id = json.loads((folder / "id_mapping.json").read_text())[image_id]
    target = _find_raw_element(transport.data, google_id)
    original_size = copy.deepcopy(target["size"])
    original_transform = copy.deepcopy(target["transform"])
    _write_png(folder)

    await client.replace_image(folder, image_id, "./assets/logo.png")

    assert transport.batch_calls[-1]["requests"] == [
        {
            "replaceImage": {
                "imageObjectId": google_id,
                "url": FAKE_URL,
                "imageReplaceMethod": "CENTER_INSIDE",
            }
        }
    ]
    replaced = _find_raw_element(transport.data, google_id)
    assert replaced["size"] == original_size
    assert replaced["transform"] == original_transform


@pytest.mark.asyncio
async def test_replace_image_accepts_public_url_without_uploader(tmp_path: Path) -> None:
    transport, _, folder = await _workspace(tmp_path)
    client = SlidesClient(transport)
    image_id = _first_clean_id(folder, transport.data, "image")
    google_id = json.loads((folder / "id_mapping.json").read_text())[image_id]

    await client.replace_image(folder, image_id, "https://example.com/new.png")

    assert transport.batch_calls[-1]["requests"] == [
        {
            "replaceImage": {
                "imageObjectId": google_id,
                "url": "https://example.com/new.png",
                "imageReplaceMethod": "CENTER_INSIDE",
            }
        }
    ]


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


def test_local_image_fit_cover_stays_clearly_unsupported() -> None:
    with pytest.raises(
        ValueError,
        match=r"fit='cover'.*unsupported.*cropProperties are read-only",
    ):
        parse_slide_content(
            '<Slide><Image id="local_logo" src="./assets/logo.png" fit="cover" '
            'x="1" y="2" w="100" h="100" /></Slide>'
        )


def test_positive_geometry_validation_applies_to_local_images() -> None:
    with pytest.raises(
        ValueError,
        match=r"Image element 'local_logo'.*finite, strictly-positive x/y/w/h",
    ):
        parse_slide_content(
            '<Slide><Image id="local_logo" src="./assets/logo.png" '
            'x="0" y="2" w="100" h="100" /></Slide>'
        )


def test_replace_image_cli_accepts_folder_element_and_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_command(args: Any) -> None:
        captured.update(
            folder=args.folder,
            element_id=args.element_id,
            new_src=args.new_src,
        )

    monkeypatch.setattr(cli, "cmd_replace_image", fake_command)
    cli.main(["replace-image", "deck-folder", "hero_image", "./hero.png"])

    assert captured == {
        "folder": "deck-folder",
        "element_id": "hero_image",
        "new_src": "./hero.png",
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
        "slidesmith.engine.content_diff._fetch_image_dimensions",
        lambda _url: pytest.fail("local contain must not fetch over the network"),
    )

    request = next(r for r in client.diff(folder) if "createImage" in r)
    transform = request["createImage"]["elementProperties"]["transform"]
    size = request["createImage"]["elementProperties"]["size"]
    visual_width = round(transform["scaleX"] * size["width"]["magnitude"])
    visual_height = round(transform["scaleY"] * size["height"]["magnitude"])
    assert visual_width == 1_270_000
    assert visual_height == 635_000
