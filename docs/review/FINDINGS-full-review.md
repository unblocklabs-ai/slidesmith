# Full-codebase GPT-5.6 review — 2026-07-20 (stage-15+)

Four parallel gpt-5.6 reviews over all of src/slidesmith/. Triaged into DEFECTS
(fix, tests-first) vs STRUCTURE (pure large-file splits — real debt, not defects;
deferred to a deliberate pass). Loop: fix → re-review until no real defect remains.

## CONFIRMED REAL DEFECTS — fix

### Correctness / bugs
- [x] FR-1 [HIGH] `replace_image` computes geometry via `get_bounds(child)` without
  composing the parent-group transform (client.py:986) — grouped-image replacement
  mispositions. Compose ancestor transform chain. VERIFIED.
- [x] FR-2 [HIGH] `conflicts.py index_presentation` discards ancestry (:39-44) — a
  remote change to an ancestor GROUP of a locally-edited child is not detected by
  the conflict guard. Record parent chain; conflict check must include ancestors of
  touched objects. VERIFIED.
- [x] FR-3 [MEDIUM] Extreme contain ratios round a positive dimension to 0 EMU
  (element_factories.py:375-389 via units.pt_to_emu truncation) → scaleX/Y 0 invalid
  request. Clamp effective contain dims to ≥1 EMU or reject with a clear error.
- [x] FR-4 [MEDIUM] Post-upload Drive permission-request failure orphans the created
  file (assets.py:227-243) — no compensating delete. Delete on permission failure,
  raise typed error.
- [x] FR-5 [MEDIUM] `assets._request` malformed Drive JSON raises `ValueError` while
  every adjacent Drive failure raises `AssetUploadError` (assets.py:268). Wrap
  `response.json()` decode in AssetUploadError.
- [x] FR-6 [MEDIUM] `browser_flow` missing-refresh-token raises `RuntimeError` not the
  established `AuthError` (browser_flow.py:126). Raise AuthError.

### Consistency / contracts
- [x] FR-7 [MEDIUM] Plain `diff` prints prose "No changes detected." breaking its JSON
  stdout contract (cli.py:130) — an agent parsing `diff` as JSON gets prose on the
  empty case. Emit `[]`; reserve prose for `--summary`.
- [x] FR-8 [MEDIUM] `ConflictError` → exit 2 for push but exit 1 for replace-image
  (cli.py:227 vs generic handler). Handle ConflictError centrally → exit 2.
- [x] FR-9 [MEDIUM] `push --per-slide` progress goes to stdout, unlike all other
  diagnostics on stderr (cli.py:204). Move progress to stderr; stdout = final result.
- [x] FR-10 [LOW] Preflight warning uses `push preflight warning:` not canonical
  `warning:` prefix (cli.py:193). Normalize; update the test that locks the wording.
- [x] FR-11 [LOW] `download_thumbnails` hard-codes `print` while sibling QA APIs inject
  an `output` callback (qa.py:64). Add output callback.

### Dead code
- [x] FR-12 [MEDIUM] `diff_presentation`'s `_id_mapping` param is never read
  (content_diff.py:284); all callers pass it. Remove param + update call sites.
- [x] FR-13 [MEDIUM] `Credential` over-modeled — only `.token` is read; provider/kind/
  expires_at/scopes/metadata unused across 5 construction paths (credentials.py).
  Trim to what's used.
- [ ] FR-14 [LOW] Delete dead + un-justified-whitelist symbols and prune the vulture
  whitelist: `_GOOGLE_SCOPE_PREFIX` (credentials.py:63), `self._access_token`/
  `self._timeout` (transport.py:132-133), `_QueryParser.query` (selector.py:210),
  `FormatResult.paths` (formatting.py:21), `PropertyState.RENDERED` + `ThemeColorType`
  (classes.py:25,30-48), `_placeholders_cover_braces` unused `matches` param
  (components.py:206), `_fetch_image_dimensions` passthrough (content_diff.py:1046),
  redundant local-source check (assets.py:79), `_number_attr` default never varies
  (layout.py:572).

### Duplication with real drift risk (extract shared helpers)
- [x] FR-15 [HIGH] `workspace.materialize` vs `client.pull` build workspaces via
  divergent pipelines; the public materializer omits stale-slide pruning, base
  revision snapshot, and QA-baseline — with save_raw=False default, a later push
  lacks the conflict base and degrades remote-change protection. Extract one
  `materialize_workspace(...)`.
- [x] FR-16 [HIGH] Default push and per-slide push independently implement the
  safety-critical fetch → conflict-guard → revision-lock → batch_update → refresh →
  persistence sequence (client.py:889-941 vs 1138-1212). Extract a shared guarded-
  execute + finalize so a safety change reaches both modes.
- [ ] FR-17 [MEDIUM] Atomic text-file commit duplicated in class_replacement.py vs
  selector.py (theme/snippet import selector's private `_commit_text_files`). Extract
  `engine.atomic_files.commit_text_files`.
- [ ] FR-18 [MEDIUM] CREATE (element_factories) vs COPY (copy_requests) element
  construction duplicate LINE/IMAGE/shape dispatch + text/paragraph/run replay.
  Extract shared `emit_recreated_element`.
- [x] FR-19 [MEDIUM] Three parallel raw-API tree traversals (conflicts / client index /
  push_progress). Extract one `iter_page_elements(data)`; build all three indexes from
  it. (Composes with FR-2's ancestry fix.)
- [ ] FR-20 [MEDIUM] Ancestor-in-group-set check duplicated (content_requests.py:406
  vs copy_requests.py:285). Extract `has_ancestor_in_set(...)`.

## TEST GAPS — add coverage (name the silent-failure branch)
- [ ] TG-1 [HIGH] push_progress ledger ownership/schema validation (:232-241) untested —
  a corrupt/foreign/wrong-version ledger could let `--resume` skip slides. Tests:
  corrupt JSON, wrong version, foreign presentation id, malformed succeeded list →
  abort before any transport call.
- [x] TG-2 [HIGH] grouped-image replace geometry (client.py:986) — add translated/
  scaled/nested-group image fixtures asserting slide-coordinate correctness (pairs
  with FR-1).
- [x] TG-3 [HIGH] conflicts ancestor-group change (conflicts.py:39) — parent-group
  transform with locally-edited child must conflict (pairs with FR-2).
- [ ] TG-4 [MEDIUM] selector multi-paragraph text join (selector.py:83) — define + test
  the separator so `["foo","bar"]` doesn't match `text=foobar`.
- [ ] TG-5 [MEDIUM] theme --map-colors exact-threshold (`>` boundary) + alpha-suffix
  preservation (theme.py:595-603).
- [ ] TG-6 [MEDIUM] transport backoff timing (monkeypatch asyncio.sleep, assert bounded
  delay sequence; non-retryable never sleeps) + batch_update 429 policy (POST path).
- [ ] TG-7 [MEDIUM] keyring malformed-but-valid JSON (list / nonnumeric expires_at) —
  align KeyringSessionStore invalid-token handling with FileSessionStore.
- [ ] TG-8 [MEDIUM] copy-style UTF-16 replay (copy_requests.py:856) — emoji + combining
  char across runs/paragraphs, assert every start/end index.
- [ ] TG-9 [MEDIUM] component duplicate-child-ID rejection (components.py:218).
- [x] TG-10 [MEDIUM] extreme-contain zero-EMU (pairs with FR-3).
- [x] TG-11 [MEDIUM] Drive post-upload permission failure cleanup (pairs with FR-4).

## STRUCTURE — deferred (real debt, NOT defects; deliberate pass, not this loop)
Large single-file splits proposed by dim-6, no behavioral defect: client.py → workspace_reader/push_executor/persistence/image_replace; copy_requests → duplicate_/recreate_/styles_json_adapter; content_diff → diff_model/summary/copy_detection/style_delta/image_geometry; selector → query/selection/sml_class_edit/atomic_files; cli → cli_commands/*; theme → schema/extract/apply/color_mapping; layout → measure/algorithms/components. The dedup extractions above (FR-15..20) already carve out the drift-risk seams; the rest is readability and can be done safely later.

## DIMS 1-2 (bugs/security — security verdict CLEAN)
- [x] FR-21 [MEDIUM] "offline"/"local-only" commands (`diff`, `check --no-thumbnails`,
  `push --preflight`) perform a network HTTP GET for remote `fit="contain"` images
  (content_diff.py:1029,1046 via get_effective_position). Violates the advertised
  no-network contract; fails outright in air-gapped/CI. Fix: in the offline lint/QA
  path, approximate or skip contain aspect for REMOTE sources (local files still read
  dims from disk); reserve network fetch for push/replace-image. SSRF-safe already —
  this is a contract bug.
- [x] FR-22 [LOW] Copied-image near-degenerate geometry: element_factories.py:351
  `scale_x = target_w_emu / native_w` lacks the `_nonzero_scale()` floor its sibling
  factories have → scaleX=0 on sub-0.08pt width. Add the floor. (Same family as FR-3.)
- Security dimension: CLEAN (SSRF image-fetch + Drive-upload, component/theme injection,
  OAuth state/PKCE, secrets, resume-ledger — all verified safe).
