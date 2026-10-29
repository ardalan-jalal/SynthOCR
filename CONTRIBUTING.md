# Contributing to SynthOCR

Thank you for helping. Kurdish text, open fonts, bug reports and code are all
welcome.

## Ways to help

- **Report a problem**: open an issue with what you did, what you expected and
  what happened. For a rendering problem, attach the image and its label.
- **Add Kurdish text**: real, openly licensed Sorani, Kurmanji or other Kurdish
  text is the most valuable contribution. Say where it comes from and under which
  license; we cannot accept text without clear terms.
- **Add an open font**: fonts must have an open license (for example the SIL Open
  Font License). Add them through `synthocr/font_catalog.json` so their source and
  checksums are pinned; see the [fonts guide](fonts/README.md).
- **Improve the code or docs**: see below.

## Set up

```bash
git clone https://github.com/ardalan-jalal/SynthOCR.git
cd SynthOCR
uv sync --locked --extra web --group dev
uv run --extra web synthocr ui --no-browser
```

The [development guide](docs/development.md) explains the layout, the main
modules and how to test a change in the browser.

## Before you open a pull request

Run the same checks as CI:

```bash
uv run --group dev ruff check .
uv run --group dev ruff format --check .
uv run --extra web --group dev pytest
node --test tests/ui.test.cjs
uv run --group dev pip-audit
```

- Keep pull requests focused on one change, with a test for new behavior or a
  fixed bug.
- Never rewrite files in `data/raw/`; add new, versioned snapshots instead.
- Keep labels exactly as written: no stripping, reordering or relabelling of
  Kurdish, Arabic or mixed-script text.
- For changes to rendering, include before and after images.

The project rules for people and coding agents are in [AGENTS.md](AGENTS.md).

## License

By contributing, you agree that your contribution is licensed under either of
the [MIT License](LICENSE-MIT) or the [Apache License, Version 2.0](LICENSE-APACHE),
at the user's option, without additional terms.
