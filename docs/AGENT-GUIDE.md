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
slidesmith check <ID> --no-thumbnails
slidesmith push <ID>
slidesmith check <ID>
slidesmith check <ID> --contact-sheet
```

`diff` does not call Google APIs and normally reads only the local workspace.
The sole network exception is an authored `Image` with `fit="contain"`: the diff
engine performs a bounded, unauthenticated image fetch to determine its pixel
dimensions. `fit="stretch"` does not fetch the source. `diff` prints the exact
request list plus a stderr legend from Google object IDs to clean SML IDs.
`diff --summary` replaces that raw JSON with a compact per-slide view of deletes,
creates, moves, copies, style changes, and text edits, followed by the total
generated request count. It is intended for a quick human review; use plain
`diff` whenever the exact API payload matters.
`check --no-thumbnails` is also local-only.
Plain `check`, run after push, downloads current slide thumbnails into
`<ID>/.qa/` before running geometry QA, so it needs authentication. `push`
re-fetches the remote deck, aborts if a locally touched object changed remotely,
uses a revision lock for the write, and refreshes the local projection after a
successful batch. Use `push --force` only when the user explicitly accepts
overwriting concurrent edits to touched properties.

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

## Authoring images from URLs

Create an image with an HTTP or HTTPS `src`, an authored point-valued frame,
and optional `fit="stretch|contain"`:

```xml
<Image id="hero_image" src="https://picsum.photos/1200/675"
       x="48" y="72" w="624" h="351" fit="contain" />
```

`stretch` is the default Google Slides API behavior: the image fills the
authored box and may be distorted. `contain` preserves the source aspect ratio.
Only `contain` causes the diff engine to download the image. That fetch sends no
credentials, rejects non-public destinations at every redirect hop, is pinned to
the validated DNS address, and is limited to 25 MB and 100 million pixels before
Pillow inspection. Slidesmith reads the bounded image dimensions and shrinks
either the authored width or height. The resulting frame stays anchored at the
authored top-left `x`, `y` position. Authored `x`, `y`, `w`, and `h` must all be
finite and strictly positive for both `stretch` and `contain`.

Google's `cropProperties` are **READ-ONLY via the API**, so `fit="cover"` is
impossible. Slidesmith rejects `cover` instead of pretending it can create that
result. Crop the source image before authoring when a cover treatment is needed.

The `createImage` API requires a publicly fetchable URL. The image must be less
than 50 MB and less than 25 megapixels. A URL following the
`https://picsum.photos/<width>/<height>` pattern, such as the example above, is
useful for testing. Private URLs, local files, data URLs, and URLs requiring an
authorization header are not supported.

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

`slidesmith auth login` always forces fresh browser consent. A successful login
mirrors the long-lived session or refresh token to the OS keyring and
`~/.config/slidesmith/session.json` when each is available. The file is mode
0600. OAuth client secrets are never copied into the session file.

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
