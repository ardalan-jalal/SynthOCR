"""Shared font discovery, bounded coverage caches, and verified font downloads."""

import hashlib
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from urllib.request import urlopen

from fontTools.ttLib import TTCollection, TTFont, TTLibError
from PIL import ImageFont

from .downloads import download, within
from .text import required_codepoints as required_codepoints

LOG = logging.getLogger(__name__)
FONT_SUFFIXES = {".ttf", ".otf", ".ttc", ".otc"}
# Pinned open-font downloads, shipped with the package so installed copies can fetch them.
CATALOG = Path(__file__).with_name("font_catalog.json")
MAX_FONT_BYTES = 64 * 1024 * 1024
PROFILES = ("sorani", "kurmanji", "kurdish")


@dataclass(frozen=True)
class Font:
    path: str
    index: int = 0

    @property
    def name(self):
        return f"{Path(self.path).name}#{self.index}"


def discover(directories: list[Path]) -> list[Font]:
    result = []
    seen = set()
    for directory in directories:
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in FONT_SUFFIXES:
                continue
            if path.stat().st_size > MAX_FONT_BYTES:
                LOG.warning("Skipping font larger than 64 MiB: %s", path)
                continue
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            try:
                if path.suffix.lower() in {".ttc", ".otc"}:
                    with TTCollection(path, lazy=True) as collection:
                        faces = len(collection.fonts)
                else:
                    faces = 1
                for index in range(faces):
                    font = Font(str(path.resolve()), index)
                    ImageFont.truetype(font.path, 12, index=index)
                    result.append(font)
            except (OSError, ValueError, TTLibError) as error:
                LOG.warning("Skipping unreadable font %s: %s", path, error)
    if not result:
        raise ValueError("No usable TTF/OTF/TTC/OTC fonts found in the configured directories")
    return result


def font_ids(fonts: list[Font]) -> dict[Font, str]:
    """Return each face's stable ID: the file's SHA-256 and the face index."""
    digests = {}
    result = {}
    for font in fonts:
        if font.path not in digests:
            with Path(font.path).open("rb") as stream:
                digests[font.path] = hashlib.file_digest(stream, "sha256").hexdigest()
        result[font] = f"{digests[font.path]}:{font.index}"
    return result


def select(fonts: list[Font], include: list[str], ids: dict[Font, str] | None = None) -> list[Font]:
    """Keep the faces named in include (file name, "name#face" or ID); empty keeps all."""
    if not include:
        return fonts
    wanted = set(include)
    ids = ids or font_ids(fonts)
    chosen = [font for font in fonts if wanted & {ids[font], Path(font.path).name, font.name}]
    if not chosen:
        raise ValueError("None of the chosen fonts are available; choose at least one font")
    return chosen


class Coverage:
    def __init__(self, limit: int = 64):
        self.limit = limit
        self.cache: OrderedDict[Font, bytes] = OrderedDict()

    def _mask(self, font: Font) -> bytes:
        if font in self.cache:
            self.cache.move_to_end(font)
            return self.cache[font]
        with TTFont(font.path, fontNumber=font.index, lazy=True) as face:
            cmap = face.getBestCmap() or {}
            maximum = min(max(cmap, default=0), 0x10FFFF)
            bits = bytearray((maximum >> 3) + 1)
            for cp, glyph in cmap.items():
                if 0 <= cp <= 0x10FFFF and glyph != ".notdef":
                    bits[cp >> 3] |= 1 << (cp & 7)
            value = bytes(bits)
        self.cache[font] = value
        if len(self.cache) > self.limit:
            self.cache.popitem(last=False)
        return value

    def missing(self, font: Font, needed: frozenset[int]) -> frozenset[int]:
        bits = self._mask(font)
        return frozenset(
            cp for cp in needed if cp >> 3 >= len(bits) or not bits[cp >> 3] & (1 << (cp & 7))
        )

    def codepoints(self, font: Font) -> frozenset[int]:
        """Expanded inspection view; generation keeps only compact bitmap caches."""
        return frozenset(
            i * 8 + b
            for i, byte in enumerate(self._mask(font))
            if byte
            for b in range(8)
            if byte & (1 << b)
        )

    def choose(self, fonts: list[Font], text: str, offset: int, variant: int = 0) -> Font | None:
        needed = required_codepoints(text)
        matches = 0
        for step in range(len(fonts)):
            font = fonts[(offset + step) % len(fonts)]
            if not self.missing(font, needed):
                if matches == variant:
                    return font
                matches += 1
        return None


def load_characters(path: Path) -> str:
    """Accept the original font-checking JSON mapping/list and plain UTF-8 text."""
    if path.suffix.lower() != ".json":
        characters = set()
        with path.open(encoding="utf-8") as stream:
            while chunk := stream.read(65536):
                characters.update(chunk)
        return "".join(sorted(characters))
    return _characters(json.loads(path.read_text(encoding="utf-8")))


def load_profile(name: str) -> str:
    """Load a bundled reference without a checkout or companion repository."""
    if name not in PROFILES:
        raise ValueError(f"Unknown font profile: {name}")
    if name == "kurdish":
        resource = files("synthocr").joinpath("kurdish_characters.json")
        return _characters(json.loads(resource.read_text(encoding="utf-8")))
    profiles = json.loads(
        files("synthocr").joinpath("font_profiles.json").read_text(encoding="utf-8")
    )
    profile = profiles[name]
    return profile["letters"] + profile["digits"] + profile["punctuation"]


def _characters(data) -> str:
    if isinstance(data, dict):
        values = list(data.values())
        if all(isinstance(v, dict) for v in values):
            return "".join(v["char"] for v in values if "char" in v)
        return "".join(data)
    if isinstance(data, list):
        return "".join(v["char"] if isinstance(v, dict) else v for v in data)
    raise ValueError("Character JSON must contain a mapping or a list")


def check(fonts: list[Font], text: str, cache_size: int = 64) -> dict:
    coverage = Coverage(cache_size)
    needed = required_codepoints(text)
    results = []
    for font in fonts:
        missing = sorted(coverage.missing(font, needed))
        results.append(
            {
                "font": font.name,
                "path": font.path,
                "face_index": font.index,
                "supported": len(needed) - len(missing),
                "required": len(needed),
                "missing": [f"U+{cp:04X}" for cp in missing],
                "missing_text": "".join(map(chr, missing)),
            }
        )
    return {
        "fonts": len(fonts),
        "characters": len(needed),
        "fully_supported": sum(not row["missing"] for row in results),
        "results": results,
    }


def _download(url: str, digest: str, target: Path, max_bytes: int):
    return download(url, digest, target, max_bytes, {"raw.githubusercontent.com"}, opener=urlopen)


def fetch(catalog: Path, destination: Path, families: list[str] | None = None) -> dict:
    entries = json.loads(catalog.read_text(encoding="utf-8"))["fonts"]
    available = {entry["family"] for entry in entries}
    if families and set(families) - available:
        raise ValueError(f"Unknown families: {sorted(set(families) - available)}")
    installed = []
    for entry in entries:
        if families and entry["family"] not in families:
            continue
        for key in ("file", "license_file"):
            value = entry[key]
            _download(
                value["url"],
                value["sha256"],
                within(destination, value["name"]),
                MAX_FONT_BYTES if key == "file" else 512 * 1024,
            )
        installed.append(entry["family"])
    return {"families": installed, "destination": str(destination)}
