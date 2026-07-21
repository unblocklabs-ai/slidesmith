"""slidesmith CLI: pull / diff / push for Google Slides SML folders."""

from __future__ import annotations

import argparse
import asyncio
import sys

from slidesmith import __version__
from slidesmith.cli_commands._support import (
    _presentation_id,
    _request_id_legend,
    _token,
    _warn_if_stale,
)
from slidesmith.cli_commands.core import (
    cmd_auth_doctor,
    cmd_auth_login,
    cmd_diff,
    cmd_pull,
    cmd_push,
    register_core_commands,
)
from slidesmith.cli_commands.editing import (
    SELECTOR_GRAMMAR,
    cmd_apply,
    cmd_fmt,
    cmd_replace_class,
    cmd_replace_image,
    cmd_reorder,
    cmd_select,
    register_editing_commands,
)
from slidesmith.cli_commands.components import (
    cmd_components,
    register_components_command,
)
from slidesmith.cli_commands.theme import (
    cmd_snippet_copy,
    cmd_snippet_paste,
    cmd_theme_apply,
    cmd_theme_extract,
    register_theme_commands,
)
from slidesmith.cli_commands.qa import cmd_check, register_qa_commands
from slidesmith.engine.conflicts import ConflictError


__all__ = [
    "asyncio",
    "build_parser",
    "cmd_apply",
    "cmd_auth_doctor",
    "cmd_auth_login",
    "cmd_check",
    "cmd_components",
    "cmd_diff",
    "cmd_fmt",
    "cmd_pull",
    "cmd_push",
    "cmd_replace_class",
    "cmd_replace_image",
    "cmd_reorder",
    "cmd_select",
    "cmd_snippet_copy",
    "cmd_snippet_paste",
    "cmd_theme_apply",
    "cmd_theme_extract",
    "SELECTOR_GRAMMAR",
    "main",
    "_presentation_id",
    "_request_id_legend",
    "_token",
    "_warn_if_stale",
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="slidesmith",
        description=(
            "Pull Google Slides to local SML files, edit them, preview the diff, "
            "and push batchUpdates back to the same deck."
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    register_core_commands(sub, globals())

    register_editing_commands(sub, globals())

    register_components_command(sub, globals())

    register_theme_commands(sub, globals())

    register_qa_commands(sub, globals())

    return p


def main(argv: list[str] | None = None) -> None:
    p = build_parser()

    args = p.parse_args(argv)
    try:
        args.func(args)
    except ConflictError as e:
        # The message already names the conflicting elements and what changed.
        print(str(e), file=sys.stderr)
        sys.exit(2)
    except Exception as e:  # surface a clean one-line error for CLI users
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
