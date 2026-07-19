"""The six contracts slidesmith must satisfy (see DESIGN.md).

C1  Pull -> no edits -> diff produces zero requests.
C2  Create a styled text box using the documented class syntax.
C3  Edit text while preserving all human styling.
C4  Human and agent edit different properties of the same element; both survive.
C5  Human and agent edit the same property; push aborts with a useful conflict.
C6  Pull -> push -> pull is idempotent.

C1/C6 run offline against the golden fixture. C2 is xfail until the parser
consumes class attributes (the donor codebase never wired classes.py in).
C3-C5 need a live deck: set SLIDESMITH_LIVE_DECK=<presentationId> to enable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from extraslide.client import diff_folder
from slidesmith.workspace import materialize

GOLDEN = (
    Path(__file__).parent.parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)

LIVE_DECK = os.environ.get("SLIDESMITH_LIVE_DECK")
live = pytest.mark.skipif(
    not LIVE_DECK,
    reason="set SLIDESMITH_LIVE_DECK=<presentationId> to run live contracts",
)


@pytest.fixture
def golden_folder(tmp_path: Path) -> Path:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return materialize(data, tmp_path)


def test_c1_pull_without_edits_diffs_to_zero_requests(golden_folder: Path) -> None:
    assert diff_folder(golden_folder) == []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known gap inherited from extraslide: the SML parser only reads "
        "id/x/y/w/h and <P> text; class attributes are dropped, so new "
        "elements lose their styling. Fixed by the typed parser rewrite."
    ),
)
def test_c2_create_styled_textbox_via_documented_syntax(golden_folder: Path) -> None:
    slide_dir = sorted((golden_folder / "slides").iterdir())[0]
    sml = slide_dir / "content.sml"
    new_element = (
        '<TextBox id="new_box1" x="100" y="100" w="200" h="50" '
        'class="fill-#ff0000 text-size-24"><P>Hello</P></TextBox>'
    )
    sml.write_text(
        sml.read_text(encoding="utf-8").replace("</Slide>", new_element + "</Slide>"),
        encoding="utf-8",
    )

    requests = diff_folder(golden_folder)
    blob = json.dumps(requests)
    assert any("createShape" in r for r in requests), "new element not created at all"
    assert "shapeBackgroundFill" in blob or "fontSize" in blob, (
        "class-derived styling was dropped on the way to batchUpdate requests"
    )


def test_c6_materialize_is_deterministic(tmp_path: Path) -> None:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    a = materialize(data, tmp_path / "a")
    b = materialize(data, tmp_path / "b")
    rels = sorted(p.relative_to(a) for p in a.rglob("content.sml"))
    assert rels == sorted(p.relative_to(b) for p in b.rglob("content.sml"))
    for rel in rels:
        assert (a / rel).read_text(encoding="utf-8") == (b / rel).read_text(
            encoding="utf-8"
        )


def test_c6_noop_rewrite_still_diffs_to_zero(golden_folder: Path) -> None:
    for sml in golden_folder.rglob("content.sml"):
        if ".pristine" in sml.parts:
            continue
        sml.write_text(sml.read_text(encoding="utf-8"), encoding="utf-8")
    assert diff_folder(golden_folder) == []


@live
def test_c3_text_edit_preserves_human_styling() -> None:
    pytest.skip("TODO: live contract — pull, edit one word, push, re-pull, assert styles unchanged")


@live
def test_c4_disjoint_edits_both_survive() -> None:
    pytest.skip("TODO: live contract — agent moves element while human recolors it; both persist")


@live
@pytest.mark.xfail(reason="requires revision locking (writeControl) — not yet implemented")
def test_c5_same_property_conflict_aborts_push() -> None:
    pytest.skip("TODO: live contract — concurrent same-property edit must abort with conflict")
