"""Theme and snippet CLI commands."""

from __future__ import annotations

import argparse
from typing import Any


def cmd_theme_extract(args: Any) -> None:
    from slidesmith.engine.theme import extract_theme, write_theme

    theme = extract_theme(args.folder, from_slides=args.from_slides)
    output = write_theme(theme, args.output)
    print(
        f"Extracted {len(theme['tokens']['palette'])} palette color(s), "
        f"{len(theme['inventory']['type']['fontFamilies'])} font family value(s), "
        f"and {len(theme['roles'])} role style(s) to {output}"
    )


def cmd_theme_apply(args: Any) -> None:
    from slidesmith.engine.theme import apply_theme, load_theme

    result = apply_theme(
        args.folder,
        load_theme(args.theme),
        to_slides=args.to_slides,
        map_colors=args.map_colors,
        dry_run=args.dry_run,
    )
    if args.verbose:
        for preview in result.previews:
            print(f"Slide {preview.slide_index} element {preview.element_id}:")
            if preview.old_classes != preview.new_classes:
                print(
                    f"  classes: {' '.join(preview.old_classes) or '(none)'} -> "
                    f"{' '.join(preview.new_classes) or '(none)'}"
                )
            for note in preview.color_notes:
                print(f"  {note}")
    for slide_index, counts in result.slide_counts.items():
        print(
            f"Slide {slide_index}: {counts['roleRestyles']} role restyle(s), "
            f"{counts['fontChanges']} font change(s), "
            f"{counts['colorChanges']} color mapping(s)"
        )
    print(
        f"Total: {result.role_restyles} role restyle(s), "
        f"{result.font_changes} font change(s), "
        f"{result.color_changes} color mapping(s) across "
        f"{result.changed_slides} changed slide(s)"
    )
    if args.dry_run:
        print("Dry run: no files written.")


def cmd_snippet_copy(args: Any) -> None:
    from slidesmith.engine.snippet import copy_snippet

    result = copy_snippet(args.folder, args.selector, args.output)
    print(
        f"Copied {result.elements} element(s) from slide {result.slide_number} "
        f"into {result.width:g}x{result.height:g} snippet {result.path}"
    )


def cmd_snippet_paste(args: Any) -> None:
    from slidesmith.engine.snippet import parse_frame, paste_snippet

    role_maps: list[tuple[str, str]] = []
    for value in args.map:
        if ":" not in value:
            raise ValueError(
                f"--map must use SNIPPET_ROLE:DESTINATION_ROLE, got {value!r}"
            )
        role_maps.append(tuple(value.split(":", 1)))
    result = paste_snippet(
        args.folder,
        args.slide,
        args.snippet,
        role_maps=role_maps,
        frame=parse_frame(args.frame),
        dry_run=args.dry_run,
    )
    print(
        f"Slide {result.slide_index}: insert {result.inserted_elements} element(s) "
        f"in {result.inserted_roots} root subtree(s) with ID prefix "
        f"{result.id_prefix}; {result.mapped_roles} role map(s)"
    )
    if args.dry_run:
        print("Dry run: no files written.")


def register_theme_commands(
    subparsers: argparse._SubParsersAction,
    handlers: dict[str, Any],
) -> None:
    sth = subparsers.add_parser(
        "theme",
        help="Extract or apply cross-deck design-language themes (local only)",
    )
    theme_sub = sth.add_subparsers(dest="theme_command", required=True)
    sthe = theme_sub.add_parser(
        "extract",
        help="Extract palette, type, and role styles into theme.json",
    )
    sthe.add_argument("folder", help="Presentation folder created by pull")
    sthe.add_argument(
        "--from-slides",
        metavar="RANGE",
        help="Inclusive source slides, e.g. 1-3 or 1,3,5-7",
    )
    sthe.add_argument("-o", "--output", default="theme.json")
    sthe.set_defaults(func=handlers["cmd_theme_extract"])
    stha = theme_sub.add_parser(
        "apply",
        help="Apply role styles, font family, and optional palette mapping",
    )
    stha.add_argument("folder", help="Target presentation folder")
    stha.add_argument("theme", help="theme.json produced by theme extract")
    stha.add_argument(
        "--to-slides",
        metavar="RANGE",
        help="Inclusive target slides, e.g. 4-24 or 4,6,8-10",
    )
    stha.add_argument(
        "--map-colors",
        action="store_true",
        help="Map near off-theme RGB colors to the extracted palette",
    )
    stha.add_argument(
        "--dry-run",
        action="store_true",
        help="Print validated change counts without writing files",
    )
    stha.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-element class and color decisions",
    )
    stha.set_defaults(func=handlers["cmd_theme_apply"])

    ssn = subparsers.add_parser(
        "snippet",
        help="Copy or paste reusable SML layout snippets (local only)",
    )
    snippet_sub = ssn.add_subparsers(dest="snippet_command", required=True)
    ssnc = snippet_sub.add_parser(
        "copy",
        help="Copy a single-slide selector match into an origin-relative snippet",
    )
    ssnc.add_argument("folder", help="Source presentation folder")
    ssnc.add_argument("selector", help="Semantic selector query")
    ssnc.add_argument("-o", "--output", required=True, help="Output snippet.sml")
    ssnc.set_defaults(func=handlers["cmd_snippet_copy"])
    ssnp = snippet_sub.add_parser(
        "paste",
        help="Insert snippet shapes and styles as new destination elements",
    )
    ssnp.add_argument("folder", help="Destination presentation folder")
    ssnp.add_argument("snippet", help="snippet.sml produced by snippet copy")
    ssnp.add_argument("--slide", type=int, required=True, help="Destination slide")
    ssnp.add_argument(
        "--frame",
        metavar="X,Y,W,H",
        help="Target point-valued frame; defaults to the snippet box at 0,0",
    )
    ssnp.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="SNIPPET_ROLE:DESTINATION_ROLE",
        help="Fill one snippet role from one destination role; may be repeated",
    )
    ssnp.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and count the insertion without writing files",
    )
    ssnp.set_defaults(func=handlers["cmd_snippet_paste"])
