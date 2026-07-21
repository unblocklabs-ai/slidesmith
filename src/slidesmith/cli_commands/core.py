"""Core CLI commands: auth, pull, diff, and push."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from slidesmith.cli_commands._support import (
    _presentation_id,
    _request_id_legend,
    _token,
    _warn_if_stale,
)
from slidesmith.engine.json_utils import read_json


def _cli_helper(name: str, fallback: Any) -> Any:
    from slidesmith import cli

    return getattr(cli, name, fallback)


def cmd_pull(args: Any) -> None:
    from slidesmith.engine.client import SlidesClient
    from slidesmith.engine.transport import GoogleSlidesTransport

    pid = _cli_helper("_presentation_id", _presentation_id)(args.url)
    token = _cli_helper("_token", _token)("slide.pull", args.url)
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
    _cli_helper("_warn_if_stale", _warn_if_stale)(args.folder)
    summary = bool(getattr(args, "summary", False))
    slide = getattr(args, "slide", None)
    if summary:
        from slidesmith.engine.client import diff_folder_with_result

        if slide is None:
            diff_result, requests = diff_folder_with_result(args.folder)
        else:
            diff_result, requests = diff_folder_with_result(
                args.folder,
                slide=slide,
            )
    else:
        from slidesmith.engine.client import diff_folder

        if slide is None:
            requests = diff_folder(args.folder)
        else:
            requests = diff_folder(args.folder, slide=slide)
    if not requests:
        if summary:
            print("No changes detected.")
        else:
            print("[]")
    elif summary:
        from slidesmith.engine.content_diff import format_diff_summary

        print(format_diff_summary(diff_result, len(requests)))
    else:
        print(json.dumps(requests, indent=2))
        mapping = read_json(Path(args.folder) / "id_mapping.json", missing_ok=False)
        legend = _cli_helper("_request_id_legend", _request_id_legend)(requests, mapping)
        if legend:
            print(f"Object IDs: {legend}", file=sys.stderr)


def cmd_push(args: Any) -> None:
    from slidesmith.engine.assets import GoogleDriveAssetUploader
    from slidesmith.engine.client import SlidesClient
    from slidesmith.engine.transport import GoogleSlidesTransport

    if args.resume and not args.per_slide:
        raise ValueError("--resume requires --per-slide")

    if args.preflight != "off":
        from slidesmith.engine.qa import push_preflight

        new_findings = push_preflight(
            args.folder,
            output=lambda message: print(message, file=sys.stderr),
        )
        if new_findings and args.preflight == "block":
            print(
                f"push preflight blocked: {new_findings} new finding(s)",
                file=sys.stderr,
            )
            sys.exit(1)
        if new_findings:
            print(
                f"warning: push preflight: {new_findings} new finding(s); proceeding",
                file=sys.stderr,
            )

    _cli_helper("_warn_if_stale", _warn_if_stale)(args.folder)
    token = _cli_helper("_token", _token)("slide.push", str(args.folder))

    async def run() -> None:
        transport = GoogleSlidesTransport(token)
        uploader = GoogleDriveAssetUploader(token)
        try:
            def progress(event: str, message: str) -> None:
                if event == "start":
                    print(message, end="\r", flush=True, file=sys.stderr)
                else:
                    print(message, flush=True, file=sys.stderr)

            resp = await SlidesClient(transport, uploader).push(
                Path(args.folder),
                force=args.force,
                per_slide=args.per_slide,
                resume=args.resume,
                progress=progress if args.per_slide else None,
            )
            for warning in resp.get("warnings", []):
                print(f"warning: {warning}", file=sys.stderr)
            if message := resp.get("message"):
                print(message)
            else:
                print(f"Push applied {len(resp.get('replies', []))} change(s).")
        finally:
            await uploader.close()
            await transport.close()

    asyncio.run(run())


def cmd_auth_doctor(_: Any) -> None:
    from slidesmith.credentials import auth_doctor_lines

    print("\n".join(auth_doctor_lines()))


def cmd_auth_login(_: Any) -> None:
    from slidesmith.credentials import CredentialsManager

    session = CredentialsManager().login(force=True)
    expires = datetime.fromtimestamp(session.expires_at).astimezone().isoformat()
    print(f"Authentication refreshed; session saved to available stores; expires {expires}")


def register_core_commands(
    subparsers: argparse._SubParsersAction,
    handlers: dict[str, Any],
) -> None:
    sa = subparsers.add_parser("auth", help="Diagnose or refresh authentication")
    auth_sub = sa.add_subparsers(dest="auth_command", required=True)
    sad = auth_sub.add_parser(
        "doctor", help="Print layered, agent-actionable authentication diagnostics"
    )
    sad.set_defaults(func=handlers["cmd_auth_doctor"])
    sal = auth_sub.add_parser(
        "login", help="Force fresh browser consent and save the session"
    )
    sal.set_defaults(func=handlers["cmd_auth_login"])

    sp = subparsers.add_parser("pull", help="Pull a presentation to a local SML folder")
    sp.add_argument("url", help="Presentation URL or ID")
    sp.add_argument("-o", "--output-dir", default=None)
    sp.add_argument("--no-raw", action="store_true", help="Skip saving raw API JSON")
    sp.set_defaults(func=handlers["cmd_pull"])

    sd = subparsers.add_parser("diff", help="Preview batchUpdate requests (local only, no API calls)")
    sd.add_argument("folder", help="Presentation folder created by pull")
    sd.add_argument(
        "--summary",
        action="store_true",
        help="Print a compact slide-grouped summary instead of request JSON",
    )
    sd.add_argument(
        "--slide",
        type=int,
        metavar="N",
        help="Limit output to one 1-based slide number",
    )
    sd.set_defaults(func=handlers["cmd_diff"])

    spu = subparsers.add_parser("push", help="Apply local edits to the same deck in place")
    spu.add_argument("folder", help="Presentation folder created by pull")
    spu.add_argument(
        "--force",
        action="store_true",
        help=(
            "Bypass the conflict guard (and the deck-wide revision lock for a "
            "default push): push even if touched elements were also edited in "
            "Google Slides; --per-slide keeps its per-slide revision locks"
        ),
    )
    spu.add_argument(
        "--per-slide",
        action="store_true",
        help=(
            "Push one revision-locked batch per changed slide with progress and "
            "a resume ledger (earlier slides remain applied if a later one fails)"
        ),
    )
    spu.add_argument(
        "--resume",
        action="store_true",
        help=(
            "With --per-slide, skip ledger entries whose slide content still "
            "matches and continue from the first unfinished slide"
        ),
    )
    spu.add_argument(
        "--preflight",
        choices=("off", "warn", "block"),
        default="off",
        help=(
            "Offline geometry lint before push: off (default), warn and proceed, "
            "or block on findings new since the workspace baseline"
        ),
    )
    spu.set_defaults(func=handlers["cmd_push"])
