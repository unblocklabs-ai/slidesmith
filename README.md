# slidesmith

**Agent + human co-editing for Google Slides.** Pull a deck into local,
Git-style SML files, edit them (you or your agent), preview the exact
`batchUpdate` diff, and push it back to the **same deck in place** — your edits
appear in Drive version history like any collaborator's, and a human can have
the deck open while you push.

slidesmith treats the deck as the source of truth. A pulled folder is a local
projection plus a pristine snapshot used to compute safe, field-masked API
requests — so every push is reviewable before it leaves your machine.

> Descends from think41/extrasuite's `extraslide` (MIT, heavily rewritten — see
> [NOTICE](NOTICE)). Its release bugs are fixed and its unfinished layers
> rewritten per [DESIGN.md](DESIGN.md).

## Requirements

- **Python 3.11+**
- A Google account with access to the decks you want to edit
- Google credentials — reused automatically from `gogcli` if you have it, or
  supplied via an env token or a service account (see [Authenticate](#authenticate))

## Install

```bash
git clone https://github.com/unblocklabs-ai/slidesmith.git
cd slidesmith
uv venv && uv pip install -e .        # installs the `slidesmith` command
```

This puts `slidesmith` on your PATH (inside the venv). Prefer `pip`? Use
`python -m venv .venv && .venv/bin/pip install -e .`. Contributors add the dev
extras with `uv pip install -e ".[dev]"` (test + lint tooling).

Verify:

```bash
slidesmith --version
slidesmith auth doctor        # diagnoses credentials and tells you what's missing
```

## Authenticate

slidesmith is **zero-config if you already use `gogcli`** — it reads the OAuth
client from `~/Library/Application Support/gogcli/credentials.json`. Otherwise,
pick one:

| Method | How |
| --- | --- |
| **gogcli client** (recommended) | Nothing to do — it's discovered automatically. |
| **Pre-obtained access token** | Export `GOG_ACCESS_TOKEN` (or `GOOGLE_WORKSPACE_CLI_TOKEN`). Short-lived (~1h); slidesmith validates it up front and, if expired, tells you exactly how to refresh. |
| **Service account** | Point `SERVICE_ACCOUNT_PATH` at the key file. |

Local image uploads use the already-requested `drive.file` scope; uploaded,
link-readable assets stay in your own Drive. Run `slidesmith auth doctor`
anytime to see which method is active and whether it's healthy.

## The core loop

```bash
slidesmith pull "https://docs.google.com/presentation/d/<ID>/edit"   # -> <ID>/
# edit <ID>/slides/01/content.sml  (or let an agent do it)
slidesmith diff <ID> --summary      # preview the batchUpdate — no API call
slidesmith push <ID>                # apply to the same deck, atomically
slidesmith check <ID> --contact-sheet   # download renders + geometry QA
```

Each slide is `slides/NN/content.sml`: HTML-like elements (`<Rect>`,
`<TextBox>`, `<Image>`, `<Line>`, …) with absolute `x/y/w/h` in points and
Tailwind-style classes for style (`fill-#173b32/40`, `text-size-24`, `bold`,
`content-align-middle`). A no-edit pull always diffs to zero requests.

## What you can do

- **Author layout without coordinate math** — one-shot `Stack`/`Grid`
  containers, `flex`, `h="auto"` text height, and reusable `components.sml` +
  `<Use>` expansion; the compiler resolves positions.
- **Add & reorder slides** — `add-slide --after N`/`--at N` scaffolds a
  positioned new slide with a round-tripping ID; `reorder` changes live
  top-level z-order by semantic query (bring-to-front / send-to-back / …).
- **Edit and place images** — public-URL or local-file `<Image>` creation, plus
  in-place `src`/`fit` edits through the normal diff/push loop; `contain` and
  `stretch` fitting with geometry pinning, Drive upload caching, and
  workspace-scoped local paths. (`cover`/crop is API-read-only and unsupported.)
- **Restyle in bulk** — `replace-class` swaps one or more validated classes
  deck-wide as a single atomic operation.
- **Target elements semantically** — local-only `select` / atomic `apply` by
  role, tag, class, ID, text, slide, and geometry, with exact/prefix/suffix/
  substring operators — no ID-level scripting. Assign round-tripping roles once,
  restyle a whole deck with one command.
- **Transfer a design language** — `theme extract`/`apply` moves palette, type,
  and role styles between decks; `snippet copy`/`paste` reuses bounded visual
  structures with explicit role-to-content mapping.
- **Push safely** — three-way conflict guard against the live deck,
  `writeControl` revision locking, atomic deck-wide batches by default, an
  opt-in resumable `--per-slide` mode, optional `off|warn|block` offline
  geometry preflight, and **persistence verification** that warns
  (`WARNING`/`NOTICE` tiered) whenever Google meaningfully drops or normalizes a
  property you sent.
- **Judge visual quality** — `check` downloads rendered PNGs, builds a labeled
  contact sheet, and runs offline geometry lint (overlap, out-of-bounds, likely
  text overflow) with a NEW / PRE-EXISTING / RESOLVED ledger keyed to your last
  pull and stable across slide renumbering.
- **Recover from auth problems** — loud, named errors; dual-store sessions
  (Keychain + 0600 file) so subprocess agents can authenticate; `auth doctor`
  for self-diagnosis.

## For agents

slidesmith ships a packaged agent skill — start there:

- **[skills/slidesmith/SKILL.md](skills/slidesmith/SKILL.md)** — the mental
  model and command surface (read this first).
- **[skills/slidesmith/recipes.md](skills/slidesmith/recipes.md)** —
  copy-paste task recipes for common jobs.
- **[docs/AGENT-GUIDE.md](docs/AGENT-GUIDE.md)** — the exhaustive reference:
  full class vocabulary, selector grammar, one-shot layout, QA judgment, and
  auth recovery.

Errors are named and actionable, diffs are exact, and warnings are believable —
the tool is built to be driven autonomously.

**Install it as a plugin.** The repo ships thin manifests so the skill installs
into Claude Code and Codex, and publishes to OpenClaw's ClawHub as a skill — see
[docs/PLUGINS.md](docs/PLUGINS.md) for the one-command install per harness. A
root `AGENTS.md` also orients any agent working in a checkout.

## Development

```bash
uv pip install -e ".[dev]"
.venv/bin/pytest -q       # full test suite
scripts/lint.sh           # Ruff (syntax/unused) + Vulture (dead-code) gate
```

slidesmith has been hardened through repeated adversarial review rounds
(findings tracked in [`docs/review/`](docs/review/)) and successive live
dogfood campaigns in which agent designers built slides, ran deck-wide
restyles, and shipped polish on real presentations. See
[CHANGELOG.md](CHANGELOG.md) for what shipped in each release.

## License

MIT. See [NOTICE](NOTICE) for attribution to the upstream `extraslide` project.
