"""Editing CLI commands: image/class replacement, selection, mutation, and formatting."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from slidesmith.cli_commands._support import (
    _token,
    _warn_if_stale,
    print_push_warnings,
)


SELECTOR_GRAMMAR = """Selector grammar:
  tag=VALUE                 exact element tag
  role=VALUE                exact local role
  class=VALUE               exact class token (class~=VALUE is retained)
  id=VALUE                  exact element ID
  id~=VALUE                 element ID substring
  text=VALUE                exact full text (case-insensitive)
  text^=VALUE               text starts with VALUE (case-insensitive)
  text$=VALUE               text ends with VALUE (case-insensitive)
  text~=VALUE               text substring (case-insensitive)
  slide=3                   one slide
  slide in 1,3,5            slide list
  slide in 2..6             inclusive slide range
  x=PT  y<PT  w>=PT  h<=PT  geometry comparisons: =, <, <=, >, >=

Combine predicates with AND, OR, and parentheses. AND binds before OR.
Quote values containing spaces, for example text="verified result".

Examples:
  slide=3 AND text^=Summary
  (role=title OR class=title) AND text$="Q4 results"
  id=hero_image OR (tag=Image AND w>=300)
"""


def _cli_helper(name: str, fallback: Any) -> Any:
    from slidesmith import cli

    return getattr(cli, name, fallback)


def cmd_replace_image(args: Any) -> None:
    from slidesmith.engine.assets import GoogleDriveAssetUploader
    from slidesmith.engine.client import SlidesClient
    from slidesmith.engine.transport import GoogleSlidesTransport

    _cli_helper("_warn_if_stale", _warn_if_stale)(args.folder)
    token = _cli_helper("_token", _token)("slide.push", str(args.folder))

    async def run() -> None:
        transport = GoogleSlidesTransport(token)
        uploader = GoogleDriveAssetUploader(token)
        try:
            response = await SlidesClient(transport, uploader).replace_image(
                Path(args.folder),
                args.element_id,
                args.new_src,
                fit=args.fit,
                dry_run=args.dry_run,
            )
            if response.get("dryRun"):
                print(json.dumps(response, indent=2))
                return
            print_push_warnings(response.get("warnings", []))
            print(f"Replaced image {args.element_id}.")
        finally:
            await uploader.close()
            await transport.close()

    asyncio.run(run())


def cmd_replace_class(args: Any) -> None:
    from slidesmith.engine.class_replacement import replace_classes

    positional = (args.old_class, args.new_class)
    if (args.old_class is None) != (args.new_class is None):
        raise ValueError("Positional class replacement requires both OLD and NEW")
    swaps = [positional] if args.old_class is not None else []
    for value in args.swap:
        if "=" not in value:
            raise ValueError(f"--swap must use OLD=NEW syntax, got '{value}'")
        old_class, new_class = value.split("=", 1)
        swaps.append((old_class, new_class))

    result = replace_classes(
        args.folder,
        swaps,
        dry_run=args.dry_run,
    )
    if args.swap:
        for (old_class, new_class), count in result.swap_counts.items():
            print(f"Swap {old_class}={new_class}: {count} replacement(s)")
    for slide_index, count in result.counts.items():
        print(f"Slide {slide_index}: {count} replacement(s)")
    print(f"Total: {result.total} replacement(s)")
    if args.dry_run:
        print("Dry run: no files written.")


def cmd_select(args: Any) -> None:
    from slidesmith.engine.selector import format_match, select_elements

    matches = select_elements(args.folder, args.query)
    for match in matches:
        print(format_match(match))
    print(f"Total: {len(matches)} match(es)")


def cmd_apply(args: Any) -> None:
    from slidesmith.engine.selector import apply_to_elements

    result = apply_to_elements(
        args.folder,
        args.query,
        add_classes=args.add_class,
        remove_classes=args.remove_class,
        set_role=args.set_role,
        clear_role=args.clear_role,
        dry_run=args.dry_run,
    )
    for slide_index, match_count in result.match_counts.items():
        mutation_count = result.mutation_counts[slide_index]
        print(
            f"Slide {slide_index}: {match_count} match(es), "
            f"{mutation_count} mutation(s)"
        )
    print(
        f"Total: {result.total_matches} match(es), "
        f"{result.total_mutations} mutation(s)"
    )
    if args.dry_run:
        print("Dry run: no files written.")


def cmd_fmt(args: Any) -> None:
    from slidesmith.engine.formatting import format_folder

    result = format_folder(args.folder, check=args.check)
    changed = len(result.changed_paths)
    if args.check:
        if changed:
            print(f"{changed} content.sml file(s) would be reformatted.")
            sys.exit(1)
        print("All content.sml files are canonically formatted.")
        return
    print(f"Formatted {changed} content.sml file(s).")


def register_editing_commands(
    subparsers: argparse._SubParsersAction,
    handlers: dict[str, Any],
) -> None:
    sri = subparsers.add_parser(
        "replace-image",
        help="Replace an existing image from a local file or public URL",
    )
    sri.add_argument("folder", help="Presentation folder created by pull")
    sri.add_argument("element_id", metavar="ELEMENT_ID", help="Clean SML image ID")
    sri.add_argument(
        "new_src",
        metavar="NEW_SRC",
        help="Local path, file:// URL, or public HTTP(S) image URL",
    )
    sri.add_argument(
        "--fit",
        choices=("stretch", "contain"),
        default="contain",
        help=(
            "Geometry after replacement: aspect-correct top-left contain "
            "(default), or preserve the exact old box with stretch"
        ),
    )
    sri.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the computed geometry and API requests without writing",
    )
    sri.set_defaults(func=handlers["cmd_replace_image"])

    src = subparsers.add_parser(
        "replace-class",
        help="Replace a class across all content.sml files (local only)",
    )
    src.add_argument("folder", help="Presentation folder created by pull")
    src.add_argument(
        "old_class", metavar="OLD", nargs="?", help="Class token to replace"
    )
    src.add_argument(
        "new_class", metavar="NEW", nargs="?", help="Replacement class token"
    )
    src.add_argument(
        "--swap",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="Additional class swap; may be repeated",
    )
    src.add_argument(
        "--dry-run",
        action="store_true",
        help="Print replacement counts without writing files",
    )
    src.set_defaults(func=handlers["cmd_replace_class"])

    ss = subparsers.add_parser(
        "select",
        help="Select SML elements with a semantic query (local only)",
        epilog=SELECTOR_GRAMMAR,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ss.add_argument("folder", help="Presentation folder created by pull")
    ss.add_argument("query", help="Semantic element query")
    ss.set_defaults(func=handlers["cmd_select"])

    sap = subparsers.add_parser(
        "apply",
        help="Mutate elements selected by a semantic query (local only)",
        epilog=SELECTOR_GRAMMAR,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sap.add_argument("folder", help="Presentation folder created by pull")
    sap.add_argument("query", help="Semantic element query")
    sap.add_argument(
        "--add-class",
        action="append",
        default=[],
        metavar="CLASS",
        help="Add an element class; may be repeated",
    )
    sap.add_argument(
        "--remove-class",
        action="append",
        default=[],
        metavar="CLASS",
        help="Remove an element class; may be repeated",
    )
    roles = sap.add_mutually_exclusive_group()
    roles.add_argument("--set-role", metavar="ROLE", help="Set local role metadata")
    roles.add_argument(
        "--clear-role",
        action="store_true",
        help="Remove local role metadata",
    )
    sap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print match and mutation counts without writing files",
    )
    sap.set_defaults(func=handlers["cmd_apply"])

    sf = subparsers.add_parser(
        "fmt",
        help="Canonically format content.sml files without changing semantics",
    )
    sf.add_argument("folder", help="Presentation folder created by pull")
    sf.add_argument(
        "--check",
        action="store_true",
        help="Exit with status 1 if any content.sml file needs formatting",
    )
    sf.set_defaults(func=handlers["cmd_fmt"])
