"""Reusable component expansion for authoring layout compilation."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from slidesmith.engine.layout_algorithms import _Frame
from slidesmith.engine.layout_measure import TextMeasurer, _parse_number
from slidesmith.engine.units import format_pt

if TYPE_CHECKING:
    from slidesmith.engine.components import ComponentLibrary


@dataclass
class UseState:
    count: int = 0
    prefixes: set[str] = field(default_factory=set)


class _CompileContainer(Protocol):
    def __call__(
        self,
        container: ET.Element,
        assigned_frame: _Frame | None,
        measurer: TextMeasurer,
        components: ComponentLibrary | None,
        use_state: UseState,
    ) -> list[ET.Element]: ...


class _CompileChildren(Protocol):
    def __call__(
        self,
        parent: ET.Element,
        measurer: TextMeasurer,
        components: ComponentLibrary | None,
        use_state: UseState,
    ) -> None: ...


def expand_use(
    use: ET.Element,
    frame: _Frame,
    measurer: TextMeasurer,
    components: ComponentLibrary | None,
    use_state: UseState,
    *,
    compile_container: _CompileContainer,
    compile_children: _CompileChildren,
) -> list[ET.Element]:
    use_state.count += 1
    component_name = use.get("component", "")
    authored_id = use.get("id")
    use_label = f"Use '{authored_id}'" if authored_id else f"Use #{use_state.count}"
    if not component_name:
        raise ValueError(f"{use_label}: missing required 'component' attribute")
    definition = components.get(component_name) if components is not None else None
    if definition is None:
        available = (
            ", ".join(sorted(components.definitions))
            if components is not None
            else ""
        ) or "(none)"
        raise ValueError(
            f"{use_label}: unknown component '{component_name}'; "
            f"available components: {available}"
        )

    id_prefix = authored_id or f"use_{component_name}_{use_state.count}"
    if id_prefix in use_state.prefixes:
        raise ValueError(
            f"{use_label}: duplicate Use id prefix '{id_prefix}'; Use ids must be unique"
        )
    use_state.prefixes.add(id_prefix)

    values = dict(use.attrib)
    values.pop("component", None)
    roots = definition.instantiate(
        values,
        id_prefix=id_prefix,
        use_label=use_label,
    )
    compiled: list[ET.Element] = []
    try:
        for root in roots:
            if root.tag in {"Stack", "Grid"}:
                compiled.extend(
                    compile_container(
                        root, None, measurer, components, use_state
                    )
                )
            else:
                compile_children(root, measurer, components, use_state)
                compiled.append(root)
    except ValueError as exc:
        raise ValueError(
            f"{use_label} of component '{component_name}': {exc}"
        ) from exc

    for root in compiled:
        _translate_tree(root, frame.x, frame.y)
    return compiled


def _translate_tree(root: ET.Element, offset_x: float, offset_y: float) -> None:
    """Translate component-authored absolute coordinates from a 0,0 origin."""
    for element in root.iter():
        if value := element.get("x"):
            element.set("x", format_pt(_parse_number(value, element, "x") + offset_x))
        if value := element.get("y"):
            element.set("y", format_pt(_parse_number(value, element, "y") + offset_y))
