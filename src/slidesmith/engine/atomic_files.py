"""Atomic helpers for committing related local text files."""

from __future__ import annotations

import stat
import tempfile
from os import replace as replace_file
from pathlib import Path


def commit_text_files(
    pending: dict[Path, str],
    *,
    allow_create: bool = True,
) -> None:
    """Replace prepared files together, restoring prior state after failure."""
    if not allow_create:
        missing = next((path for path in pending if not path.exists()), None)
        if missing is not None:
            raise FileNotFoundError(missing)

    changed = {
        path: value
        for path, value in pending.items()
        if not path.exists() or path.read_text(encoding="utf-8") != value
    }
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    existing: set[Path] = set()
    committed: list[Path] = []
    try:
        for path, value in changed.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
            if path.exists():
                existing.add(path)
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=path.parent,
                    prefix=f".{path.name}.backup.",
                    suffix=".tmp",
                    delete=False,
                ) as backup:
                    backup.write(path.read_bytes())
                    backups[path] = Path(backup.name)
                backups[path].chmod(mode)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(value)
                staged[path] = Path(temporary.name)
            staged[path].chmod(mode)
        for path, temporary_path in staged.items():
            replace_file(temporary_path, path)
            committed.append(path)
    except Exception:
        for path in reversed(committed):
            if path in existing:
                replace_file(backups[path], path)
            else:
                path.unlink(missing_ok=True)
        raise
    finally:
        for temporary_path in [*staged.values(), *backups.values()]:
            temporary_path.unlink(missing_ok=True)
