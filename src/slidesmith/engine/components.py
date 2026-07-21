"""Parse and instantiate reusable, authoring-only SML components."""

from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException


COMPONENTS_FILE = "components.sml"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
_SLOT = re.compile(
    r"\{\{([A-Za-z_][A-Za-z0-9_-]*)(?:\|([^{}]*))?\}\}"
)
_USE_LAYOUT_ATTRIBUTES = frozenset({"id", "x", "y", "w", "h", "flex"})


@dataclass(frozen=True)
class ComponentSlot:
    """One slot derived from placeholders in a component body."""

    name: str
    required: bool


@dataclass(frozen=True)
class ComponentDefinition:
    """A validated component template and its derived slot contract."""

    name: str
    body: tuple[ET.Element, ...]
    slots: tuple[ComponentSlot, ...]

    def instantiate(
        self,
        values: dict[str, str],
        *,
        id_prefix: str,
        use_label: str,
    ) -> list[ET.Element]:
        """Clone, interpolate, and deterministically prefix this definition."""
        available = {slot.name for slot in self.slots}
        unknown = sorted(set(values) - available - _USE_LAYOUT_ATTRIBUTES)
        if unknown:
            label = "slot" if len(unknown) == 1 else "slots"
            names = ", ".join(repr(name) for name in unknown)
            options = ", ".join(sorted(available)) or "(none)"
            raise ValueError(
                f"{use_label} of component '{self.name}': unknown {label} "
                f"{names}; available slots: {options}"
            )
        missing = next(
            (slot.name for slot in self.slots if slot.required and slot.name not in values),
            None,
        )
        if missing is not None:
            raise ValueError(
                f"{use_label} of component '{self.name}': "
                f"missing required slot '{missing}'"
            )

        roots = [copy.deepcopy(element) for element in self.body]
        for root in roots:
            for element in root.iter():
                for name, value in tuple(element.attrib.items()):
                    element.set(
                        name,
                        _interpolate(value, values, self.name, use_label),
                    )
                if element.text is not None:
                    element.text = _interpolate(
                        element.text, values, self.name, use_label
                    )
                if element.tail is not None:
                    element.tail = _interpolate(
                        element.tail, values, self.name, use_label
                    )
                if element_id := element.get("id"):
                    element.set("id", f"{id_prefix}__{element_id}")
        return roots

    def format_body(self) -> str:
        """Serialize the reusable body for agent-facing inspection."""
        serialized: list[str] = []
        for element in self.body:
            clone = copy.deepcopy(element)
            clone.tail = None
            ET.indent(clone, space="  ")
            serialized.append(
                ET.tostring(clone, encoding="unicode", short_empty_elements=True)
            )
        return "\n".join(serialized)


@dataclass(frozen=True)
class ComponentLibrary:
    """Definitions loaded from one optional workspace components.sml file."""

    definitions: dict[str, ComponentDefinition]

    def get(self, name: str) -> ComponentDefinition | None:
        return self.definitions.get(name)

    def __bool__(self) -> bool:
        return bool(self.definitions)


EMPTY_COMPONENTS = ComponentLibrary({})


def load_components(folder: str | Path) -> ComponentLibrary:
    """Load ``<folder>/components.sml`` or return an empty library."""
    path = Path(folder) / COMPONENTS_FILE
    if not path.exists():
        return EMPTY_COMPONENTS
    try:
        root = DefusedET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError, DefusedXmlException) as exc:
        raise ValueError(f"Malformed components.sml at {path}: {exc}") from exc

    try:
        return _parse_library(root)
    except ValueError as exc:
        raise ValueError(f"Malformed components.sml at {path}: {exc}") from exc


def _parse_library(root: ET.Element) -> ComponentLibrary:
    if root.tag != "Components":
        raise ValueError("expected a <Components> document root")
    if root.text and root.text.strip():
        raise ValueError("<Components> may contain only <Component> definitions")

    definitions: dict[str, ComponentDefinition] = {}
    for element in root:
        if element.tag != "Component":
            raise ValueError(
                f"unsupported <{element.tag}>; <Components> may contain only "
                "<Component> definitions"
            )
        name = element.get("name", "")
        if not _NAME.fullmatch(name):
            raise ValueError(
                f"Component {name!r} has an invalid or missing name; expected "
                "a letter/underscore followed by letters, digits, underscores, or hyphens"
            )
        if name in definitions:
            raise ValueError(f"duplicate Component name '{name}'")
        body = tuple(copy.deepcopy(child) for child in element)
        if not body:
            raise ValueError(f"Component '{name}' has an empty body")
        nested_use = next(
            (child for root_child in body for child in root_child.iter() if child.tag == "Use"),
            None,
        )
        if nested_use is not None:
            raise ValueError(
                f"Component '{name}' contains nested <Use>; component bodies may "
                "contain shapes, text, Stack, Grid, groups, and classes"
            )
        slots = _derive_slots(body, name)
        _validate_unique_ids(body, name)
        definitions[name] = ComponentDefinition(name, body, slots)
        if element.tail and element.tail.strip():
            raise ValueError(f"unexpected text after Component '{name}'")
    return ComponentLibrary(definitions)


def _derive_slots(
    body: tuple[ET.Element, ...], component_name: str
) -> tuple[ComponentSlot, ...]:
    required_by_name: dict[str, bool] = {}
    for value in _template_values(body):
        matches = tuple(_SLOT.finditer(value))
        if ("{{" in value or "}}" in value) and not _placeholders_cover_braces(value):
            raise ValueError(
                f"Component '{component_name}' has a malformed slot placeholder in {value!r}"
            )
        for match in matches:
            name = match.group(1)
            required_by_name[name] = required_by_name.get(name, False) or (
                match.group(2) is None
            )
    return tuple(
        ComponentSlot(name, required_by_name[name])
        for name in sorted(required_by_name)
    )


def _template_values(body: tuple[ET.Element, ...]):
    for root in body:
        for element in root.iter():
            yield from element.attrib.values()
            if element.text is not None:
                yield element.text
            if element.tail is not None:
                yield element.tail


def _placeholders_cover_braces(value: str) -> bool:
    without_placeholders = _SLOT.sub("", value)
    return "{{" not in without_placeholders and "}}" not in without_placeholders


def _validate_unique_ids(body: tuple[ET.Element, ...], component_name: str) -> None:
    seen: set[str] = set()
    for root in body:
        for element in root.iter():
            element_id = element.get("id")
            if not element_id:
                continue
            if element_id in seen:
                raise ValueError(
                    f"Component '{component_name}' repeats child id '{element_id}'"
                )
            seen.add(element_id)


def _interpolate(
    value: str,
    values: dict[str, str],
    component_name: str,
    use_label: str,
) -> str:
    def replacement(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in values:
            return values[name]
        default = match.group(2)
        if default is not None:
            return default
        raise ValueError(
            f"{use_label} of component '{component_name}': "
            f"missing required slot '{name}'"
        )

    return _SLOT.sub(replacement, value)
