---
name: slidesmith
description: >-
  Edit a Google Slides deck in place from the command line: pull it to editable
  local SML, change it (semantic selectors, roles, components, images, layout),
  preview an exact diff, and push batchUpdates back to the SAME deck. Use for any
  task that creates or restyles Google Slides while keeping them native and
  editable — new slides, deck-wide restyles, media/galleries, cross-deck theme
  transfer — instead of generating an image or a throwaway .pptx.
when_to_use: >-
  The user wants to build or modify a real Google Slides presentation and keep it
  editable/collaboratable (not a rendered image, not a new file each edit).
version: 0.4.1
---

# slidesmith

You edit a **living** Google Slides deck. `pull` mirrors it to local SML files,
you change those files, `diff` shows the exact `batchUpdate` you'd send, and
`push` applies it to the same deck — your edits appear in Drive version history
like any collaborator's. A human can have the deck open while you push.

## Mental model (read this first)

- **The deck is the source of truth.** Your local folder is a projection plus a
  pristine snapshot for diffing — a git-style working tree, not a replica.
- **SML is HTML-like.** Each slide is `slides/NN/content.sml`: elements
  (`<Rect>`, `<TextBox>`, `<Image>`, `<Line>`…) with absolute `x/y/w/h` in points
  (960×540 page) and **Tailwind-style classes** for style
  (`fill-#173b32/40`, `text-size-24`, `bold`, `text-color-#5df2b2`,
  `content-align-middle`). Text is `<P>` paragraphs, optionally with `<T class>`
  runs.
- **Quick class vocabulary.** Append `/NN` (0–100) to fill, stroke, or text
  colors for opacity; stroke dashes are `stroke-solid`, `stroke-dot`,
  `stroke-dash`, `stroke-dash-dot`, `stroke-long-dash`, and
  `stroke-long-dash-dot`; text effects are `bold`, `italic`, `underline`,
  `line-through`, `small-caps`, `superscript`, and `subscript`. The agent guide
  is the authority for the full class grammar.
- **Don't hand-compute coordinates when you can help it.** Use layout containers
  (`Stack`/`Grid`) and semantic `select`/`apply`. That's the whole point.
- **Trust the diff and the warnings.** `diff` before every `push`. If Google
  silently changes what you sent, `push` prints `warning: … (sent 'X', remote
  now 'Y')` — believe it over your local intent.

## The loop

```bash
slidesmith auth doctor                  # once, if credentials might be off
slidesmith pull <deck-url-or-id> -o .   # -> <id>/slides/NN/content.sml
# ... edit the SML (see below) ...
slidesmith diff <id> --summary          # preview; --slide N to scope
slidesmith push <id>                    # apply to the same deck
slidesmith check <id> --contact-sheet   # download renders + geometry QA
slidesmith --version                    # print the installed package version
```

To create a slide, add `slides/NN/content.sml` with a `<Slide>` root and at
least one element change targeting it: a new-ID element or a copy of a pulled
element. An empty folder produces nothing; `diff` creates the slide, Google
appends it, and the next pull renumbers the local folder.

Push diagnostics distinguish actionable `warning:` lines from lower-severity
`notice:` lines and render warnings first.

`check` writes `.qa/slide-NN.png` and a `.qa/contact-sheet.png` — **look at
them**; the offline geometry lint can't see visual intent, only overlaps/overflow.

## Core capabilities

### Semantic selection (avoid ID-scripting)
```bash
slidesmith select <id> "role=title AND slide in 4..24"
slidesmith apply  <id> "role=subtitle" --add-class text-size-18 --remove-class text-size-24
```
Predicates (combine with `AND`/`OR`/parens): `tag=`, `role=`, `id=`/`id~=`,
`text=`/`text^=`/`text$=`/`text~=`, `class~=`, `slide=`/`slide in a..b`/`slide in
a,b`, geometry `w>`/`h<=`/`x>=`/`y<`. Quote values with spaces:
`text="I CLOSE LOOPS."`. Use exact/anchored (`text=`, `id=`) to avoid
over-matching. `apply` is atomic and rejects class conflicts. Run
`slidesmith select --help` for the full grammar.

### Roles (semantic layer for restyling)
```bash
slidesmith apply <id> "text^=THE MISSION OR text^=THE ROADMAP" --set-role kicker
slidesmith apply <id> "role=kicker" --add-class fill-#173b32/40   # restyle all kickers at once
```
Roles round-trip via `roles.json`, are never sent to Google, and make deck-wide
restyles a single command.

### Layout (compiler does the math)
```xml
<Stack id="cards" x="56" y="400" w="848" h="80" direction="row" gap="24" align="stretch">
  <TextBox id="c1" flex="1" class="fill-#0a111d content-align-middle text-align-center"><P>SHIP</P></TextBox>
  <TextBox id="c2" flex="1" class="fill-#0a111d content-align-middle text-align-center"><P>SCALE</P></TextBox>
</Stack>
```
`Stack`/`Grid` compute child positions; `flex="1"` splits space; `h="auto"` sizes
a TextBox to its text. **One-shot:** after push they're plain absolute SML — keep
your authoring source if you want to re-run it.

### Components (reuse a pattern)
`components.sml` (needs a `<Components>` root):
```xml
<Components>
  <Component name="metric">
    <Rect id="box" x="0" y="0" w="200" h="110" class="fill-#0a111d stroke-{{accent}}/40 stroke-solid content-align-middle text-align-center">
      <P class="bold font-family-montserrat text-size-30 text-color-{{accent}}">{{value}}</P>
      <P class="font-family-arial text-size-10 text-color-#bdc5d4">{{label|SUPPORT}}</P>
    </Rect>
  </Component>
</Components>
```
Use it: `<Use component="metric" value="3x" label="LOOPS" accent="#5df2b2" x=.. y=.. w=.. h=../>`
(also works inside a Stack with `flex`). `slidesmith components <id> --show metric`
prints the body + slots. Slots support `{{name|default}}`.

### Images
```xml
<Image id="hero" src="https://picsum.photos/1600/900" x="60" y="140" w="480" h="270" fit="contain"/>
<Image id="logo" src="./assets/logo.png" x="800" y="40" w="100" h="40" fit="contain"/>
```
`src` = public URL **or** local path (local uploads to Drive, cached). Relative
local paths resolve from the deck/pull root (the `<ID>/` folder), e.g.
`<ID>/assets/...`, not from the `slides/NN/` directory containing the SML file.
`fit`:
`contain` (aspect-correct, top-left anchored — recommended) or `stretch` (exact
box, may distort). For an existing image, set a new `src` (optionally with
`fit`) in SML and use the normal `diff`/`push` loop; this emits
`IMAGE_UPDATE`/`replaceImage` and a geometry pin. A `fit` change requires a
`src`. For a clean-diff one-shot swap, use `slidesmith replace-image
<id> <element-id> <new-src> --dry-run`. **`fit="cover"`/cropping is impossible**
— Google's crop is API read-only; design galleries contain-first.

### Large decks / safe pushes
```bash
slidesmith push <id> --per-slide --preflight=block   # per-slide progress; abort on NEW geometry findings
slidesmith push <id> --per-slide --resume            # continue after a partial failure
```

### Cross-deck design language
```bash
slidesmith theme extract <id> --from-slides 1-3 -o theme.json
slidesmith theme apply <id> theme.json --to-slides 4-24 --map-colors --dry-run --verbose
slidesmith snippet copy <src-id> "role=title" -o title.sml
slidesmith snippet paste <dst-id> --slide 5 title.sml
```
`theme apply` is style-only (never touches text/geometry); `--map-colors` snaps
off-theme colors to the nearest palette member; needs roles on the target for
role-aware restyle.

### QA judgment
`check` labels findings NEW / PRE-EXISTING / RESOLVED vs your last pull. Accept
intentional composition so it stops warning: `check <id> --accept <finding-id>`
or add a `qa-accept-<rule>` class to the element inline (stripped before push).

## Gotchas
- Always `diff` before `push`; the deck may have changed under you (push is
  revision-locked and will abort on conflict — re-pull and retry).
- Layout/components are one-shot; a later pull returns resolved coordinates.
- Authored element IDs survive round-trips when they meet Google's 5–50-character
  object-ID grammar, are unoccupied, and don't resemble a generated
  Google/Slidesmith ID (`eNN`, `gNN`, `pNN`, `SLIDES_API…`); invalid, occupied,
  or generated-looking names are sanitized/suffixed, so don't script around
  generated `eNN` IDs.
- `fmt` before committing hand-edited SML so whitespace changes don't inflate the
  diff.
- Bounds-containment nesting: an element fully covering another may become its
  parent in the SML on re-pull. Deleting a wrapper needs care (keep the child).

See `recipes.md` for copy-paste task recipes and `docs/AGENT-GUIDE.md` (in the
repo) for the exhaustive reference.
