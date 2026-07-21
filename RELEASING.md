# Releasing slidesmith

Every release maps to an annotated `stage-N-<slug>` git tag and a GitHub
Release. `pyproject.toml` is the single source of truth for the version; the
static plugin manifests must match it, and
`tests/test_version_consistency.py` fails `pytest` if they drift.

Pre-1.0 SemVer: **minor = features, patch = fixes.**

## Version surfaces

- **Auto-derived (never hand-edit):** the CLI `__version__`
  (`src/slidesmith/__init__.py`, from `importlib.metadata`), its engine
  re-export, and `slidesmith --version`.
- **Static literals (bump together — use the script):** `pyproject.toml`
  `[project].version`, `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`.
- **No version field:** the two marketplace files, `README.md`, `AGENTS.md`,
  and `skills/slidesmith/SKILL.md`.

## Release steps

1. Ensure `CHANGELOG.md` `[Unreleased]` lists every user-facing change.
2. Pick `X.Y.Z`, the next stage number `N`, and a short `<slug>`/`<Name>`.
3. **Bump every static surface at once:**
   ```bash
   python scripts/bump_version.py X.Y.Z
   ```
4. Finalize `CHANGELOG.md`:
   - Rename `## [Unreleased]` → `## [X.Y.Z] — YYYY-MM-DD — <Name>`.
   - Add the summary paragraph, ending `Tag \`stage-N\`. <count> tests.`
   - Open a fresh `## [Unreleased]` with `### Added` (or `_Nothing yet._`).
   - At the bottom, point the `[Unreleased]` compare link at
     `stage-N-<slug>...HEAD` and add
     `[X.Y.Z]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-N-<slug>`.
5. Verify: `.venv/bin/pytest -q` (the version-consistency test must pass) and
   `./scripts/lint.sh`.
6. Refresh the editable install so `--version` reflects the bump, and confirm:
   ```bash
   uv pip install -e . -q && .venv/bin/slidesmith --version
   ```
7. Commit: `git commit -am "release: X.Y.Z — <Name> (stage-N)"`.
8. Tag (annotation title matches existing tags):
   ```bash
   git tag -a stage-N-<slug> -m "X.Y.Z — <Name>" -m "<summary>"
   ```
9. Push: `git push origin master && git push origin stage-N-<slug>`.
10. GitHub Release from the CHANGELOG section:
    ```bash
    gh release create stage-N-<slug> --title "X.Y.Z — <Name>" --notes-file <notes>
    ```

## After a release

- The plugin manifests are now at `X.Y.Z`; Claude Code only ships an update to
  installed users when that literal changes, so the bump in step 3 is what makes
  the new version reach them.
- OpenClaw: re-publish the skill to ClawHub if it changed (see
  [docs/PLUGINS.md](docs/PLUGINS.md)).
