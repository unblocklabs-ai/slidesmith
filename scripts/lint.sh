#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

.venv/bin/ruff check .
.venv/bin/vulture src/slidesmith tests docs/review tooling/vulture_whitelist.py
