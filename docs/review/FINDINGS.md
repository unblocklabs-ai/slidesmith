# Full-project review findings — 2026-07-19

Four independent review passes over the whole codebase (dims: bugs/logic, security,
dead-code/over-engineering/duplication, modularization/consistency/test-gaps).
86 findings total. Fix batches at the bottom. Check off findings as they land.

Severity legend: CRITICAL = silent data corruption; HIGH = wrong behavior or failed
push in realistic use; MEDIUM = degraded/unexpected behavior; LOW = polish/hazard.

---

## PART 1 — BUGS & LOGIC (reviewer 1; repros verified against installed package)

### [x] B-C1 [CRITICAL] Text-edit range math computed against lossy SML projection — silent text corruption
Root cause chain: `content_generator.py:341,355-364` (`_generate_text_content` skips
empty/whitespace-only paragraphs, lstrip/rstrips paragraph outer whitespace, drops
autoText) and `content_parser.py:189` (`if para_text:` drops empty `<P>`). Pristine
`old_text` is missing characters/paragraphs that exist in the remote text.
`content_requests.py:442-500` computes UTF-16 FIXED_RANGE offsets from that lossy text.
Repro: remote `"Title\n\nBody\n"` → SML `<P>Title</P><P>Body</P>`; editing Body→Bodyz
emits insertText @10 → real deck becomes `"Title\n\nBodzy\n"`. Second repro: leading
spaces shift a delete to remove `'d li'` from the wrong place.
Fix: emit paragraphs losslessly (empty `<P/>`, no trimming; represent autoText), or
compute offsets against true remote paragraph texts from `.pristine/base.json`.

### [x] B-C2 [CRITICAL] MOVE resets scaleX/scaleY to 1 ABSOLUTE; resizes silently dropped
`content_requests.py:319-333` hardcodes scale 1. SML w/h is the visual bbox
(bounds.py composes size×scale), and tool-created shapes always have scale ≠ 1
(base 3000024 EMU × scale). A pure x/y move blows the element to intrinsic size and
destroys flips/shear. Also `content_diff.py:495-511` classifies w/h changes as MOVE
but the request carries no size — resize via editing w/h is silently ignored.
Fix: compute new scale from pristine base size (`target_emu / base_size_emu`),
preserve sign/shear, emit translate+scale; or RELATIVE translate for pure moves.

### [x] B-H1 [HIGH] Deleting a pristine Google GROUP + children fails push nondeterministically
`content_requests.py:254-306` `_order_deletes_for_safe_removal` infers hierarchy from
the `"_c"` ID-naming heuristic; real Google IDs don't match, ordering comes from set
iteration (hash-randomized). Group delete cascades in Google; later child delete hits
nonexistent ID → 400 → atomic batch rejects everything.
Fix: decide hierarchy from pristine element data; emit one deleteObject for the
top-most deleted ancestor, skip descendants. Deterministic ordering.

### [x] B-H2 [HIGH] Wrapper delete silently skipped for `_c`-named (copy-minted) children
`content_requests.py:288-301` assumes any ID prefixing another via `id + "_c"` is a
group that auto-deletes. Copy-created children are named `{parent}_c{depth}_{i}` and
survive round-trip as clean ids; deleting such a wrapper + children emits only child
deletes — the wrapper survives remotely. (Live pristine-wrapper delete worked only
because pristine Google IDs don't match the heuristic.)
Fix: same as B-H1 — group-ness from pristine types, not ID spelling.

### [x] B-H3 [HIGH] Copying a containment wrapper drops same-diff edits to original children
`content_diff.py:307-334`: first pass adds every descendant clean-id of any copied
element to `copied_group_descendant_ids`; change loop `continue`s on those IDs.
Repro: edit original label text + copy its wrapper card in one diff → only COPY
emitted; the TEXT_UPDATE is silently discarded (also moves/styles).
Fix: skip only the copy instances (match by identity/position), not every instance
sharing the ID.

### [x] B-H4 [HIGH] Zero-extent geometry: 0 EMU treated as unset by Google → default 3000024 EMU substituted; zero-height shapes emit singular scaleY=0
`content_requests.py:1216-1219` (line) passes h=0 straight through → Google replaces
with default size (the observed 236.22pt). `_create_shape_request:1179-1198` turns
h=0 into scaleY 0.0 → Google rejects, failing the whole batch.
Fix: clamp emitted EMU magnitudes ≥1 and floor scales away from 0, or reject zero
extents at parse time with a clear error.

### [x] B-H5 [HIGH] Copied subtree children double-translated when authored at final positions
`content_requests.py:747-763` adds translation (dx from content_diff.py:687-702) to
authored child positions; convention (children keep SOURCE positions) is undocumented.
Repro: card copied +300pt, child authored at x=310 → created at x=610.
Fix: document the convention AND detect children already moved by ≈ the root's delta
(treat as final positions, translation 0).

### [x] B-M1 [MEDIUM] Element IDs matching `^s\d+$` shadow slide clean-ids, corrupt slide mapping
`id_manager.py:69-78` preserves authored `s2` for a shape; `client.py:575-594` then
maps slide index to the shape's ID → creates target a shape as pageObjectId → 400.
Fix: exclude `^s\d+$` (and generated-pattern lookalikes `^[egml]\d+$`) from preserved
authored ids; build slide mapping from slide order, not name parsing.

### [x] B-M2 [MEDIUM] CREATE supports only 5 tags; everything else silently becomes RECTANGLE
`content_requests.py:2184-2192` — use the full `_tag_to_type` map (see D-D1 shared
module) and fail loudly on unknown tags.

### [x] B-M3 [MEDIUM] Stale slide folders resurrect remotely-deleted slides
Neither pull nor `_refresh_after_push` prunes `slides/NN/` dirs that no longer exist
remotely; `_read_current_slides` still parses them → wholesale CREATE of a new slide.
Fix: prune stale slide folders on pull/refresh (with care for user edits present).

### [x] B-M4 [MEDIUM] `_parse_float` swallows malformed numbers → typos become copy semantics
`content_parser.py:361-368` returns None on ValueError; `w="1O0"` → element treated
as a COPY and duplicated. Fix: raise with element id on unparsable position attrs.

### [x] B-M5 [MEDIUM] GROUP copy without children is a silent no-op; missing style → unstyled RECTANGLE
`content_requests.py:640-660`, `:571`, `_create_children_from_data` style lookup
misses degrade silently. Fix: error loudly on missing children/styles for copies.

### [x] B-M6 [MEDIUM] Font-family class round-trip mangles capitalization
`classes.py:574-577` emits `font-family-ibm-plex-sans`; parse `.title()`s to
"Ibm Plex Sans" — wrong family sent to API. Fix: preserve exact name (escaped class
value or sidecar).

### [x] B-L1 [LOW] Copy text styling applies first run's style to ALL copied text (`content_requests.py:1474-1528`)
### [x] B-L2 [LOW] `style_extractor._extract_color` truncates (int) vs units.rgb_to_hex rounds — 1/255 drift between pipelines (fold into D-D2)
### [x] B-L3 [LOW] Removed run styling never reset on text updates (TODO at `content_requests.py:462-465`)
### [x] B-L4 [LOW] credentials.py: callback port TOCTOU; result_holder read unlocked at deadline; FileSessionStore fd double-close hazard
### [x] B-L5 [LOW] qa contains(threshold=1.0) float >= flicker with 2-decimal SML rounding

### [x] B-EXTRA [HIGH] (from dims 6-8 review, behavioral) push succeeds but `_refresh_after_push` fetch fails → workspace silently inconsistent; no retry/backoff anywhere for 429/5xx
Fix: catch refresh failure → clear actionable warning ("push applied; workspace stale;
re-pull required") + do NOT leave half-refreshed state; add bounded retry/backoff for
429/5xx on GET paths (pull/refresh/thumbnails).

---

## PART 2 — SECURITY (reviewer 2)

### [x] S-M1 [MEDIUM] OAuth loopback callback: no CSRF `state`; extrasuite-session variant also lacks PKCE
`credentials.py:1284, :1615, :1687`. Login-CSRF via localhost code spraying during the
5-min window (session flow exchanges attacker code). Direct-Google flow protected by
PKCE. Fix: random `state` generated, sent, verified in both flows; add PKCE to the
session flow.

### [x] S-M2 [MEDIUM] Thumbnail contentUrl fetched with Bearer token attached, no host allowlist
`transport.py:175` — token-exfiltration primitive gated on response-body URL.
Fix: fetch contentUrl with a bare client (no Authorization) and assert an expected
Google host (e.g. *.googleusercontent.com) before requesting.

### [x] S-L1 [LOW] Reflected unescaped `error` param in callback HTML (`credentials.py:1706`) — html.escape it.
### [x] S-L2 [LOW] `_presentation_id` fallback returns arbitrary input as output dir name (`cli.py:16`) — validate `^[A-Za-z0-9_-]+$`.
### [x] S-I1 [INFO] Switch xml.etree → defusedxml for version-independent XXE/amplification protection (parser, layout, cli).
### [x] S-I2 [INFO] login() sends device fingerprint (MAC/hostname) to extrasuite server — documented; consider trimming.
### [x] S-EXTRA [MEDIUM] (from dims 3-5 review L3) `_OAUTH_USER_SCOPES` requests spreadsheets/documents/forms for a slides-only tool (`credentials.py:56-64`) — trim to presentations + drive.file + openid/email. Also user-facing messages say `extrasuite auth login` instead of `slidesmith auth login` (`credentials.py:1479, :1659`, `_NO_AUTH_MESSAGE`).

---

## PART 3 — DEAD CODE / OVER-ENGINEERING / DUPLICATION (reviewer 3; all grep-verified)

### [x] D-H1 [HIGH] Remove `pyfpgrowth` and pattern-mining vestiges
Disposition: **fixed**. Removed the unused dependency, `RenderNode.pattern_id`, and the stale pattern-hints module description.

### [x] D-H2 [HIGH] Remove the dead donor authentication surface
Disposition: **fixed**. Grep-confirmed and removed `logout`, `activate`, `status`, the public `auth_mode` property, dead `Credential` helpers, the no-op `force_refresh` argument, unused profile-selection machinery, and the obsolete free-port probe. The nonexistent `_NO_AUTH_MESSAGE` flags and donor command names were **already-resolved-by-b7e668a** and were not reintroduced.

### [x] D-D1 [HIGH] Canonicalize tag/type mappings
Disposition: **fixed**. Added `shape_types.py` with one 129-entry `TAG_TO_TYPE`, a derived `TYPE_TO_TAG`, and a derived 123-entry `VALID_GOOGLE_TYPES`; generator, parser/request, and validation paths now import them.

Reconciliation notes: the current generator and `_tag_to_type` tables were exact 129-entry inverses, so the generator spelling won without any key/value changes. The former inline valid set matched the same 123 creatable shape values; the derived set explicitly excludes the six source-specific non-shapes `GROUP`, `IMAGE`, `LINE`, `SHEETS_CHART`, `TABLE`, and `VIDEO`. No generator entry was dropped. The divergent five-entry `Rect`/`TextBox`/`RoundRect`/`Ellipse`/`Line` create table and its silent `RECTANGLE` fallback were **already-resolved-by-80075bc**; Batch C removes the remaining duplicated tables.

### [x] D-D2 [HIGH] Unify styles.json conversion with typed style models
Disposition: **fixed**. Persisted styles.json stays byte/schema compatible, while fill, stroke, line, and text-run replay now constructs `Color`/`Fill`/`Stroke`/`TextStyle` values and uses the class-derived request builders. `_parse_color` calls `units.hex_to_rgb`; malformed hex now raises `ValueError` instead of becoming black.

### [x] D-D3 [HIGH] Deduplicate copied-element request generation
Disposition: **fixed**. Extracted `_create_one_copied_element` from the current post-Batch-A root and descendant paths, including current translation, native-image geometry, text-run styling, recursive visual children, reserved IDs, and missing-group-data failures.

### [x] D-M1 [MEDIUM] Dead `pull_presentation`
Disposition: **fixed**. Removed the unused convenience function and package export.

### [x] D-M2 [MEDIUM] Dead `process_and_write`
Disposition: **fixed**. Removed after grep-confirming no callers.

### [x] D-M3 [MEDIUM] Dead `generate_presentation_content`
Disposition: **fixed**. Removed after grep-confirming no callers.

### [x] D-M4 [MEDIUM] Dead RenderNode donor surface
Disposition: **fixed**. Removed unused donor properties/methods, `flatten_tree`, and the unreachable empty-node branch; retained only members used by production or vendor contracts.

### [x] D-M5 [MEDIUM] Dead classes.py donor types
Disposition: **fixed** for `AutofitType`, `ArrowStyle`, and `LineCategory`. `Transform`, `Shadow`, and `parse_position_classes` are **kept-deliberately-with-reason**: the untouched vendor suite directly covers that compatibility surface, now documented in code.

### [x] D-M6 [MEDIUM] `_pristine_element_types`
Disposition: **kept-deliberately-with-reason**. Commit `80075bc` genuinely resurrected it: current delete ordering and line-style recovery use pristine type metadata to identify groups/lines without object-ID naming heuristics.

### [x] D-M7 [MEDIUM] `_create_full_text_replace_requests`
Disposition: **kept-deliberately-with-reason**. Commit `80075bc` made it the live `old_text is None` compatibility path for B-C1's minimal text-edit algorithm.

### [x] D-M8 [MEDIUM] Shipped `LocalFileTransport`
Disposition: **fixed**. Vendor tests still require it, so the class moved to `tests/vendor/helpers.py` with test-only injection; it is absent from the shipped package and `extraslide.__all__`. Vendor test files themselves are unchanged.

### [x] D-M9 [MEDIUM] Dead `ParsedElement.to_dict` / `has_full_position`
Disposition: **fixed**. Removed after current-tree grep verification.

### [x] D-M10 [MEDIUM] Permanent no-op image-style requests
Disposition: **fixed**. Removed `_apply_image_style_requests` and both calls; image creation retains native size/scale and recursive-child behavior.

### [x] D-M11 [MEDIUM] Dead bounds helpers
Disposition: **fixed** for `Transform.identity`. `BoundingBox.absolute_from` is **kept-deliberately-with-reason** because the untouched vendor suite directly verifies it as the public inverse of `relative_to`.

### [x] D-O1 [MEDIUM] Dummy transport required for local diff
Disposition: **fixed**. `SlidesClient` transport is optional for local diffing and explicitly required only by pull/push; `DummyTransport` is gone.

### [x] D-O2 [MEDIUM] Profile machinery without a CLI profile surface
Disposition: **fixed**. Removed public multi-profile selection/status/activation metadata and active-profile file handling. Internal session-store keys remain only to separate ExtraSuite, gws, and gogcli credentials.

### [x] D-O3 [LOW] Never-varying parameters
Disposition: **fixed** for `containment_threshold` and `Fill.to_class(prefix)`. `preserve_authored` is **kept-deliberately-with-reason** because direct `IDManager` callers and untouched vendor contracts require generated clean IDs by default, while presentation import deliberately opts into authored-ID reuse.

### [x] D-O4 [LOW] Dead defensive branches
Disposition: **fixed**. Removed constant-positive `base_size_emu` guards and the unreachable empty-attributes branch.

### [x] D-D4 [MEDIUM] `_serialize_children` versus `ParsedElement.to_dict`
Disposition: **fixed**. Removed dead `to_dict`; `_serialize_children` remains the sole copy serializer because it also carries pristine `sourcePosition` data.

### [x] D-D5 [MEDIUM] Duplicate COPY Change construction
Disposition: **fixed**. Both detection branches now call `_make_copy_change`.

### [x] D-D6 [MEDIUM] Repeated class probe-dispatch
Disposition: **fixed**. Added one typed `classify_class` dispatcher used by element, paragraph, run, and mutually-exclusive-family paths.

### [x] D-D7 [MEDIUM] Class grammar encoded three times
Disposition: **fixed**. Conflict-family classification now inspects the canonical typed parser result; the duplicate `_MUTUALLY_EXCLUSIVE_CLASS_FAMILIES` regex grammar is gone.

### [x] D-D8 [MEDIUM] Secure JSON writes duplicated and non-atomic
Disposition: **fixed**. One `_write_secure_json` creates a unique 0600 temp file and atomically replaces the target; both remaining session-file write paths use it. The third profiles.json writer disappeared with D-O2 instead of preserving a dead call site.

### [x] D-D9 [MEDIUM] Pristine zip creation duplicated
Disposition: **fixed**. Online pull/refresh and offline materialization share `create_pristine_zip`.

### [x] D-D10 [LOW] Paragraph common-class intersection duplicated
Disposition: **fixed**. Generator and style extraction share `common_classes`.

### [x] D-D11 [MEDIUM] Divergent `_read_json` helpers and inline copies
Disposition: **fixed**. Added one `read_json(path, missing_ok=...)` object loader and migrated all source-tree JSON-file reads; callers must state missing-file behavior explicitly.

### [x] D-D12 [LOW] Token-endpoint form POST duplicated
Disposition: **fixed**. Authorization-code and refresh-token exchanges share `_post_form_json` and `_GOOGLE_TOKEN_URL`.

### [x] D-D13 [LOW] Object-ID grammars split across modules
Disposition: **fixed**. Google create-ID validation now lives with authored/generated ID grammars in `id_manager.py`; request allocation imports the shared validator.

### [x] D-L1 [LOW] Root-wrap backward compatibility
Disposition: **kept-deliberately-with-reason**. Pre-`<Slide>` workspaces remain loadable for migration and are normalized by the next pull/materialization; the branch is labeled accordingly in code.

### [x] D-L2 [LOW] `Change.tag` versus `metadata["tag"]`
Disposition: **fixed**. `Change.tag` is now the single representation in diff construction, summaries, style routing, and create requests; the tag-only metadata field was removed.

---

## PART 4 — MODULARIZATION / CONSISTENCY / TEST GAPS (reviewer 4)

### T-H1 [HIGH] Three divergent shape-type tables (== D-D1; one fix).
### T-H2 [HIGH] content_requests.py (2279 lines) split along existing seams: text_requests.py, copy_requests.py, class_style_requests.py, element_factories.py; orchestrator stays ~350 lines. Zero circular-import risk verified.
### T-M1 [MEDIUM] generate_batch_requests does 7 jobs inline — extract _bucket_changes, _emit_new_slide_requests, per-type emitters.
### T-M2 [MEDIUM] client.py push() concentrates diff+guard+lock+refresh — move guard helpers + ConflictError to conflicts.py; _refresh_after_push to a workspace-refresh helper.
### T-M3 [MEDIUM] credentials.py 1763 lines / five concerns — split into auth/stores.py, auth/browser_flow.py, auth/discovery.py, auth/doctor.py. (Do AFTER security fixes land.)
### T-M4 [MEDIUM] cli.py cmd_check embeds thumbnail engine — extract qa.download_thumbnails(transport, folder, qa_dir).
### T-M5 [MEDIUM] content_diff.diff_presentation 235 lines with duplicated COPY construction (== D-D5) — extract _make_copy_change, _split_original_and_copies.
### T-L1..L4 [LOW] copied_group_ids written never read; _id_counter global state → IdAllocator; duplicate _read_json (== D-D11); builtin shadowing (`id`, `format`).

### Consistency
### T-C1 [HIGH] credentials.py raises bare Exception in 10 places — add AuthError/SessionExpiredError hierarchy (transport.py already has the pattern).
### T-C2 [MEDIUM] Three stderr warning shapes — standardize `warning: ` prefix; staleness message currently unprefixed (highest value).
### T-C3 [MEDIUM] CLI no-op wording differs (diff vs push) — cmd_push should print resp["message"].
### T-C4 [MEDIUM] Library code prints to stderr (client.push) — return structured warnings in response; cli prints.
### T-C5..C8 [LOW] Docstring style divergence; `_pristine_element_types` naming; Change.tag duality; hex parsing ×3 (== D-D2); vendor-vs-wave test style note.

### Test gaps
### [x] T-G1 [HIGH] Zero coverage of _handle_http_error branches; NO retry/backoff for 429/5xx anywhere; push-succeeds-refresh-fails scenario untested (== B-EXTRA — fix + tests together).
### [x] T-G2 [HIGH] Group/deep-copy request generation untested end-to-end (translation math, groupObjects, nested recursion) — contract test from golden fixture.
### [x] T-G3 [HIGH] _order_deletes_for_safe_removal untested + nondeterministic (== B-H1/H2 — fix + tests).
### [x] T-G4 [HIGH] Conflict guard: remote-slide-deleted branch and group-copy childrenObjectIds collection never executed by tests.
### T-G5 [MEDIUM] Layout: all ten error branches, distribute, auto-row-height, empty-container-vanishes untested (parametrized tests; compile_layout is pure).
### [x] T-G6 [MEDIUM] UTF-16: combining-char edit test; old_text=None fallback; styling-removed path (== B-L3).
### T-G7 [MEDIUM] QA lint on Lines (divider-crossing-box false positive — decide+pin: likely exempt LINE from OVERLAP) and nested Groups; zero-area skip; box.w<=0 branch.
### T-G8 [MEDIUM] Auth store modes: invalid SLIDESMITH_TOKEN_STORE, keyring-forced-unavailable, corrupt session.json silent loss, both-stores-fail, legacy format.
### [x] T-G9 [MEDIUM] Copy/child ID minting bypasses reserved_object_ids until after build — route through allocator + collision test.
### T-G10 [LOW] CLI contracts: ConflictError→exit 2, top-level error→exit 1, _warn_if_stale corrupt-metadata silence, _read_qa_baseline invalid branch.

---

## FIX BATCHES (dependency order)

- **Batch A — correctness core** (bugs Part 1: B-C1, B-C2, B-H1..H5, B-M1..M6, B-L1..L5, B-EXTRA + tests T-G1/G2/G3/G4/G6/G9). Touches generator/parser/diff/requests/client/transport.
- **Batch B — security** (Part 2 all). Touches credentials/transport/cli. Before the credentials split.
- **Batch C — dead code + duplication** (Part 3 all; includes shape_types.py shared module, style-pipeline unification, helpers). Coordinate with Batch A changes.
- **Batch D — modularization + consistency** (Part 4 T-H2, T-M1..M5, T-C1..C8, T-L*). Structure-only, tests as safety net.
- **Batch E — remaining test gaps** (T-G5, T-G7, T-G8, T-G10 + anything A-D missed).
- **Re-review** after E: fresh review pass; iterate until zero findings.
