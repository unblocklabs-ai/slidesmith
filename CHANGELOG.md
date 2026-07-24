# Changelog

All notable changes to slidesmith are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this
project uses [Semantic Versioning](https://semver.org/) (pre-1.0: minor =
features, patch = fixes). Each release maps to an annotated `stage-*` git tag.

**Maintaining this file:** add every user-facing change under `[Unreleased]` in
the appropriate group (Added / Changed / Fixed / Removed) in the same PR that
makes the change. On release, rename `[Unreleased]` to the new version + date,
tag the commit `stage-N-<name>`, and open a fresh `[Unreleased]`. Keep entries
agent-legible: name the command/flag and what an operator can now do.

## [Unreleased]

_Nothing yet._

## [0.9.1] — 2026-07-24 — Sequence-aware style planning

Closes the last known silent-corruption class in the request planner,
root-caused from the R2 dogfood's uninvestigated "heading went black"
report: a text edit combined with cross-scope style changes in one push
could emit an empty style reset that reverted intended properties to
Google defaults. One build and three adversarial review rounds surfaced
seven blockers in this family; all are fixed with verbatim repro
regressions. Offline-verified only — the first live mixed text-edit +
scope-move push should be treated as this release's field validation.
Tag `stage-24`. 1008 tests.

### Fixed

- The effective-range planner now follows text edits into post-edit UTF-16
  index space (surrogate-pair safe) instead of bailing to the legacy
  ALL-range + empty-reset request sequence that caused the corruption.
- Style inheritance is assumed in exactly one provable case (same-paragraph
  insertion after a character that survives the batch un-restyled with
  identical effective style, evaluated in batch order). All other inserted
  or replacement text receives explicit fixed-range style updates and
  scoped inherit resets; effective ranges straddling inserted and surviving
  text are split. Unsound paragraph-marker anchors are removed.
- Every batch-created paragraph — including empty paragraphs and the
  right-hand halves of splits — receives explicit paragraph-style planning
  instead of relying on neighbor inheritance.
- `push`/`diff` now fail closed with an actionable error when a mixed
  text-edit + cross-scope styling change cannot be planned safely, rather
  than emitting a possibly-corrupting request sequence.
- A request-plan invariant rejects any broad-range style update followed by
  an empty fixed-range reset whose intended effective property is non-null.

## [0.9.0] — 2026-07-23 — Proof receipts & planner integrity

Driven by the third live dogfood round, run against released 0.8.0. Push
gains a machine-readable proof receipt (the dogfooder's top ask), the text
planner can no longer let Google silently clear `bold` on weight edits,
and the last field-observed false persistence-warning classes are
suppressed under strict guards — while a normalization candidate that
cannot yet be proven safe deliberately keeps warning. Every suppression
and planner change carries verbatim field payloads as regressions. Tag
`stage-23`. 978 tests.

### Added

- `slidesmith push --json` now emits a machine-readable receipt with the
  presentation ID, revisions, request/change counts, persistence verification
  and warnings, and elapsed time. Render and QA results are future scope for
  the receipt.

### Changed

- `create --dir` now creates missing parent directories before authentication
  or the remote create call.
- `replace-image` help and agent documentation now make its immediate,
  revision-locked remote mutation explicit; it is not staged by `diff`/`push`.

### Fixed

- Persistence verification suppresses Google's created-`RoundRect`
  `text-align-center` default only under the guarded create/type/authoring
  conditions, while unchanged-text `TEXT_UPDATE` checks still verify all
  intended paragraph properties before applying text-style-only comparison.
- Text-style planning pins a known effective `bold` value alongside both
  `weightedFontFamily` updates and resets, including explicit `bold: false`
  removals.
- `push --json` preserves warning parity and emits a structured partial
  per-slide receipt before re-raising a mid-run failure; `--force --json` reads
  the live revision once so `revision_before` is not stale.

### Notes

- `bold` combined with `font-weight-400` remains fail-closed: its possible
  normalization to `font-weight-700` is not suppressed because rendered
  equivalence is unproven.

## [0.8.0] — 2026-07-23 — Agent-native authoring & trust

The full seven-phase campaign from ranked agent dogfood feedback, plus a
second live-dogfood-driven hardening round. Decks can now be created and
shared without leaving slidesmith; pulled SML document order is guaranteed
to match paint order; `fit="cover"` lands with a live-validated derive path
for new remote images; QA text measurement is paragraph-, inset-, and
autofit-aware with a field-calibrated vertical inset; the new `advise` +
`group` commands surface actionable pattern suggestions; and the request
planner now resolves effective per-range styling so scope moves can never
emit default-selecting resets (the "heading turned black" class of bug).
Persistence verification stays strict while dropping every false-warning
class observed in the field. Tag `stage-22`. 927 tests.

### Added

- Added `slidesmith create --title ... [--share ...]` to create a Google Slides
  deck, materialize its pristine local workspace, and optionally share the
  app-created deck with comma-separated recipients.
- Added offline `slidesmith advise` pattern suggestions for pseudo-groups,
  buried elements, Stack candidates, and near-overflow text, plus the
  revision-locked `slidesmith group` command for native grouping of selected
  top-level siblings. Pseudo-group suggestions now require repeated similar
  clusters and expand their hints to the full selectable API object set.

### Fixed

- Geometry QA now measures paragraphs and runs independently, honors authored
  leading/paragraph spacing and Google text insets, consumes captured text
  autofit, and avoids false `TEXT_OVERFLOW` findings on mixed-size cards.
- Geometry overlap QA now compares alignment-aware paragraph ink against all
  non-text siblings, while preserving conservative text-vs-text checks and
  existing background/containment exemptions.
- Fractional paragraph line-spacing values such as `leading-88.421` now
  round-trip through pull-generated SML and its parser.
- Local cover derivation now applies EXIF orientation, rejects animated sources,
  and resamples odd-dimension crops to an exact target-aspect raster. Derived
  rasters use a versioned key and a bounded rational canvas (maximum 4096px per
  dimension and 16,777,216 pixels). Cover persistence checks require refreshed
  CENTER_CROP offsets, allowing at most 2.5e-4 opposing-offset asymmetry;
  aspect-matched local derived creates remain exempt.
- New remote `fit="cover"` images now download through the guarded 20 MB fetch
  path, derive an aspect-matched cached Pillow asset keyed by URL and content
  hash, and upload it before emitting a plain `createImage`; the retired
  create/`CENTER_CROP`/geometry-pin sequence is no longer used for new images.
- Workspace-folder CLI errors now name the attempted path, identify missing
  `presentation.json` or `.pristine/presentation.zip`, and explain that the
  argument must be the workspace directory created by `pull` or `create`.
- Post-push persistence verification now treats element, paragraph, and run
  text styles with identical effective per-span values as equivalent, including
  harmless run re-segmentation; authored drops and value changes still warn.
  The intentional exception is a redundant class removal whose effective value
  remains inherited identically from another scope, which is suppressed as
  scope-ownership noise.
- Foreground-color scope moves now compare resolved UTF-16 spans before
  request emission: unchanged effective values produce no request, while real
  changes use explicit fixed-range values instead of default-dependent resets.
- Text and paragraph scope moves now use the same effective UTF-16 span planner
  for every supported property, including font size, weight, bold, and leading;
  only genuine effective removals emit empty field-mask resets.
- Newly created elements now suppress only Google's fill/stroke/stroke-weight
  normalization additions when authored paint, dash, and state otherwise
  match; existing elements and authored removals remain warnings.
- QA and advisor text measurement now use 7.2pt horizontal and calibrated
  3.6pt vertical default insets; captured per-element inset overrides remain
  authoritative.
- Advisor near-overflow suggestions now cover the full 90%-to-105% band before
  the QA overflow threshold, including the exact tolerance boundary.

### Changed

- Geometry QA's measurement model uses a 2% residual measurement margin and a
  5% overflow decision tolerance after paragraph-aware inputs are resolved;
  pending text/run-size edits deactivate captured autofit, and the finding rule
  names and stable slide-identity keys are unchanged.
- Pull-generated visual-containment nesting is now z-order-consistent: only
  contiguous paint-order runs are nested, and SML depth-first document order
  is guaranteed to match Google's back-to-front paint order. Existing
  workspaces migrate naturally on their next pull.
- `slidesmith pull` accepts `--dir` as an alias for `-o/--output-dir`, matching
  the existing `create --dir` spelling.
- `slidesmith check` warns when local edits are pending before downloading
  thumbnails, making clear that the thumbnails/contact sheet show the remote
  deck and advising `slidesmith push` to sync first.

## [0.7.0] — 2026-07-22 — Plugin installs & keyring-native gog auth

Ships slidesmith as an installable agent plugin for three harnesses, and makes
zero-config auth work with newer `gog` releases that keep the OAuth client
secret in the OS keyring instead of `credentials.json` (reported by an agent
whose fresh gog install could not authenticate). Auth doctor gains actionable
verdicts for keyring-unreadable and ambiguous-client states, hardened through
three adversarial review rounds. Tag `stage-21`. 720 tests.

### Added
- Installable as an agent plugin: thin manifests for **Claude Code**
  (`.claude-plugin/`) and **Codex** (`.codex-plugin/` + `.agents/plugins/`),
  a root **`AGENTS.md`**, and the packaged skill made publishable to
  **OpenClaw**'s ClawHub (publish is a maintainer step). One-command install
  per harness is documented in [docs/PLUGINS.md](docs/PLUGINS.md).

### Fixed
- gogcli OAuth client discovery now supports newer gog releases that store
  only the client ID in `credentials.json` and the client secret in the OS
  keyring (service `gogcli`, `GOG_KEYRING_SERVICE_NAME` honored), resolves
  gog's data/config directories with the real precedence
  (`GOG_DATA_DIR`/`GOG_CONFIG_DIR` > `GOG_HOME` > XDG > platform defaults),
  and dedupes named clients by normalized name with data-dir precedence;
  legacy full-JSON `credentials.json` files keep working.
- `slidesmith auth doctor` no longer reports `CREDENTIAL ABSENT` for
  keyring-backed gog installs: new `GOGCLI CLIENT SECRET UNREADABLE` verdict
  with macOS Keychain "Always Allow" guidance and fallbacks, and
  `GOGCLI CLIENT AMBIGUOUS` listing conflicting named client files. Failure
  verdicts yield to any runtime-usable auth path; session-profile inspection
  now matches runtime selection (gateway → `default`, OAuth client →
  `<source>-default`), and a cached session alone no longer reports `READY`
  when the runtime would refuse to start.

## [0.6.1] — 2026-07-21 — Created-element persistence fidelity

A follow-up patch from continued dogfooding. Post-push persistence verification
no longer false-warns on newly created elements when Google stamps a default
vertical alignment the author left implicit; genuine dropped or changed authored
styles still warn. Tag `stage-20`. 691 tests.

### Fixed

- Created elements no longer false-warn when Google stamps a default alignment
  the author did not set; genuine drops and changes still warn during
  post-push persistence verification.

## [0.6.0] — 2026-07-21 — First-class slides & QA fidelity

Driven by a third round of dogfood feedback (5 items) from continued
stress-testing, converged through a three-lens adversarial holistic review.
Makes new-slide creation a first-class positioned command with round-tripping
slide IDs, keys QA findings to stable slide identity so adding a slide no longer
mislabels untouched findings, silences a near-degenerate `LINE` persistence
false-warning, and detects expired bare tokens up front with gog-specific
recovery. Tag `stage-19`. 686 tests.

### Fixed

- Post-push persistence verification no longer warns for an unrepresentable
  near-zero thickness axis on a `LINE`; along-line geometry and translation
  drift still warn.
- `slidesmith check` now keys QA findings and acceptances by stable slide clean
  IDs when present, so identified untouched findings remain `PRE-EXISTING` when
  earlier slides are inserted or deleted; freshly authored id-less slides and
  pre-slide-ID baselines use positional fallback until a pull assigns an ID.
- `add-slide` now validates `--after`/`--at` against original pulled slides
  even when other pending scaffolds exist, shifts positioned inserts once in
  request order, and sizes the title/body starter to the deck page.
- Authored `add-slide` slide IDs now become the Google `createSlide` object ID
  and survive push/refresh round-trips; an occupied object ID gets a safe
  suffix.
- Title/body starter font sizes now scale with the deck page, so an untouched
  scaffold does not self-trigger `TEXT_OVERFLOW` on smaller realistic decks.

### Added

- **`slidesmith add-slide`** now scaffolds blank or title/body slides locally
  with `--after`/`--at` deck positioning and preserves append-at-end behavior by
  default.
- Bare `GOG_ACCESS_TOKEN` and `GOOGLE_WORKSPACE_CLI_TOKEN` commands now perform
  a best-effort startup tokeninfo check. Invalid or expired tokens fail before
  deck work with gog refresh-and-re-export guidance; valid responses record
  lifetime for near-expiry warnings.

### Changed

- Bare-token 401s and `auth doctor` now report the gog throwaway-request and
  re-export path; per-slide push 401s additionally include `--resume`
  recovery guidance. OAuth and service-account 401 guidance is unchanged.
- Documentation now clarifies that relative local image paths resolve from the
  deck root rather than the individual `slides/NN/` folder.

## [0.5.0] — 2026-07-21 — Continuity & QA signal

Driven by two more rounds of ranked dogfood feedback (14 items) from an agent
running a 300+ request stress test on a real deck, then converged through a
four-lens adversarial holistic review. Closes the documentation, auth-continuity,
and QA signal-to-noise gaps the stress test exposed, adds selector-based z-order
and in-place image-source editing, and hardens local-image handling. Tag
`stage-18`. 634 tests.

### Added
- Browser OAuth login now preserves a successful access-token-only session when
  Google withholds a refresh token, reports its roughly one-hour lifetime, and
  gives the revoke-at-permissions or own-OAuth-client remedy. `auth doctor`
  identifies this usable-but-expiring state.
- **`slidesmith reorder`** sends revision-locked Google z-order requests for
  selector matches, groups multi-slide selections into one request per slide,
  supports `bring-to-front`, `bring-forward`, `send-backward`, and
  `send-to-back`, and refreshes the local SML projection after the write.

### Changed
- Push and `replace-image` diagnostics now carry `WARNING` versus `NOTICE`
  severity, render notices after actionable warnings, and summarize mixed
  counts in the CLI.
- Pull and post-push refresh materializations reuse existing ID mappings, and
  regenerated sibling SML now follows Google's back-to-front page-element
  order.
- Documentation now includes the new-slide workflow, Group authoring guardrails,
  z-order and QA-acceptance recipes, and quick class-vocabulary pointers.
- `slidesmith check` now gives large, short titles one estimated line of
  measurement uncertainty, reducing false-positive `TEXT_OVERFLOW` warnings
  while still flagging clearly overflowing body text.
- `slidesmith check` no longer reports overlaps involving leaves covering at
  least 90% of the actual slide area, and treats 95%-contained siblings as
  containment; intentional remaining overlaps can be suppressed with the
  discoverable `qa-accept-overlap` class.

### Fixed
- Color opacity classes with values above `/100` are now rejected.
- **Security:** local image sources are now constrained to the presentation
  workspace before inspection or Drive upload, and credential-bearing image URLs
  are redacted from summaries, fetch notices, and persistence warnings.
- `snippet paste` now rejects Group subtrees early with an actionable message;
  paste the children individually or use the supported pulled-group copy path.
- New authored `<Group>` elements now fail loudly with an actionable API
  limitation message; pulled and copied groups remain supported.
- **`slidesmith --version`** now prints the package version without requiring a
  subcommand.
- Authored `<Image src="…">` elements can now use zero or negative `x`/`y`
  origins for full-bleed and off-canvas placement; `w`/`h` remain finite and
  strictly positive.
- Authored `<Line>` elements with negative `w`/`h` are canonicalized before
  `diff` and `push`, preserving the segment for horizontal/vertical lines and
  lines with both negative axes. A diagonal with exactly one negative axis
  cannot preserve its direction because SML pulls expose only positive bounds.
- New authored `fit="stretch"` images now pin their visual box to the exact
  authored geometry, including source aspect ratios that differ from the box.
- Setting a new `src` (optionally with `fit`) on an existing pulled `<Image>`
  now emits a visible image replacement with the same geometry pinning and
  local-asset cache reuse as `replace-image`.
- Image replacement persistence verification now compares refreshed `sourceUrl`
  when Google returns it and warns when the replacement did not persist.
- Push-time remote `fit="stretch"` dimension-fetch failures now fall back to
  deterministic target-shaped geometry and return a NOTICE about a possible
  follow-up resize.
- Persistence verification now recognizes Google `font-weight-700` and
  `font-family-arial` additions, reports harmless defaults on existing edited
  elements as notices, and keeps authored font-family, weight, and class drops
  as warnings.
- Long-running pushes now proactively refresh expiring OAuth/service-account
  credentials and recover one expired-token 401 across GET and `batchUpdate`;
  bare-token failures include fresh-token and `--resume` guidance.

## [0.4.0] — 2026-07-20 — Design-agent roadmap

The release that turns slidesmith from a low-level editor into a design-agent
tool. Driven by ranked feedback from an agent that used it on a 24-slide deck.
Tags `stage-13` … `stage-15`. 486 tests.

### Added
- **Semantic selectors + roles** (`select`, `apply`). A real query language over
  the element tree — `tag=`, `role=`, `slide in 4..24`, geometry bands
  (`w>300`), and text/id match operators `text=` (exact), `text^=` (starts-with),
  `text$=` (ends-with), `text~=` (substring); `id=` vs `id~=`. Combine with
  `AND`/`OR`/parens. `apply --set-role` assigns round-tripping semantic roles
  (stored in `roles.json`, never sent to Google) so a whole-deck restyle is one
  command. Atomic + conflict-validated.
- **Reusable components** (`<Use component=".." .../>` + `components.sml`).
  Define a card once with `{{slots}}`, instantiate many; expands at compile time.
  `components` lists them, `components --show <name>` prints body + slots.
  Expanded instances are individually selectable by id after re-pull.
- **Per-slide resumable push** (`push --per-slide`, `--resume`, `--preflight`).
  One batch per slide with live progress and a content-hashed resume ledger;
  `--preflight=warn|block` runs geometry QA before pushing.
- **Local image authoring** (`<Image src="./file.png">`, `replace-image`).
  Local files upload to Drive and cache (`.assets.json`); `fit=contain` sizes to
  true aspect ratio, top-left pinned on both create and replace.
- **Cross-deck style transfer** (`theme extract`/`theme apply`, `snippet
  copy`/`paste`). Extract palette/type/role tokens from slides, apply to others
  with `--map-colors` (nearest-palette snap, style-only); move relative-positioned
  SML subtrees across decks.
- `diff --slide N` scopes a diff to one slide; `theme apply --dry-run --verbose`
  gives a per-element preview.

### Fixed
- Image geometry contract: create and `replace-image` no longer let Google
  silently re-center/resize; effective contained geometry is used consistently by
  lint, preflight, and persistence verification.
- Selector over-matching (added exact/anchored operators) and grammar
  discoverability (`select --help` shows the grammar).
- Per-slide push mis-partitioned new slides at the 100-slide boundary (string vs
  numeric sort); createSlide requests are now mapped by parsing the slide index
  from the request objectId.
- Persistence verification no longer false-alarms on Google's auto-injected
  default classes for newly created elements.

## [0.3.0] — 2026-07-20 — Dogfood-driven features & media

Tags `stage-10` … `stage-12`. Shipped from live agent dogfooding plus a
GPT-driven review loop.

### Added
- `replace-class --swap OLD=NEW` (atomic multi-swap maps) for bulk restyles.
- `check --contact-sheet` (one composited review image); `check --accept` /
  `--unaccept` + `qa-accept-*` inline classes with a stable-identity
  `accepted.json` ledger (NEW / PRE-EXISTING / RESOLVED).
- `slidesmith fmt` — whitespace-safe canonical SML formatting (pretty-printed
  files diff to zero).
- Push persistence verification — warns when Google silently drops or normalizes
  a property, showing sent-vs-remote values.
- `<Image src="https://…">` authoring with `fit=stretch|contain`, SSRF-guarded
  bounded fetch.

### Fixed
- Run-level font-family changes silently reset to Arial (empty-payload
  `fontFamily` vs `weightedFontFamily` seam).
- SSRF hardening, bounded image downloads, CREATE persistence blind spot,
  `replace-class` exact-attribute matching, image positive-geometry validation
  (found by a GPT review loop, converged after adversarial verification).

## [0.2.0] — 2026-07-20 — Hardening & consolidation

Tags `stage-7` … `stage-9`.

### Changed
- Consolidated the two vendored packages into a single `slidesmith.engine`;
  retired the `extraslide` name (MIT attribution retained in `NOTICE`).
- Split the request-builder monolith into focused modules; extracted an `auth/`
  package, `conflicts.py`, and a shared `shape_types` registry.

### Fixed
- Converged a six-round adversarial review loop (110 findings) covering two
  silent-corruption CRITICALs (lossy text-edit ranges; MOVE scale reset), group
  move/copy correctness, and a broad dead-code/duplication sweep.
- Added `scripts/lint.sh` (ruff + vulture) as a standing dead-code gate.

## [0.1.0] — 2026-07-19 — Foundation

Tags `stage-1` … `stage-6`. The living-deck co-editing core.

### Added
- Pull a deck to editable SML with Tailwind-style classes; field-masked `diff`;
  conflict-guarded, revision-locked in-place `push` with post-push refresh.
- Styled round-trip (fills, strokes, text runs, paragraphs, content alignment);
  inherited theme styling never baked in; no-edit pull diffs to zero.
- One-shot layout authoring: `Stack`, `Grid`, `flex`, `h="auto"`.
- Offline + thumbnail QA via `check`; authored element IDs survive round-trips.
- `auth doctor` credential diagnosis; dual-store sessions for subprocess agents.

Descends from think41/extrasuite's extraslide (MIT — see `NOTICE`), heavily
rewritten.

[Unreleased]: https://github.com/unblocklabs-ai/slidesmith/compare/stage-23-proof-receipts...HEAD
[0.9.0]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-23-proof-receipts
[0.8.0]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-22-agent-native-authoring
[0.7.0]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-21-plugins-and-keyring-auth
[0.6.1]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-20-created-element-persistence
[0.6.0]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-19-first-class-slides
[0.5.0]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-18-continuity-and-qa-signal
[0.4.0]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-15-roadmap-review-converged
[0.3.0]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-12-media-and-gpt-review
[0.2.0]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-9-one-package
[0.1.0]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-6-visual-qa
