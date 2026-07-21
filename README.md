# slidesmith

Agent + human co-editing for Google Slides. Pull a deck into local SML files,
edit them (you or your agent), preview the exact `batchUpdate` diff, and push
it back to the **same deck in place** — edits show up in Drive version history
like any collaborator's.

slidesmith descends from think41/extrasuite's extraslide (MIT, heavily rewritten
— see NOTICE). Its release bugs are fixed and its unfinished layers are being
rewritten per [DESIGN.md](DESIGN.md).

## Quickstart

```bash
uv venv && uv pip install -e ".[dev]"

slidesmith pull "https://docs.google.com/presentation/d/<ID>/edit"
# edit <ID>/slides/01/content.sml ...
slidesmith diff <ID>     # prints the batchUpdate requests, no API call
slidesmith push <ID>     # applies them to the same deck
```

Use `push --preflight=warn` to report new offline geometry findings and proceed,
or `push --preflight=block` to abort before authentication when an edit adds a
finding relative to the pull-time baseline. The default is `off`; the option
also works with `--per-slide`.

Author images from public URLs or workspace-local files. Local files are
uploaded at push time to the authenticated user's own Google Drive, made
link-readable so Slides can fetch them, and reused through `<ID>/.assets.json`
when the canonical path and SHA-256 content hash match:

```xml
<Image id="company_logo" src="./assets/logo.png"
       x="48" y="36" w="144" h="72" fit="contain" />
```

Google Slides cannot accept raw image bytes for `createImage` or `replaceImage`;
both require a publicly fetchable URL. For the normal edit loop, set a new
`src` (optionally with `fit`) on an existing image in SML, then run `diff` and
`push`; Slidesmith emits an `IMAGE_UPDATE` with `replaceImage` and pins the
authored geometry. A `fit` change requires a `src` — pulled images carry
neither attribute until you author one. For a one-shot pixel swap with a clean
local diff, use:

```bash
slidesmith replace-image <ID> company_logo ./assets/new-logo.png
```

`replace-image` remains a clean-diff-only command; it refuses pending edits
before its authoritative post-write refresh can overwrite them.

`fit="stretch"` and `fit="contain"` are supported. `fit="cover"` remains
unsupported because Slides exposes `cropProperties` as read-only.

The default push is one atomic, deck-wide `batchUpdate`. For large decks, use
`slidesmith push <ID> --per-slide` to send one revision-locked batch per
changed slide with progress and a `.push-progress.json` resume ledger. This
mode is resumable with `--per-slide --resume`, but it is intentionally **not
atomic across the whole deck**: if slide 12 fails, earlier slide batches have
already committed and the command stops at slide 12.

For local deck-wide restyles, use `slidesmith replace-class <ID> OLD NEW`, or
repeat `--swap OLD=NEW` to apply several replacements together:

```bash
slidesmith replace-class <ID> --swap font-family-arial=font-family-inter \
  --swap text-color-#333333=text-color-#111111 --dry-run
```

Positional and `--swap` forms can be combined. All swaps are validated as one
atomic change against the real SML class parser, with per-swap and per-slide
counts; `--dry-run` performs the same work without writing. Review the result
with `diff` and push separately.

Transfer a design language from representative slides into another local deck:

```bash
slidesmith theme extract source-deck --from-slides 1-3 -o theme.json
slidesmith theme apply target-deck theme.json --to-slides 4-24 --map-colors --dry-run --verbose
slidesmith theme apply target-deck theme.json --to-slides 4-24 --map-colors
```

`theme.json` contains reusable palette/type tokens, frequency inventories, and
canonical element classes for each assigned semantic role. Apply replaces the
style classes of target elements whose role exists in that map, unifies text to
the extracted primary font family, and optionally maps only nearby RGB colors
to the theme palette. It is an atomic, conflict-validated, style-only operation:
text and geometry are never changed. Role-aware restyling requires `roles.json`;
without roles, font unification and `--map-colors` still work.
Verbose dry-run output lists each element's class/color changes and explains
when an off-theme color is kept because its nearest palette color is beyond the
mapping threshold.

For reusable visual structure, copy a single-slide selection into an
origin-relative snippet and paste it as new elements in another deck:

```bash
slidesmith snippet copy source-deck 'slide=2 AND id~=hero' -o hero.sml
slidesmith snippet paste target-deck --slide 5 hero.sml \
  --frame 36,48,648,300 --map title:headline --map body:summary --dry-run
```

Each `SNIPPET_ROLE:DESTINATION_ROLE` mapping copies text from the one matching
destination-slide role into the snippet slot while retaining the snippet's
visual style. Paste never deletes or rearranges existing elements; it inserts
new shapes with collision-free IDs. The operator chooses content mappings and
the target frame—v1 does not infer them or clone an entire slide automatically.

After pushing, `slidesmith check <ID> --contact-sheet` downloads the slide PNGs
and combines them into a labeled two-column image at
`<ID>/.qa/contact-sheet.png`. Contact sheets require thumbnail downloads, so
they cannot be combined with `--no-thumbnails`.

Auth is zero-config if you already use gogcli: slidesmith reads the OAuth
client from `~/Library/Application Support/gogcli/credentials.json` (or
`GOG_ACCESS_TOKEN` / `GOOGLE_WORKSPACE_CLI_TOKEN` env vars, or a service
account via `SERVICE_ACCOUNT_PATH`). Local image uploads use the already
requested `drive.file` scope; uploaded, link-readable assets remain in the
user's Drive. If Google withholds a refresh token after browser consent,
Slidesmith keeps the access-token session for about one hour and tells you to
revoke the app at `myaccount.google.com/permissions` or configure your own
OAuth client. `auth doctor` labels that session as usable-but-expiring.

OAuth and service-account credentials are refreshed proactively during pushes
and once reactively after a 401. Bare environment-token mode performs one
startup validation against Google's fixed HTTPS tokeninfo endpoint before an
API-bound command. An invalid or already-expired token fails before any deck
work with gog-specific recovery: run a throwaway `gog` API request to force a
refresh, re-export `GOG_ACCESS_TOKEN`, and retry. A reachable valid response
also records the remaining lifetime so near-expiry runs can warn; bare tokens
still cannot be refreshed by Slidesmith. If tokeninfo is unreachable, the
command proceeds with the previous unknown-expiry behavior. Retry a failed
`--per-slide` push with `--resume` after re-exporting a fresh token.

## For agents

Start with the packaged skill: [skills/slidesmith/SKILL.md](skills/slidesmith/SKILL.md)
(mental model + command surface) and [skills/slidesmith/recipes.md](skills/slidesmith/recipes.md)
(copy-paste task recipes). For the exhaustive reference — full class vocabulary,
selector grammar, one-shot layout, QA judgment, auth recovery — read
[docs/AGENT-GUIDE.md](docs/AGENT-GUIDE.md). See [CHANGELOG.md](CHANGELOG.md) for
what shipped in each release.

## Features

- **Styled round-trip**: pull any deck to readable SML with Tailwind-style
  classes for fills, strokes, text, paragraphs, and content alignment —
  inherited theme styling is never baked in, and a no-edit pull always diffs
  to zero requests.
- **Surgical diffs**: minimal UTF-16-correct text range edits, field-masked
  style updates, and `diff --summary` for a compact review (plain `diff`
  keeps exact request JSON).
- **Safe concurrent pushes**: three-way conflict guard against the live deck,
  `writeControl` revision locking, atomic deck-wide batches by default, an
  opt-in resumable per-slide multi-batch mode, post-push workspace refresh,
  optional `off|warn|block` offline geometry preflight, and **push persistence
  verification** — a warning whenever Google meaningfully drops a property you
  sent. Sub-0.02pt geometry drift and Google's default text-layout classes on
  newly created elements are documented normalization exceptions.
- **Layout authoring**: one-shot `Stack`/`Grid` containers, `flex`,
  `h="auto"` text height, reusable `components.sml` + `<Use>` expansion, and
  `content-align-*` — the compiler does the coordinate math.
- **Image authoring**: public URL or local-file `Image` creation and normal SML
  `src`/`fit` edits with stretch or contain fitting, Drive upload reuse caching,
  and explicit contain/stretch geometry pinning for both image-edit paths;
  cover/crop stays unsupported.
- **Visual QA**: `check` downloads rendered slide PNGs, optionally creates a
  labeled two-column contact sheet with `--contact-sheet`, and runs geometry lint
  (overlap, out-of-bounds, likely text overflow) with a NEW / PRE-EXISTING /
  RESOLVED ledger keyed to your last pull.
- **Bulk restyles**: `replace-class` swaps one or more validated classes
  deck-wide as a single atomic operation.
- **Cross-deck style transfer**: `theme extract/apply` moves palette, type, and
  role styles safely; `snippet copy/paste` reuses bounded visual structures with
  explicit role-to-content mapping.
- **Semantic selectors**: local-only `select` / atomic `apply` target elements
  by role, tag, class, ID, text, slide, and geometry without ID-level scripting;
  text supports exact/prefix/suffix/substring operators and IDs support exact
  or substring matching. Both command help pages include the full grammar.
- **Agent-grade errors and auth**: loud, named errors for unknown or
  conflicting classes; dual-store sessions (Keychain + 0600 file) so
  subprocess agents can authenticate; `auth doctor` for self-rescue.
- Authored element IDs survive push/pull round trips when they meet Google's
  5–50-character object-ID grammar, are unoccupied, and do not resemble a
  generated Google/Slidesmith ID (`eNN`, `gNN`, `pNN`, `SLIDES_API…`); other
  names are sanitized and/or suffixed with a generated ID.

## Status

Production-hardened through six adversarial review rounds (110 findings fixed
— see `docs/review/FINDINGS.md`) and four live dogfood campaigns in which
agent designers built new slides, executed deck-wide restyles, and shipped
freeform polish on a real presentation. The complete pytest suite and
`scripts/lint.sh` are clean.
See the [agent guide](docs/AGENT-GUIDE.md) for the supported class vocabulary
and the complete edit/diff/push/check loop.

```bash
.venv/bin/pytest -q
```

Run `scripts/lint.sh` to check Pyflakes/syntax errors with Ruff and intentional
dead-code exceptions with Vulture.
