"""R3 track-2 persistence and effective-style regression contracts."""

from __future__ import annotations

import pytest

from slidesmith.engine.content_parser import parse_slide_content
from slidesmith.engine.diff_model import Change, ChangeType, WarningSeverity
from slidesmith.engine.persistence import _persistence_warning_severity
from slidesmith.engine.text_requests import _create_effective_style_requests


def _element(content: str):
    return parse_slide_content(content)[0]


def _severity(
    intended_content: str,
    remote_content: str,
    *,
    change_type: ChangeType = ChangeType.STYLE_UPDATE,
    newly_created: bool = True,
    author_removed_classes: frozenset[str] = frozenset(),
) -> WarningSeverity | None:
    intended = _element(intended_content)
    remote = _element(remote_content)
    change = Change(
        change_type,
        intended.clean_id,
        new_text=intended.paragraphs,
        old_text=remote.paragraphs,
        new_runs=intended.runs,
        old_runs=remote.runs,
        slide_index="01",
        author_removed_classes=author_removed_classes,
    )
    key = ("01", intended.clean_id)
    return _persistence_warning_severity(
        change,
        {key: remote},
        {key: intended},
        newly_created=newly_created,
        author_removed_classes=author_removed_classes,
    )


_ROUNDRECT_FIELD_PAIRS = (
    ("metric-native", "fill-#15243a stroke-#2b3a52 stroke-solid"),
    ("metric-proof", "fill-#15243a stroke-#2b3a52 stroke-solid"),
    ("metric-recovery", "fill-#15243a stroke-#2b3a52 stroke-solid"),
    ("friction-before", "fill-#15243a stroke-#2b3a52 stroke-solid"),
    ("friction-after", "fill-#15243a stroke-#64e5b3/70 stroke-solid"),
    ("ship-card-one", "fill-#15243a stroke-#2b3a52 stroke-solid"),
    ("ship-card-two", "fill-#15243a stroke-#2b3a52 stroke-solid"),
    ("ship-card-three", "fill-#15243a stroke-#2b3a52 stroke-solid"),
    ("ship-card-four", "fill-#15243a stroke-#2b3a52 stroke-solid"),
    ("ship-card-five", "fill-#15243a stroke-#2b3a52 stroke-solid"),
    ("ship-card-six", "fill-#15243a stroke-#2b3a52 stroke-solid"),
    (
        "scope-element",
        "fill-#15243a stroke-#2b3a52 stroke-solid",
    ),
    ("scope-run", "fill-#15243a stroke-#2b3a52 stroke-solid"),
    ("qa-render", "fill-#0e2c25 stroke-#64e5b3/60 stroke-solid"),
    ("qa-advise", "fill-#2c2112 stroke-#ffbe70/60 stroke-solid"),
    ("risk-one", "fill-#15243a stroke-#ff7a8a/60 stroke-solid"),
    ("risk-two", "fill-#15243a stroke-#ff7a8a/60 stroke-solid"),
    ("risk-three", "fill-#15243a stroke-#ffbe70/60 stroke-solid"),
    ("risk-four", "fill-#15243a stroke-#ffbe70/60 stroke-solid"),
)


@pytest.mark.parametrize(("element_id", "authored_class"), _ROUNDRECT_FIELD_PAIRS)
def test_created_roundrect_center_default_suppresses_all_19_field_pairs(
    element_id: str,
    authored_class: str,
) -> None:
    intended = (
        f'<RoundRect id="{element_id}" class="{authored_class}">'
        "<P>field</P></RoundRect>"
    )
    remote = (
        f'<RoundRect id="{element_id}" class="{authored_class} '
        "stroke-w-0.75 text-align-center leading-100 space-above-0 "
        'space-below-0 indent-start-0 indent-first-0 '
        'spacing-collapse-lists content-align-middle">'
        "<P>field</P></RoundRect>"
    )

    assert _severity(intended, remote) is None


def test_created_roundrect_center_exemption_is_not_for_existing_elements() -> None:
    intended = '<RoundRect id="field"><P>field</P></RoundRect>'
    remote = '<RoundRect id="field" class="text-align-center"><P>field</P></RoundRect>'

    assert _severity(intended, remote, newly_created=False) is WarningSeverity.WARNING


@pytest.mark.parametrize("authored_alignment", ["text-align-left", "text-align-right"])
def test_created_roundrect_authored_horizontal_alignment_still_warns(
    authored_alignment: str,
) -> None:
    intended = f'<RoundRect id="field" class="{authored_alignment}"><P>field</P></RoundRect>'
    remote = '<RoundRect id="field" class="text-align-center"><P>field</P></RoundRect>'

    assert _severity(intended, remote) is WarningSeverity.WARNING


def test_created_roundrect_removed_center_alignment_still_warns() -> None:
    intended = '<RoundRect id="field"><P>field</P></RoundRect>'
    remote = '<RoundRect id="field" class="text-align-center"><P>field</P></RoundRect>'

    assert _severity(
        intended,
        remote,
        author_removed_classes=frozenset({"text-align-center"}),
    ) is WarningSeverity.WARNING


def test_created_textbox_center_alignment_still_warns() -> None:
    intended = '<TextBox id="field"><P>field</P></TextBox>'
    remote = '<TextBox id="field" class="text-align-center"><P>field</P></TextBox>'

    assert _severity(intended, remote) is WarningSeverity.WARNING


def test_created_roundrect_center_bundle_with_dropped_text_color_warns() -> None:
    intended = (
        '<RoundRect id="field"><P><T class="text-color-#ff0000">field</T>'
        "</P></RoundRect>"
    )
    remote = (
        '<RoundRect id="field" class="text-align-center leading-100 '
        'space-above-0 space-below-0 indent-start-0 indent-first-0 '
        'spacing-collapse-lists"><P>field</P></RoundRect>'
    )

    assert _severity(intended, remote) is WarningSeverity.WARNING


@pytest.mark.parametrize("element_id", ["scope-element", "scope-run"])
def test_text_update_scope_comparison_ignores_only_paragraph_defaults(
    element_id: str,
) -> None:
    intended = (
        f'<RoundRect id="{element_id}"><P><T class="bold '
        'text-color-#f4f1e8">same text</T></P></RoundRect>'
    )
    remote = (
        f'<RoundRect id="{element_id}"><P class="text-align-center">'
        '<T class="bold text-color-#f4f1e8">same text</T></P></RoundRect>'
    )

    assert _severity(
        intended,
        remote,
        change_type=ChangeType.TEXT_UPDATE,
        newly_created=False,
    ) is None


@pytest.mark.parametrize(
    "dropped_class",
    ["text-color-#f4f1e8", "text-size-16", "bold", "font-family-arial"],
)
def test_text_update_scope_comparison_still_warns_on_dropped_run_style(
    dropped_class: str,
) -> None:
    intended = (
        '<TextBox id="scope-run"><P><T class="bold font-family-arial '
        'text-size-16 text-color-#f4f1e8">same text</T></P></TextBox>'
    )
    remaining = {
        "text-color-#f4f1e8",
        "text-size-16",
        "bold",
        "font-family-arial",
    } - {dropped_class}
    remote = (
        '<TextBox id="scope-run"><P class="text-align-center"><T '
        f'class="{" ".join(sorted(remaining))}">same text</T></P></TextBox>'
    )

    assert _severity(
        intended,
        remote,
        change_type=ChangeType.TEXT_UPDATE,
        newly_created=False,
    ) is WarningSeverity.WARNING


def test_text_update_scope_comparison_warns_on_changed_text() -> None:
    intended = '<TextBox id="scope-run"><P><T class="bold">changed</T></P></TextBox>'
    remote = '<TextBox id="scope-run"><P><T class="bold">original</T></P></TextBox>'

    assert _severity(
        intended,
        remote,
        change_type=ChangeType.TEXT_UPDATE,
        newly_created=False,
    ) is WarningSeverity.WARNING


def test_text_update_scope_comparison_warns_on_changed_auto_text_coverage() -> None:
    intended = (
        '<TextBox id="scope-run"><P><T auto-text="SLIDE_NUMBER">7</T>'
        "</P></TextBox>"
    )
    remote = '<TextBox id="scope-run"><P>7</P></TextBox>'

    assert _severity(
        intended,
        remote,
        change_type=ChangeType.TEXT_UPDATE,
        newly_created=False,
    ) is WarningSeverity.WARNING


def test_text_update_scope_comparison_catches_dropped_intended_leading() -> None:
    intended = (
        '<TextBox id="scope-leading" class="leading-120"><P><T '
        'class="bold">same text</T></P></TextBox>'
    )
    remote = (
        '<TextBox id="scope-leading"><P><T class="bold">same </T><T '
        'class="bold">text</T></P></TextBox>'
    )

    # Only TEXT_UPDATE is considered authored here. The paragraph/style
    # sibling is the divergence that used to be filtered out first.
    assert _severity(
        intended,
        remote,
        change_type=ChangeType.TEXT_UPDATE,
        newly_created=False,
    ) is WarningSeverity.WARNING


def _weighted_request(old_class: str, new_class: str) -> dict[str, object]:
    old = parse_slide_content(
        f'<TextBox id="scope-run"><P><T class="{old_class}">'
        "123456789012345</T></P></TextBox>"
    )[0]
    new = parse_slide_content(
        f'<TextBox id="scope-run"><P><T class="{new_class}">'
        "123456789012345</T></P></TextBox>"
    )[0]
    plan = _create_effective_style_requests("scope-run", old, new)
    assert plan is not None
    assert len(plan.requests) == 1
    return plan.requests[0]["updateTextStyle"]


def test_weight_update_pins_known_bold_true_on_the_same_range() -> None:
    update = _weighted_request(
        "bold font-family-arial font-weight-400",
        "bold font-family-montserrat font-weight-700",
    )

    assert update["textRange"] == {
        "type": "FIXED_RANGE",
        "startIndex": 0,
        "endIndex": 15,
    }
    assert update["style"]["bold"] is True
    assert "bold" in update["fields"]


def test_weight_only_update_does_not_invent_bold() -> None:
    update = _weighted_request(
        "font-family-arial font-weight-400",
        "font-family-montserrat font-weight-700",
    )

    assert "bold" not in update["style"]
    assert "bold" not in update["fields"]


def test_weight_update_pins_explicit_false_for_genuine_bold_removal() -> None:
    update = _weighted_request(
        "bold font-family-arial font-weight-400",
        "font-family-montserrat font-weight-700",
    )

    assert update["style"]["bold"] is False
    assert "bold" in update["fields"]


def test_weight_reset_pins_unchanged_bold_in_the_reset_request() -> None:
    update = _weighted_request(
        "bold font-weight-400",
        "bold",
    )

    assert update["style"] == {"bold": True}
    assert set(update["fields"].split(",")) == {"bold", "weightedFontFamily"}


def test_weight_reset_does_not_pin_bold_when_bold_is_unknown() -> None:
    update = _weighted_request("font-weight-400", "")

    assert update == {
        "objectId": "scope-run",
        "textRange": {
            "type": "FIXED_RANGE",
            "startIndex": 0,
            "endIndex": 15,
        },
        "style": {},
        "fields": "weightedFontFamily",
    }


@pytest.mark.parametrize(
    ("old_class", "new_class"),
    [
        (
            "font-weight-400",
            "font-weight-700",
        ),
        (
            "bold font-weight-400",
            "bold font-weight-700",
        ),
        (
            "font-family-arial bold font-weight-400",
            "font-family-arial bold font-weight-700",
        ),
    ],
)
def test_scope_run_bold_400_to_700_remains_fail_closed(
    old_class: str,
    new_class: str,
) -> None:
    # Normalization candidate only: rendered equivalence is unproven, so
    # suppression requires a maintainer decision. Keep these field records
    # warning-producing until that decision exists.
    intended = (
        f'<RoundRect id="scope-run" class="{old_class}"><P><T class="bold '
        'text-color-#f4f1e8">one range</T></P></RoundRect>'
    )
    remote = (
        f'<RoundRect id="scope-run" class="{new_class}"><P><T class="bold '
        'text-color-#f4f1e8">one range</T></P></RoundRect>'
    )

    assert _severity(
        intended,
        remote,
        change_type=ChangeType.TEXT_UPDATE,
        newly_created=False,
    ) is WarningSeverity.WARNING
