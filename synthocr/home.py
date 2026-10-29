"""A personal workspace for installed copies that run outside a project folder."""

from __future__ import annotations

import logging
import os
import sys
from datetime import date
from pathlib import Path

from .config import Config, load_config
from .corpora import snapshot
from .fonts import CATALOG, fetch
from .text import generate_text, prepare

LOG = logging.getLogger(__name__)

# Open fonts that draw Sorani, Kurmanji, Arabic or English completely (about 9 MiB).
SETUP_FONTS = (
    "notosans",
    "notoserif",
    "notonaskharabic",
    "notosansarabic",
    "notonastaliqurdu",
    "amiri",
    "harmattan",
    "ibmplexsansarabic",
    "lateef",
    "scheherazadenew",
    "reemkufi",
    "cairo",
    "markazitext",
    "readexpro",
    "almarai",
    "tajawal",
)
# Tatoeba codes for the app's four languages, and the offline template languages.
SETUP_TATOEBA = ("ckb", "kmr", "ara", "eng")
SETUP_TEMPLATES = ("ckb", "ar", "en")
CONFIG = """\
# Created by `synthocr setup`. Paths are relative to this file.
[data]
raw = "data/raw"
processed = "data/processed"

[fonts]
directories = ["fonts"]

[generation]
output = "dataset/default"
"""


class NotSetUp(ValueError):
    """No config.toml here and no personal workspace yet."""


def home() -> Path:
    """The workspace folder: SYNTHOCR_HOME, or the platform's usual data folder."""
    if value := os.environ.get("SYNTHOCR_HOME"):
        return Path(value).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "SynthOCR"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SynthOCR"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "synthocr"


def resolve_config(path: Path | None, loader=load_config) -> tuple[Config, bool]:
    """Load an explicit or local config.toml; otherwise the personal workspace's.

    The second value says whether the personal workspace is used.
    """
    if path is not None:
        return loader(path), False
    if Path("config.toml").is_file():
        return loader(Path("config.toml")), False
    workspace = home() / "config.toml"
    if not workspace.is_file():
        raise NotSetUp("SynthOCR is not set up yet. Run: synthocr setup")
    return loader(workspace), True


def ready(config: Config) -> bool:
    return any(config.path("fonts").rglob("OFL.txt")) and any(
        config.path(config.data.raw).rglob("*.jsonl")
    )


def setup(
    root: Path | None = None,
    *,
    all_fonts: bool = False,
    limit: int = 20_000,
    tatoeba: bool = True,
) -> dict:
    """Create the workspace, fetch open fonts and text, and prepare the text index.

    Safe to run again: downloads are checked against pinned checksums and kept.
    """
    root = (root or home()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "config.toml"
    if not config_path.exists():
        config_path.write_text(CONFIG, encoding="utf-8")
    config = load_config(config_path)
    LOG.info("Workspace: %s", root)

    LOG.info("Downloading open fonts…")
    fonts = fetch(CATALOG, config.path("fonts"), None if all_fonts else list(SETUP_FONTS))

    raw = config.path(config.data.raw)
    templates = []
    for language in SETUP_TEMPLATES:
        output = raw / "synthetic" / f"{language}.jsonl"
        if not output.exists():
            LOG.info("Writing offline %s text templates…", language)
            generate_text(output, limit, 42, "mixed", language)
        templates.append(language)

    sentences, skipped = 0, None
    # One snapshot is enough; running setup again on another day adds no copies.
    if tatoeba and not (raw / "tatoeba").exists():
        LOG.info("Downloading Tatoeba sentences (CC BY 2.0 France)…")
        try:
            sentences = snapshot(
                raw,
                root / "data" / "corpora.json",
                root / ".cache" / "corpora",
                list(SETUP_TATOEBA),
                date.today().isoformat(),
                limit,
            )["records"]
        except (OSError, ValueError) as error:
            # Templates and the bundled spelling cases still work offline.
            skipped = str(error)
            LOG.warning("Tatoeba download skipped: %s", error)

    LOG.info("Preparing the text index…")
    prepared = prepare(config)
    return {
        "workspace": str(root),
        "fonts": fonts["families"],
        "templates": templates,
        "tatoeba_sentences": sentences,
        "tatoeba_skipped": skipped,
        "texts": prepared["records"],
    }
