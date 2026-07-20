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

from slidesmith.engine.json_utils import read_json


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
    from slidesmith.engine.client import SlidesClient
    from slidesmith.engine.transport import GoogleSlidesTransport

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
        from slidesmith.engine.client import diff_folder_with_result

        diff_result, requests = diff_folder_with_result(args.folder)
    else:
        from slidesmith.engine.client import diff_folder

        requests = diff_folder(args.folder)
    if not requests:
        print("No changes detected.")
    elif summary:
        from slidesmith.engine.content_diff import format_diff_summary

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
    from slidesmith.engine.assets import GoogleDriveAssetUploader
    from slidesmith.engine.client import SlidesClient
    from slidesmith.engine.conflicts import ConflictError
    from slidesmith.engine.transport import GoogleSlidesTransport

    if args.resume and not args.per_slide:
        raise ValueError("--resume requires --per-slide")

    _warn_if_stale(args.folder)
    token = _token("slide.push", str(args.folder))

    async def run() -> None:
        transport = GoogleSlidesTransport(token)
        uploader = GoogleDriveAssetUploader(token)
        try:
            def progress(event: str, message: str) -> None:
                if event == "start":
                    print(message, end="\r", flush=True)
                else:
                    print(message, flush=True)

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

    try:
        asyncio.run(run())
    except ConflictError as e:
        # The message already names the conflicting elements and what changed.
        print(str(e), file=sys.stderr)
        sys.exit(2)


def cmd_replace_image(args: Any) -> None:
    from slidesmith.engine.assets import GoogleDriveAssetUploader
    from slidesmith.engine.client import SlidesClient
    from slidesmith.engine.transport import GoogleSlidesTransport

    _warn_if_stale(args.folder)
    token = _token("slide.push", str(args.folder))

    async def run() -> None:
        transport = GoogleSlidesTransport(token)
        uploader = GoogleDriveAssetUploader(token)
        try:
            response = await SlidesClient(transport, uploader).replace_image(
                Path(args.folder),
                args.element_id,
                args.new_src,
            )
            for warning in response.get("warnings", []):
                print(f"warning: {warning}", file=sys.stderr)
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


def cmd_components(args: Any) -> None:
    from slidesmith.engine.components import load_components

    library = load_components(args.folder)
    for name in sorted(library.definitions):
        definition = library.definitions[name]
        slots = ", ".join(slot.name for slot in definition.slots) or "(no slots)"
        print(f"{name}: {slots}")


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
    _warn_if_stale(folder)
    if not args.no_thumbnails:
        from slidesmith.engine.transport import GoogleSlidesTransport

        metadata = read_json(folder / "presentation.json", missing_ok=False)
        # Preserve the pre-auth workspace validation order from the inline engine.
        read_json(folder / "id_mapping.json", missing_ok=False)
        presentation_id = metadata["presentationId"]
        token = _token("slide.pull", presentation_id)
        qa_dir = folder / ".qa"
        qa_dir.mkdir(parents=True, exist_ok=True)

        async def run() -> list[Path]:
            transport = GoogleSlidesTransport(token)
            try:
                return await download_thumbnails(transport, folder, qa_dir)
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
    spu.set_defaults(func=cmd_push)

    sri = sub.add_parser(
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
    sri.set_defaults(func=cmd_replace_image)

    src = sub.add_parser(
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
    src.set_defaults(func=cmd_replace_class)

    ss = sub.add_parser(
        "select",
        help="Select SML elements with a semantic query (local only)",
    )
    ss.add_argument("folder", help="Presentation folder created by pull")
    ss.add_argument("query", help="Semantic element query")
    ss.set_defaults(func=cmd_select)

    sap = sub.add_parser(
        "apply",
        help="Mutate elements selected by a semantic query (local only)",
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
    sap.set_defaults(func=cmd_apply)

    sf = sub.add_parser(
        "fmt",
        help="Canonically format content.sml files without changing semantics",
    )
    sf.add_argument("folder", help="Presentation folder created by pull")
    sf.add_argument(
        "--check",
        action="store_true",
        help="Exit with status 1 if any content.sml file needs formatting",
    )
    sf.set_defaults(func=cmd_fmt)

    sco = sub.add_parser(
        "components",
        help="List reusable components and their derived slots (local only)",
    )
    sco.add_argument("folder", help="Presentation folder containing components.sml")
    sco.set_defaults(func=cmd_components)

    sth = sub.add_parser(
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
    sthe.set_defaults(func=cmd_theme_extract)
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
    stha.set_defaults(func=cmd_theme_apply)

    ssn = sub.add_parser(
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
    ssnc.set_defaults(func=cmd_snippet_copy)
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
    ssnp.set_defaults(func=cmd_snippet_paste)

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
    sc.set_defaults(func=cmd_check)

    args = p.parse_args(argv)
    try:
        args.func(args)
    except Exception as e:  # surface a clean one-line error for CLI users
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
