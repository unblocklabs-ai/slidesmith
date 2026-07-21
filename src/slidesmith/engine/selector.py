"""Semantic queries and atomic local edits for SML workspaces."""

from __future__ import annotations

import json
import operator
import stat
import tempfile
from dataclasses import dataclass
from os import replace as replace_file
from pathlib import Path
from typing import Callable, Iterator, Protocol, Sequence

from slidesmith.engine.class_replacement import _start_tag_spans
from slidesmith.engine.components import load_components
from slidesmith.engine.content_parser import (
    ParsedElement,
    parse_element_classes,
    parse_slide_content,
)
from slidesmith.engine.json_utils import read_json
from slidesmith.engine.layout import compile_layout

ROLES_FILE = "roles.json"


class QueryParseError(ValueError):
    """A user-facing syntax error in a semantic selector query."""


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    position: int


@dataclass(frozen=True)
class QueryContext:
    """Values exposed to one query predicate."""

    slide_number: int
    element: ParsedElement
    classes: frozenset[str]
    role: str | None


class Query(Protocol):
    """One parsed query expression."""

    def matches(self, context: QueryContext) -> bool: ...


@dataclass(frozen=True)
class _BinaryQuery:
    left: Query
    right: Query
    operation: str

    def matches(self, context: QueryContext) -> bool:
        if self.operation == "AND":
            return self.left.matches(context) and self.right.matches(context)
        return self.left.matches(context) or self.right.matches(context)


@dataclass(frozen=True)
class _StringPredicate:
    field: str
    comparison: str
    value: str

    def matches(self, context: QueryContext) -> bool:
        element = context.element
        if self.field == "tag":
            return element.tag == self.value
        if self.field == "class":
            return self.value in context.classes
        if self.field == "role":
            return context.role == self.value
        if self.field == "id":
            return _match_string(element.clean_id, self.comparison, self.value)
        return _match_string(
            "\n".join(element.paragraphs).casefold(),
            self.comparison,
            self.value.casefold(),
        )


def _match_string(actual: str, comparison: str, expected: str) -> bool:
    if comparison == "=":
        return actual == expected
    if comparison == "^=":
        return actual.startswith(expected)
    if comparison == "$=":
        return actual.endswith(expected)
    return expected in actual


@dataclass(frozen=True)
class _SlidePredicate:
    slide_numbers: frozenset[int]

    def matches(self, context: QueryContext) -> bool:
        return context.slide_number in self.slide_numbers


_COMPARISONS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "=": operator.eq,
}


@dataclass(frozen=True)
class _GeometryPredicate:
    field: str
    comparison: str
    value: float

    def matches(self, context: QueryContext) -> bool:
        actual = getattr(context.element, self.field)
        return actual is not None and _COMPARISONS[self.comparison](actual, self.value)


def _tokenize(query: str) -> list[_Token]:
    tokens: list[_Token] = []
    position = 0
    two_character = {
        "~=": "TILDE_EQ",
        "^=": "CARET_EQ",
        "$=": "DOLLAR_EQ",
        ">=": "GE",
        "<=": "LE",
        "..": "RANGE",
    }
    one_character = {
        "(": "LPAREN",
        ")": "RPAREN",
        ",": "COMMA",
        ">": "GT",
        "<": "LT",
        "=": "EQ",
    }
    while position < len(query):
        character = query[position]
        if character.isspace():
            position += 1
            continue
        pair = query[position : position + 2]
        if pair in two_character:
            tokens.append(_Token(two_character[pair], pair, position))
            position += 2
            continue
        if character in one_character:
            tokens.append(_Token(one_character[character], character, position))
            position += 1
            continue
        if character in {'"', "'"}:
            quote = character
            start = position
            position += 1
            value: list[str] = []
            while position < len(query) and query[position] != quote:
                if query[position] == "\\":
                    position += 1
                    if position >= len(query):
                        raise QueryParseError(
                            f"Query parse error at column {start + 1}: unterminated quoted value"
                        )
                value.append(query[position])
                position += 1
            if position >= len(query):
                raise QueryParseError(
                    f"Query parse error at column {start + 1}: unterminated quoted value"
                )
            position += 1
            tokens.append(_Token("VALUE", "".join(value), start))
            continue
        start = position
        while (
            position < len(query)
            and not query[position].isspace()
            and query[position] not in "(),<>=~^$\"'"
            and query[position : position + 2] != ".."
        ):
            position += 1
        if position == start:
            raise QueryParseError(
                f"Query parse error at column {position + 1}: unexpected character {character!r}"
            )
        value = query[start:position]
        kind = "NUMBER" if _is_number(value) else "VALUE"
        tokens.append(_Token(kind, value, start))
    tokens.append(_Token("EOF", "", len(query)))
    return tokens


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return bool(value) and any(character.isdigit() for character in value)


class _QueryParser:
    def __init__(self, query: str) -> None:
        self.tokens = _tokenize(query)
        self.index = 0

    @property
    def current(self) -> _Token:
        return self.tokens[self.index]

    def parse(self) -> Query:
        if self.current.kind == "EOF":
            self._error("expected a predicate, got an empty query")
        expression = self._parse_or()
        if self.current.kind != "EOF":
            self._error("expected AND, OR, or the end of the query")
        return expression

    def _parse_or(self) -> Query:
        expression = self._parse_and()
        while self._is_keyword("OR"):
            self._advance()
            expression = _BinaryQuery(expression, self._parse_and(), "OR")
        return expression

    def _parse_and(self) -> Query:
        expression = self._parse_primary()
        while self._is_keyword("AND"):
            self._advance()
            expression = _BinaryQuery(expression, self._parse_primary(), "AND")
        return expression

    def _parse_primary(self) -> Query:
        if self.current.kind == "LPAREN":
            self._advance()
            expression = self._parse_or()
            self._expect("RPAREN", "expected ')' to close the parenthesized query")
            return expression
        if self.current.kind == "RPAREN":
            self._error("unexpected ')' without a matching '('")
        return self._parse_predicate()

    def _parse_predicate(self) -> Query:
        field_token = self._expect("VALUE", "expected a predicate name")
        field = field_token.value.lower()
        if field in {"tag", "role"}:
            self._expect("EQ", f"expected '=' after {field}")
            return _StringPredicate(field, "=", self._parse_value(field))
        if field == "class":
            comparison = self._parse_string_comparison(field, {"=", "~="})
            return _StringPredicate(field, comparison, self._parse_value(field))
        if field == "id":
            comparison = self._parse_string_comparison(field, {"=", "~="})
            return _StringPredicate(field, comparison, self._parse_value(field))
        if field == "text":
            comparison = self._parse_string_comparison(
                field, {"=", "^=", "$=", "~="}
            )
            return _StringPredicate(field, comparison, self._parse_value(field))
        if field == "slide":
            return self._parse_slide_predicate()
        if field in {"w", "h", "x", "y"}:
            return self._parse_geometry_predicate(field)
        self._error(
            "unknown predicate "
            f"{field_token.value!r}; expected tag, class, role, id, text, slide, w, h, x, or y",
            field_token,
        )

    def _parse_string_comparison(
        self, field: str, allowed: set[str]
    ) -> str:
        comparisons = {
            "EQ": "=",
            "TILDE_EQ": "~=",
            "CARET_EQ": "^=",
            "DOLLAR_EQ": "$=",
        }
        comparison = comparisons.get(self.current.kind)
        if comparison not in allowed:
            expected = ", ".join(repr(value) for value in sorted(allowed))
            self._error(f"expected {expected} after {field}")
        self._advance()
        return comparison

    def _parse_value(self, field: str) -> str:
        token = self.current
        if token.kind not in {"VALUE", "NUMBER"}:
            self._error(f"expected a value for {field}")
        self._advance()
        if not token.value:
            self._error(f"expected a non-empty value for {field}", token)
        return token.value

    def _parse_slide_predicate(self) -> Query:
        if self.current.kind == "EQ":
            self._advance()
            return _SlidePredicate(frozenset({self._parse_slide_number()}))
        if not self._is_keyword("IN"):
            self._error("expected '=' or 'in' after slide")
        self._advance()
        first = self._parse_slide_number()
        if self.current.kind == "RANGE":
            self._advance()
            last = self._parse_slide_number()
            if last < first:
                self._error(
                    f"slide range start {first} is greater than end {last}",
                    self.tokens[self.index - 1],
                )
            return _SlidePredicate(frozenset(range(first, last + 1)))
        numbers = {first}
        while self.current.kind == "COMMA":
            self._advance()
            numbers.add(self._parse_slide_number())
        return _SlidePredicate(frozenset(numbers))

    def _parse_slide_number(self) -> int:
        token = self._expect("NUMBER", "expected a positive integer slide number")
        try:
            number = int(token.value)
        except ValueError:
            self._error("expected a positive integer slide number", token)
        if number < 1:
            self._error("slide numbers must be at least 1", token)
        return number

    def _parse_geometry_predicate(self, field: str) -> Query:
        comparisons = {"GT": ">", "GE": ">=", "LT": "<", "LE": "<=", "EQ": "="}
        token = self.current
        comparison = comparisons.get(token.kind)
        if comparison is None:
            self._error(f"expected >, >=, <, <=, or = after {field}")
        self._advance()
        value_token = self._expect("NUMBER", f"expected a numeric value after {field}{comparison}")
        return _GeometryPredicate(field, comparison, float(value_token.value))

    def _is_keyword(self, keyword: str) -> bool:
        return self.current.kind == "VALUE" and self.current.value.upper() == keyword

    def _expect(self, kind: str, message: str) -> _Token:
        if self.current.kind != kind:
            self._error(message)
        return self._advance()

    def _advance(self) -> _Token:
        token = self.current
        self.index += 1
        return token

    def _error(self, message: str, token: _Token | None = None) -> None:
        current = token or self.current
        raise QueryParseError(
            f"Query parse error at column {current.position + 1}: {message}"
        )


def parse_query(query: str) -> Query:
    """Parse a semantic selector query into an evaluable expression tree."""
    return _QueryParser(query).parse()


@dataclass(frozen=True)
class SelectorMatch:
    """One selected SML element."""

    slide_index: str
    slide_number: int
    element: ParsedElement
    classes: tuple[str, ...]
    role: str | None


@dataclass(frozen=True)
class ApplyResult:
    """Per-slide match and mutation counts from an apply operation."""

    matches: tuple[SelectorMatch, ...]
    match_counts: dict[str, int]
    mutation_counts: dict[str, int]

    @property
    def total_matches(self) -> int:
        return len(self.matches)

    @property
    def total_mutations(self) -> int:
        return sum(self.mutation_counts.values())


@dataclass(frozen=True)
class _SlideDocument:
    index: str
    number: int
    path: Path
    content: str
    elements: tuple[ParsedElement, ...]
    classes_by_id: dict[str, tuple[str, ...]]


def select_elements(folder_path: str | Path, query: str | Query) -> list[SelectorMatch]:
    """Select every matching element from the parsed workspace tree."""
    parsed_query = parse_query(query) if isinstance(query, str) else query
    folder = Path(folder_path)
    roles = _read_roles(folder)
    matches: list[SelectorMatch] = []
    for slide in _read_slides(folder):
        for element in slide.elements:
            classes = slide.classes_by_id.get(element.clean_id, ())
            role = roles.get(element.clean_id)
            context = QueryContext(
                slide_number=slide.number,
                element=element,
                classes=frozenset(classes),
                role=role,
            )
            if parsed_query.matches(context):
                matches.append(
                    SelectorMatch(
                        slide_index=slide.index,
                        slide_number=slide.number,
                        element=element,
                        classes=classes,
                        role=role,
                    )
                )
    return matches


def apply_to_elements(
    folder_path: str | Path,
    query: str | Query,
    *,
    add_classes: Sequence[str] = (),
    remove_classes: Sequence[str] = (),
    set_role: str | None = None,
    clear_role: bool = False,
    dry_run: bool = False,
) -> ApplyResult:
    """Apply local metadata/style changes after validating the complete result."""
    if set_role is not None and clear_role:
        raise ValueError("--set-role and --clear-role cannot be used together")
    if not add_classes and not remove_classes and set_role is None and not clear_role:
        raise ValueError(
            "apply requires --add-class, --remove-class, --set-role, or --clear-role"
        )
    additions = _unique_tokens(add_classes, label="--add-class")
    removals = _unique_tokens(remove_classes, label="--remove-class")
    for class_name in additions:
        parse_element_classes(class_name, "apply validation")
    if set_role is not None and not set_role:
        raise ValueError("--set-role requires a non-empty role")

    parsed_query = parse_query(query) if isinstance(query, str) else query
    folder = Path(folder_path)
    components = load_components(folder)
    roles = _read_roles(folder)
    slides = _read_slides(folder)
    matches: list[SelectorMatch] = []
    matches_by_slide: dict[str, list[SelectorMatch]] = {}
    for slide in slides:
        for element in slide.elements:
            classes = slide.classes_by_id.get(element.clean_id, ())
            role = roles.get(element.clean_id)
            context = QueryContext(slide.number, element, frozenset(classes), role)
            if parsed_query.matches(context):
                if not element.clean_id:
                    raise ValueError(
                        f"Cannot mutate matched <{element.tag}> on slide {slide.index}: "
                        "the element has no id"
                    )
                match = SelectorMatch(
                    slide.index,
                    slide.number,
                    element,
                    classes,
                    role,
                )
                matches.append(match)
                matches_by_slide.setdefault(slide.index, []).append(match)

    pending: dict[Path, str] = {}
    mutated_ids_by_slide: dict[str, set[str]] = {
        slide.index: set() for slide in slides
    }
    for slide in slides:
        selected_ids = {
            match.element.clean_id for match in matches_by_slide.get(slide.index, [])
        }
        expanded_ids = selected_ids - set(_classes_by_id(slide.content))
        if expanded_ids and (additions or removals):
            element_id = sorted(expanded_ids)[0]
            raise ValueError(
                f"Cannot mutate classes on component-expanded element '{element_id}'; "
                "edit components.sml or parameterize its class with a slot"
            )
        updated, changed_ids = _mutate_element_classes(
            slide.content,
            selected_ids,
            additions,
            removals,
        )
        # This is the existing parser and class-conflict validator used by diff.
        # Validate all prospective files before the first disk write.
        parse_slide_content(updated, components=components)
        if updated != slide.content:
            pending[slide.path] = updated
        mutated_ids_by_slide[slide.index].update(changed_ids)

    updated_roles = dict(roles)
    for match in matches:
        element_id = match.element.clean_id
        changed = False
        if set_role is not None and updated_roles.get(element_id) != set_role:
            updated_roles[element_id] = set_role
            changed = True
        elif clear_role and element_id in updated_roles:
            del updated_roles[element_id]
            changed = True
        if changed:
            mutated_ids_by_slide[match.slide_index].add(element_id)

    if updated_roles != roles:
        pending[folder / ROLES_FILE] = _serialize_roles(updated_roles)

    if not dry_run:
        _commit_text_files(pending)

    match_counts = {
        slide.index: len(matches_by_slide.get(slide.index, [])) for slide in slides
    }
    mutation_counts = {
        slide.index: len(mutated_ids_by_slide[slide.index]) for slide in slides
    }
    return ApplyResult(tuple(matches), match_counts, mutation_counts)


def format_match(match: SelectorMatch) -> str:
    """Format one stable, compact CLI selection row."""
    text = " ".join("".join(match.element.paragraphs).split())
    if text:
        summary = f'text="{_shorten(text)}"'
    elif match.classes:
        summary = f'class="{_shorten(" ".join(match.classes))}"'
    else:
        summary = "(no text or classes)"
    return (
        f"slide {match.slide_number:02d}  {match.element.clean_id}  "
        f"<{match.element.tag}>  {summary}"
    )


def _shorten(value: str, limit: int = 64) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


def _unique_tokens(values: Sequence[str], *, label: str) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if not value or len(value.split()) != 1:
            raise ValueError(f"{label} must be exactly one whitespace-free class token")
        if value not in result:
            result.append(value)
    return tuple(result)


def _read_slides(folder: Path) -> list[_SlideDocument]:
    paths = sorted((folder / "slides").glob("*/content.sml"))
    if not paths:
        raise ValueError(f"No content.sml files found under {folder / 'slides'}")
    components = load_components(folder)
    slides: list[_SlideDocument] = []
    for path in paths:
        try:
            number = int(path.parent.name)
        except ValueError as exc:
            raise ValueError(
                f"Invalid slide folder '{path.parent.name}': expected a numeric index"
            ) from exc
        content = path.read_text(encoding="utf-8")
        compiled = compile_layout(content, components=components)
        roots = parse_slide_content(compiled)
        slides.append(
            _SlideDocument(
                index=path.parent.name,
                number=number,
                path=path,
                content=content,
                elements=tuple(_walk_elements(roots)),
                classes_by_id=_classes_by_id(compiled),
            )
        )
    return slides


def _walk_elements(elements: Sequence[ParsedElement]) -> Iterator[ParsedElement]:
    for element in elements:
        yield element
        yield from _walk_elements(element.children)


def _read_roles(folder: Path) -> dict[str, str]:
    path = folder / ROLES_FILE
    values = read_json(path, missing_ok=True)
    roles: dict[str, str] = {}
    for element_id, role in values.items():
        if not isinstance(role, str) or not role:
            raise ValueError(
                f"Invalid role for element '{element_id}' in {path}: expected a non-empty string"
            )
        roles[element_id] = role
    return roles


def _serialize_roles(roles: dict[str, str]) -> str:
    return json.dumps(roles, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


@dataclass(frozen=True)
class _Attribute:
    name: str
    name_start: int
    value_start: int
    value_end: int
    end: int


def _attributes(start_tag: str, tag_name: str) -> list[_Attribute]:
    attributes: list[_Attribute] = []
    position = 1 + len(tag_name)
    while position < len(start_tag):
        while position < len(start_tag) and start_tag[position].isspace():
            position += 1
        if position >= len(start_tag) or start_tag[position] in "/>":
            break
        name_start = position
        while (
            position < len(start_tag)
            and not start_tag[position].isspace()
            and start_tag[position] not in "=/>"
        ):
            position += 1
        name = start_tag[name_start:position]
        while position < len(start_tag) and start_tag[position].isspace():
            position += 1
        if position >= len(start_tag) or start_tag[position] != "=":
            continue
        position += 1
        while position < len(start_tag) and start_tag[position].isspace():
            position += 1
        if position >= len(start_tag) or start_tag[position] not in {'"', "'"}:
            continue
        quote = start_tag[position]
        value_start = position + 1
        value_end = start_tag.find(quote, value_start)
        if value_end == -1:
            break
        attributes.append(
            _Attribute(name, name_start, value_start, value_end, value_end + 1)
        )
        position = value_end + 1
    return attributes


def _classes_by_id(content: str) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for start, end, tag_name in _start_tag_spans(content):
        if tag_name in {"Slide", "P", "T"}:
            continue
        start_tag = content[start:end]
        values = {
            attribute.name: start_tag[attribute.value_start : attribute.value_end]
            for attribute in _attributes(start_tag, tag_name)
        }
        element_id = values.get("id")
        if element_id:
            result[element_id] = tuple(values.get("class", "").split())
    return result


def _mutate_element_classes(
    content: str,
    selected_ids: set[str],
    additions: Sequence[str],
    removals: Sequence[str],
) -> tuple[str, set[str]]:
    if not selected_ids or (not additions and not removals):
        return content, set()
    remove_set = set(removals)
    pieces: list[str] = []
    cursor = 0
    changed_ids: set[str] = set()
    for start, end, tag_name in _start_tag_spans(content):
        pieces.append(content[cursor:start])
        start_tag = content[start:end]
        attributes = _attributes(start_tag, tag_name)
        by_name = {attribute.name: attribute for attribute in attributes}
        id_attribute = by_name.get("id")
        element_id = (
            start_tag[id_attribute.value_start : id_attribute.value_end]
            if id_attribute is not None
            else None
        )
        if element_id in selected_ids:
            class_attribute = by_name.get("class")
            old_classes = (
                start_tag[class_attribute.value_start : class_attribute.value_end].split()
                if class_attribute is not None
                else []
            )
            new_classes = [value for value in old_classes if value not in remove_set]
            for addition in additions:
                if addition not in new_classes:
                    new_classes.append(addition)
            if new_classes != old_classes:
                start_tag = _replace_class_attribute(
                    start_tag,
                    class_attribute,
                    new_classes,
                )
                changed_ids.add(element_id)
        pieces.append(start_tag)
        cursor = end
    pieces.append(content[cursor:])
    return "".join(pieces), changed_ids


def _replace_class_attribute(
    start_tag: str,
    attribute: _Attribute | None,
    classes: Sequence[str],
) -> str:
    value = " ".join(classes)
    if attribute is not None and value:
        return (
            start_tag[: attribute.value_start]
            + value
            + start_tag[attribute.value_end :]
        )
    if attribute is not None:
        remove_start = attribute.name_start
        while remove_start > 0 and start_tag[remove_start - 1].isspace():
            remove_start -= 1
        return start_tag[:remove_start] + start_tag[attribute.end :]
    if not value:
        return start_tag
    closing = start_tag.rfind("/>")
    if closing == -1:
        closing = start_tag.rfind(">")
    insertion = closing
    while insertion > 0 and start_tag[insertion - 1].isspace():
        insertion -= 1
    return start_tag[:insertion] + f' class="{value}"' + start_tag[insertion:]


def _commit_text_files(pending: dict[Path, str]) -> None:
    """Replace prepared files together, restoring prior state after I/O failure."""
    changed = {
        path: value
        for path, value in pending.items()
        if not path.exists() or path.read_text(encoding="utf-8") != value
    }
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    existing: set[Path] = set()
    committed: list[Path] = []
    try:
        for path, value in changed.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
            if path.exists():
                existing.add(path)
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=path.parent,
                    prefix=f".{path.name}.backup.",
                    suffix=".tmp",
                    delete=False,
                ) as backup:
                    backup.write(path.read_bytes())
                    backups[path] = Path(backup.name)
                backups[path].chmod(mode)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(value)
                staged[path] = Path(temporary.name)
            staged[path].chmod(mode)
        for path, temporary_path in staged.items():
            replace_file(temporary_path, path)
            committed.append(path)
    except Exception:
        for path in reversed(committed):
            if path in existing:
                replace_file(backups[path], path)
            else:
                path.unlink(missing_ok=True)
        raise
    finally:
        for temporary_path in [*staged.values(), *backups.values()]:
            temporary_path.unlink(missing_ok=True)
