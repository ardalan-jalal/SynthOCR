"""One maintained command line interface for every stage of the data pipeline."""

import argparse
import json
import logging
import math
import re
import sqlite3
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from . import __version__
from .config import AUGMENTATION_FIELDS, DOCUMENT_LAYERS, DOCUMENT_PRESETS, load_config
from .corpora import snapshot
from .fonts import (
    CATALOG,
    PROFILES,
    check,
    discover,
    fetch,
    font_ids,
    load_characters,
    load_profile,
)
from .home import NotSetUp, home, ready, resolve_config, setup
from .pipeline import export, generate
from .preview import preview
from .spelling import LANGUAGES as SPELLING_LANGUAGES
from .spelling import spelling_cases, write_spelling
from .text import GENRES, generate_text, prepare
from .verify import verify


class BackgroundWeight(argparse.Action):
    """Collect a small explicit recipe-weight mapping without silent replacement."""

    def __call__(self, parser, namespace, value, option_string=None):
        name, separator, raw = value.partition("=")
        if not separator or name not in DOCUMENT_PRESETS:
            raise argparse.ArgumentError(
                self, "expected a supported background preset and weight, e.g. office-scan=3"
            )
        try:
            weight = float(raw)
        except ValueError as error:
            raise argparse.ArgumentError(self, "weight must be a finite number") from error
        if not math.isfinite(weight) or not 0 <= weight <= 1_000_000:
            raise argparse.ArgumentError(self, "weight must be between 0 and 1000000")
        weights = dict(getattr(namespace, self.dest) or {})
        if name in weights:
            raise argparse.ArgumentError(self, f"duplicate weight for {name}")
        weights[name] = weight
        setattr(namespace, self.dest, weights)


class LanguageWeight(argparse.Action):
    """Collect relative per-language shares, e.g. ckb=70 kmr=30."""

    def __call__(self, parser, namespace, value, option_string=None):
        name, separator, raw = value.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", name):
            raise argparse.ArgumentError(self, "expected a language tag and weight, e.g. ckb=70")
        try:
            weight = float(raw)
        except ValueError as error:
            raise argparse.ArgumentError(self, "weight must be a finite number") from error
        if not math.isfinite(weight) or not 0 <= weight <= 1_000_000:
            raise argparse.ArgumentError(self, "weight must be between 0 and 1000000")
        weights = dict(getattr(namespace, self.dest) or {})
        if name in weights:
            raise argparse.ArgumentError(self, f"duplicate weight for {name}")
        weights[name] = weight
        setattr(namespace, self.dest, weights)


class OutputLocation(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        name, separator, path = value.partition("=")
        if not separator or not path or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name):
            raise argparse.ArgumentError(
                self, "use NAME=PATH with a lowercase location name, e.g. training=/data/training"
            )
        if name == "default":
            raise argparse.ArgumentError(
                self, "'default' is reserved; use --output-root for the default location"
            )
        locations = dict(getattr(namespace, self.dest) or {})
        if name in locations:
            raise argparse.ArgumentError(self, f"duplicate output location: {name}")
        if len(locations) >= 8:
            raise argparse.ArgumentError(
                self, "at most eight additional output locations are supported"
            )
        locations[name] = path
        setattr(namespace, self.dest, locations)


def parser():
    root = argparse.ArgumentParser(prog="synthocr", description=__doc__)
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument(
        "--config",
        type=Path,
        help="Config file; defaults to ./config.toml, else your SynthOCR workspace",
    )
    commands = root.add_subparsers(dest="command", required=True)
    setup_command = commands.add_parser(
        "setup", help="Download open fonts and text into your SynthOCR workspace"
    )
    setup_command.add_argument(
        "--all-fonts",
        action="store_true",
        help="Fetch every open font family, not only the Kurdish set",
    )
    setup_command.add_argument(
        "--limit", type=int, default=20_000, help="Text entries per language (default 20000)"
    )
    setup_command.add_argument(
        "--offline", action="store_true", help="Skip the Tatoeba download; fonts are still fetched"
    )
    ui = commands.add_parser("ui", help="Open the local web UI for generating datasets")
    ui.add_argument("--host", choices=["127.0.0.1", "localhost", "::1"], default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8000)
    ui.add_argument("--cache-mb", type=int, default=256, help="Maximum disposable UI cache in MiB")
    ui.add_argument("--output-root", type=Path, help="Save UI datasets here (default: dataset/ui)")
    ui.add_argument(
        "--output-location",
        dest="output_locations",
        action=OutputLocation,
        metavar="NAME=PATH",
        help="Add a named storage location users can choose; repeat for up to eight locations",
    )
    ui.add_argument("--no-browser", action="store_true", help="Start without opening a browser")
    prep = commands.add_parser("prepare", help="Stream raw text into indexed processed data")
    prep.add_argument("--force", action="store_true")
    experiment = commands.add_parser("experiment", help="Compare reproducible rendering presets")
    experiment.add_argument("--output", type=Path, required=True)
    experiment.add_argument("--samples", type=int, default=12)
    experiment.add_argument("--seed", type=int, default=42)
    for command in ("kurdish", "generate", "benchmark"):
        sub = commands.add_parser(
            command,
            help={
                "kurdish": "Generate and verify Kurdish images (Sorani by default)",
                "generate": "Generate images for any language",
                "benchmark": "Measure real throughput using temporary output",
            }[command],
        )
        sub.add_argument("--samples", type=int)
        sub.add_argument("--workers", type=int)
        sub.add_argument("--seed", type=int)
        sub.add_argument("--output", type=str)
        sub.add_argument("--format", choices=["png", "jpg", "webp", "tif"])
        sub.add_argument("--augmentation", type=float, help="Fraction of images to augment (0..1)")
        sub.add_argument("--background", choices=["paper", "plain", "gradient", "noise"])
        sub.add_argument("--color-mode", choices=["grayscale", "rgb"])
        sub.add_argument("--background-color", help="Background hex color, e.g. #f5f1e8")
        sub.add_argument("--background-color-end", help="Gradient end hex color")
        sub.add_argument("--text-color", help="Text hex color, e.g. #1a1a1a")
        sub.add_argument("--texture-strength", type=float, help="Paper/noise strength (0..0.25)")
        sub.add_argument("--rotation", type=float, help="Maximum rotation in degrees (0..10)")
        sub.add_argument("--blur", type=float, help="Maximum blur radius in pixels (0..2)")
        sub.add_argument("--contrast", type=float, help="Contrast variation around 1 (0..0.5)")
        sub.add_argument(
            "--background-preset",
            dest="background_presets",
            action="append",
            choices=DOCUMENT_PRESETS,
            help="Repeat to mix document types across images",
        )
        sub.add_argument(
            "--background-weight",
            dest="background_weights",
            action=BackgroundWeight,
            metavar="PRESET=WEIGHT",
            help="Relative share of a selected document type; repeat, omitted weights are 1",
        )
        sub.add_argument(
            "--preset-colors",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="Use document colors; --no-preset-colors uses your custom palette",
        )
        for layer in DOCUMENT_LAYERS:
            sub.add_argument(
                "--" + layer.replace("_", "-"),
                type=float,
                help="Override effect strength (0..1); zero disables, omitted inherits the preset",
            )
        if command == "kurdish":
            sub.add_argument("--language", choices=["sorani", "kurmanji", "both"], default="sorani")
            sub.add_argument(
                "--check", action="store_true", help="Check every bundled spelling case instead"
            )
        else:
            sub.add_argument(
                "--suite",
                choices=["kurdish-spelling"],
                help="Use bundled spelling cases instead of raw inputs",
            )
            sub.add_argument(
                "--language",
                dest="languages",
                action="append",
                help="Filter source language tags; repeat to combine, e.g. ckb and kmr",
            )
            sub.add_argument(
                "--language-weight",
                dest="language_weights",
                action=LanguageWeight,
                metavar="LANG=WEIGHT",
                help="Relative share of a selected language; repeat, omitted weights are 1",
            )
            sub.add_argument(
                "--exclude-genre", dest="exclude_genres", action="append", help="Repeat to combine"
            )
            sub.add_argument(
                "--font",
                dest="fonts",
                action="append",
                metavar="FILE",
                help="Use only this font (file name, NAME#FACE or ID from `fonts list`); repeat",
            )
    font = commands.add_parser("fonts", help="Use the same fonts for inspection and generation")
    font_commands = font.add_subparsers(dest="font_command", required=True)
    font_commands.add_parser("list")
    checker = font_commands.add_parser("check")
    source = checker.add_mutually_exclusive_group()
    source.add_argument("--chars", type=Path, help="Character JSON or UTF-8 text")
    source.add_argument("--profile", choices=PROFILES, help="Bundled reference character set")
    source.add_argument("--text", default="سڵاو Hello 0123456789")
    audit = checker.add_mutually_exclusive_group()
    audit.add_argument(
        "--require-all", action="store_true", help="Exit 1 if any font lacks any required character"
    )
    audit.add_argument(
        "--require-any", action="store_true", help="Exit 1 if no font covers the entire reference"
    )
    download = font_commands.add_parser("fetch")
    download.add_argument("--family", action="append", help="Repeat to select catalog families")
    download.add_argument("--catalog", help="Font catalog JSON; defaults to the bundled catalog")
    download.add_argument("--destination", default="fonts")
    text = commands.add_parser("text", help="Create seeded genre examples without external APIs")
    text.add_argument("--output", type=Path, required=True)
    text.add_argument("--count", type=int, default=1000)
    text.add_argument("--seed", type=int, default=42)
    text.add_argument(
        "--genre",
        default="mixed",
        choices=["mixed", *GENRES],
    )
    text.add_argument(
        "--language",
        choices=["mixed", "en", "ckb", "ar", "fa", "de", "fr", "es", "tr"],
        default="en",
    )
    spelling = commands.add_parser(
        "spelling", help="Export Kurdish spelling examples and Unicode probes"
    )
    spelling.add_argument("--output", type=Path, required=True)
    spelling.add_argument("--language", choices=SPELLING_LANGUAGES, default="mixed")
    spelling.add_argument(
        "--words-only",
        action="store_true",
        help="Only authored words and phrases; omit mechanical probes",
    )
    convert = commands.add_parser("export", help="Stream a model-specific dataset view")
    convert.add_argument("--dataset", type=Path, default=Path("dataset/default"))
    convert.add_argument("--output", type=Path, required=True)
    convert.add_argument("--format", required=True, choices=["csv", "imagefolder", "tesseract"])
    check_data = commands.add_parser(
        "verify", help="Verify image integrity, counts, bounds and splits"
    )
    check_data.add_argument("--dataset", type=Path, default=Path("dataset/default"))
    check_data.add_argument("--metadata-only", action="store_true")
    sheet = commands.add_parser(
        "preview", help="Render a multilingual showcase with licensed fonts"
    )
    sheet.add_argument("--output", type=Path, default=Path(".local/preview.png"))
    corpora = commands.add_parser("corpora", help="Add versioned, attributed Tatoeba raw data")
    corpora.add_argument(
        "--language", action="append", required=True, help="Repeat ISO 639-3 codes"
    )
    corpora.add_argument(
        "--snapshot", required=True, help="ISO date; existing snapshots stay immutable"
    )
    corpora.add_argument(
        "--limit", type=int, default=10000, help="Maximum attributed records per language"
    )
    corpora.add_argument("--catalog", default="data/corpora.json")
    corpora.add_argument("--cache", default=".local/corpora")
    return root


def _kurdish(config, args):
    """A Sorani-first entry point using the same generator and integrity checker."""
    languages = {"sorani": ["ckb"], "kurmanji": ["kmr"], "both": ["ckb", "kmr"]}[args.language]
    required = (
        {
            row["attribution"]["source_id"]
            for row in spelling_cases()
            if row["language"] in languages
        }
        if args.check
        else set()
    )
    overrides = {
        name: getattr(args, name)
        for name in ("samples", "workers", "seed", "output", "format", *AUGMENTATION_FIELDS)
        if getattr(args, name) is not None
    }
    if args.check:
        overrides.setdefault("samples", len(required))
        if overrides["samples"] < len(required):
            raise ValueError(
                f"Spelling check needs at least {len(required)} samples; omit --samples"
            )
    prefix = "check-" if args.check else ""
    overrides.setdefault("output", f"dataset/{prefix}{args.language}-{uuid4().hex[:8]}")
    # Keep diagnostic contexts whole even if ordinary corpus lines wrap sooner.
    max_chars = config.data.max_chars
    if args.check:
        max_chars = max(
            max_chars,
            max(len(row["text"]) for row in spelling_cases() if row["language"] in languages),
        )
    config = replace(
        config,
        data=replace(config.data, normalization="NFC", max_chars=max_chars),
        fonts=replace(config.fonts, coverage="strict"),
        generation=replace(
            config.generation,
            **overrides,
            languages=languages,
            direction="auto",
            language="",
            suite="kurdish-spelling" if args.check else "",
            exclude_genres=[] if args.check else [*config.generation.exclude_genres, "spelling"],
        ),
    ).validate()
    result = generate(config)
    result["verification"] = verify(config.path(config.generation.output))
    if args.check:
        missing = required.copy()
        with (config.path(config.generation.output) / "manifest.jsonl").open(
            encoding="utf-8"
        ) as stream:
            for row in map(json.loads, stream):
                missing.discard(row["attribution"]["source_id"])
        if missing:
            raise ValueError(
                f"Spelling check missed {len(missing)} cases; inspect fonts and chunking"
            )
        result["spelling_cases_checked"] = len(required)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
    try:
        if args.command == "setup":
            if args.limit < 1:
                raise ValueError("limit must be a positive integer")
            print(
                json.dumps(
                    setup(all_fonts=args.all_fonts, limit=args.limit, tatoeba=not args.offline),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        try:
            config, personal = resolve_config(args.config, load_config)
        except NotSetUp:
            # The app sets itself up on first use; other commands say what to run.
            if args.command != "ui" or args.config is not None:
                raise
            logging.getLogger(__name__).info("First run: setting up your workspace at %s", home())
            setup()
            config, personal = resolve_config(None, load_config)
        if personal and args.command == "ui" and not ready(config):
            setup()
        if personal:
            # Paths typed on the command line mean the current folder, not the workspace.
            for name in ("output", "dataset"):
                value = getattr(args, name, None)
                if value is not None and not Path(value).is_absolute():
                    setattr(args, name, type(value)(Path.cwd() / value))
        if args.command == "ui":
            if not 1 <= args.port <= 65535:
                raise ValueError("port must be between 1 and 65535")
            if args.cache_mb <= 0:
                raise ValueError("cache-mb must be a positive integer")
            try:
                import uvicorn

                from .web import create_app
            except ImportError as error:
                raise ValueError(
                    "The web UI needs its optional dependencies. "
                    "Start with: uv run --extra web synthocr ui"
                ) from error
            app = create_app(
                config,
                output_root=config.path(str(args.output_root)) if args.output_root else None,
                output_locations={
                    name: config.path(path) for name, path in args.output_locations.items()
                }
                if args.output_locations
                else None,
                cache_mb=args.cache_mb,
            )
            if not args.no_browser:
                import threading
                import webbrowser

                host = f"[{args.host}]" if ":" in args.host else args.host
                timer = threading.Timer(1, webbrowser.open, args=(f"http://{host}:{args.port}",))
                timer.daemon = True
                timer.start()
            uvicorn.run(app, host=args.host, port=args.port, workers=1)
            return 0
        elif args.command == "prepare":
            result = prepare(config, args.force)
        elif args.command == "experiment":
            from .experiments import run_experiment

            result = run_experiment(config, config.path(str(args.output)), args.samples, args.seed)
        elif args.command == "kurdish":
            result = _kurdish(config, args)
        elif args.command in {"generate", "benchmark"}:
            overrides = {
                name: getattr(args, name)
                for name in (
                    "samples",
                    "workers",
                    "seed",
                    "output",
                    "format",
                    "languages",
                    "language_weights",
                    "suite",
                    "exclude_genres",
                    *AUGMENTATION_FIELDS,
                )
                if getattr(args, name) is not None
            }
            config = replace(config, generation=replace(config.generation, **overrides))
            if args.fonts:
                config = replace(config, fonts=replace(config.fonts, include=args.fonts))
            config = config.validate()
            if args.command == "benchmark":
                with tempfile.TemporaryDirectory(prefix="synthocr-benchmark-") as directory:
                    config = replace(
                        config, generation=replace(config.generation, output=directory)
                    )
                    result = generate(config)
                    result.pop("output", None)
                    try:
                        import resource

                        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                        result["parent_peak_rss_bytes"] = (
                            peak if sys.platform == "darwin" else peak * 1024
                        )
                    except ImportError:
                        pass
            else:
                result = generate(config)
        elif args.command == "fonts":
            if args.font_command == "fetch":
                result = fetch(
                    config.path(args.catalog) if args.catalog else CATALOG,
                    config.path(args.destination),
                    args.family,
                )
            else:
                fonts = discover([config.path(p) for p in config.fonts.directories])
                if args.font_command == "list":
                    ids = font_ids(fonts)
                    result = {
                        "fonts": [
                            {"path": f.path, "face_index": f.index, "id": ids[f]} for f in fonts
                        ]
                    }
                else:
                    text = (
                        load_characters(config.path(str(args.chars)))
                        if args.chars
                        else load_profile(args.profile)
                        if args.profile
                        else args.text
                    )
                    result = check(fonts, text, config.fonts.coverage_cache_size)
                    if args.profile:
                        result["profile"] = args.profile
                    if (args.require_all and result["fully_supported"] != len(fonts)) or (
                        args.require_any and not result["fully_supported"]
                    ):
                        print(json.dumps(result, ensure_ascii=False, indent=2))
                        return 1
        elif args.command == "text":
            result = generate_text(
                config.path(str(args.output)), args.count, args.seed, args.genre, args.language
            )
        elif args.command == "spelling":
            result = write_spelling(config.path(str(args.output)), args.language, args.words_only)
        elif args.command == "verify":
            result = verify(config.path(str(args.dataset)), not args.metadata_only)
        elif args.command == "preview":
            result = preview(config, config.path(str(args.output)))
        elif args.command == "corpora":
            result = snapshot(
                config.path(config.data.raw),
                config.path(args.catalog),
                config.path(args.cache),
                args.language,
                args.snapshot,
                args.limit,
            )
        else:
            result = export(
                config.path(str(args.dataset)), config.path(str(args.output)), args.format
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, EOFError) as error:
        logging.error("%s", error)
        return 1
    except KeyboardInterrupt:
        logging.error("Interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
