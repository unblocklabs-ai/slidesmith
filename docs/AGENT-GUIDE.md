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
inspect and validate them before writing remotely:

```bash
slidesmith diff <ID>
slidesmith check <ID> --no-thumbnails
slidesmith check <ID>
slidesmith push <ID>
```

`diff` is local-only and prints the exact request list. `check --no-thumbnails`
is also local-only. Plain `check` downloads current slide thumbnails into
`<ID>/.qa/` before running geometry QA, so it needs authentication. `push`
re-fetches the remote deck, aborts if a locally touched object changed remotely,
uses a revision lock for the write, and refreshes the local projection after a
successful batch. Use `push --force` only when the user explicitly accepts
overwriting concurrent edits to touched properties.

If `diff`, `push`, or `check` says the workspace was pulled more than 24 hours
ago, re-pull before continuing. A re-pull replaces the projection; preserve any
uncommitted SML edits first.

## Workspace and SML structure

```text
<ID>/
├── presentation.json       title, page size, slide count, revisionId, pulledAt
├── id_mapping.json         clean SML IDs -> Google object IDs
├── styles.json             pulled style details used by diff and QA
├── slides/NN/content.sml   one XML document per slide
├── .pristine/              baseline used by diff and conflict detection
├── .raw/                   optional raw pull response
└── .qa/                    thumbnails created by check
```

Each `content.sml` has one `Slide` root. Elements use point-valued `x`, `y`, `w`,
and `h` attributes. Preserve IDs when modifying pulled elements: identity drives
the diff and conflict guard. Paragraphs are `P` nodes. A paragraph may contain
plain text and styled `T` runs; only text-family classes are valid on `T`.

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

Pulled decks may contain nested `Group` elements and many Google shape tags;
keep the pulled tag unless intentionally replacing an element. Common authoring
tags are `Rect`, `RoundRect`, `Ellipse`, `TextBox`, `Line`, and `Group`. Unknown
tags fall back to a rectangle when created, so do not guess tag names.

XML rules still apply: escape `&`, `<`, and `>` in text; quote attributes; keep
`T` inside `P`; and do not put layout containers inside `P` or `T`.

## Class vocabulary

The accepted vocabulary below is derived from the parsing functions in
`src/extraslide/classes.py`. Class names are whitespace-separated. Numeric
dimensions are points. Hex colors are exactly six digits and include the `#`.
Opacity is an integer suffix after `/` (normally 0–100).

Theme names accepted by the color model are `dark1`, `light1`, `dark2`,
`light2`, `accent1` through `accent6`, `text1`, `text2`, `background1`,
`background2`, `hyperlink`, and `followed-hyperlink`.

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
or on a `T` run for an explicit range style.

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

Paragraph classes are element-level in current SML; they apply through the
shape's paragraph style.

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

## Layout authoring

`Stack`, `Grid`, and `TextBox h="auto"` are authoring conveniences compiled to
absolute SML during `diff` or `push`.

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

Layout is one-shot. Containers are flattened, `flex` is removed, and `h="auto"`
becomes a numeric height. No container becomes a Slides object. After push and a
later pull, only absolute elements remain; layout intent is not reconstructed.
Keep reusable authoring intent elsewhere if it must survive a re-pull.

## QA and intentional bleed

`slidesmith check` reports stable rule names with element IDs and suggested
fixes:

- `OVERLAP`: sibling leaves overlap by more than 15% of the smaller element.
  Exact containment is allowed because background shapes commonly contain text
  or icons.
- `OUT_OF_BOUNDS`: any element edge crosses the page boundary.
- `TEXT_OVERFLOW`: approximate wrapped text height exceeds box height by more
  than the 10% tolerance.

Findings are warnings by default; `--strict` exits 1 when any finding exists.
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

`slidesmith auth login` always forces fresh browser consent. A successful login
mirrors the long-lived session or refresh token to the OS keyring and
`~/.config/slidesmith/session.json` when each is available. The file is mode
0600. OAuth client secrets are never copied into the session file.

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
