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

After pushing, `slidesmith check <ID> --contact-sheet` downloads the slide PNGs
and combines them into a labeled two-column image at
`<ID>/.qa/contact-sheet.png`. Contact sheets require thumbnail downloads, so
they cannot be combined with `--no-thumbnails`.

Auth is zero-config if you already use gogcli: slidesmith reads the OAuth
client from `~/Library/Application Support/gogcli/credentials.json` (or
`GOG_ACCESS_TOKEN` / `GOOGLE_WORKSPACE_CLI_TOKEN` env vars, or a service
account via `SERVICE_ACCOUNT_PATH`).

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
- **Visual QA**: `check` downloads rendered slide PNGs, optionally creates a
  labeled two-column contact sheet with `--contact-sheet`, and runs geometry lint
  (overlap, out-of-bounds, likely text overflow) with a NEW / PRE-EXISTING /
  RESOLVED ledger keyed to your last pull.
- **Bulk restyles**: `replace-class` swaps one or more validated classes
  deck-wide as a single atomic operation.
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
freeform polish on a real presentation. 330 tests; `scripts/lint.sh` clean.
See the [agent guide](docs/AGENT-GUIDE.md) for the supported class vocabulary
and the complete edit/diff/push/check loop.

```bash
.venv/bin/pytest -q
```

Run `scripts/lint.sh` to check Pyflakes/syntax errors with Ruff and intentional
dead-code exceptions with Vulture.
