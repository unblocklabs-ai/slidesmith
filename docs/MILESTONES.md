# Milestone notes

## M4 — Local image assets and replacement (done)

- Added local-path and `file://` image authoring with push-time Drive upload.
- Added `.assets.json` reuse keyed by resolved local path plus content SHA-256.
- Added `slidesmith replace-image` for revision-locked replacement from either
  local files or public URLs, with explicit contain/stretch geometry pinning.
- Kept `fit="cover"` explicitly unsupported because Slides crop properties are
  read-only.

## M3 — Per-slide resumable push (done)

- Added `slidesmith push <folder> --per-slide` with ordered, revision-locked
  slide batches and per-slide progress.
- Added `.push-progress.json` failure ledgers and hash-validated
  `--per-slide --resume` behavior.
- Preserved the default one-batch atomic push path and the existing one-time
  post-push refresh and persistence verification.
