# Reproducible experiments

This directory describes maintained comparisons. Generated images and reports
belong in ignored `.local/experiments/` or a directory chosen by the caller.
They are not raw training data, downloaded benchmarks or automatic model
training. Useful findings become narrow tests and documented decisions.

## Compare augmentation presets

From the repository root:

```sh
uv sync --locked
uv run synthocr experiment --output .local/experiments/backgrounds --samples 12 --seed 42
```

The output must be new or empty and separate from raw/processed storage.
`--samples` accepts 1–64 diagnostic cases, with one clean image plus every document recipe per case. At least
two cases include both Sorani and Kurmanji; a single case is Sorani. The source
cases are selected deterministically from the bundled Kurdish reference with
separate language quotas. The default prioritizes combining marks, joiners,
mixed direction, digits and accented/capital letters before filling remaining
slots by seeded hash rank. A small selection does not cover the full catalog.

Each case uses the same font and seed for a clean original and every document
variant in the shared catalog (currently twelve). See [document recipes](../docs/augmentation.md)
for their surfaces and effects. Layout limits and font size
come from the supplied config. This is a controlled preset comparison; it
does not apply a custom background selection from that config. Run ordinary
`generate` for the exact appearance configured for your dataset.

The command reads no training corpus and makes no network request. Fonts must
be in configured directories and accompanied by `OFL.txt`; fetch catalog fonts
with the maintained font command if needed. The command records that file's
hash; its presence does not independently establish redistribution rights for
an arbitrary user font. Review the source/license when adding a family.
Every case retains its source ID,
language, feature tags, probe/word kind and CC0 attribution. Intentional encoding
probes are kept as diagnostics rather than silently normalized into words.

## Read the output

- `config.json` contains effective preset settings, seed and cache limits.
- `cases.jsonl` contains the selected attributed source records in logical order.
- `manifest.jsonl` links each render to its case, language, font hash, seed,
  image hash, applied settings and structural measurements.
- `images/` contains original PNG renders. Each published PNG is decoded to
  confirm that saved dimensions agree with render metadata.
- `contact-sheet.png` and numbered continuation sheets show up to the first 12
  cases in four columns, at most twelve rows per page. The report lists every
  sheet and checksum. Each canvas has a fixed upper bound; sheets are written
  and closed one at a time, without retaining every full-size image in memory.
- `report.json` records completion status, source/config/artifact hashes,
  font/license hashes, tool versions, counts, bytes, timings and parent peak
  RSS where available. An error leaves an explicit incomplete report and is
  returned as a failed command. Retry into a fresh directory.

These are experiment artifacts, not a training dataset accepted by
`synthocr verify`. Generate and verify a normal dataset to validate the entire
pipeline and all export provenance. Use `kurdish --check --language both` for
complete bundled Kurdish reference coverage.

Image checksums are reproducible for the same software/font environment and
settings; changes to Pillow, FreeType or shaping libraries may change pixels.
Compare logical records and structural invariants across platforms rather than
requiring a universal image hash. Timing and memory fields are observational,
so complete report files are not expected to be byte-identical.

## Decide what the result supports

Bounding-box margins detect edge contact. Corner luminance helps inspect the
background frame. Luminance minima/maxima/mean describe pixels. The configured
base-color contrast calculation catches obvious color collisions. None of
these establishes downstream OCR accuracy, native spelling correctness or
universal readability. Inspect the full image beside its logical label.

`parent_peak_rss_bytes` is the process-lifetime RSS high-water mark on supported
platforms, not a difference between presets. It is unavailable through this
mechanism on Windows. Run each comparison in a fresh process, keep fonts/cache
limits identical and measure operating-system/container peak memory separately
for hosting. This command uses one process and no worker pool.

Use `synthocr benchmark --samples 1000 --workers 1` for pipeline throughput;
compare another worker count only with aggregate worker memory measured too.
Each preset's `processing_seconds` includes rendering, pixel metrics, PNG
encoding, disk writes, decoded-image checks, hashing and annotation writing.
The report's `elapsed_seconds` also includes font selection and contact sheet
creation; initial font discovery and evidence setup are excluded. Do not
compare those timings with a pipeline benchmark that excludes different work.

## Turn an observation into shared knowledge

A useful finding states:

1. **Question and fixture:** what failed, the exact command and fixed seed,
   source case ID, font hash and relevant settings.
2. **Observation:** the decoded image or measurement that demonstrates it;
   distinguish an actual result from a proposed explanation.
3. **Correction and regression:** the smallest implementation fix and test
   asserting its meaningful invariant.
4. **Limits:** what was not measured, including OCR performance, other scripts,
   another operating system, browser memory or a hosted container.

Record durable findings in the relevant document, such as
[`docs/augmentation.md`](../docs/augmentation.md), and update the applicable
project skill when the finding changes its workflow. Keep generated absolute
paths, large image collections and machine logs out of versioned documentation.
Versioned instructions and tests preserve demonstrated lessons for future
contributors; they do not modify an agent's model weights.
