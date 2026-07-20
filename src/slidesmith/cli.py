"""slidesmith CLI: pull / diff / push for Google Slides SML folders."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from extraslide.json_utils import read_json


def _presentation_id(url_or_id: str) -> str:
    m = re.search(r"/presentation/d/([a-zA-Z0-9_-]+)", url_or_id)
    presentation_id = m.group(1) if m else url_or_id
    if re.fullmatch(r"[A-Za-z0-9_-]+", presentation_id) is None:
        raise ValueError(
            "Invalid presentation URL or ID. Provide a Google Slides URL or an ID "
            "containing only letters, numbers, underscores, and hyphens."
        )
    return presentation_id


def _token(command_type: str, target: str) -> str:
    from slidesmith.credentials import CredentialsManager

    manager = CredentialsManager()
    cred = manager.get_credential(
        command={"type": command_type, "file_url": target, "file_name": ""},
        reason=f"slidesmith {command_type}",
    )
    return cred.token


def _warn_if_stale(folder: str | Path, *, now: datetime | None = None) -> None:
    """Warn when a workspace's pull timestamp is more than 24 hours old."""
    metadata_path = Path(folder) / "presentation.json"
    try:
        metadata = read_json(metadata_path, missing_ok=True)
        pulled_at_raw = metadata.get("pulledAt")
        if not isinstance(pulled_at_raw, str):
            return
        pulled_at = datetime.fromisoformat(pulled_at_raw.replace("Z", "+00:00"))
        if pulled_at.tzinfo is None:
            pulled_at = pulled_at.replace(tzinfo=timezone.utc)
    except (OSError, ValueError, AttributeError):
        return

    current = now or datetime.now(timezone.utc)
    if current.astimezone(timezone.utc) - pulled_at.astimezone(
        timezone.utc
    ) > timedelta(hours=24):
        print(
            f"warning: workspace pulled {pulled_at_raw}; deck may have changed — "
            "re-pull recommended",
            file=sys.stderr,
        )


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
    _warn_if_stale(args.folder)
    summary = bool(getattr(args, "summary", False))
    if summary:
        from extraslide.client import diff_folder_with_result

        diff_result, requests = diff_folder_with_result(args.folder)
    else:
        from extraslide.client import diff_folder

        requests = diff_folder(args.folder)
    if not requests:
        print("No changes detected.")
    elif summary:
        from extraslide.content_diff import format_diff_summary

        print(format_diff_summary(diff_result, len(requests)))
    else:
        print(json.dumps(requests, indent=2))
        mapping = read_json(Path(args.folder) / "id_mapping.json", missing_ok=False)
        legend = _request_id_legend(requests, mapping)
        if legend:
            print(f"Object IDs: {legend}", file=sys.stderr)


def _request_id_legend(
    requests: list[dict[str, Any]], id_mapping: dict[str, str]
) -> str:
    """Describe request object IDs without making stdout cease to be JSON."""
    reverse_mapping = {google_id: clean_id for clean_id, google_id in id_mapping.items()}
    labels: dict[str, str] = {}
    create_operations = {"createShape", "createLine", "createImage"}
    for request in requests:
        for operation, body in request.items():
            if not isinstance(body, dict):
                continue
            object_id = body.get("objectId")
            if not isinstance(object_id, str) or object_id in labels:
                continue
            if object_id in reverse_mapping:
                labels[object_id] = reverse_mapping[object_id]
            elif object_id.startswith("new_"):
                labels[object_id] = f"{object_id[4:]}(new)"
            elif operation in create_operations:
                labels[object_id] = f"{object_id}(new)"
    return ", ".join(
        f"{object_id} = {clean_id}" for object_id, clean_id in labels.items()
    )


def cmd_push(args: Any) -> None:
    from extraslide.client import SlidesClient
    from extraslide.conflicts import ConflictError
    from extraslide.transport import GoogleSlidesTransport

    _warn_if_stale(args.folder)
    token = _token("slide.push", str(args.folder))

    async def run() -> None:
        transport = GoogleSlidesTransport(token)
        try:
            resp = await SlidesClient(transport).push(
                Path(args.folder), force=args.force
            )
            for warning in resp.get("warnings", []):
                print(f"warning: {warning}", file=sys.stderr)
            if message := resp.get("message"):
                print(message)
            else:
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
    from extraslide.qa import check_folder, download_thumbnails

    folder = Path(args.folder)
    _warn_if_stale(folder)
    if not args.no_thumbnails:
        from extraslide.transport import GoogleSlidesTransport

        metadata = read_json(folder / "presentation.json", missing_ok=False)
        # Preserve the pre-auth workspace validation order from the inline engine.
        read_json(folder / "id_mapping.json", missing_ok=False)
        presentation_id = metadata["presentationId"]
        token = _token("slide.pull", presentation_id)
        qa_dir = folder / ".qa"
        qa_dir.mkdir(parents=True, exist_ok=True)

        async def run() -> None:
            transport = GoogleSlidesTransport(token)
            try:
                await download_thumbnails(transport, folder, qa_dir)
            finally:
                await transport.close()

        asyncio.run(run())

    exit_code = check_folder(folder, strict=args.strict)
    if exit_code:
        sys.exit(exit_code)


def cmd_auth_doctor(_: Any) -> None:
    from slidesmith.credentials import auth_doctor_lines

    print("\n".join(auth_doctor_lines()))


def cmd_auth_login(_: Any) -> None:
    from slidesmith.credentials import CredentialsManager

    session = CredentialsManager().login(force=True)
    expires = datetime.fromtimestamp(session.expires_at).astimezone().isoformat()
    print(f"Authentication refreshed; session saved to available stores; expires {expires}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="slidesmith",
        description=(
            "Pull Google Slides to local SML files, edit them, preview the diff, "
            "and push batchUpdates back to the same deck."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    sa = sub.add_parser("auth", help="Diagnose or refresh authentication")
    auth_sub = sa.add_subparsers(dest="auth_command", required=True)
    sad = auth_sub.add_parser(
        "doctor", help="Print layered, agent-actionable authentication diagnostics"
    )
    sad.set_defaults(func=cmd_auth_doctor)
    sal = auth_sub.add_parser(
        "login", help="Force fresh browser consent and save the session"
    )
    sal.set_defaults(func=cmd_auth_login)

    sp = sub.add_parser("pull", help="Pull a presentation to a local SML folder")
    sp.add_argument("url", help="Presentation URL or ID")
    sp.add_argument("-o", "--output-dir", default=None)
    sp.add_argument("--no-raw", action="store_true", help="Skip saving raw API JSON")
    sp.set_defaults(func=cmd_pull)

    sd = sub.add_parser("diff", help="Preview batchUpdate requests (local only, no API calls)")
    sd.add_argument("folder", help="Presentation folder created by pull")
    sd.add_argument(
        "--summary",
        action="store_true",
        help="Print a compact slide-grouped summary instead of request JSON",
    )
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
