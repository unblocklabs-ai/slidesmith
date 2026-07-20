# Milestone notes

## M3 — Per-slide resumable push (done)

- Added `slidesmith push <folder> --per-slide` with ordered, revision-locked
  slide batches and per-slide progress.
- Added `.push-progress.json` failure ledgers and hash-validated
  `--per-slide --resume` behavior.
- Preserved the default one-batch atomic push path and the existing one-time
  post-push refresh and persistence verification.
