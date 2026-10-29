# Fonts

Every font in this repository is openly licensed. `open/` contains 41 families
from Google Fonts, pinned to the commit in `synthocr/font_catalog.json`. Each family includes its SIL Open Font License. The catalog
pins font and license SHA-256 digests. Reinstall or verify with:

```sh
uv run synthocr fonts fetch
```

The additions cover Latin, Greek, Cyrillic, Arabic, Hebrew, Devanagari, Bengali,
Tamil, Thai, Ethiopic, Armenian, Georgian, Chinese, Japanese, Korean, Urdu,
Syriac, Tibetan, Khmer, Lao, Myanmar and more Indic scripts. Coverage varies by font; use
`synthocr fonts check` for actual characters.

To use your own fonts, add them in the app (**Look → Fonts → Add font…**), which
keeps them in its local workspace, or put TTF/OTF/TTC/OTC files in a directory
listed under `fonts.directories` in `config.toml`. Only add fonts to this
repository if their license allows redistribution, through `synthocr/font_catalog.json`.
The original user-supplied fonts were removed before the project was published
because their redistribution terms were unknown; they remain in Git history.
WOFF is a web format and is not passed to Pillow's FreeType renderer.

Font checking is part of SynthOCR. Inspect the bundled Kurdish reference
against the same configured fonts used for generation:

```sh
uv run synthocr fonts check --profile kurdish
uv run synthocr fonts check --profile sorani --require-any
uv run synthocr fonts check --profile kurmanji --require-any
```

The reference is preserved byte for byte in `synthocr/kurdish_characters.json`
from [font-checking](https://github.com/ardalan-jalal/font-checking/blob/edf290b2bbaa3393ff88b6499cad595737e04581/checking_chars.json),
commit `edf290b2bbaa3393ff88b6499cad595737e04581`, with SHA-256
`add629e50aa72be491979d18367c05db81e6b70ffd29922a570dc2b5d2571ff7`.
Its 88 entries include a space and a non-joining control; 86 require glyphs.
The upstream repository remains available for history; SynthOCR needs no
submodule, separate font folder or second checker.

Do not require every font to support every script. The integrated checker chooses a font per label
using compact Unicode bitmaps, retaining bounded memory for large CJK fonts.
Custom references remain supported with `--chars PATH`; use `--text` to check
a transcription. `--require-all` returns exit status 1 if any inspected font
lacks coverage. The reference profiles are optional audits, never generation allowlists.

`synthocr/font_profiles.json` adds practical Sorani and Latin Kurmanji profiles.
Core alphabet facts were checked against Unicode CLDR 49
([ckb](https://unicode.org/cldr/charts/49/summary/ckb.html),
[ku](https://unicode.org/cldr/charts/49/summary/ku.html)). Sorani includes three
digit sets; Kurmanji includes uppercase and lowercase letters. These are
selected audit repertoires rather than a complete CLDR export or spelling rule.
`--require-any` needs one font covering the full reference; it keeps script-specific
fonts available for other labels. Always check actual mixed-script text too.
