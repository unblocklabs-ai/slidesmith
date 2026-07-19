# slidesmith

Agent + human co-editing for Google Slides. Pull a deck into local SML files,
edit them (you or your agent), preview the exact `batchUpdate` diff, and push
it back to the **same deck in place** — edits show up in Drive version history
like any collaborator's.

Built on a surgical extraction of [ExtraSuite](https://github.com/think41/extrasuite)
(MIT — see `NOTICE`), with its release bugs fixed and its unfinished layers
being rewritten per [DESIGN.md](DESIGN.md).

## Quickstart

```bash
uv venv && uv pip install -e ".[dev]"

slidesmith pull "https://docs.google.com/presentation/d/<ID>/edit"
# edit <ID>/slides/01/content.sml ...
slidesmith diff <ID>     # prints the batchUpdate requests, no API call
slidesmith push <ID>     # applies them to the same deck
```

Auth is zero-config if you already use gogcli: slidesmith reads the OAuth
client from `~/Library/Application Support/gogcli/credentials.json` (or
`GOG_ACCESS_TOKEN` / `GOOGLE_WORKSPACE_CLI_TOKEN` env vars, or a service
account via `SERVICE_ACCOUNT_PATH`).

## For agents

Read [docs/AGENT-GUIDE.md](docs/AGENT-GUIDE.md) before editing a deck. It is the
source-derived reference for the pull/diff/check/push loop, accepted SML class
vocabulary, one-shot Stack/Grid layout, QA judgment, and auth recovery.

## Status

Working today: styled pull to editable SML, field-masked diff, conflict-aware
in-place push with post-push refresh, and baseline-aware offline/thumbnail QA via
`check`. The parser and generator preserve element, paragraph, and text-run
styling; authoring supports one-shot `Stack`, `Grid`, and automatic text height.
`slidesmith auth doctor` diagnoses credentials before a pull. See the
[agent guide](docs/AGENT-GUIDE.md) for the supported class vocabulary and the
complete edit/diff/push/check loop.

```bash
.venv/bin/pytest -q
```
