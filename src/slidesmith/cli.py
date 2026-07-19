"""slidesmith CLI: pull / diff / push for Google Slides SML folders."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def _presentation_id(url_or_id: str) -> str:
    m = re.search(r"/presentation/d/([a-zA-Z0-9_-]+)", url_or_id)
    return m.group(1) if m else url_or_id


def _token(command_type: str, target: str) -> str:
    from slidesmith.credentials import CredentialsManager

    manager = CredentialsManager()
    cred = manager.get_credential(
        command={"type": command_type, "file_url": target, "file_name": ""},
        reason=f"slidesmith {command_type}",
    )
    return cred.token


def cmd_pull(args: Any) -> None:
    from extraslide.client import SlidesClient
    from extraslide.transport import GoogleSlidesTransport

    pid = _presentation_id(args.url)
    token = _token("slide.pull", args.url)
    out = Path(args.output_dir) if args.output_dir else Path()

    async def run() -> None:
        transport = GoogleSlidesTransport(token)
        try:
            files = await SlidesClient(transport).pull(pid, out, save_raw=not args.no_raw)
            n = sum(1 for f in files if f.name == "content.sml")
            print(f"Pulled {n} slide(s) to {out / pid}/")
        finally:
            await transport.close()

    asyncio.run(run())


def cmd_diff(args: Any) -> None:
    from extraslide.client import diff_folder

    requests = diff_folder(args.folder)
    if not requests:
        print("No changes detected.")
    else:
        print(json.dumps(requests, indent=2))


def cmd_push(args: Any) -> None:
    from extraslide.client import ConflictError, SlidesClient
    from extraslide.transport import GoogleSlidesTransport

    token = _token("slide.push", str(args.folder))

    async def run() -> None:
        transport = GoogleSlidesTransport(token)
        try:
            resp = await SlidesClient(transport).push(
                Path(args.folder), force=args.force
            )
            print(f"Push applied {len(resp.get('replies', []))} change(s).")
        finally:
            await transport.close()

    try:
        asyncio.run(run())
    except ConflictError as e:
        # The message already names the conflicting elements and what changed.
        print(str(e), file=sys.stderr)
        sys.exit(2)


def cmd_check(args: Any) -> None:
    from extraslide.qa import check_folder

    folder = Path(args.folder)
    if not args.no_thumbnails:
        from extraslide.transport import GoogleSlidesTransport

        metadata = json.loads(
            (folder / "presentation.json").read_text(encoding="utf-8")
        )
        id_mapping = json.loads(
            (folder / "id_mapping.json").read_text(encoding="utf-8")
        )
        presentation_id = metadata["presentationId"]
        token = _token("slide.pull", presentation_id)
        qa_dir = folder / ".qa"
        qa_dir.mkdir(parents=True, exist_ok=True)

        async def run() -> None:
            transport = GoogleSlidesTransport(token)
            try:
                content_paths = (folder / "slides").glob("*/content.sml")
                for content_path in sorted(
                    content_paths, key=lambda path: int(path.parent.name)
                ):
                    slide_number = content_path.parent.name
                    slide_clean_id = ET.fromstring(
                        content_path.read_text(encoding="utf-8")
                    ).get("id")
                    if not slide_clean_id or slide_clean_id not in id_mapping:
                        raise ValueError(
                            f"No Google page object ID for slide {slide_number}"
                        )
                    png = await transport.get_page_thumbnail(
                        presentation_id, id_mapping[slide_clean_id]
                    )
                    output_path = qa_dir / f"slide-{slide_number}.png"
                    output_path.write_bytes(png)
                    print(output_path, flush=True)
            finally:
                await transport.close()

        asyncio.run(run())

    exit_code = check_folder(folder, strict=args.strict)
    if exit_code:
        sys.exit(exit_code)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="slidesmith",
        description=(
            "Pull Google Slides to local SML files, edit them, preview the diff, "
            "and push batchUpdates back to the same deck."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("pull", help="Pull a presentation to a local SML folder")
    sp.add_argument("url", help="Presentation URL or ID")
    sp.add_argument("-o", "--output-dir", default=None)
    sp.add_argument("--no-raw", action="store_true", help="Skip saving raw API JSON")
    sp.set_defaults(func=cmd_pull)

    sd = sub.add_parser("diff", help="Preview batchUpdate requests (local only, no API calls)")
    sd.add_argument("folder", help="Presentation folder created by pull")
    sd.set_defaults(func=cmd_diff)

    spu = sub.add_parser("push", help="Apply local edits to the same deck in place")
    spu.add_argument("folder", help="Presentation folder created by pull")
    spu.add_argument(
        "--force",
        action="store_true",
        help=(
            "Bypass the conflict guard and revision lock: push even if the "
            "elements being changed were also edited in Google Slides "
            "(their remote edits are overwritten; a warning is logged)"
        ),
    )
    spu.set_defaults(func=cmd_push)

    sc = sub.add_parser(
        "check",
        help="Download slide thumbnails and run offline geometry QA",
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
    sc.set_defaults(func=cmd_check)

    args = p.parse_args(argv)
    try:
        args.func(args)
    except Exception as e:  # surface a clean one-line error for CLI users
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
