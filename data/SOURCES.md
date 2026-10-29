# Raw corpus provenance

Raw source files are immutable inputs. Processed chunks, indices and images are
derived artifacts. Do not apply the license of one source to unrelated files.

## One active corpus per language or dialect

Preparation creates one disk-backed corpus index, `data/processed/texts.sqlite3`,
and a small `data/processed/corpora.json` registry with **one entry per canonical
language tag**. Raw snapshots are historical source material for those corpora,
not separate choices or duplicated processed corpus files. Known language-code
aliases share the same entry. A macrolanguage is broader than a specific member
language: Mandarin (`cmn`) stays separate from generic Chinese (`zh`), Iranian
Persian (`pes`) from generic Persian (`fa`), and Swahili (`swh`) from its generic
macrolanguage (`sw`). Sorani (`ckb`), Kurmanji (`kmr`), Southern Kurdish (`sdh`),
generic Kurdish (`ku`), and explicit regional or script subtags remain distinct.
Unknown source language stays in an explicit unlabelled bucket.
These distinctions follow the
[IANA language subtag registry](https://www.iana.org/assignments/language-subtag-registry/language-subtag-registry).

Some frozen early imports used those three broader tags. Their original bytes
remain unchanged. Preparation recovers a specific identity only from the exact
reviewed macro/member pair in `attribution.source_language`, or from explicit
source metadata in the companion `data/corpora.json` when the registered file's
SHA-256 matches. The original broad tag is retained as `original_language` in
processed attribution. Arbitrary conflicting labels cannot override a record's
declared language. A custom or mounted raw collection without a companion
catalog uses only its per-record metadata and keeps unresolved generic tags.
Mount the pinned source catalog alongside frozen raw snapshots when their
historical broader hints need this lossless source resolution.

Deduplication uses both language and normalized text. Identical text in different
languages remains available in each language. Every source occurrence and its
attribution is retained in the SQLite origins table and streamed with selected
records into generated datasets; deduplication never throws another author's
credit away. Diagnostic spelling probes are tagged separately. When the same
word also occurs in ordinary source text, ordinary generation uses that source
as its primary record while preserving both origins.

The SQLite index is the default processed text store. Set `data.write_jsonl =
true` only when a separate processed `texts.jsonl` copy is needed. Both the
index and registry can be rebuilt from unchanged raw sources.

## Your own text

Text without a recorded source and license is not published here. Put your own
TXT files in `raw/` and give each one a language in `config.toml`, for example
`source_languages = {"my-text.txt" = "ckb"}`. JSONL/CSV language annotations
override this collection default, and unlabelled sources are never inferred
from their script or filename.

## Tatoeba snapshot: 2026-10-24

`raw/tatoeba/2026-10-24/` contains **180,199 sentences across 20 languages** from
the official detailed sentence exports. Each row retains the exact original
sentence, owner username, sentence ID, source URL, creation/modification dates,
and a CC-BY-2.0-FR license link. Rows without an attributed owner were skipped.
Selection takes the first 10,000 attributed records per language in export order;
languages with fewer records contribute all available attributed records. This
is a deterministic convenience selection, not a frequency-balanced or
independently proofread linguistic corpus.

Credit **[Tatoeba](https://tatoeba.org) and the contributors named in each record**.
Tatoeba text is released under **CC BY 2.0 France**. Retain this attribution when
redistributing the data or derived image datasets. The `modified` flag becomes
true when normalization or chunking changes text. CSV, ImageFolder and Tesseract
exports retain attribution alongside labels.

- [Official downloads and field definitions](https://tatoeba.org/en/downloads)
- [Using the Tatoeba corpus](https://en.wiki.tatoeba.org/articles/show/using-the-tatoeba-corpus)
- [CC BY 2.0 France](https://creativecommons.org/licenses/by/2.0/fr/)

`corpora.json` records URLs, compressed archive SHA-256, raw JSONL SHA-256,
language codes, limits and actual counts. Tatoeba uses ISO 639-3; record language
hints use BCP 47 tags. Some frozen historical hints are broader macrolanguage
tags; their precise source identities are resolved from the explicit metadata
described above. Exports change weekly. Saved
compressed archives live in `.local/corpora/2026-10-24/` and are ignored by Git;
the selected frozen raw JSONL is included. Re-running the same snapshot verifies
and reuses it. Choose a new date to add a newer snapshot; never replace an old
raw file to make its checksum pass.

## Tatoeba expansion: 2026-10-25

`raw/tatoeba/2026-10-25/` adds **136,092 attributed source records in 16 immutable
snapshots**, taken from the official detailed exports published on 24 October
2026. The same CC BY 2.0 France terms, contributor attribution and convenience
selection limitations above apply. Source language codes are retained in each
new record's attribution as well as the pinned catalog metadata.

Fourteen languages or dialects gain natural source material:

- Greek (`ell` → `el`), Eastern Armenian (`hye` → `hy`), Ukrainian (`ukr` →
  `uk`), Polish (`pol` → `pl`), Portuguese (`por` → `pt`), Dutch (`nld` → `nl`),
  Italian (`ita` → `it`), Indonesian (`ind` → `id`), Azerbaijani (`aze` → `az`)
  and Uyghur (`uig` → `ug`): 10,000 attributed sentences each.
- Georgian (`kat` → `ka`): 7,173; Amharic (`amh` → `am`): 196; Swahili (`swh`):
  4,583; Southern Kurdish (`sdh`): 1,050. These selections contain all
  attributed sentences available in those exports.

The remaining two snapshots expand Sorani to **12,685** and Kurmanji to
**10,405** attributed sentences, using every attributed record in those exports.
Their first 10,000 sentences overlap the earlier frozen selections. This adds
3,090 source sentences beyond the earlier Kurdish snapshots; the processed
corpus deduplicates that overlap on disk while retaining both source records.
Southern Kurdish remains separate and is not relabelled as Sorani or Kurmanji.

There are now **316,291 frozen Tatoeba source rows** across both dates, before
deduplication and grapheme-safe chunking. Historical snapshots are not an
additional active corpus for the same language. `corpora.json` pins archive and
raw-file SHA-256 values, counts, source URLs, conversion versions and licenses.
Old conversion versions reproduce their original bytes. No existing raw file
was edited or replaced by this expansion, and no evaluation benchmark was used.

## SynthOCR templates v2

`raw/synthetic-v2/` contains **400,000 synthetic records**: 50,000 each for English,
Sorani Kurdish, Arabic, Persian, German, French, Spanish and Turkish. Each record
is marked synthetic with generator version, seed and record index. New template
vocabulary and generated template data are provided under
[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). This does not change
the rights of the original user data or Tatoeba sentences.

Seeds: `en=100`, `ckb=101`, `ar=102`, `fa=103`, `de=104`, `fr=105`, `es=106`,
`tr=107`. Reproduce into a **new** path with:

```sh
uv run synthocr text --count 50000 --genre mixed --language ckb --seed 101 --output /tmp/ckb-reproduction.jsonl
```

The eleven genres cover prose, receipts, dates, identifiers, web URLs, numeric
strings, addresses, measurements, arithmetic, contact details and table rows.
Prose uses authored compositional templates and is not claimed to be natural
corpus text. Addresses and contact details are synthetic. Sites and email
addresses use the reserved `example.org` domain. `raw/examples.jsonl` contains
additional authored multilingual checks.

## Kurdish spelling cases v1

`raw/kurdish-spelling-v1/` contains **274 distinct cases: 176 Sorani (`ckb`) and
98 Latin Kurmanji (`kmr`)**. Of these, 154 are authored words or phrases and 120
are explicitly labelled diagnostic probes. These examples and probes are
provided under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/).
They are synthetic source material, not an independently proofread dictionary,
natural corpus or held-out evaluation benchmark. No repetition is used to
inflate the case count.

`synthocr/kurdish_spelling.json` stores grouped examples and references. Core
alphabet probes are derived from the same `font_profiles.json` used by font
audits, checked against Unicode CLDR 49
([Sorani](https://unicode.org/cldr/charts/49/summary/ckb.html),
[Kurmanji](https://unicode.org/cldr/charts/49/summary/ku.html)). Unicode probes
exercise joining and marks described in
[Unicode's Arabic chapter](https://www.unicode.org/versions/Unicode16.0.0/core-spec/chapter-9/).
These references inform the repertoire and rendering checks; they do not
certify the authored words or every regional spelling.

Every record retains a case ID, feature tags, kind, version and catalog SHA-256.
Kinds separate words, joining and case probes, encoding variants, marks and
formatting. Original Unicode is retained in raw data, including intentionally
different keyboard encodings, join controls and decomposed accents. Preparation
uses the configured normalization without rewriting raw files.

Reproduce into **new** paths:

```sh
uv run synthocr spelling --language ckb --output /tmp/sorani-spelling-v1.jsonl
uv run synthocr spelling --language kmr --output /tmp/kurmanji-spelling-v1.jsonl
```

Frozen SHA-256 values:

- Catalog: `484aa9b32cf7fd76946a7e0c48c9df0fa3b439523ed2ed11b8066ff63adc1eec`
- `ckb.jsonl`: `aa6125dbe36fce5de44a3aaa120e54a609d9224c4342bff87e32741ee85a49b9`
- `kmr.jsonl`: `6a57426648d2c44fa6f24420c67ed918793241fbdb152312ee9d8770eb715ff7`

Tests reproduce both snapshots byte for byte. Future catalog or alphabet changes
must use a new version and raw snapshot directory instead of overwriting v1.
For a case-complete rendering run independent of the ordinary corpus, use
`synthocr generate --suite kurdish-spelling --samples 1000 --output dataset/spelling-001`.

## Evaluation

The default split hashes exact normalized text. It keeps the same label in one
split across font variants. It does not guarantee document, near-duplicate,
translation-pair or author isolation. Use an externally held-out corpus when
evaluating a model. No FLORES benchmark data was imported.
