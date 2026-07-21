"""Components CLI command."""

from __future__ import annotations

import argparse
from typing import Any


def cmd_components(args: Any) -> None:
    from slidesmith.engine.components import load_components

    library = load_components(args.folder)
    if args.show:
        definition = library.get(args.show)
        if definition is None:
            available = ", ".join(sorted(library.definitions)) or "(none)"
            raise ValueError(
                f"Unknown component '{args.show}'; available components: {available}"
            )
        print(definition.name)
        print("Slots:")
        if definition.slots:
            for slot in definition.slots:
                requirement = "required" if slot.required else "optional"
                print(f"  {slot.name} ({requirement})")
        else:
            print("  (none)")
        print("Body:")
        for line in definition.format_body().splitlines():
            print(f"  {line}")
        return
    for name in sorted(library.definitions):
        definition = library.definitions[name]
        slots = ", ".join(slot.name for slot in definition.slots) or "(no slots)"
        print(f"{name}: {slots}")


def register_components_command(
    subparsers: argparse._SubParsersAction,
    handlers: dict[str, Any],
) -> None:
    sco = subparsers.add_parser(
        "components",
        help="List reusable components and their derived slots (local only)",
    )
    sco.add_argument("folder", help="Presentation folder containing components.sml")
    sco.add_argument(
        "--show",
        metavar="NAME",
        help="Print one component's body and required/optional slot list",
    )
    sco.set_defaults(func=handlers["cmd_components"])
