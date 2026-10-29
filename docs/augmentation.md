# Image augmentation

Augmentation changes the appearance of an image while retaining its logical
label, source language, attribution and split. The CLI, configuration, API and
browser preview use `synthocr.render.Renderer`. It generates backgrounds with
Pillow; no external image download or second effects implementation is needed.

The web page's **Look** step offers three presets: **Clean** (no effects),
**Balanced** (about a quarter clean, the rest office scans, aged paper and phone
photos) and **Varied** (every document type, effects on every image). They are
defined in `synthocr.config.IMAGE_PRESETS`; the page receives them from the API
and submits their settings. Its **Customize** panel sets the share of images
with effects, the selected backgrounds (equal weights), each effect layer as
Auto (inherit), Off (0), Light (0.2) or Strong (0.6), and rotation, blur and
contrast limits with sliders (0–10°, 0–2 px, 0–0.5). Everything below is available from the CLI,
`config.toml` and the API.

Select several document types to include a mixture in a dataset. Each image
chooses one selected type; paper, grain, shadows, stains, folds, lines, scanner
bands, speckles and binding shadows can be layered together.

The preview shows one original beside one augmented version. Its background
selector switches examples using the images already received.
Its text, font and random seed stay fixed while effects change. **New sample**
changes the example explicitly. Choose **Clean** for black text on white;
your custom choices remain available when you return to **Custom**.

## Document types and combined effects

The twelve document recipes offer different paper and capture conditions:

- **Clean paper**: a near-white sheet with subtle surface texture.
- **Office scan**: a light scanned sheet with fine grain and uneven lighting.
- **Aged paper**: warm paper with fibers, stains and gentle creases.
- **Notebook**: off-white ruled paper.
- **Phone photo**: uneven illumination over a warm sheet.
- **Photocopy**: grain, speckles and slight scanner banding.
- **Thermal receipt**: thin, warm paper with horizontal density variation.
- **Newsprint**: textured gray paper with fine print-like speckles.
- **Recycled paper**: warm paper with visible fibers and scattered flecks.
- **Book page**: warm paper shaded near the binding edge.
- **Fax**: pale grayscale paper with speckles and scan bands.
- **Crumpled paper**: intersecting folds with highlights and shadows.

These are procedural simulations, not downloaded document photographs or
evidence of a particular scanner, camera or printing process. Recipes change
the background; they do not add unrelated text or simulate full document
layouts. Inspect them against the documents your model will actually read.

Optional customization independently overrides each component. Several may be
enabled together. A disabled component is removed even when the recipe includes
it; resetting customization restores the recipe. Controls for strength appear
only when their effect is customized and enabled.

In configuration, `background_presets` is a list of document IDs. An empty list
retains the legacy background settings. The nine strengths `paper_texture`,
`grain`, `shadow`, `stains`, `creases`, `ruled_lines`, `scan_banding`, `speckles`
and `binding_shadow` accept 0–1. Omitting a strength inherits its recipe value;
`0` removes that component; a positive value overrides it for all selected
document types. JSON/API requests use `null` for inheritance. TOML has no null
literal, so omit the corresponding line.

`scan_banding` adds low-contrast horizontal brightness bands, `speckles` adds
small paper or scanning flecks, and `binding_shadow` adds an edge falloff like
a book gutter. Their strengths are independent of general grain and lighting.
They are applied behind the text, preserving the separate shaped ink mask.

`preset_colors = true` uses each recipe's base background and palette. Set it to
`false` to apply your chosen base style, colors and grayscale/RGB mode to all
selected recipes while retaining their layers. Geometry and blur/contrast
settings remain separately adjustable. For example:

```sh
uv run synthocr generate --output dataset/mixed-documents --augmentation 1 --background-preset office-scan --background-preset notebook --shadow 0.25 --grain 0.15 --stains 0
uv run synthocr generate --output dataset/scanned-documents --augmentation 1 --background-preset book-page --background-preset thermal-receipt --scan-banding 0.2 --speckles 0.15 --binding-shadow 0
```

The first command mixes scans and notebook paper, adds shadow and grain to
both, and removes stains. The second mixes book pages and receipt paper with
bands and speckles while removing the binding shadow. Annotations record the chosen
`background_preset`, effective colors, resolved `layers` and actual effect values.

## Backgrounds and colors

For legacy configurations without document presets, **Paper** produces soft,
low-frequency texture. **Solid** (`plain` in config)
uses one uniform color. **Gradient** blends a start and end color vertically
over the entire canvas. **Noise** adds fine grain using bounded tiles.

The color mode is **Grayscale** or **RGB color**. In grayscale, chosen colors
are converted to luminance; choosing a blue swatch does not produce a blue
output unless RGB is selected. Background and text colors are six-digit hex
values, such as `#f5f1e8` and `#1a1a1a`. The end color affects only gradients;
texture strength affects only paper and noise. Dependent controls are hidden
when they do not apply.

The base colors need at least a 3:1 luminance contrast ratio when augmentation
is enabled. Validation checks intermediate gradient colors as well as its end
points, in the chosen color mode. This rejects obvious foreground/background
collisions. It is not a WCAG compliance claim or proof of OCR readability:
antialiasing, blur, texture and contrast variation change actual pixels.
Inspect the rendered result, especially small text and marks.

## Dataset mix and probabilities

`augmentation` is the probability of applying the selected appearance to each
image: `0` produces clean images, `1` applies it to every image. The clean
share of a dataset is `1 - augmentation`; the rest is divided among the
selected backgrounds by their weights.

CLI, TOML and API requests express the background mix with `background_weights`,
a mapping from selected `background_presets` IDs to relative weights:

- `{}` is the default: every selected background has equal probability, with
  the original seeded selection behavior preserved.
- An omitted selected ID has weight `1`. A weight of `0` excludes that type from
  generated augmented images while keeping it selected.
- Values must be finite numbers from `0` to `1_000_000`. Keys must name selected
  types, and at least one selected type must have a positive weight.

For example, select Office scan, Aged paper and Phone photo with weights
`2:1:1` and augmentation `0.75`. Expected shares of the entire dataset are
37.5% scan, 18.75% aged paper, 18.75% photo and 25% clean. A 1,000-image dataset
would therefore have approximately 375 scans, rather than an exact quota.
The seed drives independent image choices; percentages and displayed counts
are expectations. No mix is claimed to be optimal for every recognition model.

```sh
uv run synthocr generate --output dataset/weighted-documents --augmentation 0.75 --background-preset office-scan --background-preset aged-paper --background-preset phone-photo --background-weight office-scan=2 --background-weight aged-paper=1 --background-weight phone-photo=1
```

Repeat `--background-weight NAME=WEIGHT` for explicit CLI weights. In the
existing TOML `[generation]` section, the same example uses:

```toml
augmentation = 0.75
background_presets = ["office-scan", "aged-paper", "phone-photo"]
background_weights = { office-scan = 2, aged-paper = 1, phone-photo = 1 }
```

The run stores these settings. For an augmented image with a document recipe,
`augmentation.background_probability` records that recipe's probability
**among augmented images**: `0.5` for Office scan in the example. Multiply it
by the run's `augmentation` value for its expected share of the entire dataset.
Clean images retain empty augmentation metadata. A preview illustrates the
selected appearance; it is not a sample-count forecast.

## Variation settings

These settings control an augmented image:

- `rotation`: maximum absolute angle in degrees, from 0 to 10. A seeded angle
  is chosen between its negative and positive limits.
- `blur`: maximum Gaussian radius in pixels, from 0 to 2. The applied radius
  is chosen between zero and this limit.
- `contrast`: maximum variation around a factor of 1, from 0 to 0.5. For
  example, `0.1` chooses a factor between 0.9 and 1.1.
- `texture_strength`: grain amplitude, from 0 to 0.25. Zero removes texture
  from paper and noise. It does not change solid or gradient backgrounds.

The CLI/config defaults retain legacy paper, grayscale and texture strength
0.04, with 25% augmented images. The UI's **Balanced** preset applies office
scan, aged paper or phone photo to 75% of images using recipe palettes and
layers. **Clean** applies no augmentation. **Varied** includes all twelve document
types and augments every image. The CLI and configuration can change or disable
individual effects. Stronger effects are not automatically better training data; choose
the appearance of the documents the target model will actually read.

For a reproducible custom run, edit the existing `[generation]` section of a
copy of `config.toml`:

```toml
[generation]
augmentation = 1.0
background = "gradient"
color_mode = "rgb"
background_color = "#f5f1e8"
background_color_end = "#e4e9ed"
text_color = "#1a1a1a"
texture_strength = 0.04
rotation = 2.0
blur = 0.55
contrast = 0.1
```

Keep the other data/font/output settings in that configuration. Run
`uv run synthocr --config path/to/config.toml generate --output dataset/new-run`
and then `uv run synthocr verify --dataset path/to/output`. Relative outputs
resolve from the configuration's directory. `generate --help` lists command-line
overrides. Effective values and applied per-image values are saved with the run
and annotations, so another installation can inspect what produced the images.

## Layout and bounded memory

The renderer shapes text with RAQM and creates a separate ink mask. It rotates
that mask before generating the background across the expanded canvas. It
composites text into that full background, applies blur/contrast, and records
the resulting ink bounds. Labels retain logical reading order; no RTL reversal
or fabricated character boxes are involved.

Configured width, height and pixel limits apply after rotation as well as
before it. The renderer reduces oversized text before allocating a canvas, and
augmented images reserve a margin for resampling and the configured blur halo.
It raises a clear error when ink reaches the edge or dimensions cannot fit.
Avoid accepting clipped output to reach a requested sample count.

Paper uses a small texture source, gradients a fixed-size ramp, and fine noise
bounded tiles. These operations still allocate the current image and mask;
they do not load an entire dataset. RGB buffers need more memory and disk space
than grayscale. Font caches and pending tasks remain bounded, and one worker
is the lowest-memory starting point.

## Reproduce a comparison

```sh
uv run synthocr experiment --output .local/experiments/backgrounds --samples 12 --seed 42
```

The command compares a clean original and all twelve document presets on identical
case/font/seed combinations,
including Sorani and Kurmanji when at least two cases are requested. Inspect
its contact sheets, full images, decoded metrics and settings. Sheets wrap into
four columns and at most twelve image rows per page, showing at most twelve
cases across all pages. `contact-sheet.png` is the first page; subsequent pages
use `contact-sheet-002.png` and so on. The report's `contact_sheets` list records
each page's filename, hash, dimensions and case range. The manifest retains all
requested cases and recipes, even when a case is omitted from the sheets. See
[the experiment workflow](../experiments/README.md) for evidence and limitations.
It uses fonts accompanied by OFL license files and bundled attributed
diagnostics without reading or changing the training corpus. Accompanying
license files are hashed as evidence; arbitrary user fonts still require
review of their source and redistribution terms.

## Regressions to preserve

These are concrete renderer invariants, retained in tests rather than informal
visual claims:

- **A rotated background continues into the corners.** Rotating a textured
  image with white fill once exposed a separate white frame. The background is
  now generated after rotating the ink. Corner tests cover seeded rotations,
  clean margins and encoded PNG/JPEG/WebP/TIFF output.
- **Small requested padding does not clip marks.** Zero/one-pixel padding with
  rotation and blur needs an internal safety margin. Tests include Sorani,
  Urdu/Nastaliq, Latin accents and Devanagari, with extreme bounds refused.
- **Coverage is per label and source language stays explicit.** A font failing
  an entire reference alphabet may still render a selected label. Glyph checks
  cannot prove shaping; inspect joiners and combining marks. Keep Sorani and
  Kurmanji source tags distinct even when a renderer hint is overridden.
- **Encoded images match their records.** Verification checks decoded bounds,
  hashes, label identities and attribution. An attractive preview cannot
  replace verification of the generated images.
- **Comparisons keep the sample fixed.** Background selection and layer
  changes retain text, font and random seed. Tests compare the same effective
  point size even when rotation forces a long line to shrink. Each layer has
  an independent random stream, so enabling grain does not move a shadow.
- **Preview images are released between renders.** The full-catalog comparison
  closes each Pillow image explicitly before rendering the next. Turning
  augmentation off returns a single clean comparison without duplicate
  after-images. The browser replaces its bounded preview instead of retaining
  an image history.

When a new effect fails, capture the smallest relevant case, fixed seed and
font hash. Add a focused regression and update this list only when the finding
changes a future contributor's decision. No experiment establishes linguistic
correctness or trains an agent automatically.
