# Develop SynthOCR

SynthOCR generates text images and attributed annotations for recognition
models. The Python package owns the behavior. The CLI and optional FastAPI app
call the same processing, font, rendering, verification and export functions.
There is no frontend framework, JavaScript package manager or separate font
checking application.

## Set up a checkout

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), clone the
repository and run these commands from its root:

```sh
uv sync --locked --extra web --group dev
uv run synthocr --help
uv run --extra web synthocr ui --no-browser
```

Open the loopback URL printed by the server. The command syntax works in a Unix
shell or PowerShell; activating a virtual environment is unnecessary. For
CLI-only use, omit `--extra web`. The repository's `.python-version` selects the
development interpreter; Python 3.11 through 3.15 are supported. Keep dependencies in
`pyproject.toml` and the resolved `uv.lock` together.

Pillow must report RAQM support for multilingual shaping. The project uses
official wheels where suitable. On macOS with Python 3.15, the pinned Pillow
source override requires native build libraries; the CI setup installs FreeType,
HarfBuzz, FriBiDi, libraqm, JPEG, TIFF and WebP libraries. Follow that platform's
build setup or use a supported Python 3.11–3.14 environment. A source build may
need a compiler as well. On macOS with Python 3.11–3.14, the Pillow wheel loads
FriBiDi at runtime: Homebrew's Python finds Homebrew's `libfribidi`, but
uv-managed interpreters do not, so prefer Homebrew's Python there (for example
`--python /opt/homebrew/opt/python@3.13/bin/python3.13`). Check the installed result with:

```sh
uv run --extra web python -c "from PIL import features; assert features.check_feature('raqm')"
```

On Windows, Pillow's wheel includes RAQM but loads FriBiDi at runtime, looking
for `fribidi.dll`, `fribidi-0.dll` or MSYS2's `libfribidi-0.dll` on `PATH`. CI
installs `mingw-w64-ucrt-x86_64-fribidi` with the runner's preinstalled MSYS2
and adds `C:\msys64\ucrt64\bin` to `PATH`; users can do the same.

The renderer rejects missing RAQM. Diagnose the environment rather than
disabling shaping or substituting a contributor's installed fonts. CI defines
the full checks on Linux and macOS plus a focused Windows 3.14 job for output
locations, filesystem locking and verified web generation. Check those job
results before making platform-validation claims.

All configured paths are relative to the configuration file, not the shell's
current directory. Use a separate config for another installation:

```sh
uv run --extra web synthocr --config deployment/config.toml ui --no-browser
uv run synthocr --config deployment/config.toml generate --output dataset/example
```

Here `deployment/config.toml` is an illustrative user-created file. Create it
from `config.toml`, and set its raw, processed, font and output paths for that
installation. Do not commit home directories or require a particular checkout
name. See [hosting](hosting.md) for a container and persistent volumes. The UI
remains on loopback; a remote user can reach it through a private tunnel. A
successful local smoke test does not validate a public deployment.

## Keep one source for each responsibility

The repository has one application package and one collection of project
workflows. Use these locations rather than creating parallel implementations:

```text
synthocr/            reusable Python behavior and bundled reference resources
synthocr/static/     browser UI served by the same package; no separate app build
tests/               regressions for that package and its public entry points
data/raw/            immutable source snapshots
data/processed/      reproducible SQLite store and one corpus entry per language
fonts/               shared font assets, catalogs and redistribution licenses
dataset/             generated datasets, ignored by Git
docs/                user guides and this contributor guide
experiments/         maintained experiment instructions, not generated results
.agents/skills/      canonical task-specific agent workflows
.local/              disposable local evidence and caches, ignored by Git
```

`AGENTS.md` owns project-wide invariants. `CLAUDE.md` imports that file, and
`.claude/skills` is a symlink to `.agents/skills`; neither is a second instruction
set. Keep the link rather than copying skills. On a checkout without symlink
support, use the canonical `.agents/skills/` paths directly.

The README introduces the product and links to detailed guides. User-facing
behavior belongs in `docs/ui.md`, augmentation semantics in
`docs/augmentation.md`, and reusable experiment output/methodology in
`experiments/README.md`. Skills describe how to carry out a task and reference
those definitions. Add a new document only when it has a distinct reader or
responsibility; avoid mirrors of configuration, API models or existing guides.

## Find the behavior to change

- `home.py` finds the configuration: `--config`, then `./config.toml`, then the
  personal workspace that `synthocr setup` creates for installed copies. Setup
  only uses the maintained font fetch, Tatoeba importer and text templates.
  `font_catalog.json` (pinned open-font downloads) ships in the package.
- `config.py` defines typed settings and common validation. `cli.py` exposes
  maintained reusable operations; add subcommands here rather than one-off
  scripts.
- `text.py`, `corpus_store.py` and `corpora.py` prepare immutable raw inputs,
  consolidate one active corpus per explicit language/dialect in shared SQLite,
  and retain all source origins. `data/corpora.json` pins source snapshots;
  generated `data/processed/corpora.json` describes active corpora.
- `fonts.py` discovers fonts and provides the bounded glyph coverage checker
  used by inspection and generation. `spelling.py` exposes attributed Kurdish
  reference cases and distinguishes words from diagnostic probes.
- `render.py` performs RAQM layout, ink masking and augmentation. `pipeline.py`
  schedules bounded work, writes images/manifests/origins, and exports views.
  `verify.py` checks decoded artifacts and provenance before export.
- `web.py` owns validated HTTP requests, disk-backed uploads and job state,
  loopback/root confinement and bounded disposable caches. `static/index.html`,
  `app.css` and `app.js` supply semantic controls, presentation and interaction;
  the small `theme.js` bootstrap applies the saved theme before first paint.
  UI previews call the shared renderer; JavaScript must not recreate effects.
- `experiments.py` compares deterministic diagnostic renders and writes visual
  evidence. It does not train or evaluate a recognition model.

Keep a new feature complete across its actual entry points. A rendering option
needs common validation, effective render metadata, CLI/config support and the
same values in preview and job requests. A hidden control should not submit an
unexpected active option. A custom upload retains its original bytes with the
output; normalization and filtering happen in reproducible processed storage.

Use a single-column Text → Look → Generate flow. The page offers the image
presets, a small Customize panel and a preview; other tuning options (exact
mixes, effect strengths, splits, seed, workers) stay in the CLI, configuration
and API. The browser saves the verified export into a folder the user picks
(Chrome/Edge) or downloads a ZIP; show save and download errors inline.
`config.background_distribution` resolves selected recipe weights
into conditional probabilities; the UI presents shares of the entire dataset.
Keep that distinction in estimates and annotations, and preserve the legacy
seeded choices when weights are omitted. See [augmentation](augmentation.md)
for the public weighting contract rather than duplicating it in each caller.

Fonts added in the app go to `<output-root>/.jobs/fonts` and join discovery
for preview and generation; `GET/POST/DELETE /api/fonts` lists, adds and
removes them, and a chosen subset reaches the pipeline as `fonts.include`.

The server generates into its own workspace (`--output-root`, plus optional
`--output-location`s) and never receives or opens folder paths from the user's
computer. To save a dataset, the page asks the server to prepare the verified
export (POST on the download URL, which returns only a same-origin URL and
filename), streams that stored ZIP and writes each entry into the folder the
user picked with `showDirectoryPicker`, checking every CRC and refusing a
nonempty destination. Other browsers use a native download. Never fetch a whole
ZIP into browser memory, and keep `_stored_zip`'s layout (stored entries with
sizes in the local headers) that the page relies on. Preserve confinement, locks
and the one active-job boundary across every API entry point. See
[the UI guide](ui.md) and [hosting](hosting.md).

## Choose the project workflow

Project skills are versioned at `.agents/skills/`; `.claude/skills` links to the
same definitions. They describe repository-specific decisions, not a particular
developer's machine. Read the workflow relevant to the task:

- [generate-dataset](../.agents/skills/generate-dataset/SKILL.md): source
  preparation, attributed generation, Unicode handling and verified exports.
- [validate-fonts](../.agents/skills/validate-fonts/SKILL.md): shared coverage,
  licensing and visual inspection of multilingual shaping.
- [improve-ui](../.agents/skills/improve-ui/SKILL.md): understandable controls,
  API consistency, accessibility, bounded browser behavior and actual UI QA.
- [run-experiments](../.agents/skills/run-experiments/SKILL.md): matched rendering
  comparisons, resource evidence and focused regression promotion.

The root `AGENTS.md` holds project-wide invariants. Keep conditional procedures
in the relevant skill or documentation. Update a workflow when a demonstrated
failure changes what the next contributor should check. Avoid adding another
copy of the same rule to each agent system.

## Validate a change

Run focused tests while editing, then the complete required checks once the
change is ready. Repeat a check when a later edit affects what it validated:

```sh
uv sync --locked --extra web --group dev
uv run --extra web --group dev ruff check .
uv run --extra web --group dev ruff format --check .
uv run --extra web --group dev pytest
node --test tests/ui.test.cjs
uv run --extra web --group dev pip-audit
uv lock --check
uv build
```

The UI behavior tests use Node's built-in runner; there is no JavaScript dependency
installation or frontend build step. Only the theme preference is stored in
browser local storage. Format HTML, CSS and JavaScript with Prettier 3 (four
spaces, from `.editorconfig`): `npx prettier@3.3.3 --check synthocr/static tests/ui.test.cjs`.

For compatibility, run the same tests with `uv run --isolated --python 3.11
--extra web --group dev pytest` (the oldest supported version). The isolated environment avoids replacing
another running contributor/test process's `.venv`. Keep `--extra web` when
returning to UI work. Do not update a lockfile just because a test command
omitted the extra.

Rendering changes need actual image inspection, the relevant augmentation/font
tests, an [experiment](../experiments/README.md), and a benchmark. Inspect ink
margins, background edges, text direction, combining marks and decoded formats.
Run `uv run synthocr kurdish --check --language both` to verify all bundled
Kurdish reference cases. Preserve the separate linguistic-review limitations
in [the Kurdish guide](kurdish.md).

UI changes need the optional web dependencies and an actual browser smoke test
of the changed interaction. For a visual change, check a fresh page, the changed
controls, keyboard access and a small viewport; a full generation/upload/save
tour is unnecessary. Add relevant API tests when request handling changes, and
check generated metadata when preview or generation settings change. Exercise
cancellation or upload failure only when that behavior is affected. Include
dark default, persisted theme choice and folder saving when changing those
controls. Headless browsers cannot click the folder dialog; replace
`showDirectoryPicker` with an init script returning a folder from
`navigator.storage.getDirectory()` and read the written files back. Use fresh output and an alternate port/root if another user has the
app open. Never stop an unrelated server or delete an existing dataset to make
a smoke test pass.

For language shares, check that totals stay 100, a zero share excludes the
language and the generated label counts match (shares are exact quotas while
text lasts). For Customize, compare the chosen levels with generated metadata.
Background mixes are probabilistic: use fixed seeds to check reproducibility,
but do not assert exact counts from them.

For storage or memory claims, distinguish original datasets, processed SQLite,
disposable download caches, parent RSS, all worker processes and browser memory.
The download cache limit is not an original-dataset disk quota. Report what was
measured and what remains untested, including an unavailable container daemon.

## Text processing

To add your own text, put UTF-8 files anywhere under `data/raw/`: TXT (one
text per line), CSV with a `text` column, or JSONL such as
`{"text": "سڵاو جیهان", "language": "ckb", "genre": "prose"}`. Tag each
record's language; nothing is guessed from the script. Then run
`uv run synthocr prepare`. To add fonts, put them in `fonts/` (see
`fonts/README.md` for licenses) or run `uv run synthocr fonts fetch --family
NAME`, and check them with `uv run synthocr fonts check --text '…'`.

`synthocr prepare` streams `data/raw/` into `data/processed/texts.sqlite3`,
`corpora.json` and `stats.json`. Text is normalized to NFC by default
(`normalization = "none"` keeps it exactly), whitespace is collapsed and long
lines wrap at whitespace or whole grapheme clusters. Joiners, combining marks,
variation selectors and bidi controls are kept; nonvisual C0/C1 controls are
removed, and numeric-only samples stay. SQLite deduplicates by language and
text on disk, keeping every origin. Source hashes and settings invalidate stale
processed data; corrupt derived data is rebuilt, and sources changing during
preparation are rejected. `data.write_jsonl = true` adds an optional processed
JSONL copy.

`synthocr corpora` adds versioned Tatoeba snapshots and reuses one only if its
checksum matches. Upstream exports change weekly, so reproducing an old import
needs the saved archive in `.local/corpora/`; the frozen JSONL is committed.
`synthocr text` writes seeded offline templates in 11 genres (`en`, `ckb`,
`ar`, `fa`, `de`, `fr`, `es`, `tr` or `mixed`), each marked synthetic. See
`data/SOURCES.md` for counts, selection rules and licenses.

Strict font coverage (the default) picks, for each label, a font that has every
character; it never intersects all fonts or applies a fixed Kurdish allowlist.
The bitmap checker ignores only true default-ignorable characters; visible
format signs such as U+0600 and U+06DD still need glyphs. A cmap check cannot
prove shaping quality, so inspect renders of marks, joiners and mixed direction.
Font discovery handles TTF/OTF and TTC/OTC faces, deduplicates by content and
reports unreadable files; WOFF is not supported.

## Dataset format and checks

Each manifest record stores the logical label, font and face, actual font size,
language, direction, genre, source location, split, seed, image size, the text
ink rectangle `[left, top, right, bottom]` (right/bottom exclusive, tracked
through rotation; not a character box), image checksum and attribution.
`origins.jsonl` holds every origin of each text, linked by `text_id` and a label
checksum. Font IDs combine a content hash and face index.

Direction follows the first strong character unless overridden. Oversized text
shrinks to fit the limits instead of being cropped; augmentation keeps at least
four pixels of padding, and ink reaching the canvas edge fails generation. A
text gets one split by seeded hash, even across fonts, so exact transcriptions
never leak between splits. Each supported text/font pair is used at most once
per run; with too few pairs, generation reports failure and the partial run
cannot be exported.

`synthocr verify` decodes every image and checks hashes, counts, dimensions,
bounds, font inventory, unique IDs/images/pairs, disjoint splits, unlisted
images and the origins sidecar; `--metadata-only` skips image decoding. Hashes
detect corruption; they are not signatures. Exports hard-link images when
possible, are built in a temporary directory and published only after success,
and never write into a nonempty destination.

## Performance and memory

Users never need to tune memory: generation streams everything, so memory grows
with worker count, cache sizes, font complexity and image bounds, not with the
corpus or dataset size. SQLite uses a 4 MiB page cache; native font objects
(32 per worker) and glyph bitmaps (128 in the parent) have separate bounded
caches; workers get at most twice the worker count in pending tasks and save
their own images. One worker uses the least memory.

```sh
uv run synthocr benchmark --samples 1000 --workers 1
uv run synthocr benchmark --samples 1000 --workers 2
```

Benchmarks use temporary output and report throughput and parent-process peak
RSS, which excludes worker processes. Timing excludes preparation and font
discovery. Compare like with like (fonts, labels, bounds, format, augmentation,
Python and Pillow build) before claiming a speed or memory change.

## Dependencies and CI

The CLI needs Pillow (with RAQM), fontTools and regex; the `web` extra adds
FastAPI and Uvicorn. `pip install .` works on Python 3.11+, but on macOS with
Python 3.15 use uv, which applies the locked Pillow 13.0.0 source build with
`raqm=enable` (the wheel does not find Homebrew's FriBiDi). Revisit that
override when the wheel is fixed. Keep `uv.lock` committed; dependency upgrades
are separate reviewed changes.

CI runs once per merge into `main` as one short Linux job, to stay within the
monthly Actions minutes (docs-only changes are skipped; you can also start it by
hand from the Actions tab): `ruff check`, `ruff format --check`, the UI tests and
the full test suite, which covers rendering, exports, verification and every
Kurdish spelling case. Before a release, or after changing native dependencies,
the Dockerfile, paths or file locking, also check locally: `pip-audit`,
`uv build`, a Docker build of the container, and macOS or Windows if you changed
something platform-specific. A rendering smoke matrix
forces augmentation across every usable font face. Dependabot proposes weekly
updates; the Pillow override, native libraries, fonts and corpora still need
manual review. Font downloads are HTTPS-only, size-limited, checksummed and
written atomically. Coverage checks do not sandbox native font parsers, so use
trusted font files.

Version raw corpora, fonts and licenses, catalogs, configuration and skills.
Generated datasets, processed data, caches and credentials are ignored.
`.gitattributes` keeps raw sources and downloaded assets byte-exact so their
checksums hold.

## History

The original unlicensed input texts are not part of the public repository.
`main.py` is a thin compatibility entry point; the former
YAML configuration and `src.*` API were replaced by `config.toml` and the
`synthocr` package. The separate
[font-checking repository](https://github.com/ardalan-jalal/font-checking) is
integrated: its reference characters ship in the package and checking shares
the generation engine (see `fonts/README.md`). `synthocr preview --output
.local/preview.png` reproduces the README image.

## Release to PyPI

Publishing a GitHub release runs `.github/workflows/release.yml`, which checks
that the tag matches the version, builds the package and uploads it to PyPI with
trusted publishing (no token is stored).

Once, on [pypi.org](https://pypi.org/manage/account/publishing/), add a pending
publisher: project `synthocr`, owner `ardalan-jalal`, repository `SynthOCR`,
workflow `release.yml`, and leave the environment empty. Then for each release:

1. Run the checks above, plus `uv run --group dev pip-audit` and `uv build`.
2. Set the version: `uv version --bump minor` (or `patch`/`major`), and commit.
3. Create a GitHub release with the tag `v` plus that version, for example
   `v2.2.0`.

The package contains only the code, the web app and small reference files
(about 130 KB). Fonts and text are fetched by `synthocr setup`.

## Keep useful evidence

Generated datasets, experimental reports, screenshots, download caches and
machine-specific logs belong in ignored `.local/` or an explicit external
directory. The maintained experiment command writes settings, source/font
checksums and image evidence without adding a new dependency or network call.
Version a narrow regression and an explanation of the invariant instead of
committing every run. This lets another contributor reproduce the finding and
decide whether a later change preserves it.
