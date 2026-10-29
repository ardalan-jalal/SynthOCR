"""Bounded, reproducible visual comparisons using the maintained renderer."""

import hashlib
import heapq
import json
import math
import platform
import sys
import time
from dataclasses import asdict, replace
from importlib.metadata import version
from pathlib import Path

from PIL import Image, ImageDraw, ImageStat, features

from . import __version__
from .config import (
    DOCUMENT_LAYERS,
    DOCUMENT_PRESETS,
    Config,
    augmentation_contrast,
    resolve_document_settings,
)
from .downloads import sha256
from .fonts import Coverage, discover
from .render import Renderer
from .spelling import spelling_cases

PRESETS = (
    ("clean", {"augmentation": 0}),
    *((name, {"augmentation": 1, "background_presets": [name]}) for name in DOCUMENT_PRESETS),
)
MAX_CASES = 64
SHEET_CASES = 12
SHEET_COLUMNS = 4
SHEET_ROWS = 12


def _write_json(path: Path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def _selected_cases(samples: int, seed: int):
    # Hash-ranked reservoirs keep at most `samples` rows, independent of any
    # future growth in the diagnostic catalog. Small feature reservoirs make
    # the default useful for actual shaping failures, rather than mostly words.
    limits = {"ckb": (samples + 1) // 2, "kmr": samples // 2}
    priorities = {
        "ckb": (
            "combining_marks",
            "join_controls",
            "mixed_direction",
            "digit_sets",
            "word_boundaries",
            "core_alphabet",
        ),
        "kmr": (
            "canonical_accents",
            "capitalization",
            "mixed_direction",
            "word_boundaries",
            "accented_letters",
            "punctuation",
        ),
    }
    selected = {language: [] for language in limits}
    features = {language: {} for language in limits}
    for case in spelling_cases():
        language = case["language"]
        limit = limits[language]
        if not limit:
            continue
        identifier = case["attribution"]["source_id"]
        rank = int.from_bytes(hashlib.sha256(f"{seed}:{identifier}".encode()).digest(), "big")
        item = (-rank, identifier, case)
        for feature in case["attribution"]["features"]:
            if feature in priorities[language] and (
                feature not in features[language] or item[0] > features[language][feature][0]
            ):
                features[language][feature] = item
        heap = selected[language]
        if len(heap) < limit:
            heapq.heappush(heap, item)
        elif rank < -heap[0][0]:
            heapq.heapreplace(heap, item)
    ordered = {}
    for language, limit in limits.items():
        candidates = [
            features[language][feature]
            for feature in priorities[language]
            if feature in features[language]
        ] + sorted(selected[language], reverse=True)
        chosen = {}
        for _, identifier, case in candidates:
            if len(chosen) < limit:
                chosen.setdefault(identifier, case)
        ordered[language] = list(chosen.values())
    rows = []
    for index in range(max(limits.values())):
        for language in limits:
            if index < len(ordered[language]):
                rows.append(ordered[language][index])
    if len(rows) != samples:
        raise ValueError("The bundled diagnostic catalog has too few cases for this experiment")
    return rows


def _peak_rss():
    try:
        import resource
    except ImportError:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def _metrics(image, metadata):
    width, height = image.size
    left, top, right, bottom = metadata["bbox"]
    if not 0 <= left < right <= width or not 0 <= top < bottom <= height:
        raise ValueError("Rendered ink bounds are invalid")
    with image.convert("L") as gray:
        low, high = gray.getextrema()
        return {
            "ink_margin_px": min(left, top, width - right, height - bottom),
            "luminance_min": low,
            "luminance_max": high,
            "luminance_mean": round(ImageStat.Stat(gray).mean[0], 3),
            "corner_luminance": [
                gray.getpixel(point)
                for point in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
            ],
        }


def _contact_sheet(output: Path, cases: int):
    # Keep one small page and one decoded source image alive. Adding recipes
    # creates more pages rather than a wider canvas; only 12 cases are shown.
    shown = min(cases, SHEET_CASES)
    cell_width, cell_height, gap = 320, 112, 12
    columns = min(len(PRESETS), SHEET_COLUMNS)
    rows_per_case = math.ceil(len(PRESETS) / columns)
    cases_per_page = max(1, SHEET_ROWS // rows_per_case)
    case_header = 24
    case_height = case_header + rows_per_case * cell_height
    pages = []
    for first in range(0, shown, cases_per_page):
        count = min(cases_per_page, shown - first)
        filename = "contact-sheet.png" if not pages else f"contact-sheet-{len(pages) + 1:03}.png"
        dimensions = (columns * cell_width + gap, 54 + count * case_height)
        sheet = Image.new("RGB", dimensions, "#edf0ee")
        try:
            draw = ImageDraw.Draw(sheet)
            draw.text(
                (gap, 12),
                f"Document backgrounds - cases {first + 1} to {first + count}",
                fill="#183d32",
            )
            with (output / "manifest.jsonl").open(encoding="utf-8") as stream:
                for line in stream:
                    row = json.loads(line)
                    if not first <= row["case_index"] < first + count:
                        continue
                    case_y = 44 + (row["case_index"] - first) * case_height
                    if not row["preset_index"]:
                        draw.text(
                            (gap, case_y),
                            f"{row['case_id']} | {row['language']}",
                            fill="#40504a",
                        )
                    x = row["preset_index"] % columns * cell_width + gap
                    y = case_y + case_header + row["preset_index"] // columns * cell_height
                    draw.rectangle(
                        (x, y, x + cell_width - gap, y + cell_height - gap), fill="white"
                    )
                    label = (
                        "Original"
                        if row["preset"] == "clean"
                        else DOCUMENT_PRESETS[row["preset"]]["label"]
                    )
                    draw.text((x + 6, y + 5), label, fill="#183d32")
                    thumb = Image.open(output / row["image"])
                    try:
                        thumb.thumbnail((cell_width - 2 * gap, cell_height - 38))
                        sheet.paste(
                            thumb,
                            (x + gap, y + 22 + (cell_height - 38 - thumb.height) // 2),
                        )
                    finally:
                        thumb.close()
            with (output / filename).open("xb") as stream:
                sheet.save(stream, format="PNG")
        finally:
            sheet.close()
        pages.append(
            {
                "file": filename,
                "sha256": sha256(output / filename),
                "first_case_index": first,
                "cases": count,
                "width": dimensions[0],
                "height": dimensions[1],
            }
        )
    return pages


def run_experiment(config: Config, output: Path, samples: int = 12, seed: int = 42) -> dict:
    """Compare document presets without reading the training corpus.

    This writes attributed diagnostic images and evidence, not a training
    dataset. Structural measurements do not establish OCR or linguistic quality.
    A failed render leaves an explicit incomplete report and is re-raised.
    """
    if type(samples) is not int or not 1 <= samples <= MAX_CASES:
        raise ValueError(f"samples must be an integer between 1 and {MAX_CASES}")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    config.validate()
    output = Path(output)
    if output.is_symlink():
        raise ValueError("Experiment output must not be a symlink")
    output = output.resolve()
    for protected in (config.path(config.data.raw), config.path(config.data.processed)):
        if output == protected or protected in output.parents or output in protected.parents:
            raise ValueError("Experiment output must be separate from raw and processed data")
    if output.exists() and (not output.is_dir() or next(output.iterdir(), None) is not None):
        raise FileExistsError(f"Experiment output is not empty: {output}")
    cases = _selected_cases(samples, seed)
    fonts = [
        face
        for face in discover([config.path(path) for path in config.fonts.directories])
        if Path(face.path).with_name("OFL.txt").is_file()
    ]
    if not fonts:
        raise ValueError(
            "Experiments require fonts accompanied by OFL.txt; use synthocr fonts fetch"
        )
    coverage = Coverage(min(config.fonts.coverage_cache_size, 64))
    base = replace(
        config.generation,
        seed=seed,
        workers=1,
        format="png",
        direction="auto",
        language="",
        background_color="#f5f1e8",
        background_color_end="#e4e9ed",
        text_color="#1a1a1a",
        texture_strength=0.04,
        rotation=2,
        blur=0.55,
        contrast=0.1,
        background_presets=[],
        background_weights={},
        preset_colors=True,
        **dict.fromkeys(DOCUMENT_LAYERS),
    )
    variants = [(name, replace(base, **options)) for name, options in PRESETS]
    renderers = [Renderer(settings, min(config.fonts.cache_size, 4)) for _, settings in variants]
    output.mkdir(parents=True, exist_ok=True)
    (output / "images").mkdir()
    settings = {
        "schema_version": 1,
        "samples": samples,
        "seed": seed,
        "font_cache_per_preset": min(config.fonts.cache_size, 4),
        "coverage_cache": min(config.fonts.coverage_cache_size, 64),
        "presets": {
            name: {key: value for key, value in asdict(value).items() if key != "output"}
            for name, value in variants
        },
    }
    _write_json(output / "config.json", settings)
    with (output / "cases.jsonl").open("x", encoding="utf-8") as stream:
        for case in cases:
            stream.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")
    report = {
        "schema_version": 1,
        "status": "incomplete",
        "purpose": "Structural rendering comparison; not an OCR accuracy or spelling evaluation",
        "samples": samples,
        "seed": seed,
        "images": 0,
        "config_sha256": sha256(output / "config.json"),
        "cases_sha256": sha256(output / "cases.jsonl"),
        "catalog_sha256": cases[0]["attribution"]["catalog_sha256"],
        "environment": {
            "synthocr": __version__,
            "python": platform.python_version(),
            "pillow": version("pillow"),
            "freetype": features.version_module("freetype2"),
            "raqm": features.version_feature("raqm"),
            "system": platform.system(),
            "machine": platform.machine(),
        },
        "fonts": {},
        "presets": {
            name: {"images": 0, "bytes": 0, "processing_seconds": 0.0} for name, _ in variants
        },
        "timing_scope": (
            "Per-preset processing includes rendering, metrics, encoding, disk I/O, "
            "decoding, hashing and annotation writing. Elapsed time also includes "
            "font selection and contact sheet creation; setup/discovery is excluded."
        ),
        "parent_peak_rss_bytes": None,
        "memory_scope": (
            "Process-lifetime parent RSS high-water mark; no workers; unavailable on Windows"
        ),
    }
    started = time.perf_counter()
    try:
        with (output / "manifest.jsonl").open("x", encoding="utf-8") as manifest:
            for case_index, case in enumerate(cases):
                identifier = case["attribution"]["source_id"]
                preferred = "notonaskharabic" if case["language"] == "ckb" else "notosans"
                choices = sorted(
                    fonts, key=lambda face: (Path(face.path).parent.name != preferred, face.name)
                )
                face = coverage.choose(choices, case["text"], 0)
                if face is None:
                    raise ValueError(
                        f"No font accompanied by OFL.txt covers diagnostic case {identifier}"
                    )
                font_digest = sha256(Path(face.path))
                font_id = f"{font_digest}#{face.index}"
                report["fonts"][font_id] = {
                    "filename": Path(face.path).name,
                    "face_index": face.index,
                    "sha256": font_digest,
                    "license_file": "OFL.txt",
                    "license_note": "Accompanying license file; not an independent licensing audit",
                    "license_sha256": sha256(Path(face.path).with_name("OFL.txt")),
                }
                render_seed = int.from_bytes(
                    hashlib.sha256(f"{seed}:{identifier}".encode()).digest()[:8], "big"
                )
                for preset_index, (name, variant) in enumerate(variants):
                    tick = time.perf_counter()
                    image, metadata = renderers[preset_index].render(
                        case["text"], face, render_seed, case["language"]
                    )
                    filename = f"images/{case_index:03}-{name}.png"
                    try:
                        metrics = _metrics(image, metadata)
                        with (output / filename).open("xb") as stream:
                            image.save(stream, format="PNG", compress_level=1)
                    finally:
                        image.close()
                    # Decode the published bytes as well as inspecting the render.
                    with Image.open(output / filename) as decoded:
                        decoded.load()
                        if decoded.size != (metadata["width"], metadata["height"]):
                            raise ValueError("Encoded image dimensions differ from render metadata")
                    row = {
                        **case,
                        "case_id": identifier,
                        "case_index": case_index,
                        "preset": name,
                        "preset_index": preset_index,
                        "font": face.name,
                        "font_id": font_id,
                        "seed": render_seed,
                        "image": filename,
                        "image_sha256": sha256(output / filename),
                        "metrics": metrics,
                        "base_color_contrast": round(
                            augmentation_contrast(
                                resolve_document_settings(
                                    variant, metadata["augmentation"].get("background_preset", "")
                                )
                            ),
                            3,
                        )
                        if variant.augmentation
                        else 21.0,
                        **metadata,
                    }
                    manifest.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                    stats = report["presets"][name]
                    stats["images"] += 1
                    stats["bytes"] += (output / filename).stat().st_size
                    stats["processing_seconds"] += time.perf_counter() - tick
                    report["images"] += 1
        report["contact_sheets"] = _contact_sheet(output, samples)
        report["manifest_sha256"] = sha256(output / "manifest.jsonl")
        report["contact_sheet_sha256"] = sha256(output / "contact-sheet.png")
        report["contact_sheet_cases"] = min(samples, SHEET_CASES)
        report["status"] = "complete"
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        report["elapsed_seconds"] = time.perf_counter() - started
        report["parent_peak_rss_bytes"] = _peak_rss()
        _write_json(output / "report.json", report)
    return {"output": str(output), **report}
