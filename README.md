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

Author images from public URLs or workspace-local files. Local files are
uploaded at push time to the authenticated user's own Google Drive, made
link-readable so Slides can fetch them, and reused through `<ID>/.assets.json`
when the canonical path and SHA-256 content hash match:

```xml
<Image id="company_logo" src="./assets/logo.png"
       x="48" y="36" w="144" h="72" fit="contain" />
```

Google Slides cannot accept raw image bytes for `createImage` or `replaceImage`;
both require a publicly fetchable URL. Replace the content of an existing image
without changing its position or size with:

```bash
slidesmith replace-image <ID> company_logo ./assets/new-logo.png
```

Run `replace-image` only with a clean local SML diff; the command refuses
pending edits before its authoritative post-write refresh can overwrite them.

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
slidesmith theme apply target-deck theme.json --to-slides 4-24 --map-colors --dry-run
slidesmith theme apply target-deck theme.json --to-slides 4-24 --map-colors
```

`theme.json` contains reusable palette/type tokens, frequency inventories, and
canonical element classes for each assigned semantic role. Apply replaces the
style classes of target elements whose role exists in that map, unifies text to
the extracted primary font family, and optionally maps only nearby RGB colors
to the theme palette. It is an atomic, conflict-validated, style-only operation:
text and geometry are never changed. Role-aware restyling requires `roles.json`;
without roles, font unification and `--map-colors` still work.

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
user's Drive.

## For agents

Read [docs/AGENT-GUIDE.md](docs/AGENT-GUIDE.md) before editing a deck. It is the
source-derived reference for the pull/diff/check/push loop, accepted SML class
vocabulary, one-shot Stack/Grid layout, QA judgment, and auth recovery.

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
  and **push persistence verification** — a warning whenever Google silently
  drops or normalizes a property you sent.
- **Layout authoring**: one-shot `Stack`/`Grid` containers, `flex`,
  `h="auto"` text height, reusable `components.sml` + `<Use>` expansion, and
  `content-align-*` — the compiler does the coordinate math.
- **Image authoring**: public URL or local-file `Image` creation with stretch or
  contain fitting, Drive upload reuse caching, and geometry-preserving
  `replace-image`; cover/crop stays explicitly unsupported.
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
  by role, tag, class, ID, text, slide, and geometry without ID-level scripting.
- **Agent-grade errors and auth**: loud, named errors for unknown or
  conflicting classes; dual-store sessions (Keychain + 0600 file) so
  subprocess agents can authenticate; `auth doctor` for self-rescue.
- Authored element IDs survive push/pull round trips.

## Status

Production-hardened through six adversarial review rounds (110 findings fixed
— see `docs/review/FINDINGS.md`) and three live dogfood campaigns in which
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
