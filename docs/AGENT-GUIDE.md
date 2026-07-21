# Slidesmith agent guide

This is the cold-start reference for editing a Google Slides deck through
Slidesmith. The deck remains the source of truth. A pulled folder is a local
projection plus a pristine snapshot used to calculate safe, field-masked Google
Slides `batchUpdate` requests.

## Safe working loop

Start by diagnosing authentication, then pull immediately before editing:

```bash
slidesmith auth doctor
slidesmith pull "https://docs.google.com/presentation/d/<ID>/edit"
```

The pull creates `<ID>/`. Make the smallest possible edits under `slides/`, then
inspect and validate them before writing remotely, then check the rendered deck:

```bash
slidesmith diff <ID>
slidesmith diff <ID> --summary
slidesmith diff <ID> --slide 1
slidesmith check <ID> --no-thumbnails
slidesmith push <ID>
slidesmith check <ID>
slidesmith check <ID> --contact-sheet
```

`diff` does not call Google APIs. For an authored HTTP(S) `Image` with
`fit="contain"`, the diff engine performs a bounded, unauthenticated image fetch
to determine its pixel dimensions. A local image is read with Pillow instead;
it never needs a network request during `diff`. `fit="stretch"` does not fetch a
remote source during offline diff. `diff` prints the request list plus a stderr legend from Google
object IDs to clean SML IDs. A local image's previewed `createImage.url` remains
the authored local source because its public Drive URL does not exist yet;
`push` replaces that one outgoing field after upload or cache lookup.
`diff --summary` replaces that raw JSON with a compact per-slide view of deletes,
creates, moves, copies, style changes, and text edits, followed by the total
generated request count. It is intended for a quick human review; use plain
`diff` whenever the exact API payload matters.
`diff --slide N` limits either raw JSON or `--summary` output to one 1-based
slide, which is useful for inspecting a focused edit without unrelated deck
changes.
`check --no-thumbnails` is also local-only.
Plain `check`, run after push, downloads current slide thumbnails into
`<ID>/.qa/` before running geometry QA, so it needs authentication. `push`
re-fetches the remote deck, aborts if a locally touched object changed remotely,
uses a revision lock for the write, and refreshes the local projection after a
successful batch. Use `push --force` only when the user explicitly accepts
overwriting concurrent edits to touched properties.

Plain `push` is atomic across the deck because it sends one `batchUpdate`. For
a large push where resumability matters more than deck-wide atomicity, use:

```bash
slidesmith push <ID> --per-slide
# If a later slide fails after earlier slides committed:
slidesmith push <ID> --per-slide --resume
# Optional offline geometry gate (also works with --per-slide):
slidesmith push <ID> --preflight=block
```

Per-slide mode partitions the generated request stream by target slide and
sends one revision-locked `batchUpdate` per changed slide in slide order,
refreshing the required revision between writes. It reports progress such as
`slide 03/24 ✓ (7 changes)`. If a slide fails or conflicts, Slidesmith stops
there and records each successful slide index plus its local content hash in
`<ID>/.push-progress.json`. `--resume` skips only the matching successful
prefix; if a recorded slide's SML or shared `components.sml` changed, that
slide is not skipped. The ledger is removed only after every slide succeeds
and the normal one-time post-push refresh and persistence verification finish.
`--resume` is invalid without `--per-slide`.

`--preflight=off|warn|block` controls an offline geometry-lint pass before any
authentication or API call. `off` is the default. `warn` prints the normal QA
report and proceeds when active findings are `NEW` relative to
`.pristine/qa-baseline.json`; `block` prints the report and exits 1 instead.
Pre-existing and accepted findings do not block. The same gate runs once before
a deck-wide or `--per-slide` push. Geometry QA uses the effective post-layout
box. In particular, an authored `Image fit="contain"` is checked at its
aspect-correct contained width/height, not its larger authored frame.

This is a deliberate atomicity tradeoff: earlier slide batches remain applied
when a later slide fails. Do not use `--per-slide` when the deck must change as
one all-or-nothing operation; use plain `push` instead.

Add `--contact-sheet` to plain `check` to compose the downloaded PNGs into a
labeled two-column overview at `<ID>/.qa/contact-sheet.png`. It is useful for a
quick whole-deck visual scan. It requires those downloads and therefore cannot
be used with `--no-thumbnails`.

After a successful refresh, `push` compares the intended local changes with the
remote-truth projection. When both values are cheap to derive from the captured
intent and refreshed SML, the warning names the specific field and shows both,
for example `warning: 1 change(s) did not persist remotely: text on title did
not persist (sent 'Q4', remote now 'Q3') — the API may not support these
values`. Text content, geometry, affected element/paragraph/run style classes
use this sent-versus-remote form. For creates, deletes, copies, or another field
without a cheap refreshed value, the detail keeps the generic
`title (style update)` form. Either warning means Google accepted the batch but
normalized or dropped an authored value. Treat the refreshed SML as
authoritative and choose a supported alternative.

Persistence verification intentionally suppresses two always-normalized cases:
geometry differences smaller than 0.02 pt on every changed box field, and only
Google-added default classes on newly created elements. The exact shared
default-class set is `font-weight-400`, `text-align-left`, `leading-100`,
`space-above-0`, `space-below-0`, `indent-start-0`, `indent-first-0`,
`spacing-never-collapse`, and `spacing-collapse-lists`, plus shape-specific
content alignment: `content-align-top` only for `TextBox`, and
`content-align-middle` only for non-`TextBox` Google shape types such as `Rect`,
`RoundRect`, and `Ellipse`. Google documents that an unspecified alignment uses
the new-editor default for the shape kind; pulled API evidence confirms middle
for those geometric shape creates but not for `TextBox`, so a middle-aligned
`TextBox` still warns. Suppression applies only when every authored class is
still present remotely and the entire difference is additions from this set.
Component-expanded children and image creates use the same created-element
normalization path. The aspect-correct effective width/height of an authored
`Image fit="contain"` is also the intended geometry, so Google's expected
authored-frame-to-contained-frame correction does not warn as a persistence
failure. Differences at or above 0.02 pt and any meaningful text, geometry, or
style drop still warn.

Visual work is iterative: edit, `diff`, run the offline check, `push`, then run
plain `check` and inspect the new thumbnails. Repeat that push-then-check loop
until the rendered deck is correct; the local approximation is not a substitute
for the Slides render.

If `diff`, `push`, or `check` says the workspace was pulled more than 24 hours
ago, re-pull before continuing. A re-pull replaces the projection; preserve any
uncommitted SML edits first.

## Workspace and SML structure

```text
<ID>/
├── presentation.json       title, page size, slide count, revisionId, pulledAt
├── id_mapping.json         clean SML IDs -> Google object IDs
├── styles.json             pulled style details used by diff and QA
├── .assets.json            local path + SHA-256 -> uploaded Drive file ID/URL
├── roles.json              optional local semantic roles keyed by clean ID
├── components.sml          optional reusable authoring-only component definitions
├── slides/NN/content.sml   one XML document per slide
├── .pristine/              baseline used by diff and conflict detection
├── .raw/                   optional raw pull response
└── .qa/                    thumbnails created by check
```

Each `content.sml` has one `Slide` root. Elements use point-valued `x`, `y`, `w`,
and `h` attributes. Preserve IDs when modifying pulled elements: identity drives
the diff and conflict guard. Paragraphs are `P` nodes. A paragraph may contain
plain text and styled `T` runs. A `P` may carry paragraph- and text-family
defaults for that paragraph; a nested `T` may carry text-family overrides.

When creating an element, choose a descriptive ID such as `mission_swarm` or
`q3-scorecard`. Authored IDs must be 5–50 characters, start with an ASCII letter
or underscore, and contain only ASCII letters, digits, underscores, and hyphens.
Slidesmith sends a valid, unoccupied authored ID directly as the Google object
ID, so a later pull preserves it in SML and in `id_mapping.json`. If that Google
ID is already occupied, the create request uses a collision-safe numeric suffix.
Names that Google generated (`g…_N_N`, `pN…`, and `SLIDES_API…`) remain clean
`eN`/`gN` IDs on pull. Do not author names beginning with `new_`: that prefix is
reserved for compatibility with older Slidesmith creations and is stripped on
pull.

```xml
<Slide id="s1">
  <Rect id="e1" x="48" y="36" w="240" h="72"
        class="fill-#14213d stroke-none">
    <P><T class="bold text-size-24 text-color-#ffffff">Quarterly review</T></P>
  </Rect>
  <Ellipse id="accent" x="640" y="-45" w="150" h="150"
           class="fill-theme-accent1/40 stroke-none" />
</Slide>
```

This paragraph-scoping example is parsed by the documentation contract test:

```sml-paragraph
<TextBox id="summary" x="48" y="72" w="360" h="90" class="text-size-14">
  <P class="text-align-left leading-110 text-color-#333333">Default paragraph</P>
  <P class="text-align-right leading-140 bold">Right-aligned <T class="italic text-color-theme-accent1">override</T></P>
</TextBox>
```

Pulled decks may contain nested `Group` elements and many Google shape tags;
keep the pulled tag unless intentionally replacing an element. Common authoring
tags are `Rect`, `RoundRect`, `Ellipse`, `TextBox`, `Line`, and `Group`. Unknown
tags fall back to a rectangle when created, so do not guess tag names.

XML rules still apply: escape `&`, `<`, and `>` in text; quote attributes; keep
`T` inside `P`; and do not put layout containers inside `P` or `T`.

## Z-order

New page elements are created on top of the existing Google Slides stack. After
a pull (including the authoritative refresh after a live command), SML sibling
order is regenerated from Google's back-to-front render order: the first
element is behind later elements. That file order is a pulled projection, not
the z-order authoring model; use `reorder` to change stacking:

```bash
slidesmith reorder <ID> 'id=hero_image' --op send-to-back
slidesmith reorder <ID> 'tag=Rect AND slide in 2..6' --op bring-forward
slidesmith reorder <ID> 'id=hero_image' --op send-to-back --dry-run
```

The four operations are `bring-to-front`, `bring-forward`, `send-backward`,
and `send-to-back`. A selector may match elements on several slides; Slidesmith
sends one atomic z-order request per slide. Only top-level page elements can be
reordered; elements inside a `Group` are rejected because the Slides API does
not accept grouped children in this request. `reorder` requires a clean local
diff, uses the live revision lock, and refreshes the workspace after success so
the next no-edit `diff` is zero. `--dry-run` prints the exact requests without
authentication or API calls. A full-bleed background image should typically be
sent to the back immediately after it is created.

## Semantic selection and apply

Use `select` to find elements at any depth of the same parsed SML tree used by
`diff`. Both commands are local-only and make no API calls:

```bash
slidesmith select <ID> 'role=subtitle OR (tag=TextBox AND text~=overview)'
slidesmith apply <ID> 'id~=subtitle AND slide in 4..24' --set-role subtitle
slidesmith apply <ID> 'role=subtitle' \
  --remove-class text-size-18 --add-class text-size-20 --dry-run
slidesmith apply <ID> 'role=subtitle' \
  --remove-class text-size-18 --add-class text-size-20
slidesmith diff <ID> --summary
slidesmith push <ID>
```

`select` prints every matching nested or top-level element with its slide, clean
ID, tag, and a short text/class summary, followed by the total. `apply` accepts
repeatable `--add-class` and `--remove-class`, plus mutually exclusive
`--set-role` / `--clear-role`. It reports match and mutated-element counts per
slide and in total. A matched element counts as one mutation even if several of
its classes and its role change. `--dry-run` calculates the same result without
writing any file.

Class changes are transactional across the workspace: Slidesmith constructs all
prospective SML first and parses every resulting slide through the normal SML
parser and its existing mutually-exclusive-class validation before the first
write. If one result is invalid, the error names the offending element and no
SML or role file is written. Run `diff` and `push` separately after reviewing a
successful apply.

The query grammar is:

```ebnf
query          = or-expression ;
or-expression = and-expression, { "OR", and-expression } ;
and-expression = primary, { "AND", primary } ;
primary        = predicate | "(", or-expression, ")" ;

predicate      = "tag", "=", value
               | "class", ("=" | "~="), value
               | "role", "=", value
               | "id", ("=" | "~="), value
               | "text", ("=" | "^=" | "$=" | "~="), value
               | "slide", "=", positive-integer
               | "slide", "in", slide-set
               | geometry-field, comparison, number ;

slide-set      = positive-integer, "..", positive-integer
               | positive-integer, { ",", positive-integer } ;
geometry-field = "w" | "h" | "x" | "y" ;
comparison     = ">" | ">=" | "<" | "<=" | "=" ;
value          = bare-value | quoted-value ;
```

| Predicate | Operators | Meaning |
| --- | --- | --- |
| `tag`, `role` | `=` | Exact, case-sensitive value |
| `class` | `=`, `~=` | Exact class-token membership (`~=` retained for compatibility) |
| `id` | `=`, `~=` | Exact ID or case-sensitive substring |
| `text` | `=`, `^=`, `$=`, `~=` | Case-insensitive full text, prefix, suffix, or substring |
| `slide` | `=`, `in` | One slide, comma list, or inclusive `2..6` range |
| `x`, `y`, `w`, `h` | `=`, `<`, `<=`, `>`, `>=` | Point-valued geometry comparison |

Whitespace is allowed between tokens. `AND` binds more tightly than `OR`; use
parentheses to override precedence. Keywords and predicate names are
case-insensitive. Tags, roles, class membership, and ID substrings are
case-sensitive; every text operator is case-insensitive and uses the element's
concatenated paragraph text. `class=` and `class~=` test exact membership on the element's
own `class` attribute (it is not a glob and does not inspect `P` or `T`
classes). Slide ranges are inclusive. Geometry uses the parsed element's
absolute point-valued box; a predicate does not match when that dimension is
absent. Quote a value with single or double quotes when it contains whitespace.
Run `slidesmith select --help` or `slidesmith apply --help` for this complete
grammar and examples.

Roles deliberately live in the workspace sidecar `roles.json`, keyed by clean
element ID, instead of in SML. Pull and post-push refresh replace only generated
workspace files and leave this sidecar untouched. On every selection,
Slidesmith reattaches a saved role to the currently parsed element with that
clean ID, so roles survive pull → edit → push → re-pull as long as element
identity survives. Because roles never enter SML, the diff or request generator
cannot serialize them into a Google Slides `batchUpdate`; they are local
selection metadata only.

## Cross-deck style transfer

M5 has two separate, local-only tools: themes transfer a deck's design language
without changing content or layout; snippets transfer selected visual structure
as newly inserted elements. Both leave API writes to the normal `diff` → `push`
review loop.

### Extract and apply a theme

Assign roles to representative source and target elements first when you want
semantic restyling, then extract a theme from the strongest design slides:

```bash
slidesmith apply source-deck 'slide=1 AND id~=hero_title' --set-role title
slidesmith apply target-deck 'slide in 4..24 AND id~=title' --set-role title
slidesmith theme extract source-deck --from-slides 1-3 -o theme.json
slidesmith theme apply target-deck theme.json --to-slides 4-24 --map-colors --dry-run --verbose
slidesmith theme apply target-deck theme.json --to-slides 4-24 --map-colors
slidesmith diff target-deck --summary
```

Slide specifications are inclusive and accept ranges or comma-separated mixes,
such as `1-3` or `1,3,5-7`. With no range, extract/apply inspects every slide.
Extraction writes readable versioned JSON with this shape:

```json
{
  "version": 1,
  "source": {"folder": "source-deck", "slides": [1, 2, 3]},
  "tokens": {
    "palette": ["#112233", "#f2ede2"],
    "themeColors": ["theme:accent1"],
    "primaryFontFamily": {
      "family": "Montserrat",
      "class": "font-family-montserrat"
    },
    "typeScale": [
      {"tier": "display", "pt": 53.0, "class": "text-size-53", "count": 3},
      {"tier": "title", "pt": 24.0, "class": "text-size-24", "count": 4},
      {"tier": "subtitle", "pt": 18.0, "class": "text-size-18", "count": 9}
    ],
    "typeScalePt": [53.0, 24.0, 18.0]
  },
  "roles": {
    "title": {
      "classes": [
        "font-family-montserrat",
        "text-size-53",
        "text-color-#f2ede2"
      ],
      "samples": 3,
      "canonicalSamples": 2,
      "elementIds": ["cover_title", "section_title", "summary_title"]
    }
  },
  "inventory": {
    "palette": [
      {"color": "#112233", "count": 8, "uses": {"fill": 6, "stroke": 2}}
    ],
    "type": {"fontFamilies": [], "fontSizes": []}
  }
}
```

The palette counts explicit RGB fill, stroke, text, and highlight classes;
unresolved Slides theme references are retained separately in `themeColors`
and the inventory as values such as `theme:accent1`. Nearest-color mapping uses
only concrete RGB values because a local SML workspace does not resolve theme
references to stable RGB coordinates.
The type inventory records every explicit font-family and font-size class with
its frequency, after authoring constructs have been compiled so source
component styles count as actually used. `tokens` gives the compact reusable
palette, dominant primary font family, and descending size ladder. Its inferred
tiers are rank labels (`display`, `title`, `subtitle`, `body`, `caption`,
`micro`, then `tier-N`), not semantic role assignments. For each
role, the most frequent exact element-class set becomes canonical; ties are
deterministic. `samples` is the number of role-bearing elements inspected and
`canonicalSamples` is the number using the chosen class set. Roles with no
explicit classes are represented by an empty canonical class list.

Theme apply performs three style-only operations on the selected slides:

1. If an element has a role found in `theme.json`, replace its element style
   classes with that role's canonical classes. Preserve local `qa-accept-*`
   annotations.
2. Unify text-bearing elements and explicit `P`/`T` font-family classes to the
   extracted primary family. This works even when the target has no roles.
3. With `--map-colors`, replace an explicit off-theme RGB fill, stroke, text,
   or highlight color only when the nearest palette color is within Euclidean
   RGB distance 48: `sqrt((R1-R2)^2 + (G1-G2)^2 + (B1-B2)^2)`. On the full
   0–441.7 RGB scale, 48 absorbs nearby tint/encoding drift while leaving
   unrelated accents alone. Alpha suffixes such as `/60` are preserved.

All prospective target files pass through the normal SML parser and its
mutually-exclusive-class checks before the first write; files commit together
with rollback on I/O failure. `--dry-run` performs the same extraction,
transformation, and validation and prints identical counts without writes.
Add `--verbose` to print each affected element's slide, ID, and class/color
transition. Colors outside the distance threshold are also listed as kept with
their nearest theme color and the beyond-threshold reason.
Theme apply never changes text nodes, `x/y/w/h`, element IDs, or tree structure.

The semantic boundary is important: role-aware restyling requires roles already
assigned on the target. Without them, theme apply can still unify font family
and map colors, but it cannot safely decide whether an element is a title,
subtitle, metric, or body. Component-expanded role targets also cannot be
edited at one slide instance; edit `components.sml` or expose the style as a
component slot. This is design-language transfer, not layout cloning.
Font/color classes inside a target component instance have the same boundary:
change or parameterize `components.sml` when the compiled style is not explicit
on that slide's raw SML.

### Copy and paste a layout snippet

Use a semantic selector to copy one or more elements from exactly one source
slide. If both a parent and its descendants match, the parent subtree is copied
once. The output stores source roles temporarily, normalizes all copied
coordinates to the selection's `(0,0)` bounding-box origin, and records its
width and height. Source `Stack`, `Grid`, `Use`, and `h="auto"` constructs are
compiled first, so the snippet contains their resulting plain shapes rather
than authoring-only intent:

```bash
slidesmith snippet copy competitive-deck \
  'slide=2 AND (id~=hero OR role=hero-art)' -o hero.sml
```

Paste the visual structure into a destination frame expressed in points:

```bash
slidesmith snippet paste target-deck --slide 5 hero.sml \
  --frame 36,48,648,300 \
  --map title:headline \
  --map body:summary \
  --dry-run
slidesmith snippet paste target-deck --slide 5 hero.sml \
  --frame 36,48,648,300 \
  --map title:headline \
  --map body:summary
slidesmith diff target-deck --summary
```

`--frame X,Y,W,H` translates and independently scales the snippet's explicit
`x/y/w/h` geometry into the chosen frame. Omitting it uses the snippet's
original size at `(0,0)`. Paste retains shape/style classes and source text by
default. A repeatable mapping means `SNIPPET_ROLE:DESTINATION_ROLE`: exactly one
element with the destination role must exist on that slide, and its paragraphs
replace the text in exactly one snippet role slot. The inserted slot receives
the destination role; unmapped snippet roles keep their names. Mapped text uses
the snippet paragraph and first-run styling rather than the destination style.

Paste inserts new root subtrees before the slide closes and prefixes every
inserted ID with the next available deterministic `snippet_N__` namespace. It
adds inserted roles to `roles.json`, validates the complete destination SML,
and commits SML plus roles atomically. `--dry-run` validates and counts the same
insertion without writes. Existing destination elements—including their text,
geometry, styles, IDs, and roles—are never edited or deleted.

V1 intentionally does not infer which destination content belongs in which
visual slot, remove the old destination elements, clone a whole slide, import
theme masters, or reconstruct component/layout intent after paste. The operator
supplies role mappings and decides whether to delete or retain old content
after reviewing `diff`. Snippet transfer is bounded visual structure + styling;
theme transfer is the deck-wide design-language tool.

## Formatting SML safely

Use the local-only formatter instead of a generic XML pretty-printer:

```bash
slidesmith fmt <ID>
slidesmith fmt <ID> --check
```

`fmt` rewrites every `slides/NN/content.sml` with Slidesmith's canonical
two-space indentation while keeping each `P` and its `T` runs inline. It first
parses every file, regenerates all prospective output, and asserts that parsing
before and after produces identical slide semantics before writing anything.
Generator-emitted files are already canonical, so formatting them is a
byte-for-byte no-op. `--check` performs the same validation without writing and
exits with status 1 when any file would be reformatted.

Whitespace in a plain `P` or inside a `T` run is text content, including
intentional leading and trailing spaces. Only newline-bearing indentation between
child tags is formatting. A generic XML formatter can move indentation into mixed
`P`/`T` content; run `slidesmith fmt` to normalize such a file safely before
reviewing its diff.

## Class vocabulary

The accepted vocabulary below is derived from the parsing functions in
`src/slidesmith/engine/classes.py`. Class names are whitespace-separated. Numeric
dimensions are points. Hex colors are exactly six digits and include the `#`.
Opacity is an integer suffix after `/` (normally 0–100).

Theme names accepted by the color model are `dark1`, `light1`, `dark2`,
`light2`, `accent1` through `accent6`, `text1`, `text2`, `background1`,
`background2`, `hyperlink`, and `followed-hyperlink`.

### Shape family

Vertical text placement is explicit shape styling:

```sml-classes
content-align-top
content-align-middle
content-align-bottom
```

### Fill family

- `fill-none` disables rendering; `fill-inherit` requests inherited state.
- `fill-#rrggbb` sets an RGB fill; append `/opacity` for transparency.
- `fill-theme-name` sets a theme fill; append `/opacity` if needed.

Every line in this block is parsed by the documentation contract test:

```sml-classes
fill-none
fill-inherit
fill-#1a2b3c
fill-#1a2b3c/45
fill-theme-accent1
fill-theme-background1/80
```

### Stroke family

- `stroke-none` and `stroke-inherit` set the outline state.
- `stroke-#rrggbb[/opacity]` and `stroke-theme-name[/opacity]` set color.
- `stroke-w-points` sets outline width.
- Dash choices are `stroke-solid`, `stroke-dot`, `stroke-dash`,
  `stroke-dash-dot`, `stroke-long-dash`, and `stroke-long-dash-dot`.

```sml-classes
stroke-none
stroke-inherit
stroke-#334455
stroke-#334455/60
stroke-theme-accent2
stroke-theme-text1/75
stroke-w-1.5
stroke-solid
stroke-dot
stroke-dash
stroke-dash-dot
stroke-long-dash
stroke-long-dash-dot
```

### Text family

Text classes may be placed on a shape or `TextBox` for element-level styling,
on `P` for that paragraph's default run styling, or on `T` for an explicit
range override. The precedence is element, then paragraph, then text run.

- Decorations: `bold`, `italic`, `underline`, `line-through`, `small-caps`.
- Baseline: `superscript`, `subscript`.
- Font: `font-family-name-with-hyphens`, `text-size-points`,
  `font-weight-integer`.
- Foreground: `text-color-#rrggbb[/opacity]` or `text-color-theme-name`.
- Highlight: `bg-#rrggbb`.

```sml-classes
bold
italic
underline
line-through
small-caps
superscript
subscript
font-family-open-sans
text-size-18.5
font-weight-600
text-color-#abcdef
text-color-#abcdef/55
text-color-theme-text1
bg-#fff2cc
```

`font-family-open-sans` parses back to “Open Sans.” Use positive sizes and
normal CSS-like weights such as 400, 600, or 700.

### Paragraph family

Paragraph classes may be element-level defaults or scoped to one `P`. A
`P class` changes only that paragraph's fixed text range.

- Alignment: `text-align-left`, `text-align-center`, `text-align-right`,
  `text-align-justify`.
- Line spacing percentage: `leading-integer`.
- Paragraph spacing in points: `space-above-points`, `space-below-points`.
- Indentation in points: `indent-start-points`, `indent-first-points`.
- Direction: `dir-rtl`.
- Spacing mode: `spacing-never-collapse`, `spacing-collapse-lists`.

```sml-classes
text-align-left
text-align-center
text-align-right
text-align-justify
leading-120
space-above-6
space-below-8.5
indent-start-18
indent-first-9
dir-rtl
spacing-never-collapse
spacing-collapse-lists
```

The source contains output-side handling for bullet and shadow descriptions,
but the SML parser does not currently accept those as authoring classes. Do not
use output-only class names. Position-like class helpers are also not accepted
on SML elements; use `x`, `y`, `w`, and `h` attributes.

Unknown classes fail loudly and name both the class and element ID. This is
intentional: never invent a Tailwind-looking class and assume it will parse.

## Bulk restyling

Replace one exact class token across every `slides/NN/content.sml` file with the
local-only command:

```bash
slidesmith replace-class <ID> font-family-arial font-family-inter --dry-run
slidesmith replace-class <ID> font-family-arial font-family-inter
```

For a coordinated restyle, repeat `--swap OLD=NEW`; positional `OLD NEW` can
also be combined with additional flags:

```bash
slidesmith replace-class <ID> \
  --swap font-family-arial=font-family-inter \
  --swap text-color-#333333=text-color-#111111 --dry-run
slidesmith replace-class <ID> bold font-weight-700 \
  --swap text-size-18=text-size-20
```

The command covers element, `P`, and `T` class attributes, prints per-swap and
per-slide counts plus a total, and preserves the surrounding SML formatting.
`--dry-run` performs the same validation and counting without writing files.
Every `NEW` and all combined prospective slide contents are validated by the
real class parser before any write. An unknown class, a conflict created only by
the combination of swaps, or another failure therefore leaves every slide
unchanged and names the affected element where applicable. This command does
not call Google APIs or run a diff; inspect the result with
`slidesmith diff <ID>` and push it separately.

## Authoring images from URLs or local files

Create an image with an HTTP or HTTPS `src`, an authored point-valued frame,
and optional `fit="stretch|contain"`:

```xml
<Image id="hero_image" src="https://picsum.photos/1200/675"
       x="48" y="72" w="624" h="351" fit="contain" />
```

Local paths and absolute `file://` URLs use the same element syntax. Relative
paths are resolved from the pulled presentation folder, not from the individual
`slides/NN/` directory:

```xml
<Image id="company_logo" src="./assets/logo.png"
       x="48" y="36" w="144" h="72" fit="contain" />
```

Google Slides `createImage` and `replaceImage` require a publicly fetchable URL;
the API cannot accept raw image bytes. At push time Slidesmith therefore uploads
each local file into the authenticated user's own Google Drive, creates an
`anyone`/`reader` link permission, and passes Drive's `webContentLink` (or the
equivalent Drive download URL when that field is absent) as the request URL.
This uses the already-requested per-file
`https://www.googleapis.com/auth/drive.file` OAuth scope. The uploaded files
remain in the user's Drive and are link-readable; Slidesmith does not delete
them after insertion.

Successful uploads are recorded in `<ID>/.assets.json`. Each entry stores the
workspace-relative canonical path (or an absolute path for a file outside the
workspace), the file-content SHA-256, Drive `fileId`, and public URL. A later
push of that exact path and content hash reuses the URL without uploading
again. Changing the bytes creates a new entry and upload. Do not hand-edit this
cache unless repairing a known-bad Drive file or URL.

`stretch` is the default Google Slides API behavior: the image fills the
authored box and may be distorted. `contain` preserves the source aspect ratio.
During offline `diff`, only a remote `contain` source downloads an image. At
push time, remote `stretch` dimensions are also fetched when possible to improve
intrinsic geometry. These fetches send no credentials, reject non-public
destinations at every redirect hop, are pinned to the validated DNS address,
and are limited to 25 MB and 100 million pixels before Pillow inspection. Local
dimensions are read directly with Pillow and never pass through the HTTP fetcher.
Slidesmith shrinks either
the authored width or height, keeping the resulting frame anchored at the
authored top-left `x`, `y` position. Authored `x` and `y` must be finite; `w` and
`h` must be finite and strictly positive for local and remote images under both
`stretch` and `contain`. This post-contain frame is the single effective geometry used by
request generation, diff/persistence comparison, and offline QA/preflight.

For remote-URL images, offline `diff` may omit push-time geometry-pin requests
and exact intrinsic sizing because pixel dimensions are only fetched at push.
Local-file images have their dimensions available offline, so their previews are
exact.

Google's `cropProperties` are **READ-ONLY via the API**, so `fit="cover"` is
impossible. Slidesmith rejects `cover` instead of pretending it can create that
result. Crop the source image before authoring when a cover treatment is needed.

For direct HTTP(S) sources, the image must be publicly fetchable, less than 50
MB, and less than 25 megapixels. A URL following the
`https://picsum.photos/<width>/<height>` pattern, such as the example above, is
useful for testing. Private URLs, data URLs, and URLs requiring an authorization
header are not supported. Local files work only through the Drive upload path
described above.

Drive permission changes can take time to propagate, and Workspace policy may
forbid `anyone` sharing. If Slides rejects a freshly uploaded URL, keep
`.assets.json` for diagnosis and retry after confirming the Drive file is
link-readable; the exact URL and permission behavior must be verified against a
live account because offline tests inject a fake uploader.

### Replacing an existing image

Replace the pixels of a pulled image with explicit, previewable geometry:

```bash
slidesmith replace-image <ID> hero_image ./assets/new-hero.png
slidesmith replace-image <ID> hero_image https://example.com/new-hero.png
slidesmith replace-image <ID> hero_image ./assets/new-hero.png --fit stretch
slidesmith replace-image <ID> hero_image ./assets/new-hero.png --dry-run
```

The command validates the clean SML ID against the freshly fetched deck and
fails if the target is not an image. The default `--fit contain` reads the new
image dimensions, fits that aspect ratio inside the old bounds, and anchors the
result at the old top-left `x`, `y`; one of `w` or `h` may therefore shrink.
`--fit stretch` keeps the exact old `x`, `y`, `w`, and `h`, accepting image
distortion. Both modes emit `replaceImage` with
`imageReplaceMethod="CENTER_INSIDE"` followed by an explicit relative
`updatePageElementTransform` that undoes Google's automatic centering and pins
the selected geometry. `--dry-run` prints that geometry and both requests
without uploading or writing.

Remote dimensions use the same SSRF-guarded, redirect-validated, bounded fetcher
as authored `fit="contain"`; local dimensions are read with Pillow before the
existing Drive upload and `.assets.json` reuse path. As with `createImage`,
Slides fetches the URL once and stores its own copy. Some existing image effects
may be removed by Google's replacement operation. Run it only on a clean
workspace: the command refuses pending SML changes before its authoritative
post-write refresh can overwrite them. Cover/crop remains impossible because
Google exposes image crop properties as read-only through this API.

An `Image` inside `Stack` or `Grid` participates like any other child. Give it
the required fixed `w`/`h` for that container axis, use a positive `flex` for
Stack main-axis sizing, or let Grid assign its cell frame. The container removes
`flex` and writes the computed absolute image geometry before diffing; `contain`
then shrinks one axis within that computed frame.

## Copying pulled elements

To copy a pulled element, repeat it with the same `id`, set the copy's `x` and
`y`, and omit `w` and `h`. Slidesmith recreates the element at the new position,
replays the writable pristine styling from `styles.json`, then applies authored
`P` and `T` classes as overrides. This preserves formatting that SML classes
cannot express, including hyperlinks. A same-slide copy containing dynamic
`autoText` anywhere in its descendant tree uses `duplicateObject`; a cross-slide
copy containing `autoText` fails loudly because the Slides API cannot preserve
that dynamic field while recreating the element.

For copied groups, leave child coordinates either at their source positions or
move them by exactly the same parent delta. Slidesmith accepts both forms and
avoids applying the delta twice. If an authored child position matches neither
the source position nor source plus the parent delta, Slidesmith applies the
parent delta and returns a warning. Treat that warning as an ambiguity: inspect
the resulting request/thumbnail and correct the child coordinates if the shift
was not intended.

Google exposes image `outline` and `link` as writable copy-time properties, but
exposes crop, transparency, brightness, contrast, recolor, and shadow as
read-only. Slidesmith safely drops those adjustments instead of sending an
invalid atomic batch and returns a warning naming the lost properties. A copied
adjusted image therefore uses the original image content but may not look
identical; verify it in the post-push thumbnail and recreate the adjustment in
the source image when exact fidelity matters.

## Layout authoring

Components, `Stack`, `Grid`, and `TextBox h="auto"` are authoring conveniences
compiled to absolute SML during `diff`, `push`, or offline QA.

### Reusable components

Define reusable bodies in the optional workspace file `<ID>/components.sml`.
The file is an XML document with one `Components` root and one or more named
`Component` children. A component body is normal SML: shapes, text, groups,
classes, and nested `Stack`/`Grid` containers are valid. A component cannot
contain another `Use` in this version.

```xml
<Components>
  <Component name="stat-card">
    <RoundRect id="card" x="0" y="0" w="200" h="120"
               class="fill-{{accent|#f5f7fa}} stroke-none" />
    <TextBox id="title" x="12" y="10" w="176" h="24">
      <P>{{title}}</P>
    </TextBox>
    <TextBox id="value" x="12" y="48" w="176" h="40"
             class="text-size-28 bold">
      <P>{{value}}</P>
    </TextBox>
  </Component>
</Components>
```

`{{slot}}` is required. `{{slot|default}}` supplies an inline default and makes
that occurrence optional. Placeholders work in element text and attribute
values, including class strings. Values come from attributes on `Use`; XML
escaping still applies at the use site. List the loaded definitions and the
slots derived from their bodies with:

```bash
slidesmith components <ID>
slidesmith components <ID> --show stat-card
```

The list form prints every component and its derived slots. `--show` prints one
component's body plus whether each slot is required or optional, so an agent can
inspect the reusable definition before authoring a `Use`.

Use a component from a slide like this:

```xml
<Use id="revenue" component="stat-card" title="Revenue" value="$4.2M"
     accent="#5df2b2" x="60" y="200" w="200" h="120" />
```

Component geometry uses **position-only translation, never scaling**. Author
the body at its final point dimensions relative to a `(0,0)` origin. For a
top-level `Use`, Slidesmith adds the authored `x` and `y` to every expanded body
coordinate. The `Use`'s `w` and `h` are its layout footprint; they do not resize
or distort the body. This rule keeps existing point geometry exact and avoids
implicit aspect-ratio decisions. If a different size is needed, define another
component or parameterize body dimensions with slots.

A `Use` inside `Stack` or `Grid` is one child. It omits `x` and `y`, supplies
the same `w`/`h` or `flex` data any other child would, and receives its computed
origin from the container before its body expands:

```xml
<Stack direction="row" x="36" y="80" w="648" h="120" gap="18">
  <Use id="revenue" component="stat-card" title="Revenue" value="$4.2M"
       w="200" h="120" />
  <Use id="margin" component="stat-card" title="Margin" value="34%"
       flex="1" h="120" />
</Stack>
```

Expanded IDs use the deterministic prefix `<Use-id>__`: `card` above becomes
`revenue__card`. Every descendant ID is prefixed, so repeated component
instances cannot collide. If `Use` omits `id`, Slidesmith uses
`use_<component-name>_<1-based-document-occurrence>` as the prefix. Explicit
`Use` IDs must be unique within the slide.

After push and re-pull, the expanded children are ordinary SML elements with
those IDs. Target a specific instance through the normal selector engine:

```bash
slidesmith select <ID> 'id=revenue__card'
slidesmith apply <ID> 'id=revenue__card' --add-class fill-#eef2ff
```

The re-pulled slide does not reconstruct `Use`, but its deterministic child IDs
remain selectable and editable.

Compilation is interleaved with layout rather than a blind before/after pass.
For a nested `Use`, the parent Stack/Grid first computes the Use frame; then
Slidesmith clones the component, interpolates slots, prefixes IDs, compiles any
Stack/Grid or `h="auto"` inside the relative body, translates the resulting
absolute elements by the Use origin, and splices them into the slide. Parsing,
diffing, QA, and request generation see only those plain absolute elements.
Google never sees `Use`, `Component`, layout containers, roles, or
`qa-accept-*` classes. Pull generation never emits `Use`, and a later pull does
not reconstruct component intent.

Unknown component names, unknown or missing required slots, duplicate Use IDs,
malformed placeholders, and malformed `components.sml` fail loudly. Unknown
component and slot errors list the currently available component or slot names;
all errors name the offending Use/component (and the components file where
applicable). A workspace with no `components.sml` and a slide with no authoring
constructs stays on the strict byte-identical passthrough path.

### Stack

Top-level stacks require `x`, `y`, `w`, and `h`. Attributes are:

- `direction="row|column"` (default `row`)
- `gap="points"` and `padding="points"` (default 0, never negative)
- `align="start|center|end|stretch"` (default `start`)
- `distribute="none|space-between"` (default `none`)

Children supply their fixed main-axis size (`w` in a row, `h` in a column), or
use a positive `flex` weight to divide remaining main-axis space. Non-stretched
children also need a cross-axis size. `stretch` assigns the cross-axis size.

```xml
<Stack id="cards" direction="row" x="36" y="80" w="648" h="180"
       gap="18" padding="12" align="stretch">
  <RoundRect id="card_a" w="180" class="fill-#ffffff stroke-#dddddd" />
  <RoundRect id="card_b" flex="1" class="fill-#f5f7fa stroke-none" />
  <RoundRect id="card_c" flex="2" class="fill-theme-accent1/15 stroke-none" />
</Stack>
```

### Grid

Top-level grids require `x`, `y`, `w`, `h`, and a positive integer `columns`.
`gap` is the equal horizontal and vertical gap. `row-h` fixes each row height;
without it, every child needs `h`, except `TextBox h="auto"`, and each row uses
its tallest child's height. Grid assigns each child's full cell frame.

```xml
<Grid id="metrics" x="48" y="90" w="624" h="220"
      columns="3" gap="12" row-h="96">
  <Rect id="metric_1" class="fill-#ffffff stroke-none" />
  <Rect id="metric_2" class="fill-#ffffff stroke-none" />
  <Rect id="metric_3" class="fill-#ffffff stroke-none" />
</Grid>
```

### Automatic text height

`h="auto"` works only on `TextBox`, requires a positive `w`, and uses the box's
`font-family-*`, `text-size-*`, and weight (`font-weight-*` or `bold`) to estimate
wrapped height. Measurement is deterministic and deliberately conservative, not
pixel-identical to Google Slides. Run thumbnail QA after using it.

```xml
<TextBox id="summary" x="54" y="310" w="420" h="auto"
         class="font-family-roboto text-size-16 font-weight-400">
  <P>Long copy is measured and the height is resolved before diffing.</P>
</TextBox>
```

### Nesting and one-shot semantics

Stacks and grids can nest. A nested container receives its frame from its
parent, so it does not declare `x`, `y`, `w`, or `h`; it declares its own layout
attributes. Layout containers may contain shapes or other layout containers,
but never appear inside `P` or `T`.

Children of either container must not declare `x` or `y`. Doing so raises:

```text
Element '<id>' inside Stack cannot declare x or y; its container assigns the position
```

This catches ambiguous intent instead of silently ignoring coordinates.

Layout is one-shot. Components are spliced, containers are flattened, `flex` is
removed, and `h="auto"` becomes a numeric height. No authoring construct becomes
a Slides object. After push and a later pull, only absolute elements remain;
layout intent is not reconstructed. Keep `components.sml` under version control
when reusable authoring intent must survive a re-pull.

## QA and intentional bleed

`slidesmith check` reports stable rule names with element IDs and suggested
fixes:

- `OVERLAP`: sibling leaves overlap by more than 15% of the smaller element.
  Exact containment is allowed because background shapes commonly contain text
  or icons.
- `OUT_OF_BOUNDS`: any element edge crosses the page boundary.
- `TEXT_OVERFLOW`: approximate wrapped text height exceeds box height by more
  than the 10% tolerance.

Only an explicit pull saves the offline findings snapshot to
`.pristine/qa-baseline.json`; a post-push pristine refresh does not change that
ledger. Later checks label current findings `NEW` (since the last pull) or
`PRE-EXISTING`, list findings that disappeared as `RESOLVED`, and summarize the
counts. Findings introduced by an edit therefore stay `NEW` after push until the
next explicit pull establishes a new reference point. Findings are warnings by
default; `--strict` exits 1 when any current finding exists.
Each current finding line includes a stable ID in the exact form
`RULE:slide:sorted-element-id[,sorted-element-id...]`. The numeric slide has no
zero padding, and element IDs are sorted lexically, so finding-list and SML
element order do not change the ID. Accept an intentional finding locally with
`slidesmith check <ID> --no-thumbnails --accept <finding-id>`; reverse that with
`--unaccept <finding-id>`. These flags may each be repeated, but cannot be mixed
in one command. `--accept` must name a current finding; `--unaccept` is
idempotent.

Local acceptances are stored in `<ID>/.qa/accepted.json` as a versioned
`accepted` object keyed by the stable finding ID. They stay in that workspace
across later pulls and checks, but do not travel with a fresh pull into a new
folder. Accepted findings still print with `[ACCEPTED]`; the active finding
summary and `--strict` exit status exclude them, and a separate `QA accepted:`
tally reports their count.

To carry the intent in committed SML, add `qa-accept-<lowercase-rule>` to any
element involved in the finding, for example `qa-accept-out-of-bounds`. The
next check matches that element and rule and creates the same accepted.json
entry. The class remains one-shot authoring sugar: the SML parser removes every
`qa-accept-*` token before typed style comparison and request generation, so it
is never sent to Google and may disappear when a push or pull regenerates SML.
If the class remains in local SML, remove it before using `--unaccept`, or a
later check will accept the finding again.

They are prompts for judgment, not automatic proof of a defect. Decorative
bleed is intentional when the element is clearly non-content (for example, a
large accent circle crossing one edge), the visible crop matches the design,
no text or essential mark is clipped, and the thumbnail confirms the result.
Do not dismiss an out-of-bounds text box, logo, data mark, or unexplained shape
as bleed. For overlaps, confirm the stacking is intentional and legible. For
text overflow, inspect the thumbnail and favor resizing/rewording over trusting
the approximation blindly.

An agent should report which findings were intentionally accepted and why.

## Authentication troubleshooting

Run:

```bash
slidesmith auth doctor
```

The doctor reports OAuth client discovery and its exact source, direct Keychain
readability (including the caught exception), file-store presence and token
expiry, a verdict, and one next command. It distinguishes missing OAuth client
credentials, denied/broken Keychain access, an absent session, and an expired
session.

`slidesmith auth login` always forces fresh browser consent. A refreshable
login stores its refresh token in the OS keyring and
`~/.config/slidesmith/session.json` when each is available. If Google withholds
a refresh token, the access-only session stores the short-lived access token
instead; it expires in about one hour. The file is mode 0600 in either mode.
OAuth client secrets are never copied into the session file.

### Auth recovery during a run

OAuth access tokens carry their known expiry. Slidesmith re-mints an OAuth
credential through its stored refresh token before a per-slide batch (and
before an atomic write when it is near expiry), then retries one request after
a 401 with the new bearer header. The same recovery applies to presentation
GETs and `batchUpdate`; a second 401 is terminal. Service-account credentials
are re-minted through `google-auth` when available.

`GOG_ACCESS_TOKEN` and `GOOGLE_WORKSPACE_CLI_TOKEN` are bare environment
tokens, so their expiry is unknown (about one hour is typical) and they cannot
be refreshed by Slidesmith. A terminal 401 explains that a fresh token must be
re-exported. For a long `--per-slide` push, the progress ledger is written
before the error; re-export the token and run `slidesmith push <ID>
--per-slide --resume` to pick up where it left off. An OAuth login where
Google withheld a refresh token is treated the same way: it remains usable for
about one hour, and `auth doctor` prints the revoke-at-permissions or own-client
remedy.

Slidesmith now requests only Google Slides, per-file Drive, OpenID, and email
scopes. Run `slidesmith auth login` once to re-consent after upgrading to this
scope set. Existing stored sessions issued with the previous broader scopes
continue to work until they are refreshed.

For an agent or subprocess that must avoid Keychain entirely:

```bash
SLIDESMITH_TOKEN_STORE=file slidesmith auth doctor
SLIDESMITH_TOKEN_STORE=file slidesmith pull "<presentation-url-or-id>"
```

Set `SLIDESMITH_TOKEN_STORE=keyring` to force Keychain reads. With no setting,
Slidesmith tries Keychain and, on any Keychain exception, emits one stderr notice
and falls back to the file. If the doctor says `CREDENTIAL ABSENT`, configure a
gws or gogcli OAuth client first; if it says `TOKEN EXPIRED`, `SESSION TOKEN
ABSENT`, or `KEYRING DENIED OR BROKEN`, run `slidesmith auth login` from a
browser-capable session and retry the agent command.
