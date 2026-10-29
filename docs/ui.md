# Dataset generator

Start the local app:

```sh
uv tool install "synthocr[web]"   # once; or pip install "synthocr[web]"
synthocr ui
```

From a clone of the repository, use `uv run --extra web synthocr ui` instead; it
then uses the repository's `config.toml`, fonts and text. An installed copy uses
your SynthOCR folder (`~/Library/Application Support/SynthOCR` on macOS,
`%LOCALAPPDATA%\SynthOCR` on Windows, `~/.local/share/synthocr` on Linux, or
`SYNTHOCR_HOME`). On first start it runs `synthocr setup`, which downloads 16 open
font families and 5,000 Tatoeba sentences per language (CC BY 2.0 France) and
writes offline text templates; `--offline` skips Tatoeba, `--all-fonts` fetches
all 41 families and `--limit` sets the sentences per language.

Your browser opens `http://127.0.0.1:8000`. The page makes OCR training data:
text images, each with its exact text label (ground truth), saved to a folder
on your computer. Model training happens separately.

## Make a dataset

1. **Text.** Select one or more of **Sorani**, **Kurmanji**, **Arabic** and
   **English**. Use the **Built-in text** (real sentences, words and numbers) or
   **My file** (a UTF-8 TXT, CSV or JSONL file up to 50 MiB). Type the **Number
   of images** (up to 100,000).

   With two or more languages, **Share of images** sets each language's
   percentage, with the image count it gives. Changing one share adjusts the
   others in proportion so the total stays 100%. A share of 0 leaves that
   language out. If a language runs out of text, the others fill the rest, so
   check the counts in your labels.
2. **Look.** Choose a style. The preview shows the same text in that style.
   - **Clean**: black text on white.
   - **Balanced**: about a quarter clean, the rest scanned pages, aged paper and
     phone photos.
   - **Varied**: every image on a different realistic paper.

   **Customize** adjusts the chosen style:
   - **Images with effects**: the percentage that get a background and effects;
     the rest stay clean black text on white.
   - **Backgrounds**: which of the 12 document backgrounds to use.
   - **Effects**: paper texture, grain, shadows, stains, folds, notebook lines,
     scan bands, speckles and book spine shadow, each **Auto** (the
     background's own amount), **Off**, **Light** or **Strong**.
   - **Tilt, blur and contrast**: sliders up to 10°, 2 px and 50%. Each image
     gets a random amount up to the value you set; 0 turns it off.
   - **Text size**: how tall the letters are, 24 to 96 pixels. Long lines
     shrink to fit.

   The style then shows as custom; pick a style again to start over. **Another
   example** shows a different text sample.

   **Fonts** lists every font with the languages it can draw. All are used by
   default; untick fonts, or use **Only for my languages** or **None** and pick
   a few. **Add font…** adds your own TTF, OTF, TTC or OTC files (up to 32 MiB
   each); they are kept in the app's workspace on your computer, never in the
   project's `fonts/` folder, and **Remove** deletes them again. Each image uses
   one chosen font that can draw all of its text; the page warns when none of
   the chosen fonts can draw a selected language.
3. **Generate.** Click **Choose folder…** and pick a folder on your computer,
   enter a **Dataset name**, and choose a **Label format**:

   | Format | What you get |
   | --- | --- |
   | Images + labels.jsonl | Images, `manifest.jsonl` (one JSON line per image), settings and source attribution. Works with any training code. |
   | Images + labels.csv | Images and `labels.csv` with the image file name and its text. |
   | Images + .gt.txt files | Each image next to a `.gt.txt` file holding its text, for Tesseract training. |
   | Hugging Face ImageFolder | Images and `metadata.jsonl` for `datasets.load_dataset("imagefolder")`. |

   Click **Generate dataset**.

Progress appears below the form. When every image and label has been checked,
the page writes the files into a new folder named after the dataset inside the
folder you chose, for example `Datasets/sorani-20261029-1402/`. It never
overwrites: if that folder already exists and is not empty, it stops and asks
you to choose another. Each file's checksum is verified as it is written. Every
format keeps `origins.jsonl` so the source of each text stays attributed.

Saving into a folder needs **Chrome** or **Edge**, which let a page write to a
folder you choose. In other browsers, click **Download ZIP instead** and unzip
it where you like. The ZIP option is also there in Chrome and Edge.

Below the result, a few generated images are shown with their labels; click one
for full size. **Earlier datasets** lists previous runs; open one to save it to
a folder again or download it.

The page produces a single dataset: every image is in one set, with no
train/validation/test split. Split it however your training code expects. Each
run uses a fresh random seed, which is saved with the dataset, so two runs never
repeat the same images.

## Text and languages

Languages select which source records are used. They never filter characters
inside a label. A Sorani sentence containing English words, Arabic digits or
mixed punctuation stays one Sorani label with its complete text, in its
original reading order. The app never guesses a record's language from its
script. Language tags are `ckb` (Sorani), `kmr` (Kurmanji), `ar` and `en`; the
aliases `ara` and `eng` are treated as Arabic and English while keeping their
original attribution.

**Uploaded files.** TXT has one text per line and no language column, so every
line gets one language: the selected one, or, when several are selected, the
one you pick under **Language of this file** (nothing is preselected; the app
never guesses). CSV needs a `text` column and JSONL a `text` field; both may add
a `language` column so one file can mix languages. Only records in the selected
languages are used. The original file is kept, unchanged, with the generated
dataset.

## Advanced options

The page keeps only the choices most people need. Use the command line or
`config.toml` for anything else: image format, font size, seed, worker count,
train/validation/test splits, exact background weights, exact effect
strengths, colors, and the Kurdish font rendering test. To use only certain
fonts on the command line, repeat `--font FILE` (names and IDs are listed by
`uv run synthocr fonts list`). Language shares are
`--language-weight ckb=70 --language-weight kmr=30` on the command line. See
the [README](../README.md), [augmentation settings](augmentation.md) and the
[Kurdish guide](kurdish.md). The web API, documented at `/docs`, accepts the
same options.

## Running the app

```sh
uv run --extra web synthocr ui --port 8080 --no-browser
uv run --extra web synthocr --config /path/to/config.toml ui
```

The app listens only on this computer (`127.0.0.1`). It also keeps its own copy
of each dataset in its workspace (`dataset/ui/` by default), which is what
**Earlier datasets** reads. One dataset is generated at a time; closing the tab
does not stop it, but the copy into your folder happens in the page, so keep the
tab open until it says **Saved**. To quit, wait for or cancel the current run,
then press **Ctrl+C** in the terminal. A run interrupted by quitting is shown as
failed after restart, never as complete.

Large datasets are prepared on disk before they are copied or downloaded, within
a limit (256 MiB by default). If a dataset is larger, the page says so; make
fewer images, or start the app with a larger `--cache-mb`. To serve the app from
another machine, see the [hosting guide](hosting.md).
