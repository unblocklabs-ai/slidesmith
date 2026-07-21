"""Guard against version drift across the statically-maintained surfaces.

``pyproject.toml`` is the single source of truth for the package version. The
Claude Code and Codex plugin manifests carry *static* ``version`` literals
because plugin install reads each manifest file as committed on GitHub — there
is no build step to derive them — which makes them the only drift risk. Bump
every surface at once with ``python scripts/bump_version.py X.Y.Z``.

Everything else is safe: the CLI ``__version__`` derives from installed package
metadata, and the marketplace files intentionally carry no version.

This test deliberately does NOT compare ``importlib.metadata.version`` against
``pyproject`` — in an editable checkout the installed metadata lags a bump until
reinstall, which would fail spuriously right after ``bump_version.py`` runs.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

_PLUGIN_MANIFESTS = (
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
)
_MARKETPLACES = (
    ".claude-plugin/marketplace.json",
    ".agents/plugins/marketplace.json",
)


def _pyproject_version() -> str:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


@pytest.mark.parametrize("manifest", _PLUGIN_MANIFESTS)
def test_plugin_manifest_version_matches_pyproject(manifest: str) -> None:
    expected = _pyproject_version()
    actual = json.loads((_ROOT / manifest).read_text(encoding="utf-8")).get("version")
    assert actual == expected, (
        f"{manifest} version {actual!r} != pyproject {expected!r}; "
        f"run `python scripts/bump_version.py {expected}` to sync every surface."
    )


@pytest.mark.parametrize("marketplace", _MARKETPLACES)
def test_marketplace_files_carry_no_version(marketplace: str) -> None:
    # Marketplace files omit a version on purpose so they can't silently become
    # a new drift surface. If a version is ever added, route it through
    # scripts/bump_version.py and extend this guard.
    data = json.loads((_ROOT / marketplace).read_text(encoding="utf-8"))
    assert "version" not in data, (
        f"{marketplace} unexpectedly has a top-level 'version'; add it to "
        "scripts/bump_version.py and this check before shipping."
    )
    for entry in data.get("plugins", []):
        assert "version" not in entry, (
            f"{marketplace} plugin entry {entry.get('name')!r} has a 'version'; "
            "route it through scripts/bump_version.py."
        )
