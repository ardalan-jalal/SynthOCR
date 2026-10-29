"""Small, typed TOML configuration with paths anchored to the config file."""

import re
import tomllib
from dataclasses import dataclass, field, fields, replace
from math import fsum, isfinite
from pathlib import Path, PurePosixPath

DOCUMENT_LAYERS = (
    "paper_texture",
    "grain",
    "shadow",
    "stains",
    "creases",
    "ruled_lines",
    "scan_banding",
    "speckles",
    "binding_shadow",
)
LAYER_FIELDS = DOCUMENT_LAYERS
# Plain names for the effect layers, shared by the API and the web page.
DOCUMENT_LAYER_LABELS = {
    "paper_texture": ("Paper texture", "Fibers and uneven paper"),
    "grain": ("Grain", "Scanner or camera noise"),
    "shadow": ("Shadows", "Uneven light across the page"),
    "stains": ("Stains", "Soft marks on the paper"),
    "creases": ("Folds", "Light creases in the page"),
    "ruled_lines": ("Notebook lines", "Horizontal writing guides"),
    "scan_banding": ("Scan bands", "Faint lines from scanning or printing"),
    "speckles": ("Speckles", "Paper fibers and toner dots"),
    "binding_shadow": ("Book spine shadow", "Shading near a bound edge"),
}

# Recipes supply ready-made palettes and layer defaults. Explicit overrides
# remain available, and geometry settings are independent of the recipe.
DOCUMENT_PRESETS = {
    "clean-paper": {
        "label": "Clean paper",
        "description": "Light paper fibers and a small amount of fine grain.",
        "background": "plain",
        "color_mode": "grayscale",
        "background_color": "#f8f8f5",
        "background_color_end": "#f8f8f5",
        "text_color": "#202020",
        "layers": dict(zip(DOCUMENT_LAYERS, (0.18, 0.03, 0, 0, 0, 0, 0, 0, 0), strict=True)),
    },
    "office-scan": {
        "label": "Office scan",
        "description": "Fine scanner grain, soft illumination and faint scan bands.",
        "background": "plain",
        "color_mode": "grayscale",
        "background_color": "#f4f4f4",
        "background_color_end": "#f4f4f4",
        "text_color": "#202020",
        "layers": dict(zip(DOCUMENT_LAYERS, (0.12, 0.22, 0.16, 0, 0, 0, 0.12, 0, 0), strict=True)),
    },
    "aged-paper": {
        "label": "Aged paper",
        "description": "Mottled fibers, faded stains and light fold marks.",
        "background": "plain",
        "color_mode": "rgb",
        "background_color": "#ecdeb9",
        "background_color_end": "#ecdeb9",
        "text_color": "#342a1e",
        "layers": dict(
            zip(DOCUMENT_LAYERS, (0.5, 0.14, 0.15, 0.55, 0.2, 0, 0, 0.12, 0), strict=True)
        ),
    },
    "notebook": {
        "label": "Notebook",
        "description": "Ruled writing paper with a soft paper texture.",
        "background": "plain",
        "color_mode": "rgb",
        "background_color": "#f7f5ea",
        "background_color_end": "#f7f5ea",
        "text_color": "#242c35",
        "layers": dict(zip(DOCUMENT_LAYERS, (0.22, 0.06, 0, 0, 0, 0.6, 0, 0, 0), strict=True)),
    },
    "phone-photo": {
        "label": "Phone photo",
        "description": "Directional lighting, soft edge shadows and camera grain.",
        "background": "gradient",
        "color_mode": "rgb",
        "background_color": "#f3f0e6",
        "background_color_end": "#d9d6ca",
        "text_color": "#252a2b",
        "layers": dict(zip(DOCUMENT_LAYERS, (0.16, 0.2, 0.72, 0, 0.08, 0, 0, 0, 0), strict=True)),
    },
    "photocopy": {
        "label": "Photocopy",
        "description": "Stronger copy grain with subtle uneven toner density.",
        "background": "plain",
        "color_mode": "grayscale",
        "background_color": "#eeeeee",
        "background_color_end": "#eeeeee",
        "text_color": "#232323",
        "layers": dict(
            zip(DOCUMENT_LAYERS, (0.3, 0.7, 0.22, 0.06, 0, 0, 0.2, 0.25, 0), strict=True)
        ),
    },
    "thermal-receipt": {
        "label": "Thermal receipt",
        "description": "Warm receipt paper with fine horizontal printer bands.",
        "background": "plain",
        "color_mode": "rgb",
        "background_color": "#f4eee0",
        "background_color_end": "#f4eee0",
        "text_color": "#3c3933",
        "layers": dict(
            zip(DOCUMENT_LAYERS, (0.12, 0.12, 0.08, 0, 0.08, 0, 0.6, 0.03, 0), strict=True)
        ),
    },
    "newsprint": {
        "label": "Newsprint",
        "description": "Gray recycled newsprint with fibers and fine paper flecks.",
        "background": "plain",
        "color_mode": "rgb",
        "background_color": "#d9d7cb",
        "background_color_end": "#d9d7cb",
        "text_color": "#2c2b27",
        "layers": dict(zip(DOCUMENT_LAYERS, (0.6, 0.3, 0.08, 0, 0, 0, 0, 0.55, 0), strict=True)),
    },
    "recycled-paper": {
        "label": "Recycled paper",
        "description": "Natural beige paper with visible fibers and uneven flecks.",
        "background": "plain",
        "color_mode": "rgb",
        "background_color": "#e6ddc8",
        "background_color_end": "#e6ddc8",
        "text_color": "#302d25",
        "layers": dict(zip(DOCUMENT_LAYERS, (0.65, 0.12, 0.06, 0.1, 0, 0, 0, 0.8, 0), strict=True)),
    },
    "book-page": {
        "label": "Book page",
        "description": "Cream book paper with a soft shadow near the binding.",
        "background": "plain",
        "color_mode": "rgb",
        "background_color": "#f3ead5",
        "background_color_end": "#f3ead5",
        "text_color": "#302a23",
        "layers": dict(
            zip(DOCUMENT_LAYERS, (0.25, 0.08, 0.08, 0, 0, 0, 0, 0.04, 0.7), strict=True)
        ),
    },
    "fax": {
        "label": "Fax",
        "description": "Grayscale fax paper with horizontal bands and scattered toner.",
        "background": "plain",
        "color_mode": "grayscale",
        "background_color": "#f0f0ee",
        "background_color_end": "#f0f0ee",
        "text_color": "#292929",
        "layers": dict(zip(DOCUMENT_LAYERS, (0.08, 0.5, 0.06, 0, 0, 0, 0.8, 0.45, 0), strict=True)),
    },
    "crumpled-paper": {
        "label": "Crumpled paper",
        "description": "Soft diagonal folds and creases on lightly textured paper.",
        "background": "plain",
        "color_mode": "rgb",
        "background_color": "#eeeadd",
        "background_color_end": "#eeeadd",
        "text_color": "#2b2b26",
        "layers": dict(
            zip(DOCUMENT_LAYERS, (0.3, 0.1, 0.2, 0.04, 0.8, 0, 0, 0.05, 0), strict=True)
        ),
    },
}

AUGMENTATION_FIELDS = (
    "augmentation",
    "background",
    "color_mode",
    "background_color",
    "background_color_end",
    "text_color",
    "texture_strength",
    "rotation",
    "blur",
    "contrast",
    "background_presets",
    "background_weights",
    "preset_colors",
    *DOCUMENT_LAYERS,
)


def background_distribution(settings) -> dict[str, float]:
    """Conditional recipe shares for validated settings; missing weights are one."""
    weights = {
        name: float(settings.background_weights.get(name, 1))
        for name in settings.background_presets
    }
    total = fsum(weights.values())
    return {name: weight / total for name, weight in weights.items()}


def document_layers(settings, preset: str = "") -> dict[str, float]:
    """Resolve explicit overrides; zero disables and None inherits a recipe."""
    defaults = DOCUMENT_PRESETS[preset]["layers"] if preset else {}
    return {
        name: float(
            defaults.get(name, 0) if getattr(settings, name) is None else getattr(settings, name)
        )
        for name in DOCUMENT_LAYERS
    }


def resolve_document_settings(settings, preset: str = ""):
    """Return a preset's ready-made palette, or retain explicit custom colors."""
    if not preset or not settings.preset_colors:
        return settings
    recipe = DOCUMENT_PRESETS[preset]
    return replace(
        settings,
        **{
            name: recipe[name]
            for name in (
                "background",
                "color_mode",
                "background_color",
                "background_color_end",
                "text_color",
            )
        },
        texture_strength=0,
    )


def _rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))


def _luminance(rgb, grayscale):
    if grayscale:
        gray = int(
            0.5
            + sum(
                channel * weight for channel, weight in zip(rgb, (0.299, 0.587, 0.114), strict=True)
            )
        )
        rgb = (gray, gray, gray)
    linear = [
        value / 255 / 12.92 if value / 255 <= 0.04045 else ((value / 255 + 0.055) / 1.055) ** 2.4
        for value in rgb
    ]
    return sum(
        value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
    )


def augmentation_contrast(settings) -> float:
    """Minimum base-color contrast across the actual RGB or grayscale gradient."""
    foreground = _luminance(_rgb(settings.text_color), settings.color_mode == "grayscale")
    start = _rgb(settings.background_color)
    end = _rgb(settings.background_color_end) if settings.background == "gradient" else start
    minimum = 21.0
    for step in range(17):
        color = tuple(int(0.5 + a + (b - a) * step / 16) for a, b in zip(start, end, strict=True))
        background = _luminance(color, settings.color_mode == "grayscale")
        minimum = min(
            minimum, (max(foreground, background) + 0.05) / (min(foreground, background) + 0.05)
        )
    return minimum


def validate_augmentation(settings) -> None:
    for name, maximum in (
        ("augmentation", 1),
        ("texture_strength", 0.25),
        ("rotation", 10),
        ("blur", 2),
        ("contrast", 0.5),
    ):
        value = getattr(settings, name)
        if type(value) not in {int, float} or not isfinite(value) or not 0 <= value <= maximum:
            raise ValueError(f"{name} must be a finite number between 0 and {maximum}")
    presets = settings.background_presets
    if (
        not isinstance(presets, list)
        or len(presets) > len(DOCUMENT_PRESETS)
        or any(not isinstance(value, str) or value not in DOCUMENT_PRESETS for value in presets)
        or len(set(presets)) != len(presets)
    ):
        raise ValueError("background_presets must be a list of distinct supported document presets")
    weights = settings.background_weights
    if not isinstance(weights, dict) or len(weights) > len(DOCUMENT_PRESETS):
        raise ValueError("background_weights must be a mapping of selected presets to weights")
    for name, value in weights.items():
        if name not in presets:
            raise ValueError("background_weights keys must be selected background_presets")
        if type(value) not in {int, float} or not 0 <= value <= 1_000_000 or not isfinite(value):
            raise ValueError(
                "background_weights values must be finite numbers between 0 and 1000000"
            )
    if presets and not any(weights.get(name, 1) > 0 for name in presets):
        raise ValueError(
            "background_weights must give at least one selected preset a positive weight"
        )
    if type(settings.preset_colors) is not bool:
        raise ValueError("preset_colors must be a boolean")
    for name in DOCUMENT_LAYERS:
        value = getattr(settings, name)
        if value is not None and (
            type(value) not in {int, float} or not isfinite(value) or not 0 <= value <= 1
        ):
            raise ValueError(f"{name} must be null or a finite number between 0 and 1")
    if not isinstance(settings.background, str) or settings.background not in {
        "paper",
        "plain",
        "gradient",
        "noise",
    }:
        raise ValueError("background must be paper, plain, gradient or noise")
    if not isinstance(settings.color_mode, str) or settings.color_mode not in {"grayscale", "rgb"}:
        raise ValueError("color_mode must be grayscale or rgb")
    for name in ("background_color", "background_color_end", "text_color"):
        value = getattr(settings, name)
        if not isinstance(value, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
            raise ValueError(f"{name} must be a six-digit hex color, such as #f5f1e8")
    # Preset palettes are bundled reviewed values. Explicit palettes retain the
    # same guard as legacy custom backgrounds, including intermediate gradients.
    if (
        settings.augmentation > 0
        and (not presets or not settings.preset_colors)
        and augmentation_contrast(settings) < 3
    ):
        raise ValueError(
            "Text and background colors need at least 3:1 base contrast across the background; "
            "choose lighter paper or darker text"
        )


@dataclass(frozen=True)
class Data:
    raw: str = "data/raw"
    processed: str = "data/processed"
    max_chars: int = 100
    normalization: str = "NFC"
    deduplicate: bool = True
    write_jsonl: bool = False
    max_line_chars: int = 1_000_000
    source_languages: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Fonts:
    directories: list[str] = field(default_factory=lambda: ["fonts"])
    coverage: str = "strict"
    cache_size: int = 32
    coverage_cache_size: int = 128
    # Fonts to use, by file name, "name#face" or content ID; empty uses all.
    include: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Generation:
    output: str = "dataset/default"
    samples: int = 1000
    seed: int = 42
    workers: int = 1
    font_size: int = 48
    padding: int = 12
    max_width: int = 4096
    max_height: int = 512
    max_pixels: int = 2_097_152
    format: str = "png"
    direction: str = "auto"
    language: str = ""
    languages: list[str] = field(default_factory=list)
    # Relative share of each selected language; empty draws from all of them as one pool.
    language_weights: dict[str, float] = field(default_factory=dict)
    exclude_genres: list[str] = field(default_factory=list)
    suite: str = ""
    augmentation: float = 0.25
    background: str = "paper"
    color_mode: str = "grayscale"
    background_color: str = "#f5f1e8"
    background_color_end: str = "#e4e9ed"
    text_color: str = "#1a1a1a"
    texture_strength: float = 0.04
    rotation: float = 2
    blur: float = 0.55
    contrast: float = 0.1
    background_presets: list[str] = field(default_factory=list)
    background_weights: dict[str, float] = field(default_factory=dict)
    preset_colors: bool = True
    paper_texture: float | None = None
    grain: float | None = None
    shadow: float | None = None
    stains: float | None = None
    creases: float | None = None
    ruled_lines: float | None = None
    scan_banding: float | None = None
    speckles: float | None = None
    binding_shadow: float | None = None
    train_ratio: float = 0.9
    validation_ratio: float = 0.05


# Convenience starting points for the UI. These are complete ordinary appearance
# settings, so editing a preset uses the same validation and renderer as custom
# settings. Keep API and CLI defaults independent for backwards compatibility.
IMAGE_PRESETS = {
    identifier: {
        "label": label,
        "description": description,
        "settings": {name: getattr(settings, name) for name in AUGMENTATION_FIELDS},
    }
    for identifier, label, description, settings in (
        (
            "balanced",
            "Balanced",
            "A mix of clean images, scanned pages, aged paper and phone photos.",
            Generation(
                augmentation=0.75,
                background_presets=["office-scan", "aged-paper", "phone-photo"],
            ),
        ),
        (
            "clean",
            "Clean",
            "Black text on white, with no image effects.",
            Generation(augmentation=0),
        ),
        (
            "varied",
            "Varied",
            "All document styles, with image effects applied to every image.",
            Generation(augmentation=1, background_presets=list(DOCUMENT_PRESETS)),
        ),
    )
}


@dataclass(frozen=True)
class Config:
    root: Path
    data: Data = field(default_factory=Data)
    fonts: Fonts = field(default_factory=Fonts)
    generation: Generation = field(default_factory=Generation)

    def path(self, value: str) -> Path:
        return (self.root / value).resolve()

    def validate(self) -> Config:
        positive = {
            "max_chars": self.data.max_chars,
            "max_line_chars": self.data.max_line_chars,
            "samples": self.generation.samples,
            "font_size": self.generation.font_size,
            "max_width": self.generation.max_width,
            "max_height": self.generation.max_height,
            "max_pixels": self.generation.max_pixels,
            "cache_size": self.fonts.cache_size,
            "coverage_cache_size": self.fonts.coverage_cache_size,
        }
        for name, value in positive.items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        g = self.generation
        validate_augmentation(g)
        if type(g.workers) is not int or not 1 <= g.workers <= 16:
            raise ValueError("workers must be between 1 and 16")
        if type(g.seed) is not int:
            raise ValueError("seed must be an integer")
        if type(g.padding) is not int or not 0 <= g.padding < min(g.max_width, g.max_height) / 2:
            raise ValueError("padding must be nonnegative and fit inside the image")
        if (
            g.font_size > 1024
            or g.max_pixels > 16_777_216
            or self.fonts.cache_size > 256
            or self.fonts.coverage_cache_size > 512
        ):
            raise ValueError("font_size, max_pixels or cache_size exceeds the memory safety limit")
        for value in (g.augmentation, g.train_ratio, g.validation_ratio):
            if not isinstance(value, (float, int)) or not isfinite(value) or not 0 <= value <= 1:
                raise ValueError("augmentation and split ratios must be finite values in [0, 1]")
        if g.train_ratio + g.validation_ratio > 1:
            raise ValueError("train_ratio + validation_ratio must be <= 1")
        if g.format not in {"png", "jpg", "webp", "tif"}:
            raise ValueError("format must be png, jpg, webp or tif")
        if g.direction not in {"auto", "ltr", "rtl", "ttb"}:
            raise ValueError("direction must be auto, ltr, rtl or ttb")
        if self.fonts.coverage not in {"strict", "off"}:
            raise ValueError("coverage must be strict or off")
        if (
            not isinstance(self.fonts.include, list)
            or len(self.fonts.include) > 1000
            or any(
                not isinstance(value, str) or not 0 < len(value) <= 300
                for value in self.fonts.include
            )
        ):
            raise ValueError("fonts.include must be a list of at most 1000 font names or IDs")
        if not isinstance(g.suite, str) or g.suite not in {"", "kurdish-spelling"}:
            raise ValueError("suite must be empty or kurdish-spelling")
        if self.data.normalization not in {"NFC", "NFD", "NFKC", "NFKD", "none"}:
            raise ValueError("Unknown Unicode normalization")
        if type(self.data.deduplicate) is not bool:
            raise ValueError("deduplicate must be a boolean")
        if type(self.data.write_jsonl) is not bool:
            raise ValueError("write_jsonl must be a boolean")

        def valid_language(value):
            return isinstance(value, str) and re.fullmatch(
                r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", value
            )

        if (
            not isinstance(g.languages, list)
            or len(g.languages) > 100
            or any(not valid_language(value) for value in g.languages)
        ):
            raise ValueError("languages must be a list of at most 100 language tags")
        if (
            not isinstance(g.exclude_genres, list)
            or len(g.exclude_genres) > 100
            or any(not isinstance(value, str) or not value for value in g.exclude_genres)
        ):
            raise ValueError("exclude_genres must be a list of at most 100 nonempty genre names")
        weights = g.language_weights
        if not isinstance(weights, dict) or len(weights) > 100:
            raise ValueError("language_weights must map selected languages to weights")
        if weights:
            from .corpus_store import canonical_language

            selected = {canonical_language(value) for value in g.languages}
            keys = [canonical_language(name) if valid_language(name) else "" for name in weights]
            if not all(keys) or len(set(keys)) != len(keys) or not set(keys) <= selected:
                raise ValueError("language_weights keys must be distinct selected languages")
            if any(
                type(value) not in {int, float}
                or not isfinite(value)
                or not 0 <= value <= 1_000_000
                for value in weights.values()
            ):
                raise ValueError(
                    "language_weights values must be finite numbers between 0 and 1000000"
                )
            # Omitted languages default to weight 1, as with background_weights.
            canonical = dict(zip(keys, weights.values(), strict=True))
            if not any(canonical.get(language, 1) > 0 for language in selected):
                raise ValueError(
                    "language_weights must give at least one language a positive weight"
                )
        if not isinstance(self.data.source_languages, dict):
            raise ValueError("source_languages must map relative raw source paths to language tags")
        for source, language in self.data.source_languages.items():
            if (
                not isinstance(source, str)
                or not source
                or PurePosixPath(source).is_absolute()
                or ".." in PurePosixPath(source).parts
                or "\\" in source
                or PurePosixPath(source).as_posix() != source
                or not valid_language(language)
            ):
                raise ValueError(
                    "source_languages must map relative raw source paths to language tags"
                )
        if not isinstance(self.fonts.directories, list) or not self.fonts.directories:
            raise ValueError("fonts.directories must be a nonempty list")
        paths = [self.data.raw, self.data.processed, g.output, *self.fonts.directories]
        if any(not isinstance(p, str) or not p for p in paths):
            raise ValueError("Paths must be nonempty strings")
        raw, processed, output = map(self.path, (self.data.raw, self.data.processed, g.output))
        for a, b in ((raw, processed), (raw, output), (processed, output)):
            if a == b or a in b.parents or b in a.parents:
                raise ValueError("raw, processed and output paths must be separate directories")
        return self


def load_config(path: str | Path = "config.toml") -> Config:
    path = Path(path).resolve()
    with path.open("rb") as stream:
        values = tomllib.load(stream)
    unknown = values.keys() - {"data", "fonts", "generation"}
    if unknown:
        raise ValueError(f"Unknown config sections: {sorted(unknown)}")
    sections = {}
    for name, cls in (("data", Data), ("fonts", Fonts), ("generation", Generation)):
        section = values.get(name, {})
        if not isinstance(section, dict):
            raise ValueError(f"{name} must be a TOML table")
        unknown = section.keys() - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown {name} settings: {sorted(unknown)}")
        sections[name] = cls(**section)
    return Config(root=path.parent, **sections).validate()
