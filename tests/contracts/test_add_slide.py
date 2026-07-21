"""Contracts for local slide scaffolding and positioned slide creation."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from slidesmith.cli import main
from slidesmith.engine.client import SlidesClient, diff_folder
from slidesmith.engine.content_diff import Change, ChangeType, DiffResult
from slidesmith.engine.content_parser import parse_slide_content
from slidesmith.engine.content_requests import generate_batch_requests
from slidesmith.engine.id_manager import is_valid_google_object_id
from slidesmith.engine.push_progress import partition_requests_by_slide
from slidesmith.engine.qa import check_folder, finding_id, lint_folder
from slidesmith.engine.slide_scaffold import scaffold_slide
from slidesmith.engine.transport import PresentationData, Transport
from slidesmith.workspace import materialize


GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)


def _workspace(tmp_path: Path) -> Path:
    return materialize(json.loads(GOLDEN.read_text(encoding="utf-8")), tmp_path)


def test_add_slide_dry_run_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = _workspace(tmp_path)
    before = {
        path.relative_to(folder): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file()
    }

    main(["add-slide", str(folder), "--at", "2", "--blank", "--dry-run"])

    after = {
        path.relative_to(folder): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file()
    }
    output = capsys.readouterr().out
    assert before == after
    assert "would scaffold" in output
    assert "insertionIndex=1" in output
    assert "Dry run: no files written." in output


def test_add_slide_scaffolds_positioned_blank_slide_and_diff_request() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as temp_dir:
        folder = _workspace(Path(temp_dir))
        result = scaffold_slide(
            folder,
            after=2,
            blank=True,
            slide_id="agenda_slide",
        )

        assert result.content_path == folder / "slides" / "05" / "content.sml"
        content = result.content_path.read_text(encoding="utf-8")
        assert content == (
            '<Slide id="agenda_slide" insertion-index="2">\n</Slide>\n'
        )
        requests = diff_folder(folder)
        assert requests[0] == {
            "createSlide": {
                "objectId": requests[0]["createSlide"]["objectId"],
                "insertionIndex": 2,
            }
        }
        assert requests[0]["createSlide"]["objectId"] == "agenda_slide"


def test_add_slide_layout_and_append_default_keep_legacy_request_shape(
    tmp_path: Path,
) -> None:
    folder = _workspace(tmp_path)
    result = scaffold_slide(folder, layout="title-body", slide_id="launch_slide")
    content = result.content_path.read_text(encoding="utf-8")
    assert '<TextBox id="launch_slide_title"' in content
    assert '<TextBox id="launch_slide_body"' in content

    create_slide = next(
        request["createSlide"]
        for request in diff_folder(folder)
        if "createSlide" in request
    )
    assert "insertionIndex" not in create_slide


def test_at_translates_one_based_position_to_zero_based_index(tmp_path: Path) -> None:
    folder = _workspace(tmp_path)
    scaffold_slide(folder, at=3, blank=True, slide_id="middle_slide")
    create_slide = next(
        request["createSlide"]
        for request in diff_folder(folder)
        if "createSlide" in request
    )
    assert create_slide["insertionIndex"] == 2


def test_position_bounds_ignore_pending_scaffolds(tmp_path: Path) -> None:
    folder = _workspace(tmp_path)

    scaffold_slide(folder, at=5, blank=True, slide_id="append_slide")

    with pytest.raises(ValueError, match=r"--at 6 exceeds deck length 4"):
        scaffold_slide(folder, at=6, blank=True, slide_id="too_far")
    with pytest.raises(ValueError, match=r"--after 5 exceeds deck length 4"):
        scaffold_slide(folder, after=5, blank=True, slide_id="after_too_far")


def test_append_scaffold_does_not_extend_original_position_bounds(
    tmp_path: Path,
) -> None:
    folder = _workspace(tmp_path)

    scaffold_slide(folder, blank=True, slide_id="append_slide")

    with pytest.raises(ValueError, match=r"--at 6 exceeds deck length 4"):
        scaffold_slide(folder, at=6, blank=True, slide_id="too_far")


def test_append_scaffold_keeps_original_coordinates_for_later_positioned_add(
    tmp_path: Path,
) -> None:
    folder = _workspace(tmp_path)

    scaffold_slide(folder, blank=True, slide_id="append_slide")
    scaffold_slide(folder, at=5, blank=True, slide_id="last_original_position")

    create_requests = [
        request["createSlide"]
        for request in diff_folder(folder)
        if "createSlide" in request
    ]

    assert create_requests[0]["insertionIndex"] == 4
    assert "insertionIndex" not in create_requests[1]


def test_empty_mapping_falls_back_to_pristine_pulled_slide_set(
    tmp_path: Path,
) -> None:
    folder = _workspace(tmp_path)
    (folder / "id_mapping.json").write_text("{}", encoding="utf-8")

    scaffold_slide(folder, blank=True, slide_id="append_slide")

    with pytest.raises(ValueError, match=r"--at 6 exceeds deck length 4"):
        scaffold_slide(folder, at=6, blank=True, slide_id="too_far")


def test_multiple_scaffolds_keep_original_coordinates_for_request_shift(
    tmp_path: Path,
) -> None:
    folder = _workspace(tmp_path)
    scaffold_slide(folder, at=1, blank=True, slide_id="first_insert")
    scaffold_slide(folder, at=2, blank=True, slide_id="second_insert")

    create_requests = [
        request["createSlide"]
        for request in diff_folder(folder)
        if "createSlide" in request
    ]

    assert [request["insertionIndex"] for request in create_requests] == [0, 2]
    deck = ["A", "B", "C", "D"]
    labels = {
        request["objectId"]: label
        for request, label in zip(
            create_requests, ("first", "second"), strict=True
        )
    }
    for request in create_requests:
        deck.insert(request["insertionIndex"], labels[request["objectId"]])
    assert deck == ["first", "A", "second", "B", "C", "D"]


@pytest.mark.parametrize("page_size", [(720.0, 405.0), (320.0, 180.0)])
def test_title_body_scaffold_stays_within_small_page_bounds(
    tmp_path: Path, page_size: tuple[float, float]
) -> None:
    folder = _workspace(tmp_path)
    metadata_path = folder / "presentation.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    page_width, page_height = page_size
    metadata["pageSize"] = {"width": page_width, "height": page_height}
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    result = scaffold_slide(folder, layout="title-body", slide_id="small_slide")
    elements = parse_slide_content(result.content_path.read_text(encoding="utf-8"))

    assert len(elements) == 2
    for element in elements:
        assert element.x is not None and element.y is not None
        assert element.w is not None and element.h is not None
        assert 0 <= element.x <= page_width
        assert 0 <= element.y <= page_height
        assert element.x + element.w <= page_width
        assert element.y + element.h <= page_height
    assert not any(
        finding.rule == "TEXT_OVERFLOW" and finding.slide_number == 5
        for finding in lint_folder(folder)
    )


def test_multiple_positioned_slides_shift_later_indices_and_land_in_order() -> None:
    changes = [
        Change(
            ChangeType.CREATE_SLIDE,
            "first_slide",
            slide_index="05",
            insertion_index=1,
        ),
        Change(
            ChangeType.CREATE_SLIDE,
            "second_slide",
            slide_index="06",
            insertion_index=3,
        ),
    ]
    requests = generate_batch_requests(DiffResult(changes=changes), {}, {})
    create_requests = [request["createSlide"] for request in requests]

    assert [request["insertionIndex"] for request in create_requests] == [1, 4]
    deck = ["A", "B", "C", "D"]
    labels = {
        request["objectId"]: label
        for request, label in zip(create_requests, ("first", "second"), strict=True)
    }
    for request in create_requests:
        deck.insert(request["insertionIndex"], labels[request["objectId"]])
    assert deck == ["A", "first", "B", "C", "second", "D"]


def test_append_default_create_request_remains_unpositioned() -> None:
    requests = generate_batch_requests(
        DiffResult(
            changes=[
                Change(
                    ChangeType.CREATE,
                    "new_box",
                    slide_index="03",
                    new_position={"x": 0, "y": 0, "w": 10, "h": 10},
                    tag="Rect",
                )
            ]
        ),
        {},
        {},
    )
    assert requests[0]["createSlide"] == {
        "objectId": requests[0]["createSlide"]["objectId"]
    }


def test_positioned_new_slides_partition_by_object_id_across_100_boundary(
    tmp_path: Path,
) -> None:
    for slide_index in ("99", "100"):
        content_path = tmp_path / "deck" / "slides" / slide_index / "content.sml"
        content_path.parent.mkdir(parents=True, exist_ok=True)
        content_path.write_text(
            f'<Slide id="slide_{slide_index}"><Rect id="box_{slide_index}" '
            'x="0" y="0" w="10" h="10" /></Slide>',
            encoding="utf-8",
        )
    changes = [
        Change(ChangeType.CREATE_SLIDE, f"slide_{index}", slide_index=index)
        for index in ("99", "100")
    ] + [
        Change(
            ChangeType.CREATE,
            f"box_{index}",
            slide_index=index,
            new_position={"x": 0, "y": 0, "w": 10, "h": 10},
            tag="Rect",
        )
        for index in ("99", "100")
    ]
    diff_result = DiffResult(changes=changes)
    requests = generate_batch_requests(diff_result, {}, {})
    batches = partition_requests_by_slide(
        requests,
        diff_result,
        {},
        {},
        {"slides": []},
        tmp_path / "deck",
    )

    assert [batch.slide_index for batch in batches] == ["99", "100"]
    assert batches[0].requests[0]["createSlide"]["objectId"] == "slide_99"
    assert batches[1].requests[0]["createSlide"]["objectId"] == "slide_100"


def test_reserved_and_invalid_slide_ids_are_rejected(tmp_path: Path) -> None:
    folder = _workspace(tmp_path)
    with pytest.raises(ValueError, match="reserved 'new_'"):
        scaffold_slide(folder, slide_id="new_slide_id")
    with pytest.raises(ValueError, match="5-50 characters"):
        scaffold_slide(folder, slide_id="bad")
    assert is_valid_google_object_id("agenda_slide")


def test_authored_slide_id_falls_back_when_google_object_id_is_taken() -> None:
    diff_result = DiffResult(
        changes=[Change(ChangeType.CREATE_SLIDE, "agenda_slide", slide_index="05")]
    )

    requests = generate_batch_requests(
        diff_result,
        {"existing_element": "agenda_slide"},
        {},
    )

    assert requests == [{"createSlide": {"objectId": "agenda_slide_2"}}]
    assert diff_result.generated_slide_ids == {"05": "agenda_slide_2"}


def test_scaffold_refuses_target_folder_if_it_appears_during_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _workspace(tmp_path)
    original_mkdir = Path.mkdir

    def race_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == folder / "slides" / "05":
            raise FileExistsError(self)
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", race_mkdir)
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        scaffold_slide(folder, blank=True)


class _CreateSlideTransport(Transport):
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = copy.deepcopy(data)
        self.data["revisionId"] = "rev-1"
        self.revision = 1

    async def get_presentation(self, presentation_id: str) -> PresentationData:
        return PresentationData(
            presentation_id,
            copy.deepcopy(self.data),
        )

    async def batch_update(
        self,
        presentation_id: str,
        requests: list[dict[str, Any]],
        required_revision_id: str | None = None,
    ) -> dict[str, Any]:
        for request in requests:
            body = request.get("createSlide")
            if isinstance(body, dict):
                slide = {"objectId": body["objectId"], "pageElements": []}
                index = body.get("insertionIndex")
                if index is None:
                    self.data.setdefault("slides", []).append(slide)
                else:
                    self.data.setdefault("slides", []).insert(index, slide)
                continue

            body = request.get("createShape")
            if not isinstance(body, dict):
                continue
            properties = body["elementProperties"]
            slide = next(
                slide
                for slide in self.data["slides"]
                if slide["objectId"] == properties["pageObjectId"]
            )
            slide["pageElements"].append(
                {
                    "objectId": body["objectId"],
                    "size": copy.deepcopy(properties["size"]),
                    "transform": copy.deepcopy(properties["transform"]),
                    "shape": {"shapeType": body["shapeType"]},
                }
            )
        self.revision += 1
        self.data["revisionId"] = f"rev-{self.revision}"
        return {"replies": [{} for _ in requests]}

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_per_slide_positioned_multi_add_matches_atomic_order(
    tmp_path: Path,
) -> None:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    atomic_transport = _CreateSlideTransport(data)
    per_slide_transport = _CreateSlideTransport(data)
    atomic_client = SlidesClient(atomic_transport)
    per_slide_client = SlidesClient(per_slide_transport)

    atomic_root = tmp_path / "atomic"
    per_slide_root = tmp_path / "per-slide"
    await atomic_client.pull(data["presentationId"], atomic_root, save_raw=False)
    await per_slide_client.pull(
        data["presentationId"], per_slide_root, save_raw=False
    )
    atomic_folder = atomic_root / data["presentationId"]
    per_slide_folder = per_slide_root / data["presentationId"]
    for folder in (atomic_folder, per_slide_folder):
        scaffold_slide(folder, at=3, blank=True, slide_id="third_insert")
        scaffold_slide(folder, at=1, blank=True, slide_id="first_insert")

    await atomic_client.push(atomic_folder)
    await per_slide_client.push(per_slide_folder, per_slide=True)

    def labels(transport: _CreateSlideTransport) -> list[str]:
        result = []
        for slide in transport.data["slides"]:
            object_id = slide["objectId"]
            if object_id == "third_insert":
                result.append("third")
            elif object_id == "first_insert":
                result.append("first")
            else:
                result.append(object_id)
        return result

    expected = [
        "first",
        data["slides"][0]["objectId"],
        data["slides"][1]["objectId"],
        "third",
        data["slides"][2]["objectId"],
        data["slides"][3]["objectId"],
    ]
    assert labels(atomic_transport) == expected
    assert labels(per_slide_transport) == labels(atomic_transport)


@pytest.mark.asyncio
async def test_push_refresh_strips_intent_and_no_edit_diff_is_zero(
    tmp_path: Path,
) -> None:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    transport = _CreateSlideTransport(data)
    client = SlidesClient(transport)
    await client.pull(data["presentationId"], tmp_path, save_raw=False)
    folder = tmp_path / data["presentationId"]
    scaffold_slide(folder, after=1, blank=True, slide_id="inserted_slide")

    response = await client.push(folder)

    assert response["replies"]
    assert diff_folder(folder) == []
    assert all(
        "insertion-index" not in path.read_text(encoding="utf-8")
        for path in sorted((folder / "slides").glob("*/content.sml"))
    )


@pytest.mark.asyncio
async def test_authored_slide_id_and_qa_acceptance_survive_push_refresh(
    tmp_path: Path,
) -> None:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    transport = _CreateSlideTransport(data)
    client = SlidesClient(transport)
    await client.pull(data["presentationId"], tmp_path, save_raw=False)
    folder = tmp_path / data["presentationId"]
    result = scaffold_slide(folder, blank=True, slide_id="agenda_slide")
    result.content_path.write_text(
        "<Slide id=\"agenda_slide\">\n"
        "  <Rect id=\"accept_left\" x=\"10\" y=\"10\" w=\"100\" h=\"100\" />\n"
        "  <Rect id=\"accept_right\" x=\"50\" y=\"50\" w=\"100\" h=\"100\" />\n"
        "</Slide>\n",
        encoding="utf-8",
    )

    before = next(
        finding
        for finding in lint_folder(folder)
        if finding.rule == "OVERLAP" and finding.slide_number == 5
    )
    acceptance_id = finding_id(before)
    assert before.slide_id == "agenda_slide"
    assert check_folder(folder, accept=[acceptance_id], output=lambda _: None) == 0

    diff_result, requests = client.diff_with_result(folder)
    create_slide = next(request["createSlide"] for request in requests if "createSlide" in request)
    assert create_slide["objectId"] == "agenda_slide"
    assert diff_result.generated_slide_ids == {"05": "agenda_slide"}

    await client.push(folder)

    refreshed_slide = next(
        path
        for path in sorted((folder / "slides").glob("*/content.sml"))
        if 'id="agenda_slide"' in path.read_text(encoding="utf-8")
    )
    assert 'id="agenda_slide"' in refreshed_slide.read_text(encoding="utf-8")
    after = next(
        finding
        for finding in lint_folder(folder)
        if finding.rule == "OVERLAP" and finding.slide_number == 5
    )
    assert finding_id(after) == acceptance_id
    output: list[str] = []
    assert check_folder(folder, output=output.append) == 0
    assert any("[ACCEPTED]" in line and acceptance_id in line for line in output)
