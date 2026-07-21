"""Lex and rewrite XML attributes and SML class attributes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from slidesmith.engine.class_replacement import _start_tag_spans


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
