"""Semantic selector grammar, matching, mutation, and role contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slidesmith import cli
from slidesmith.engine.client import diff_folder
from slidesmith.engine.content_parser import ParsedElement
from slidesmith.engine.selector import (
    QueryContext,
    QueryParseError,
    apply_to_elements,
    parse_query,
    select_elements,
)
from slidesmith.workspace import materialize

GOLDEN = (
    Path(__file__).parent
    / "vendor"
    / "golden"
    / "simple_presentation"
    / "presentation.json"
)


@pytest.fixture
def query_context() -> QueryContext:
    return QueryContext(
        slide_number=3,
        element=ParsedElement(
            clean_id="mission_title",
            tag="Rect",
            x=56,
            y=120,
            w=400,
            h=40,
            paragraphs=["Mission DONE"],
        ),
        classes=frozenset({"text-size-53", "bold"}),
        role="subtitle",
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("tag=Rect", True),
        ("tag=Image", False),
        ("class~=text-size-53", True),
        ("class~=text-size-5", False),
        ("class=text-size-53", True),
        ("role=subtitle", True),
        ("role=title", False),
        ("id=mission_title", True),
        ("id=mission", False),
        ("id~=mission", True),
        ("id~=Mission", False),
        ('text="Mission DONE"', True),
        ("text=Mission", False),
        ("text^=Mission", True),
        ("text^=DONE", False),
        ("text$=DONE", True),
        ("text$=Mission", False),
        ("text~=done", True),
        ("text~=missing", False),
        ("slide=3", True),
        ("slide=4", False),
    ],
)
def test_each_scalar_predicate(
    query_context: QueryContext,
    query: str,
    expected: bool,
) -> None:
    assert parse_query(query).matches(query_context) is expected


def test_and_or_parentheses_and_precedence(query_context: QueryContext) -> None:
    assert parse_query("tag=Rect OR tag=Image AND slide=9").matches(query_context)
    assert not parse_query("(tag=Rect OR tag=Image) AND slide=9").matches(
        query_context
    )
    assert parse_query(
        "(tag=Image OR tag=Rect) AND (role=subtitle AND text~=DONE)"
    ).matches(query_context)


def test_quoted_predicate_value(query_context: QueryContext) -> None:
    assert parse_query('text~="mission done"').matches(query_context)


def test_exact_text_does_not_overmatch_longer_text(query_context: QueryContext) -> None:
    assert not parse_query("text=verified").matches(query_context)
    verified_context = QueryContext(
        slide_number=1,
        element=ParsedElement(
            clean_id="status",
            tag="TextBox",
            paragraphs=["verified result"],
        ),
        classes=frozenset(),
        role=None,
    )
    assert not parse_query("text=verified").matches(verified_context)
    assert parse_query("text~=verified").matches(verified_context)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("slide in 2..4", True),
        ("slide in 4..24", False),
        ("slide in 1,3,9", True),
        ("slide in 1,7,9", False),
    ],
)
def test_slide_range_and_list_predicates(
    query_context: QueryContext,
    query: str,
    expected: bool,
) -> None:
    assert parse_query(query).matches(query_context) is expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("w>300", True),
        ("w>=400", True),
        ("h<41", True),
        ("h<=40", True),
        ("x=56", True),
        ("y>120", False),
        ("x<=55.99", False),
        ("y>=119.5", True),
    ],
)
def test_geometry_predicates_support_every_field_and_operator(
    query_context: QueryContext,
    query: str,
    expected: bool,
) -> None:
    assert parse_query(query).matches(query_context) is expected


def test_geometry_predicate_does_not_match_missing_dimension() -> None:
    context = QueryContext(1, ParsedElement("group", "Group"), frozenset(), None)
    assert not parse_query("w=0").matches(context)


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("", "empty query"),
        ("tag Rect", "expected '=' after tag"),
        ("slide in 4..", "positive integer slide number"),
        ("slide in 9..4", "greater than end"),
        ("(tag=Rect", "expected ')'"),
        ("tag=Rect AND", "expected a predicate name"),
        ("flavor=vanilla", "unknown predicate 'flavor'"),
        ("w~=300", "expected >, >=, <, <=, or ="),
    ],
)
def test_malformed_queries_raise_specific_parse_errors(
    query: str,
    message: str,
) -> None:
    with pytest.raises(QueryParseError) as excinfo:
        parse_query(query)
    assert message in str(excinfo.value)


def test_cli_malformed_query_is_one_line_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = _simple_workspace(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["select", str(folder), "tag Rect"])
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: Query parse error at column 5:")
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("command", ["select", "apply"])
def test_selector_command_help_prints_full_grammar(
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([command, "--help"])
    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    for expected in (
        "tag=VALUE",
        "role=VALUE",
        "class=VALUE",
        "class~=VALUE",
        "id=VALUE",
        "id~=VALUE",
        "text=VALUE",
        "text^=VALUE",
        "text$=VALUE",
        "text~=VALUE",
        "AND",
        "OR",
        "parentheses",
        'text="verified result"',
        "slide=3",
        "slide in 1,3,5",
        "slide in 2..6",
        "x=PT",
        "y<PT",
        "w>=PT",
        "h<=PT",
        "Examples:",
    ):
        assert expected in output


def test_select_match_set_on_golden_fixture_includes_nested_elements(
    tmp_path: Path,
) -> None:
    folder = _golden_workspace(tmp_path)

    matches = select_elements(folder, "slide=3 AND tag=TextBox")

    assert [match.element.clean_id for match in matches] == [
        "focus_subtitle",
        "focus_title1",
        "focus_title2",
    ]


def test_select_cli_prints_rows_and_total(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = _golden_workspace(tmp_path)

    cli.main(["select", str(folder), "slide=3 AND id~=focus_title"])

    output = capsys.readouterr().out
    assert "slide 03  focus_title1  <TextBox>  text=\"FOCUS\"" in output
    assert "slide 03  focus_title_accent  <Rect>" in output
    assert "slide 03  focus_title2  <TextBox>  text=\"AREAS\"" in output
    assert output.endswith("Total: 3 match(es)\n")


def test_apply_adds_and_removes_element_classes(tmp_path: Path) -> None:
    folder = _simple_workspace(tmp_path)
    path = folder / "slides" / "01" / "content.sml"

    result = apply_to_elements(
        folder,
        "id~=title",
        add_classes=["underline"],
        remove_classes=["italic"],
    )

    content = path.read_text(encoding="utf-8")
    assert 'class="bold text-size-18 underline"' in content
    assert "italic" not in content
    assert result.match_counts == {"01": 1, "02": 0}
    assert result.mutation_counts == {"01": 1, "02": 0}
    assert result.total_matches == 1
    assert result.total_mutations == 1


def test_apply_adds_class_attribute_without_reformatting(tmp_path: Path) -> None:
    folder = _simple_workspace(tmp_path)
    path = folder / "slides" / "02" / "content.sml"
    before = path.read_text(encoding="utf-8")

    apply_to_elements(folder, "id~=image", add_classes=["stroke-none"])

    assert path.read_text(encoding="utf-8") == before.replace(
        'h="20" />', 'h="20" class="stroke-none" />'
    )


def test_apply_conflict_rejection_is_atomic_across_sml_and_roles(
    tmp_path: Path,
) -> None:
    folder = _simple_workspace(tmp_path)
    before = _file_snapshot(folder)

    with pytest.raises(ValueError) as excinfo:
        apply_to_elements(
            folder,
            "id~=title OR id~=body",
            add_classes=["text-size-24"],
            set_role="headline",
        )

    message = str(excinfo.value)
    assert "title" in message
    assert "text-size-18" in message
    assert "text-size-24" in message
    assert _file_snapshot(folder) == before
    assert not (folder / "roles.json").exists()


def test_set_select_and_clear_role_round_trip(tmp_path: Path) -> None:
    folder = _simple_workspace(tmp_path)

    set_result = apply_to_elements(folder, "id~=title", set_role="subtitle")
    assert set_result.total_mutations == 1
    assert [
        match.element.clean_id for match in select_elements(folder, "role=subtitle")
    ] == ["title"]

    clear_result = apply_to_elements(folder, "role=subtitle", clear_role=True)
    assert clear_result.total_mutations == 1
    assert select_elements(folder, "role=subtitle") == []
    assert json.loads((folder / "roles.json").read_text(encoding="utf-8")) == {}


def test_roles_survive_repull_materialization(tmp_path: Path) -> None:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    folder = materialize(data, tmp_path)
    apply_to_elements(folder, "id~=focus_subtitle", set_role="subtitle")

    repulled_folder = materialize(data, tmp_path)

    assert repulled_folder == folder
    assert [
        match.element.clean_id for match in select_elements(folder, "role=subtitle")
    ] == ["focus_subtitle"]


def test_role_never_appears_in_generated_batch_update_request(
    tmp_path: Path,
) -> None:
    folder = _golden_workspace(tmp_path)
    apply_to_elements(
        folder,
        "id~=focus_subtitle",
        add_classes=["bold"],
        set_role="subtitle",
    )

    requests = diff_folder(folder)

    assert requests
    assert "role" not in json.dumps(requests).casefold()
    assert "role=" not in (
        folder / "slides" / "03" / "content.sml"
    ).read_text(encoding="utf-8")


def test_apply_dry_run_performs_zero_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = _simple_workspace(tmp_path)
    before = _file_snapshot(folder)

    cli.main(
        [
            "apply",
            str(folder),
            "tag=Rect OR tag=Image",
            "--add-class",
            "underline",
            "--add-class",
            "small-caps",
            "--remove-class",
            "italic",
            "--remove-class",
            "fill-none",
            "--set-role",
            "card",
            "--dry-run",
        ]
    )

    assert _file_snapshot(folder) == before
    assert not (folder / "roles.json").exists()
    assert capsys.readouterr().out == (
        "Slide 01: 2 match(es), 2 mutation(s)\n"
        "Slide 02: 1 match(es), 1 mutation(s)\n"
        "Total: 3 match(es), 3 mutation(s)\n"
        "Dry run: no files written.\n"
    )


def test_apply_requires_a_mutation_option(tmp_path: Path) -> None:
    folder = _simple_workspace(tmp_path)
    with pytest.raises(ValueError, match="apply requires"):
        apply_to_elements(folder, "tag=Rect")


def _golden_workspace(tmp_path: Path) -> Path:
    return materialize(json.loads(GOLDEN.read_text(encoding="utf-8")), tmp_path)


def _simple_workspace(tmp_path: Path) -> Path:
    folder = tmp_path / "deck"
    slide_01 = folder / "slides" / "01"
    slide_02 = folder / "slides" / "02"
    slide_01.mkdir(parents=True)
    slide_02.mkdir(parents=True)
    (slide_01 / "content.sml").write_text(
        '<Slide id="s1">\n'
        '  <Rect id="title" x="56" y="20" w="400" h="40" '
        'class="bold italic text-size-18"><P>DONE title</P></Rect>\n'
        '  <Rect id="body" x="56" y="80" w="300" h="100" '
        'class="fill-none"><P>Body</P></Rect>\n'
        "</Slide>",
        encoding="utf-8",
    )
    (slide_02 / "content.sml").write_text(
        '<Slide id="s2">\n  <Image id="image" x="10" y="10" w="20" h="20" />\n'
        "</Slide>",
        encoding="utf-8",
    )
    return folder


def _file_snapshot(folder: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(folder): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def _multi_para_ctx() -> QueryContext:
    """Element whose text spans two paragraphs — joined with '\\n', not bare."""
    return QueryContext(
        slide_number=1,
        element=ParsedElement(
            clean_id="two_para",
            tag="TextBox",
            x=0,
            y=0,
            w=100,
            h=40,
            paragraphs=["foo", "bar"],
        ),
        classes=frozenset(),
        role=None,
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ('text="foobar"', False),   # TG-4: paragraphs are NOT bare-concatenated
        ('text="foo\nbar"', True),  # they join with a newline
        ("text^=foo", True),
        ("text$=bar", True),
        ("text~=foo", True),
        ("text~=bar", True),
    ],
)
def test_multi_paragraph_text_join_is_newline(query: str, expected: bool) -> None:
    ctx = _multi_para_ctx()
    assert parse_query(query).matches(ctx) is expected
