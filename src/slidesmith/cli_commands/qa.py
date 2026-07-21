"""Quality-assurance CLI command."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from slidesmith.cli_commands._support import _token, _transport_options, _warn_if_stale
from slidesmith.engine.json_utils import read_json


def _cli_helper(name: str, fallback: Any) -> Any:
    from slidesmith import cli

    return getattr(cli, name, fallback)


def cmd_check(args: Any) -> None:
    from slidesmith.engine.qa import (
        check_folder,
        create_contact_sheet,
        download_thumbnails,
    )

    folder = Path(args.folder)
    if args.contact_sheet and args.no_thumbnails:
        raise ValueError(
            "--contact-sheet requires thumbnail downloads; remove --no-thumbnails"
        )
    _cli_helper("_warn_if_stale", _warn_if_stale)(folder)
    if not args.no_thumbnails:
        from slidesmith.engine.transport import GoogleSlidesTransport

        metadata = read_json(folder / "presentation.json", missing_ok=False)
        # Preserve the pre-auth workspace validation order from the inline engine.
        read_json(folder / "id_mapping.json", missing_ok=False)
        presentation_id = metadata["presentationId"]
        token = _cli_helper("_token", _token)("slide.pull", presentation_id)
        qa_dir = folder / ".qa"
        qa_dir.mkdir(parents=True, exist_ok=True)

        async def run() -> list[Path]:
            transport = GoogleSlidesTransport(token, **_transport_options(token))
            try:
                return await download_thumbnails(
                    transport,
                    folder,
                    qa_dir,
                    output=print,
                )
            finally:
                await transport.close()

        downloaded_paths = asyncio.run(run())
        if args.contact_sheet:
            print(create_contact_sheet(qa_dir, downloaded_paths))

    exit_code = check_folder(
        folder,
        strict=args.strict,
        accept=args.accept,
        unaccept=args.unaccept,
    )
    if exit_code:
        sys.exit(exit_code)


def register_qa_commands(
    subparsers: argparse._SubParsersAction,
    handlers: dict[str, Any],
) -> None:
    sc = subparsers.add_parser(
        "check",
        help="Download slide thumbnails and run offline geometry QA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Suppression: add the qa-accept-overlap class to an element when an "
            "overlap is intentional. Leaf elements covering at least 90% of the "
            "slide area are automatically treated as backgrounds and exempt from "
            "pairwise overlap findings."
        ),
    )
    sc.add_argument("folder", help="Presentation folder created by pull")
    sc.add_argument(
        "--no-thumbnails",
        action="store_true",
        help="Run offline geometry lint only (no network or authentication)",
    )
    sc.add_argument(
        "--strict",
        action="store_true",
        help="Exit with status 1 when geometry findings are reported",
    )
    sc.add_argument(
        "--contact-sheet",
        action="store_true",
        help="Compose downloaded thumbnails into .qa/contact-sheet.png",
    )
    acceptance = sc.add_mutually_exclusive_group()
    acceptance.add_argument(
        "--accept",
        action="append",
        default=[],
        metavar="FINDING_ID",
        help="Accept a current finding by its stable ID; may be repeated",
    )
    acceptance.add_argument(
        "--unaccept",
        action="append",
        default=[],
        metavar="FINDING_ID",
        help="Remove an accepted finding by its stable ID; may be repeated",
    )
    sc.set_defaults(func=handlers["cmd_check"])
