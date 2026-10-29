---
name: generate-dataset
description: Prepare raw corpora, generate multilingual text-image datasets, and export training views in this SynthOCR repository.
---

Read `AGENTS.md` and `config.toml`. Use the maintained `synthocr` CLI; inspect
`uv run synthocr --help` and the relevant subcommand help when needed. Without a
local `config.toml`, the CLI uses the personal workspace from `synthocr setup`
(see `synthocr/home.py`); relative `--output`/`--dataset` paths then refer to
the current folder.

For the web app, run `uv run --extra web synthocr ui`. It uses the same generator
and verifier, and saves into a folder the user picks in the browser. See
`docs/ui.md` for text sources, language shares, upload rules and saving;
`docs/hosting.md` covers the private server.
Use `improve-ui` when changing this workflow rather than maintaining UI rules
here. Storage resolves from configuration or `--output-root`, never a particular
developer's checkout. Keep original upload bytes with the dataset and preserve
honest cancellation/failure status.

For ordinary Kurdish work, start with `uv run synthocr kurdish`. It selects
Sorani, applies NFC, strict per-label font coverage and automatic direction,
then verifies every generated image. Use `--language kurmanji` or `--language
both` for other selections. Each invocation picks a fresh output unless an
explicit `--output` is supplied. The normal Kurdish path excludes the entire
`spelling` genre; use `kurdish --check --language both` for all 274 diagnostic
cases. This check rejects partial coverage and keeps contexts whole. See
`docs/kurdish.md`. Use general `generate` for custom Unicode policies or other
languages; its optional `--exclude-genre` filter is applied on disk.

Keep corpora in `data/raw/` unchanged. Accept TXT lines, CSV with a `text` column,
or JSONL containing `text`, optional `language`, `genre` and `attribution`. Add new genres with
`synthocr text` or supplied corpora rather than a temporary script. Use
`uv run synthocr prepare` to produce the disk-backed processed index and
`corpora.json`: exactly one active corpus per canonical language or explicit
dialect in the shared SQLite store. Keep raw snapshots as immutable provenance.
Processed JSONL is an optional duplicate (`data.write_jsonl`), disabled by default.
Preserve specific language codes rather than folding varieties into macrolanguages.

For a new configuration, generate a small sample into a new output directory,
inspect representative images and their manifest labels, then run the requested
volume. Use `--samples`, `--seed`, `--workers` and `--output` for run overrides.
One worker uses the least memory; measure before increasing the worker count.
For background/color controls and effect ranges, read `docs/augmentation.md`.
For renderer changes, use `run-experiments` and the required checks in
`docs/development.md`. Preview and generation share the same effective settings;
a successful comparison is structural evidence, not OCR accuracy or linguistic
correctness.
Retain strict font coverage unless the user specifically wants missing glyphs.
For Sorani, use `generate --language ckb`; repeat `--language kmr` to include
Kurmanji. This selects exact source tags, ignoring case. Add
`--language-weight ckb=70 --language-weight kmr=30` for fixed shares: they are
exact quotas while text lasts (a language that runs out is filled from the
others, so report actual counts), omitted languages weigh 1 and 0 excludes one.
Without weights, languages are sampled as one pool in proportion to their text.
Repeat `--font FILE` (file name, `NAME#FACE` or the ID from `fonts list`) to
use only certain fonts; this sets `fonts.include` and the run records exactly
those faces. Set explicit
`data.source_languages` for reviewed TXT sources; leave unknown languages
unlabelled. Per-record language takes precedence. Do not confuse the source
filter with `generation.language`, which only supplies a shaping hint.
For common Sorani and Latin Kurmanji spelling cases, run
`synthocr generate --suite kurdish-spelling --samples 1000 --output NEW_PATH`.
The bundled 274-case suite uses its own temporary corpus, retaining case IDs,
feature tags and words/probe kinds in attribution. With default processing and
the supplied fonts, at least 274 samples cover every case; smaller requests
select a subset. Source-language filters still apply. Use `synthocr spelling`
to export the cases, or add `--words-only` to exclude diagnostic probes.
Keep words distinct from logical letter-context and encoding probes; neither
is an independently proofread dictionary. Run `tests/test_spelling.py` when
changing Kurdish text handling. Never rewrite a frozen raw spelling snapshot;
bump the version and create a new snapshot for catalog or alphabet changes.
Run `synthocr verify --dataset OUTPUT` after generation; it checks every image,
annotation, checksum, identity and text split. Preserve per-record attribution.
Use `synthocr preview --output .local/preview.png` for a reproducible multilingual
showcase using licensed fonts and the same renderer. Inspect actual run images
as well when validating a changed generation configuration.

Use `synthocr corpora` for versioned Tatoeba imports. Read `data/SOURCES.md` and
`data/corpora.json`; choose a new snapshot date for new data and never overwrite
raw sources to bypass checksums. Upstream export URLs are mutable: reproduce old
snapshots from saved compressed archives or reuse the frozen raw JSONL.

Use `synthocr export` for CSV, ImageFolder or Tesseract views. Preserve logical
Unicode label order and text-based splits. A failed run is incomplete; inspect
its `stats.json` and choose a fresh destination after correcting the cause.
New datasets retain all selected origins in `origins.jsonl`, linked by `text_id`
and a label checksum. Preserve that sidecar in every export; the manifest's primary
attribution alone may omit other sources. Verify the label and primary attribution
against the sidecar, and reject symlinked artifacts. Report actual generated counts
and the output path, not only requested counts.
