# SynthOCR project guidance

Build a small, model-neutral text-image generator. Keep new behavior in the
`synthocr` package and expose reusable operations through its CLI.

- Use `uv sync --locked` and `uv run`. Python is pinned in `.python-version`;
  keep Python 3.14 compatibility. Edit dependencies in `pyproject.toml` and update
  `uv.lock` together. Keep runtime dependencies small.
- Raw data in `data/raw/` is the source of truth. Never rewrite raw corpora as
  part of normalization. Processed data and images are reproducible outputs.
- Keep one active corpus per explicit language or dialect in `corpora.json`,
  backed by the shared SQLite store. Preserve all overlapping source origins;
  processed JSONL is an optional duplicate, not the default storage format.
- Stream corpora, manifests and exports. Keep SQLite caches, font caches, and
  pending worker tasks bounded. Do not load the corpus into a list or introduce
  arrays proportional to the dataset size.
- Preserve Unicode shaping, joiners, combining sequences and original logical
  label order. Use RAQM. Never restore a global Kurdish character allowlist,
  remove numeric-only samples, reverse RTL labels, or invent character boxes.
- Font inspection and generation must share `synthocr.fonts`, including font
  selection (`fonts.include`). The Kurdish reference is bundled in the package;
  do not add a companion checkout, duplicate font folder or second checking
  implementation. Fonts users add in the app stay in the workspace.
- The local UI uses FastAPI with the optional `web` extra and plain static
  assets. Keep it on loopback, serialize generation jobs and preserve
  cancellation status. The server never receives or opens client folder paths;
  the browser writes verified exports into the folder the user picks.
- The code is licensed MIT OR Apache-2.0 (see `NOTICE`). Only commit fonts and
  text whose license allows redistribution, with the terms recorded. Downloads
  require pinned sources, SHA-256 checks and accompanying licenses.
- Preserve corpus attribution and distinguish synthetic templates from natural
  text. New raw snapshots are immutable. Never use an evaluation benchmark as
  general training corpus material.
- Language selection uses source metadata. Add explicit `data.source_languages`
  for reviewed unlabelled corpora; never guess from script. Preserve source
  language when overriding renderer hints, and keep filtered selection on disk.
- New generation must refuse nonempty output directories and report partial
  failure honestly. Keep JSONL as the primary model-neutral annotation format.
- Verify checksums, decoded images, identities, bounds and splits with the
  maintained `synthocr verify` command. Export only verified data and publish
  exports atomically; preserve attribution in every format.
  Schema 3 datasets and every export retain `origins.jsonl`; validate the label
  identity and primary attribution against that sidecar as well as its checksum.
- Validate changes with `uv run --group dev ruff check .`,
  `uv run --group dev ruff format --check .`, `uv run --group dev pytest`,
  `node --test tests/ui.test.cjs` and `uv run --group dev pip-audit`. Use the benchmark command for changes
  to rendering or memory use; distinguish parent RSS from total worker memory.
  Add `--extra web` to sync/run commands when testing the UI or API, including
  `tests/test_web.py` and `tests/test_ui_cli.py`, and smoke-test the browser.
- Project workflows live in `.agents/skills/`; `.claude/skills` points to the same
  definitions. Prefer updating those workflows over adding one-off scripts.
- Build for contributors and users on Linux, macOS and Windows. Resolve paths
  from the configuration or explicit arguments; never encode a developer's home
  directory, installed fonts, browser, or private service in product behavior.
- For UI work, use `.agents/skills/improve-ui/SKILL.md`; for rendering or memory
  comparisons, use `.agents/skills/run-experiments/SKILL.md`. The architecture
  and contributor checks are in `docs/development.md`.
- New augmentation options must reach the same renderer through configuration,
  CLI and API, and retain effective settings in generated annotations. Keep the
  web page simple: presets plus a small Customize panel; tuning stays in the CLI.
- Record reproducible observations in experiment reports. Turn demonstrated
  failures into focused regressions and update the relevant workflow; do not
  claim an experiment proves OCR accuracy or changes an agent's model weights.
