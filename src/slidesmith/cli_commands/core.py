"""Core CLI commands: auth, create, pull, diff, and push."""

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
    _require_workspace,
    _request_id_legend,
    print_push_warnings,
    _transport_options,
    _token,
    _warn_if_stale,
)
from slidesmith.engine.json_utils import read_json


def _share_emails(value: str | None) -> list[str]:
    """Parse one comma-separated --share value into non-empty email addresses."""
    if value is None:
        return []
    emails = [part.strip() for part in value.split(",")]
    if not emails or any(not email for email in emails):
        raise ValueError("--share expects one or more comma-separated email addresses")
    return emails


def _presentation_url(presentation_id: str) -> str:
    return f"https://docs.google.com/presentation/d/{presentation_id}/edit"


def _remote_deck_context(presentation_id: str, output_path: Path) -> str:
    return (
        f"Presentation ID: {presentation_id}\n"
        f"URL: {_presentation_url(presentation_id)}\n"
        "The remote deck exists and can be pulled normally with: "
        f"slidesmith pull {presentation_id} -o {output_path}"
    )


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
        transport = GoogleSlidesTransport(token, **_transport_options(token))
        try:
            files = await SlidesClient(transport).pull(pid, out, save_raw=not args.no_raw)
            n = sum(1 for f in files if f.name == "content.sml")
            print(f"Pulled {n} slide(s) to {out / pid}/")
        finally:
            await transport.close()

    asyncio.run(run())


def cmd_create(args: Any) -> None:
    from slidesmith.engine.client import SlidesClient, validate_create_output_parent
    from slidesmith.engine.permissions import (
        DrivePermissionError,
        GoogleDrivePermissionsClient,
    )
    from slidesmith.engine.transport import GoogleSlidesTransport

    emails = _share_emails(args.share)
    parent = validate_create_output_parent(args.dir)

    # Reuse the existing write-capable command type so gateway auth works for
    # every mode that already supports push; Google scopes are the same for the
    # subsequent Slides and Drive requests.
    token = _cli_helper("_token", _token)("slide.push", str(parent))

    async def run() -> None:
        transport = GoogleSlidesTransport(token, **_transport_options(token))
        permission_client: GoogleDrivePermissionsClient | None = None
        try:
            def report_created(created: Any) -> None:
                workspace = parent / created.presentation_id
                # Flush immediately: the announcement is the only record of
                # the remote deck if a later step crashes the process.
                print(
                    f"Created presentation: {created.presentation_id}",
                    flush=True,
                )
                print(
                    f"URL: {_presentation_url(created.presentation_id)}",
                    flush=True,
                )
                print(f"Workspace: {workspace}", flush=True)

            created = await SlidesClient(transport).create(
                args.title,
                parent,
                on_created=report_created,
            )

            if not emails:
                return

            try:
                permission_client = GoogleDrivePermissionsClient(
                    client=transport._client
                )
            except Exception as exc:
                raise RuntimeError(
                    f"{_remote_deck_context(created.presentation_id, parent)}\n"
                    f"Sharing setup failed: {exc}"
                ) from exc
            succeeded: list[str] = []
            failed: list[tuple[str, str]] = []
            for email in emails:
                try:
                    await permission_client.create_permission(
                        created.presentation_id,
                        permission_type="user",
                        role=args.role,
                        email_address=email,
                        send_notification_email=False,
                    )
                except DrivePermissionError as exc:
                    message = str(exc)
                    if exc.status_code == 403:
                        message = (
                            f"{message}; token may be missing the "
                            "https://www.googleapis.com/auth/drive.file scope"
                        )
                    failed.append((email, message))
                except Exception as exc:  # report one failed recipient and continue
                    failed.append((email, str(exc)))
                else:
                    succeeded.append(email)

            if succeeded:
                print(f"Shared with: {', '.join(succeeded)} (role={args.role})")
            if failed:
                print("Share failures:")
                for email, message in failed:
                    print(f"  {email}: {message}")
                print(
                    "The deck was created and materialized; sharing failed for "
                    f"{len(failed)} recipient(s)."
                )
                print(
                    f"{_remote_deck_context(created.presentation_id, parent)}\n"
                    f"Sharing failed for {len(failed)} recipient(s).",
                    file=sys.stderr,
                )
                if not succeeded:
                    raise SystemExit(1)
        finally:
            if permission_client is not None:
                await permission_client.close()
            await transport.close()

    asyncio.run(run())


def cmd_diff(args: Any) -> None:
    _cli_helper("_require_workspace", _require_workspace)(args.folder)
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

    _cli_helper("_require_workspace", _require_workspace)(args.folder)

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
        transport = GoogleSlidesTransport(token, **_transport_options(token))
        uploader = GoogleDriveAssetUploader(str(token))
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
            print_push_warnings(resp.get("warnings", []))
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
    sp.add_argument(
        "-o",
        "--output-dir",
        "--dir",
        dest="output_dir",
        default=None,
        help="Parent directory for the ID-named workspace (--dir is an alias for --output-dir)",
    )
    sp.add_argument("--no-raw", action="store_true", help="Skip saving raw API JSON")
    sp.set_defaults(func=handlers["cmd_pull"])

    sc = subparsers.add_parser(
        "create",
        help="Create a presentation and materialize its local SML workspace",
    )
    sc.add_argument("--title", required=True, help="Title for the new presentation")
    sc.add_argument(
        "--share",
        default=None,
        metavar="EMAIL[,EMAIL...]",
        help="Share the app-created deck with one or more comma-separated emails",
    )
    sc.add_argument(
        "--role",
        choices=("writer", "commenter", "reader"),
        default="writer",
        help="Drive role for --share recipients (default: writer)",
    )
    sc.add_argument(
        "--dir",
        default=".",
        help="Parent directory for the ID-named local workspace (default: .)",
    )
    sc.set_defaults(func=handlers["cmd_create"])

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
