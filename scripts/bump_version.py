#!/usr/bin/env python3
"""Bump the version across every statically-maintained surface at once.

Usage:
    python scripts/bump_version.py X.Y.Z

Rewrites the three static ``version`` literals so they can never be bumped
inconsistently by hand:
  - pyproject.toml               [project].version
  - .claude-plugin/plugin.json   version
  - .codex-plugin/plugin.json    version

Everything else is derived (the CLI ``__version__`` reads package metadata) or
carries no version (the marketplace files). The bump is failure-atomic: every
file is validated and its new content computed *before* anything is written, so
a bad input never leaves a half-bumped tree. After running this, finalize
CHANGELOG.md, tag, and cut the release per RELEASING.md;
``tests/test_version_consistency.py`` verifies the result on every ``pytest``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
# Plain numeric X.Y.Z, no leading zeros and no pre-release/build metadata —
# this repo only ships such versions, matching the documented usage.
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_PYPROJECT_VERSION = re.compile(r'(?m)^version = "[^"]+"')
_PLUGIN_MANIFESTS = (
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
)


def _plan_pyproject(new: str) -> tuple[Path, str]:
    path = _ROOT / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    # Line-anchored so only [project].version is a candidate; require exactly one
    # so a future [tool.*] `version = ` can never be rewritten by mistake.
    matches = _PYPROJECT_VERSION.findall(text)
    if len(matches) != 1:
        raise SystemExit(
            "pyproject.toml: expected exactly 1 line-start version assignment, "
            f"found {len(matches)}"
        )
    return path, _PYPROJECT_VERSION.sub(f'version = "{new}"', text, count=1)


def _plan_json(rel: str, new: str) -> tuple[Path, str]:
    path = _ROOT / rel
    data = json.loads(path.read_text(encoding="utf-8"))
    if "version" not in data:
        raise SystemExit(f"{rel}: no 'version' key to bump")
    data["version"] = new
    return path, json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not _SEMVER.match(argv[1]):
        print("usage: python scripts/bump_version.py X.Y.Z", file=sys.stderr)
        return 2
    new = argv[1]
    # Phase 1: validate every file and compute its new content — no writes yet.
    plans = [_plan_pyproject(new)]
    plans += [_plan_json(manifest, new) for manifest in _PLUGIN_MANIFESTS]
    # Phase 2: commit the writes only once all inputs are known-good.
    for path, text in plans:
        path.write_text(text, encoding="utf-8")
    print(f"Bumped to {new}:")
    print("  pyproject.toml")
    for manifest in _PLUGIN_MANIFESTS:
        print(f"  {manifest}")
    print(
        "\nNext: finalize CHANGELOG.md, tag, and cut the release per RELEASING.md.\n"
        "Verify with: pytest tests/test_version_consistency.py"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
