# Kurdish: generate, check, use

After `uv sync --locked`, generate Sorani images with:

```sh
uv run synthocr kurdish
```

This prepares the supplied corpus, generates 1,000 images and verifies them.
The result prints the new output folder under `dataset/`. Run it again for a
fresh folder. Raw text stays unchanged. The supplied configuration uses one
worker to keep memory low.

Choose another language or a larger run:

```sh
uv run synthocr kurdish --language kurmanji
uv run synthocr kurdish --language both --samples 10000
uv run synthocr kurdish --output dataset/my-sorani-run
```

Outputs are images plus `manifest.jsonl`, with the text, language, font and
attribution for every image. Use these with any recognition model, or use the
general `synthocr export` command for CSV, ImageFolder or Tesseract views.

## Check the difficult cases

```sh
uv run synthocr kurdish --check --language both
```

This renders and verifies **274 cases: 176 Sorani and 98 Latin Kurmanji**. It
checks every selected case ID. Omit `--language both` to check only Sorani.
Each case is kept whole even when ordinary corpus lines use shorter wrapping.
The check uses a temporary corpus, so it also works before preparing your
training data. Its output folders start with `check-`.

The examples include initial vowels, ی/ێ, ر/ڕ, ل/ڵ, و/ۆ/وو, ه/ە, compounds,
ezafe, definite and indefinite endings, plurals, possessive suffixes and verb
prefixes. Kurmanji examples include ç, ê, î, ş, û, uppercase forms, ezafe and
oblique endings. Core alphabets were checked against Unicode CLDR 49
([Sorani](https://unicode.org/cldr/charts/49/summary/ckb.html),
[Kurmanji](https://unicode.org/cldr/charts/49/summary/ku.html)).

There are 154 authored word/phrase examples and 120 labelled diagnostic probes.
Probes exercise letter contexts, keyboard encodings, joining, marks, digits,
punctuation and mixed direction. They are deliberately separate from normal
Kurdish training: `kurdish` excludes the entire `spelling` genre. Export the
authored examples without probes if you need them separately:

```sh
uv run synthocr spelling --language ckb --words-only --output .local/sorani-words.jsonl
```

## Preserve the spelling

`kurdish` uses canonical NFC normalization, strict font coverage and automatic
direction. A suitable font is chosen for each label. RAQM handles shaping;
labels stay in logical reading order.

Distinct letters and encodings remain distinct: ر/ڕ, ل/ڵ, ی/ێ, ک/ك, ی/ي/ى
and ه/ھ/ە. Joiners, combining marks and bidi controls are preserved. Digits
remain useful samples, including `123`, `١٢٣` and `۱۲۳`. The command does not
translate, transliterate or automatically correct spelling. Use the general
`generate` command when you need custom normalization or direction.

Rendering checks establish that labels are supported, visible and structurally
valid. The authored examples still need native linguistic review before use
as a proofread spelling resource. The original Sorani news collection includes
Arabic quotations; its collection-level language tag does not independently
certify each sentence. Curate source annotations when you need that distinction.

## Add your own text

Add a UTF-8 JSONL file under `data/raw/`, one record per line:

```json
{"text":"سڵاو جیهان", "language":"ckb"}
{"text":"Ez bi kurdî dinivîsim.", "language":"kmr"}
```

Run `uv run synthocr kurdish` again. Changed source hashes trigger preparation
automatically. For a reviewed TXT file, add its relative path to the existing
`[data] source_languages` map in `config.toml`, for example
`source_languages = {"my-text.txt" = "ckb"}`.
JSONL/CSV record languages take precedence. Unlabelled sources are not guessed
from their script, and misspelled source paths are rejected.

For focused font inventory checks:

```sh
uv run synthocr fonts check --profile sorani --require-any
uv run synthocr fonts check --profile kurmanji --require-any
```

These audit the reference alphabets rather than restricting accepted text. A
font only needs the characters in its selected transcription. The unchanged
upstream `kurdish` font profile remains available for comparison. Font provenance
is in [fonts/README.md](../fonts/README.md); corpus provenance is in
[data/SOURCES.md](../data/SOURCES.md).
