"""Bounded generation and streaming exports, independent of any training framework."""

import csv
import hashlib
import json
import logging
import multiprocessing
import os
import platform
import random
import shutil
import sqlite3
import tempfile
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
from math import gcd
from pathlib import Path

from . import __version__
from .config import Config
from .corpus_store import canonical_language, iter_origins
from .downloads import sha256
from .fonts import Coverage, discover, font_ids, select
from .render import initialize, release, render_task, require_shaping
from .spelling import write_spelling
from .text import prepare
from .verify import verified_records

LOG = logging.getLogger(__name__)


class GenerationCancelled(ValueError):
    """A cooperative stop leaves its partial dataset marked as cancelled."""


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise GenerationCancelled("Generation cancelled; partial output has been retained")


def _bounded_map(executor, function, items, limit):
    # Executor.map(buffersize=...) needs Python 3.14; keep at most `limit` tasks pending.
    pending = deque()
    for item in items:
        if len(pending) == limit:
            yield pending.popleft().result()
        pending.append(executor.submit(function, item))
    while pending:
        yield pending.popleft().result()


def _font_name(config, face):
    path = Path(face.path)
    try:
        return path.relative_to(config.root).as_posix()
    except ValueError:
        return path.name


COLUMNS = "texts.id,text,source,line,genre,language,attribution"


def _selection(db, config, languages, table):
    """Return the matching record count and a lookup by 1-based selection position."""
    if not languages and not config.generation.exclude_genres:
        count = db.execute("SELECT count(*) FROM texts").fetchone()[0]
        return count, f"SELECT {COLUMNS} FROM texts WHERE texts.id=?"
    # Renumber the selected source IDs on disk for constant-memory permutation.
    # Keep original text IDs in manifests, font choices and deterministic seeds.
    db.execute("PRAGMA temp_store=FILE")
    db.execute(f"CREATE TEMP TABLE {table} (id INTEGER PRIMARY KEY, text_id INTEGER NOT NULL)")
    conditions, parameters = [], []
    if languages:
        placeholders = ",".join("?" for _ in languages)
        conditions.append(f"lower(language) IN ({placeholders})")
        parameters.extend(canonical_language(language) for language in languages)
    if config.generation.exclude_genres:
        placeholders = ",".join("?" for _ in config.generation.exclude_genres)
        conditions.append(f"coalesce(genre,'') NOT IN ({placeholders})")
        parameters.extend(config.generation.exclude_genres)
    db.execute(
        f"INSERT INTO {table}(text_id) SELECT id FROM texts "
        f"WHERE {' AND '.join(conditions)} ORDER BY id",
        parameters,
    )
    count = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return count, (
        f"SELECT {COLUMNS} FROM texts JOIN {table} ON texts.id={table}.text_id WHERE {table}.id=?"
    )


def _candidates(config, db, fonts, stats, count, query, rng, coverage, cancel):
    """Yield supported (record, face, variant) pairs in a seeded constant-memory order."""
    if not count:
        return
    start = rng.randrange(count)
    stride = rng.randrange(1, count + 1)
    while gcd(stride, count) != 1:
        stride = stride % count + 1
    for variant in range(len(fonts)):
        found = False
        for index in range(count):
            _check_cancel(cancel)
            position = (start + index * stride) % count + 1
            row = db.execute(query, (position,)).fetchone()
            text_id, text = row[0], row[1]
            offset = (text_id + config.generation.seed) % len(fonts)
            if config.fonts.coverage == "strict":
                face = coverage.choose(fonts, text, offset, variant)
            else:
                face = fonts[(offset + variant) % len(fonts)]
            if face is None:
                if variant == 0:
                    stats["unsupported_texts"] += 1
                    if len(stats["unsupported_examples"]) < 5:
                        stats["unsupported_examples"].append({"text_id": text_id, "text": text})
                continue
            found = True
            yield row, face, variant
        if not found:
            return


def _quotas(names, weights, total):
    """Divide total by relative weight, giving remainders to the largest fractions."""
    values = [weights.get(name, 1) for name in names]
    exact = [total * value / sum(values) for value in values]
    quotas = [int(value) for value in exact]
    order = sorted(range(len(names)), key=lambda i: (quotas[i] - exact[i], i))
    for index in order[: total - sum(quotas)]:
        quotas[index] += 1
    return quotas


def _interleave(streams, quotas, total):
    """Draw each stream up to its quota, spread evenly; an exhausted stream's
    remainder is filled from the others so the requested total is still reached."""
    produced = [0] * len(streams)
    active = set(range(len(streams)))
    drawn = 0
    while drawn < total and active:
        owed = [i for i in active if produced[i] < quotas[i]]
        candidates = owed or sorted(active)
        # Pick the stream furthest behind its proportional target at this point.
        index = max(
            candidates,
            key=lambda i: (quotas[i] * (drawn + 1) / total - produced[i], -i),
        )
        item = next(streams[index], None)
        if item is None:
            active.discard(index)
            continue
        produced[index] += 1
        drawn += 1
        yield item


def _tasks(config, db, fonts, output, stats, font_ids, cancel=None):
    coverage = Coverage(config.fonts.coverage_cache_size)
    languages = config.generation.languages
    weights = {
        canonical_language(name): value
        for name, value in config.generation.language_weights.items()
    }
    selected = list(dict.fromkeys(canonical_language(language) for language in languages))
    if weights:
        # A zero share excludes that language; it is never used to fill gaps.
        selected = [language for language in selected if weights.get(language, 1) > 0]
    if weights and len(selected) > 1:
        streams, counts = [], []
        for index, language in enumerate(selected):
            count, query = _selection(db, config, [language], f"selected_{index}")
            counts.append(count)
            rng = random.Random(f"{config.generation.seed}:{language}")
            streams.append(
                _candidates(config, db, fonts, stats, count, query, rng, coverage, cancel)
            )
        if not any(counts):
            raise ValueError(
                "No processed records match the requested languages/genres; check source metadata"
            )
        candidates = _interleave(
            streams,
            _quotas(selected, weights, config.generation.samples),
            config.generation.samples,
        )
    else:
        count, query = _selection(db, config, selected if weights else languages, "selected")
        if not count:
            raise ValueError(
                "No processed records match the requested languages/genres; check source metadata"
            )
        rng = random.Random(config.generation.seed)
        candidates = _candidates(config, db, fonts, stats, count, query, rng, coverage, cancel)
    for submitted, (row, face, variant) in enumerate(candidates):
        text_id, text, source, line, genre, language, attribution = row
        sample_id = f"{submitted:08d}"
        seed = int.from_bytes(
            hashlib.sha256(f"{config.generation.seed}:{text_id}:{variant}".encode()).digest()[:8]
        )
        yield {
            "id": sample_id,
            "text_id": text_id,
            "text": text,
            "source": source,
            "line": line,
            "genre": genre,
            "language": language,
            "face": face,
            "font_name": _font_name(config, face),
            "font_id": font_ids[face],
            "attribution": json.loads(attribution),
            "seed": seed,
            "destination": str(output / "images" / f"{sample_id}.{config.generation.format}"),
        }
        # Stop before drawing another candidate, which could count unsupported text.
        if submitted + 1 == config.generation.samples:
            return


def generate(
    config: Config,
    *,
    progress: Callable[[dict], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict:
    config.validate()
    if config.generation.suite == "kurdish-spelling":
        with tempfile.TemporaryDirectory(prefix="synthocr-spelling-") as directory:
            raw, processed = Path(directory) / "raw", Path(directory) / "processed"
            write_spelling(raw / "spelling.jsonl")
            return _generate(
                replace(
                    config,
                    data=replace(
                        config.data,
                        raw=str(raw),
                        processed=str(processed),
                        source_languages={},
                    ),
                ),
                progress=progress,
                cancel=cancel,
            )
    return _generate(config, progress=progress, cancel=cancel)


def _generate(config: Config, *, progress=None, cancel=None) -> dict:
    config.validate()
    _check_cancel(cancel)
    if progress:
        progress({"stage": "preparing", "generated": 0, "requested": config.generation.samples})
    require_shaping()
    prepared = prepare(config)
    _check_cancel(cancel)
    fonts = discover([config.path(p) for p in config.fonts.directories])
    face_ids = font_ids(fonts)
    fonts = select(fonts, config.fonts.include, face_ids)
    _check_cancel(cancel)
    output = config.path(config.generation.output)
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"Output directory must be empty: {output}. Choose a new --output.")
    output.mkdir(parents=True, exist_ok=True)
    (output / "images").mkdir()
    started = time.perf_counter()
    stats = {
        "requested": config.generation.samples,
        "generated": 0,
        "fonts": len(fonts),
        "unsupported_texts": 0,
        "unsupported_examples": [],
        "status": "running",
    }
    run = {
        "schema_version": 3,
        "version": __version__,
        "python": platform.python_version(),
        "config": {
            "data": asdict(config.data),
            "fonts": asdict(config.fonts),
            "generation": asdict(config.generation),
        },
        "corpus_fingerprint": prepared["fingerprint"],
        "corpus_sources": prepared["sources"],
        "font_files": [],
    }
    for face in fonts:
        digest = face_ids[face].rsplit(":", 1)[0]
        run["font_files"].append(
            {
                "font": _font_name(config, face),
                "face_index": face.index,
                "sha256": digest,
                "id": face_ids[face],
            }
        )
    (output / "run.json").write_text(
        json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    db_path = config.path(config.data.processed) / "texts.sqlite3"
    db = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA cache_size=-4096")
    db.execute("PRAGMA temp_store=FILE")
    db.execute("CREATE TEMP TABLE emitted_origins (text_id INTEGER PRIMARY KEY)")
    executor = None
    try:
        actual_fingerprint = db.execute("SELECT fingerprint FROM metadata").fetchone()[0]
        if actual_fingerprint != prepared["fingerprint"]:
            raise ValueError(
                "Processed corpus changed before generation; retry with a stable corpus"
            )
        if progress:
            progress({"stage": "generating", "generated": 0, "requested": stats["requested"]})
        tasks = _tasks(config, db, fonts, output, stats, face_ids, cancel)
        if config.generation.workers == 1:
            initialize(config.generation, config.fonts.cache_size)
            results = map(render_task, tasks)
        else:
            # Spawn avoids unsafe forking of native font engines on macOS.
            executor = ProcessPoolExecutor(
                max_workers=config.generation.workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=initialize,
                initargs=(config.generation, config.fonts.cache_size),
            )
            results = _bounded_map(executor, render_task, tasks, 2 * config.generation.workers)
        with (
            (output / "manifest.jsonl").open("w", encoding="utf-8") as manifest,
            (output / "origins.jsonl").open("w", encoding="utf-8") as origins,
        ):
            for record in results:
                _check_cancel(cancel)
                manifest.write(json.dumps(record, ensure_ascii=False) + "\n")
                inserted = db.execute(
                    "INSERT OR IGNORE INTO emitted_origins VALUES(?)", (record["text_id"],)
                ).rowcount
                if inserted:
                    for origin in iter_origins(db, record["text_id"]):
                        origin["text_sha256"] = hashlib.sha256(record["text"].encode()).hexdigest()
                        origins.write(json.dumps(origin, ensure_ascii=False) + "\n")
                stats["generated"] += 1
                if progress:
                    progress(
                        {
                            "stage": "generating",
                            "generated": stats["generated"],
                            "requested": stats["requested"],
                        }
                    )
                if stats["generated"] % 1000 == 0:
                    LOG.info("Generated %s/%s images", stats["generated"], stats["requested"])
        _check_cancel(cancel)
        if stats["generated"] != stats["requested"]:
            raise ValueError(
                f"Only {stats['generated']}/{stats['requested']} unique supported "
                "text/font pairs are available. Add text or fonts, or request fewer samples."
            )
        stats["status"] = "complete"
    except BaseException as error:
        stats["status"] = "cancelled" if isinstance(error, GenerationCancelled) else "failed"
        stats["error"] = str(error)
        raise
    finally:
        if executor:
            executor.shutdown(wait=True, cancel_futures=True)
        else:
            release()
        db.close()
        stats["seconds"] = time.perf_counter() - started
        stats["images_per_second"] = stats["generated"] / max(stats["seconds"], 1e-9)
        stats["run_sha256"] = sha256(output / "run.json")
        if (output / "manifest.jsonl").exists():
            stats["manifest_sha256"] = sha256(output / "manifest.jsonl")
        if (output / "origins.jsonl").exists():
            stats["origins_sha256"] = sha256(output / "origins.jsonl")
        (output / "stats.json").write_text(
            json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return stats | {"output": str(output)}


def export(dataset: Path, output: Path, format: str, *, max_bytes: int | None = None) -> dict:
    """Publish an attributed view atomically, optionally limiting apparent bytes.

    Hardlinked images count toward the limit just like copied images. This
    bounds temporary exports as well as their eventual downloadable content.
    """
    from .verify import dataset_files

    if format not in {"csv", "imagefolder", "tesseract"}:
        raise ValueError("Export format must be csv, imagefolder or tesseract")
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise ValueError("max_bytes must be a nonnegative integer or None")
    dataset = dataset.resolve()
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"Export destination must be empty: {output}")
    consumed = 0

    def check(size):
        if max_bytes is not None and consumed + size > max_bytes:
            raise ValueError(f"Export exceeds the configured byte limit ({max_bytes} bytes)")

    def charge(size):
        nonlocal consumed
        check(size)
        consumed += size

    def write_text(path, value):
        if max_bytes is not None:
            charge(len(value.encode("utf-8")))
        path.write_text(value, encoding="utf-8", newline="")

    def copy_file(source, destination):
        if max_bytes is None:
            with source.open("rb") as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            return
        check(source.stat().st_size)
        with source.open("rb") as src, destination.open("xb") as dst:
            while value := src.read(min(1024 * 1024, max_bytes - consumed + 1)):
                charge(len(value))
                dst.write(value)

    class Labels:
        def __init__(self, stream):
            self.stream = stream

        def write(self, value):
            if max_bytes is not None:
                charge(len(value.encode("utf-8")))
            return self.stream.write(value)

    if max_bytes is not None:
        minimum = 0
        for path in dataset_files(dataset / "images"):
            minimum += path.stat().st_size
            check(minimum)
        origins = dataset / "origins.jsonl"
        if origins.is_symlink():
            raise ValueError("Dataset artifacts must not be symbolic links")
        if origins.is_file():
            check(minimum + origins.stat().st_size)
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with tempfile.TemporaryDirectory(prefix=".export-", dir=output.parent) as directory:
        temporary = Path(directory)
        metadata_name = {
            "csv": "labels.csv",
            "imagefolder": "metadata.jsonl",
            "tesseract": "attribution.jsonl",
        }[format]
        with (temporary / metadata_name).open("x", encoding="utf-8", newline="") as stream:
            labels = Labels(stream)
            writer = csv.DictWriter(
                labels,
                fieldnames=[
                    "image",
                    "text",
                    "text_id",
                    "split",
                    "language",
                    "genre",
                    "attribution",
                ],
            )
            if format == "csv":
                writer.writeheader()
            for record, path in verified_records(dataset):
                destination = temporary / f"{record['id']}{path.suffix}"
                size = path.stat().st_size
                check(size)
                try:
                    os.link(path, destination)
                except OSError:
                    copy_file(path, destination)
                else:
                    charge(size)
                if format == "csv":
                    writer.writerow(
                        {
                            "image": destination.name,
                            "text": record["text"],
                            "text_id": record["text_id"],
                            "split": record["split"],
                            "language": record["language"],
                            "genre": record["genre"],
                            "attribution": json.dumps(record["attribution"], ensure_ascii=False),
                        }
                    )
                else:
                    metadata = {k: v for k, v in record.items() if k != "image"}
                    metadata["file_name"] = destination.name
                    labels.write(json.dumps(metadata, ensure_ascii=False) + "\n")
                    if format == "tesseract":
                        write_text(destination.with_suffix(".gt.txt"), record["text"])
                count += 1
        provenance = {}
        if (dataset / "origins.jsonl").is_file():
            copy_file(dataset / "origins.jsonl", temporary / "origins.jsonl")
            provenance["origins_sha256"] = sha256(temporary / "origins.jsonl")
        write_text(
            temporary / "export.json",
            json.dumps(
                {
                    "status": "complete",
                    "records": count,
                    "format": format,
                    "source_manifest_sha256": sha256(dataset / "manifest.jsonl"),
                    **provenance,
                },
                indent=2,
            )
            + "\n",
        )
        if output.exists():
            output.rmdir()  # Refuses to remove anything if another writer added content.
        os.rename(temporary, output)
    return {"records": count, "format": format, "output": str(output)}
