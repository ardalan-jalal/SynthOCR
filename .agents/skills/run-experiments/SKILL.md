---
name: run-experiments
description: Run reproducible SynthOCR rendering and resource comparisons, inspect the evidence and turn demonstrated failures into maintained regression tests.
---

# Compare a rendering change

Read `AGENTS.md`, `experiments/README.md` and `docs/augmentation.md`. Use the
maintained CLI, shared `Renderer` and bundled attributed cases. No new service,
model or corpus download is needed for a rendering comparison.

## Ask a falsifiable question

Name the observable change before measuring: for example, whether an expanded
gradient has a white frame, whether zero padding clips combining marks, or
whether gallery decoding grows with all images rather than the current page.
Choose a small fixture that can reveal that failure. Keep text, source case IDs,
font hashes, seed, format, bounds, worker count and cache limits equal when
comparing implementations. Use a new output directory for every run.

Run the built-in augmentation comparison:

```sh
uv run synthocr experiment --output .local/experiments/augmentation-before --samples 12 --seed 42
uv run synthocr experiment --output .local/experiments/augmentation-after --samples 12 --seed 42
```

The command compares a clean original and every registered document recipe on the
same deterministic selection of Sorani and Kurmanji cases. Inspect
`contact-sheet.png` and its numbered continuation sheets, individual images, `manifest.jsonl` and `report.json`.
Selections with at least two cases include both varieties. Use `kurdish --check
--language both` for complete reference coverage; a small experiment is not a
substitute for that audit.

## Interpret evidence conservatively

Decoded dimensions, bounding-box margins, pixel statistics and image hashes
support structural checks. They do not establish linguistic correctness,
legibility for all readers or downstream OCR accuracy. Inspect the images and
logical labels together. Do not interpret intentional joiner/encoding probes as
natural training sentences. Do not use an evaluation benchmark as new training
data.

Use the metric definitions in `experiments/README.md`. Parent RSS is a
process-lifetime high-water mark, not per-variant allocation, browser memory or
total worker memory. Use a fresh process per comparison and record
system/container peak memory separately when assessing hosting. Compare runs
on the same host with the same measured work; repeat meaningful measurements,
keeping I/O and preparation differences explicit.

For throughput use the separate maintained benchmark:

```sh
uv run synthocr benchmark --samples 1000 --workers 1
uv run synthocr benchmark --samples 1000 --workers 2
```

Do not claim a memory reduction from different font sets, sample counts or
worker settings. Preserve bounded caches and disk-backed selection while
investigating. Do not add a one-off corpus-sized array or parallel task queue to
make an experiment faster.

## Retain what the evidence taught

Keep generated reports, images and machine-specific paths in ignored `.local/`
or an explicit external directory. Record a useful finding in the relevant
maintained document with the command, invariant, observed failure and limitation.
Add a focused regression to `tests/test_augmentation.py`, `test_experiments.py`
or the affected API tests when the failure can be asserted reliably. Avoid
timing thresholds and image hashes tied to a particular Pillow/FreeType build.

Update the existing workflow only when the finding changes a future decision.
This is versioned project knowledge and regression coverage, not automatic
self-training. Re-run the focused test, experiment and project checks after a
fix. Leave failed experiment evidence marked incomplete and choose a fresh
destination for a retry.

The comparison requires an accompanying `OFL.txt` and records its hash. This is
license evidence, not an independent audit of arbitrary user font rights; check
catalog provenance and redistribution terms when adding a family.
