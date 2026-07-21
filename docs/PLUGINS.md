# Using slidesmith as an agent plugin

slidesmith ships a single packaged agent **skill** (`skills/slidesmith/SKILL.md`)
plus thin manifests so the repo can be installed as a plugin by several agent
harnesses. In every case the skill teaches the agent how to drive the
`slidesmith` CLI — so **install the CLI first** (see the README) and make sure
`slidesmith` is on `PATH`.

## Claude Code

Manifests: `.claude-plugin/plugin.json` (the plugin) and
`.claude-plugin/marketplace.json` (a one-plugin marketplace whose `source` is the
repo root). Install:

```
/plugin marketplace add unblocklabs-ai/slidesmith
/plugin install slidesmith@slidesmith
```

The `skills/` directory is auto-discovered; no extra configuration is needed.

## Codex

Manifest: `.codex-plugin/plugin.json` (references the skill via `"skills":
"./skills/"`), with a marketplace at `.agents/plugins/marketplace.json`. Codex
also auto-loads the root **`AGENTS.md`** whenever an agent runs in this repo, so
a Codex agent is oriented even without installing the plugin. To install as a
plugin:

```
codex plugin marketplace add <path-or-repo>
```

Then enable it from the Codex `/plugins` picker.

## OpenClaw

OpenClaw distributes slidesmith as a **skill via ClawHub** (its native
registry). The skill carries an OpenClaw `metadata.openclaw` block declaring the
`slidesmith` binary dependency, so once published, install is one command:

```
openclaw skills install @<owner>/slidesmith
```

Publishing to ClawHub is a **maintainer action** (via the `clawhub` CLI, done
once per release), not something a checkout does on its own — see the release
notes. Until then, an OpenClaw user can also consume slidesmith through the
Claude-compatible marketplace, which OpenClaw reads directly:

```
openclaw plugins install slidesmith --marketplace unblocklabs-ai/slidesmith
```

Why the skill route (not a native OpenClaw plugin): confirmed against
`docs.openclaw.ai`, a *native* OpenClaw plugin is defined as "manifest **plus a
JS runtime module**," and if an `openclaw.plugin.json` is present OpenClaw
classifies the directory as a native plugin — which would fail here, since
slidesmith is a Python CLI with no runtime module. So this repo ships **no**
`openclaw.plugin.json`; the skill (with its `metadata.openclaw` block) is the
whole OpenClaw surface, published to ClawHub. The `--marketplace` fallback above
works because OpenClaw reads Claude's marketplace format directly.

## Keeping versions in sync

The plugin manifest `version` fields (`.claude-plugin/plugin.json`,
`.codex-plugin/plugin.json`) track the package version in `pyproject.toml`. Bump
them together at
each release (Claude Code only ships updates when the plugin `version` changes).
