# slidesmith — agent guide

slidesmith is a command-line tool for **agent + human co-editing of Google
Slides**. You pull a deck into local, Git-style SML files, edit them, preview the
exact `batchUpdate` diff, and push it back to the **same deck in place**. The
deck stays the source of truth; every push is reviewable before it leaves the
machine.

## When to use it

Use slidesmith whenever the user wants to **build or restyle a real Google
Slides presentation and keep it native/editable** — new slides, deck-wide
restyles, media/galleries, cross-deck theme transfer — instead of generating an
image or a throwaway `.pptx`.

## Setup

- Requires Python 3.11+ and the `slidesmith` CLI (`uv pip install -e .` in a
  checkout puts it on PATH).
- Authenticate first: `slidesmith auth doctor` diagnoses credentials. Auth is
  zero-config if the user has `gogcli`; newer gog versions keep the client ID in
  `credentials.json` and the client secret in the OS keyring. Otherwise a
  `GOG_ACCESS_TOKEN` env token or a service account (`SERVICE_ACCOUNT_PATH`)
  works.

## The core loop

```bash
slidesmith pull "<deck-url-or-id>"      # -> <ID>/slides/NN/content.sml
# edit the SML (see the skill/guide for the class + selector vocabulary)
slidesmith diff <ID> --summary          # preview the batchUpdate — no API call
slidesmith push <ID>                    # apply to the same deck, atomically
slidesmith check <ID> --contact-sheet   # download renders + geometry QA
```

## Commands you'll actually invoke

`pull`, `diff`, `push`, `check` (the loop); `add-slide` (scaffold a positioned
new slide); `select`/`apply` (semantic queries + roles, local-only); `replace-class`
(bulk restyle); `reorder` (z-order); `replace-image`; `theme extract/apply` and
`snippet copy/paste` (cross-deck); `fmt`; `components`; `auth doctor`. Every
command supports `--help`.

## Rules of thumb

- **Always `diff` before `push`.** `diff` is local and never calls the API.
- **Trust the warnings.** If Google normalizes or drops what you sent, `push`
  prints a `warning:`/`notice:` line — believe it over your local intent.
- **Don't hand-compute coordinates** when a `Stack`/`Grid` layout container or a
  semantic `select`/`apply` will do it.
- A no-edit pull always diffs to zero.

## Deeper reference

- **[skills/slidesmith/SKILL.md](skills/slidesmith/SKILL.md)** — the mental model
  and command surface (the packaged agent skill).
- **[skills/slidesmith/recipes.md](skills/slidesmith/recipes.md)** — copy-paste
  task recipes.
- **[docs/AGENT-GUIDE.md](docs/AGENT-GUIDE.md)** — exhaustive reference: full
  class vocabulary, selector grammar, layout, QA judgment, auth recovery.
