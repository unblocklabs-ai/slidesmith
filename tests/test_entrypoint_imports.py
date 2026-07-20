"""Import-order regression guard.

The test suite imports modules in collection order, which can mask circular
imports that only bite on the CLI entry path. Each case runs in a fresh
interpreter so every import order is exercised from cold.
"""

import subprocess
import sys

import pytest

ORDERS = [
    "import slidesmith.cli",
    "import slidesmith.workspace; import extraslide.client",
    "import extraslide.client; import slidesmith.workspace",
    "import slidesmith.credentials",
    "import extraslide",
]


@pytest.mark.parametrize("stmt", ORDERS)
def test_cold_import_order(stmt: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", stmt], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"{stmt!r} failed:\n{proc.stderr}"
