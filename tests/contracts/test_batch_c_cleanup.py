"""Regression contracts for review-fix Batch C cleanup."""

from pathlib import Path

import pytest

from extraslide.content_requests import _parse_color, _tag_to_type
from extraslide.json_utils import read_json
from extraslide.shape_types import TAG_TO_TYPE, TYPE_TO_TAG, VALID_GOOGLE_TYPES


def test_shape_tables_are_derived_from_one_round_trip_mapping() -> None:
    assert len(TAG_TO_TYPE) == len(TYPE_TO_TAG) == 129
    assert TYPE_TO_TAG == {
        element_type: tag for tag, element_type in TAG_TO_TYPE.items()
    }
    assert all(
        _tag_to_type(tag) == element_type
        for tag, element_type in TAG_TO_TYPE.items()
    )
    assert VALID_GOOGLE_TYPES == frozenset(TAG_TO_TYPE.values()) - {
        "GROUP",
        "IMAGE",
        "LINE",
        "SHEETS_CHART",
        "TABLE",
        "VIDEO",
    }


@pytest.mark.parametrize("color", ["#12345", "#12gg00", "not-a-color"])
def test_malformed_styles_json_colors_raise(color: str) -> None:
    with pytest.raises(ValueError):
        _parse_color(color)


def test_shared_read_json_requires_explicit_missing_policy(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    assert read_json(missing, missing_ok=True) == {}
    with pytest.raises(ValueError, match="Missing Slidesmith workspace file"):
        read_json(missing, missing_ok=False)
