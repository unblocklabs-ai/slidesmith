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

[Unreleased]: https://github.com/unblocklabs-ai/slidesmith/compare/stage-19-first-class-slides...HEAD
[0.6.0]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-19-first-class-slides
[0.5.0]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-18-continuity-and-qa-signal
[0.4.0]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-15-roadmap-review-converged
[0.3.0]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-12-media-and-gpt-review
[0.2.0]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-9-one-package
[0.1.0]: https://github.com/unblocklabs-ai/slidesmith/releases/tag/stage-6-visual-qa
