# slidesmith — design

One living Google Slides deck that an agent and a human edit together. No
image-of-slide hacks, no pptx re-uploads, no new files. The agent works on a
local text projection; pushes land as `batchUpdate` calls on the same deck.

## Architecture: two representations, not one

A single "HTML both ways" format cannot work: layout intent (Stack, gap=24) is
not recoverable from absolute output — when a human moves three cards in the
Slides UI, no algorithm can tell whether they came from a Stack or from three
unrelated positions. So:

```
Authoring DSL          Stack / Grid / gap / padding / AutoSize   (intent, agent-managed)
      | compile (layout engine)
Resolved scene graph   IDs, absolute geometry, styles, text runs (SML; can represent ANY pulled deck)
      | reconcile (diff vs pristine base)
Google Slides          batchUpdate to the same presentation
```

Human moves in the UI are overrides: the element becomes "detached from
layout" (or the layout source is updated), never silently re-flowed.

## The six contracts (tests/contracts/)

1. Pull → no edits → diff produces zero requests.
2. Create a styled text box/shape using the documented syntax.
3. Edit text while preserving all human styling.
4. Human and agent edit different properties of the same element; both survive.
5. Human and agent edit the same property; push aborts with a useful conflict.
6. Pull → push → pull is idempotent.

Status: C1, C2, and C6 (the offline halves) pass against the golden fixture.
C3–C5 are live-deck stubs (`SLIDESMITH_LIVE_DECK=<presentationId>`).

## Key design decisions

- **Field masks do most of contract 4.** `batchUpdate` with field masks only
  touches named fields, so if diffs stay property-granular and are computed
  against *base* (the pristine snapshot), human edits to other properties
  survive by construction. No property-merge engine needed for v1.
- **Conflicts abort, they don't merge (v1).** Three-way compare
  (base/local/remote) is used only to *detect* same-property conflicts and
  delete-vs-edit; the v1 response is a clear abort message, not a merge UI.
- **revisionId is a write guard, not a change detector.** Revision IDs are
  opaque, per-user, ~24h-valid, and can change without a real edit. Detect
  human changes by comparing freshly fetched remote content against base;
  pass `writeControl.requiredRevisionId` (captured at that fetch) on the
  write; refetch-and-rebase on 400.
- **Ownership via chosen objectIds.** The API allows caller-chosen objectIds
  (5–50 chars) at creation. Slidesmith sends a valid, unoccupied authored SML ID
  directly and recognizes it again on pull; older `new_<id>` creations normalize
  back to `<id>`. Collisions receive a safe suffix, while Google-generated ID
  patterns continue to receive local `eN`/`gN` names. This preserves meaningful
  identity in the deck and keeps `id_mapping.json` stable without relying on an
  ownership prefix. Layout intent lives only in this repo's folder; a deck
  without its folder degrades to "everything detached/absolute" — correct.
- **The deck is the source of truth.** The local folder is a projection plus a
  pristine base for diffing (git-style working tree), not a co-equal replica.
- **Authoring layout is one-shot in v1.** `Stack`, `Grid`, and `h="auto"` are
  compiled to plain absolutely-positioned SML when content is read for a diff
  or push. Containers never become deck elements, and after the push a later
  pull returns only the resolved absolute SML. Persisting layout intent across
  pull/push cycles is deliberately deferred to a later milestone.

## API constraints that shaped this (verified July 2026)

- Autofit is read-only: only `NONE` can be written, and size/text edits
  silently reset autofit. No text-measurement API exists. → `AutoSize` must
  self-measure (Google Fonts files + opentype.js/browser) with a tolerance
  margin, verified via `getThumbnail` (60/min/user, ~1–3s).
- Inherited placeholder/layout/master styles come back as *unset fields*; the
  reader must resolve the chain, and the writer must distinguish "set
  explicit" from "leave inherited" to avoid baking theme values in.
- Writes: 60 batchUpdate/min/user — one batched call per agent commit.

## Deferred / candidates

- **Local approximate preview.** A fast local renderer could shorten layout
  iteration, but it would only approximate Google Slides font metrics, wrapping,
  theme inheritance, and effects. Thumbnail QA against the pushed deck remains
  the visual source of truth.

## Provenance

slidesmith descends from think41/extrasuite's extraslide (MIT, heavily rewritten
— see NOTICE). The descended code covers the engine package (transport, EMU/pt
units, transforms, ID mapping, style extraction, request builders, and the SML
processor/parser/diff) plus the zero-config gogcli credential resolver. Known
donor bugs fixed here: the CLI awaited the synchronous `diff()`.

Planned rewrites (donor code is scaffolding, not contract): the SML parser
(must consume class attributes and nested text runs — see C2 xfail), the
reconciler (style diffing + three-way conflict detection + writeControl), the
authoring DSL and layout engine, and thumbnail-based visual verification.
