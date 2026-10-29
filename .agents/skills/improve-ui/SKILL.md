---
name: improve-ui
description: Improve SynthOCR's FastAPI and static web interface, including rendering controls, uploads, accessibility and bounded browser behavior.
---

# Improve the dataset generator UI

Read `AGENTS.md`, `docs/ui.md` and the relevant sections of
`docs/development.md`. Inspect `synthocr/static/index.html`, `app.js`, `app.css`
and the matching API models in `synthocr/web.py` before changing a control.

## Keep it simple

The page has one job: make OCR images with exact text labels (ground truth)
and save them on the user's computer. A first-time user must understand every control without
reading documentation. Keep three steps in one column:

1. **Text**: languages, text source (built-in or file; there is no typed-text
   box) and a typed image count (no quick-count buttons or limit hints). With several languages, a share per language (whole percentages that
   always total 100; editing one rebalances the others in proportion), sent as
   `language_weights`.
2. **Look**: the server's `config.IMAGE_PRESETS` as three style choices, a
   closed **Customize** panel (images with effects, background choices, each
   effect layer as Auto/Off/Light/Strong, tilt/blur/contrast sliders and text
   size), a closed **Fonts** panel and a preview of the same text. Effect names
   come from `config.DOCUMENT_LAYER_LABELS` through `/api/info`.
3. **Generate**: a folder chosen on the user's computer, dataset name, label
   format and one button.

After verification the page writes the dataset into the chosen folder, with
**Download ZIP instead** beside it, plus a few generated images with their
labels and the list of earlier datasets.

The **Fonts** panel reads `/api/fonts` (name, file, which page languages the
face draws, whether the user added it). No selection means all fonts; a choice
is sent as `fonts` (IDs) in preview and job requests and becomes
`fonts.include`. **Add font…** streams to `POST /api/fonts`, which validates the
face, de-duplicates by content and stores it in the workspace's `.jobs/fonts`;
only those can be removed, and not during a run. Warn, without blocking, when no
chosen font draws a selected language; block only when no font is chosen.

Do not add more tuning controls to the page. Image format, seed,
workers, train/validation/test splits, exact background weights, exact
effect strengths, colors and the font rendering test stay in the CLI,
`config.toml` and the API. The page sends a preset's settings with only the
Customize overrides, `train_ratio: 1`/`validation_ratio: 0` (one set, no split)
and a fresh random seed per run; the seed is recorded in the dataset. Never
mention memory, workers, caches or server storage in ordinary UI text. Before
adding any control, ask whether most users need it; if not, document the CLI
option instead.

Use plain words: "images", "text", "label", "ground truth". Read preset labels
and descriptions from `/api/info` rather than copying them into JavaScript.
Format choices describe what the user gets (for example, "Images + labels.csv").

SynthOCR is a local application for users and contributors. Use the shared
visual identity without personal greetings or hardcoded contributor paths.
The page never sends filesystem paths to the server and the server has no
endpoint for choosing or opening arbitrary folders. Saving uses the browser's
`showDirectoryPicker` (Chrome/Edge); other browsers download a ZIP.
Dark is the first-visit default; provide an accessible light/dark switch and persist an explicit choice. Apply the stored
theme before first paint, and keep the app usable if browser storage is
unavailable. Theme colors must preserve focus, error, disabled, selected and RTL
text readability in both modes.

The preview uses the shared renderer, never CSS imitations. It keeps the same
text, font and seed across style changes; **Another example** changes the text.
Show a clean image when a style leaves some images clean, and a few of the
style's backgrounds, rotating through them on each new example.

## Languages and text

Show Sorani, Kurmanji, Arabic and English together, with the same control style.
Each choice is independently selectable and reversible. Send canonical `ckb`,
`kmr`, `ar` and `en` selections through the API's `languages` array while
preserving the legacy `language` API. Source-record aliases use the shared
language logic: `ara` and `ar` share an Arabic corpus, while distinct dialect
and region tags stay distinct. Language tags describe records, never inferred
script. Preserve English words, Arabic digits, punctuation and mixed direction
inside Kurdish labels without stripping or relabelling them.

Zero selections is a visible validation state. A TXT file has no language
column: with several languages selected, show **Language of this file** with
nothing preselected and block generation until the user chooses; that one
language is then sent for the upload and the job, and share rows are hidden.
Mixtures use CSV/JSONL with a `language` column.
Switching sources must not silently relabel an upload. The model-neutral CLI
and other language-tagged raw corpora remain available; this UI scope does not
authorize deleting unrelated source data.

## Keep the interface portable and bounded

To save into a folder, the page prepares the verified export, streams the
server's stored ZIP and writes each entry into a new `<folder>/<dataset name>/`
with `createWritable`, checking every CRC. It refuses a nonempty existing
folder, rejects unsafe entry names and never holds the whole archive in memory.
Keep the ZIP layout (stored entries, sizes in local headers) that this relies on.
Retain API/CLI path validation, workspace locks and protection of raw
sources/fonts behind this flow.

Prepare and validate an export before initiating its native browser download.
Keep size/busy/failure messages inline and actionable. Never fetch a full
archive as a Blob, build an in-memory ZIP or navigate away to a JSON error page.
Disable repeat preparation requests, reject unexpected download URLs, and say a
transfer started rather than claiming it finished. Only verified runs offer
downloads.

Keep plain static assets, local fonts and the optional `web` extra. Load gallery
thumbnails a page at a time, retain original-image links, debounce previews and
abort stale requests. Pause work in hidden tabs. Never read an entire upload,
manifest or dataset into browser memory. Client validation improves feedback
but cannot replace server checks. Preserve one active job, cancellation status,
immutable raw uploads and bounded listings and caches. Existing output is never
overwritten.

## Check the actual interaction

Use the setup and required checks in `docs/development.md`; include `--extra web
--group dev` for UI/API tests, and run `node --test tests/ui.test.cjs`. Choose
focused API regressions for changed request fields and failure behavior.

Use the available browser-control tool for a short smoke test of the changed
interaction, a fresh load, keyboard access and a narrow (390 px) viewport. Check
labels, focus, visible errors, the console and that the page does not scroll
sideways. Check both themes when changing colors. Capture a final screenshot
under ignored `.local/` when it helps review. A screenshot alone does not
establish that controls work.

After changing HTML targets, run `tests/test_ui_assets.py` and reload the page.
Check that initialization finishes, a preview renders, generation is enabled
when valid, and the console has no new uncaught errors. A missing target in
synchronous startup code can stop initialization even when JavaScript syntax
and API tests pass. Every `pattern` attribute must compile with the browser's
`v` regex flag; an invalid pattern silently disables validation.

For a full pass: switch all three styles, use Customize and confirm the
preview; add a font, generate with only that font and check `font_id` in the
manifest, then remove it; set language shares and check the generated label counts; generate a
small dataset from built-in text and a tagged upload; save it to a folder and
download every format, checking contents and `origins.jsonl`. Headless browsers
cannot click the folder dialog: replace `showDirectoryPicker` with an init
script returning a folder from `navigator.storage.getDirectory()`, which runs
the same writing code, and read the files back. For uploads, use a temporary
UTF-8 fixture with explicit language tags, including a Kurdish label with mixed
script and an `ara` alias record; assert label preservation. Remove smoke-test
outputs afterward.

If rendering or memory changed, also use `run-experiments`. Report measured
evidence and remaining platform limitations; do not describe a local browser
pass as a production deployment.
