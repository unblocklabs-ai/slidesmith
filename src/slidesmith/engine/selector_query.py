"""Semantic selector query grammar and matching."""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Callable, Protocol

from slidesmith.engine.content_parser import ParsedElement


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
