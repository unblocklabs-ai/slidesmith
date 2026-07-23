"""CLI registration for the local, advisory-only deck pattern scanner."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from slidesmith.cli_commands._support import _require_workspace


def cmd_advise(args: Any) -> None:
    from slidesmith.engine.advisor import advise_folder, format_suggestions

    _require_workspace(args.folder)
    suggestions = advise_folder(Path(args.folder), rule=args.rule)
    if args.json:
        print(json.dumps([suggestion.to_dict() for suggestion in suggestions], indent=2))
        return
    print("\n".join(format_suggestions(suggestions)))


def register_advisor_commands(
    subparsers: argparse._SubParsersAction,
    handlers: dict[str, Any],
) -> None:
    """Register the offline advisor command."""
    advise = subparsers.add_parser(
        "advise",
        help="Suggest maintainability actions from local deck patterns",
        epilog=(
            "Suggestions are advisory only: they never fail a command, block a "
            "push, or become QA findings. Rules read the pulled workspace only; "
            "no network or Google API call is made."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    advise.add_argument("folder", help="Presentation folder created by pull")
    advise.add_argument(
        "--rule",
        metavar="ID",
        help="Limit output to one rule ID (for example: pseudo-group)",
    )
    advise.add_argument(
        "--json",
        action="store_true",
        help="Emit a stable JSON list for agents",
    )
    advise.set_defaults(func=handlers["cmd_advise"])


__all__ = ["cmd_advise", "register_advisor_commands"]
