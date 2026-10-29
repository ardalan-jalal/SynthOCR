---
name: validate-fonts
description: Inspect multilingual glyph coverage, add licensed font families, and audit SynthOCR fonts against bundled reference profiles or supplied text.
---

Read `AGENTS.md`. Use the configured font directories for both inspection and
generation. Use `uv run synthocr fonts list` to inventory usable, deduplicated
TTF, OTF and collection faces.

For focused Kurdish audits, run:

```sh
uv run synthocr fonts check --profile sorani --require-any
uv run synthocr fonts check --profile kurmanji --require-any
uv run synthocr fonts check --profile kurdish
```

The last command retains the original upstream reference unchanged. Sorani
checks three digit sets; Kurmanji checks the Latin alphabet in both cases.
These profiles are practical audits, not language detectors or allowlists.
Check actual text for borrowed letters and Arabic-script Kurmanji.

Use `--text` for a sample or `--chars` for another UTF-8/JSON character source.
`--require-any` checks that at least one face covers the whole reference.
`--require-all` makes every-font coverage an explicit audit requirement; it is
not a generation requirement. Do not discard a font merely because it lacks
unrelated characters. Strict generation chooses a font per transcription.

Fonts added in the web app are stored in the workspace's `.jobs/fonts` folder,
validated with Pillow/fontTools and de-duplicated by content; they are the
user's own and never join `fonts/` or the catalog. `fonts list` prints each
face's ID for `--font` and `fonts.include`.

Use `uv run synthocr fonts fetch --family FAMILY` for catalog additions. Catalog
updates must pin an upstream commit, record SHA-256 for each font and license,
and retain the OFL text. Keep existing user fonts; do not claim licenses for
them without evidence.

Cmap coverage does not prove shaping or appearance. Render and inspect samples
with joiners, marks and mixed direction when assessing a font. Keep RAQM and
grapheme-safe chunking, and never delete unsupported text from the raw corpus.
Use configured font directories on every platform; a contributor's installed
system font is not a portable dependency. RGB backgrounds, blur and rotation
also need image inspection: adequate glyph coverage cannot prove useful
contrast or intact marks. See `docs/augmentation.md` and the `run-experiments`
workflow for a repeatable, licensed-font comparison. Keep the chosen font hash
and source case ID with a demonstrated failure so another contributor can
reproduce it without that contributor's home directory or browser state.

Use `synthocr kurdish --check --language both` for a reusable Kurdish rendering
audit with automatic verification and explicit case completeness. Use `--language
sorani` or `--language kurmanji` for a narrower audit. It includes every core Sorani letter in
four logical contexts, Kurmanji letter cases, words, marks, keyboard variants,
join controls, digits and mixed direction. It tests common patterns rather than
certifying a dictionary or all Kurdish varieties. Inspect the case IDs and
feature tags in attribution; encoding probes are intentionally distinct from
word examples. Use `tests/test_spelling.py` for regressions. An alphabet or
catalog change needs a new spelling snapshot version; preserve existing raw v1.

Font checking and its Kurdish reference are included in the installed SynthOCR
package. No submodule is required. The shared bitmap-based checker is authoritative. Ignore Unicode's true
default-ignorable characters, while still requiring glyphs for visible format
signs such as U+0600 and U+06DD. Add a regression when adjusting this distinction.
