"""Stream integrity checks over a completed dataset using disk-backed uniqueness indexes."""

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from PIL import Image

from .corpus_store import canonical_language
from .downloads import sha256
from .text import required_codepoints


def dataset_files(root: Path, *, _depth: int = 0):
    """Walk generated files without collecting a directory's entries in memory."""
    if _depth > 32 or root.is_symlink():
        raise ValueError("Dataset directories must not contain symbolic links or excessive nesting")
    with os.scandir(root) as entries:
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink():
                raise ValueError("Dataset files must not be symbolic links")
            if entry.is_dir(follow_symlinks=False):
                yield from dataset_files(path, _depth=_depth + 1)
            elif entry.is_file(follow_symlinks=False):
                yield path
            else:
                raise ValueError("Dataset entries must be ordinary files or directories")


def _origin_signature(source, line, genre, attribution):
    return hashlib.sha256(
        json.dumps([source, line, genre, attribution], ensure_ascii=False, sort_keys=True).encode()
    ).digest()


def verified_records(dataset: Path, check_images: bool = True):
    dataset = dataset.resolve()
    if any(
        (dataset / name).is_symlink()
        for name in ("manifest.jsonl", "run.json", "stats.json", "origins.jsonl")
    ):
        raise ValueError("Dataset artifacts must not be symbolic links")
    stats = json.loads((dataset / "stats.json").read_text(encoding="utf-8"))
    if stats.get("status") != "complete" or stats.get("generated") != stats.get("requested"):
        raise ValueError("Cannot verify or export an incomplete dataset")
    for name, key in (("manifest.jsonl", "manifest_sha256"), ("run.json", "run_sha256")):
        if not stats.get(key):
            raise ValueError(
                "Dataset predates integrity metadata; regenerate it before verification"
            )
        if sha256(dataset / name) != stats[key]:
            raise ValueError(f"Dataset checksum mismatch: {name}")
    run = json.loads((dataset / "run.json").read_text(encoding="utf-8"))
    selected_languages = run["config"]["generation"].get("languages", [])
    if not isinstance(selected_languages, list) or any(
        not isinstance(language, str) or not language for language in selected_languages
    ):
        raise ValueError("Invalid language selection in run metadata")
    selected_languages = {canonical_language(language) for language in selected_languages}
    excluded_genres = run["config"]["generation"].get("exclude_genres", [])
    if not isinstance(excluded_genres, list) or any(
        not isinstance(genre, str) or not genre for genre in excluded_genres
    ):
        raise ValueError("Invalid genre exclusion in run metadata")
    inventory = {font["id"]: font for font in run["font_files"]}
    count = 0
    with tempfile.TemporaryDirectory(prefix="synthocr-verify-") as directory:
        db = sqlite3.connect(Path(directory) / "seen.sqlite3")
        try:
            db.execute("PRAGMA cache_size=-4096")
            db.execute("PRAGMA temp_store=FILE")
            db.execute("CREATE TABLE seen(id TEXT PRIMARY KEY, image TEXT UNIQUE)")
            db.execute("CREATE TABLE labels(text TEXT PRIMARY KEY, split TEXT)")
            db.execute(
                "CREATE TABLE pairs(text TEXT, font TEXT, language TEXT, "
                "PRIMARY KEY(text,font,language))"
            )
            provenance = run.get("schema_version", 2) >= 3
            if provenance:
                if not stats.get("origins_sha256") or not (dataset / "origins.jsonl").is_file():
                    raise ValueError("Missing origins provenance metadata")
                if sha256(dataset / "origins.jsonl") != stats["origins_sha256"]:
                    raise ValueError("Dataset checksum mismatch: origins.jsonl")
                db.execute(
                    "CREATE TABLE origins(text_id INTEGER PRIMARY KEY, language TEXT, "
                    "text_sha256 TEXT, used INTEGER DEFAULT 0)"
                )
                db.execute(
                    "CREATE TABLE origin_identities(text_id INTEGER, signature BLOB, "
                    "PRIMARY KEY(text_id,signature))"
                )
                with (dataset / "origins.jsonl").open(encoding="utf-8") as stream:
                    for line in stream:
                        origin = json.loads(line)
                        if not isinstance(origin, dict):
                            raise ValueError("Invalid corpus origin: expected an object")
                        text_id = origin.get("text_id")
                        if (
                            type(text_id) is not int
                            or text_id <= 0
                            or not isinstance(origin.get("source"), str)
                            or not origin["source"]
                            or type(origin.get("line")) is not int
                            or origin["line"] <= 0
                            or not isinstance(origin.get("genre"), str)
                            or not isinstance(origin.get("source_language"), str)
                            or not isinstance(origin.get("attribution"), dict)
                            or not isinstance(origin.get("text_sha256"), str)
                            or len(origin["text_sha256"]) != 64
                        ):
                            raise ValueError("Invalid corpus origin")
                        language = canonical_language(origin["source_language"])
                        db.execute(
                            "INSERT OR IGNORE INTO origins(text_id,language,text_sha256) "
                            "VALUES(?,?,?)",
                            (text_id, language, origin["text_sha256"]),
                        )
                        if db.execute(
                            "SELECT language,text_sha256 FROM origins WHERE text_id=?", (text_id,)
                        ).fetchone() != (language, origin["text_sha256"]):
                            raise ValueError("Corpus origin languages disagree")
                        db.execute(
                            "INSERT OR IGNORE INTO origin_identities VALUES(?,?)",
                            (
                                text_id,
                                _origin_signature(
                                    origin["source"],
                                    origin["line"],
                                    origin["genre"],
                                    origin["attribution"],
                                ),
                            ),
                        )
                        if db.total_changes % 10000 == 0:
                            db.commit()
            with (dataset / "manifest.jsonl").open(encoding="utf-8") as stream:
                for line in stream:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("Manifest records must be objects")
                    identifier = record.get("id")
                    if (
                        not isinstance(identifier, str)
                        or not identifier.isascii()
                        or not identifier.isdigit()
                    ):
                        raise ValueError("Manifest IDs must be numeric ASCII strings")
                    relative = record.get("image")
                    if not isinstance(relative, str) or Path(relative).is_absolute():
                        raise ValueError(
                            "Manifest image must be a relative path within the dataset"
                        )
                    path = (dataset / relative).resolve()
                    if not path.is_relative_to(dataset / "images") or not path.is_file():
                        raise ValueError(
                            "Manifest image must be a file within the dataset images directory"
                        )
                    text, split = record.get("text"), record.get("split")
                    if not isinstance(text, str) or not required_codepoints(text):
                        raise ValueError("Manifest contains an empty or non-renderable label")
                    if split not in {"train", "validation", "test"}:
                        raise ValueError("Unknown manifest split")
                    language = record.get("language", "")
                    if not isinstance(language, str) or (
                        selected_languages
                        and canonical_language(language) not in selected_languages
                    ):
                        raise ValueError("Manifest language does not match the requested languages")
                    if record.get("genre") in excluded_genres:
                        raise ValueError("Manifest contains an excluded genre")
                    if provenance:
                        text_id = record.get("text_id")
                        if type(text_id) is not int or text_id <= 0:
                            raise ValueError("Invalid manifest text identity")
                        origin = db.execute(
                            "SELECT language,text_sha256 FROM origins WHERE text_id=?", (text_id,)
                        ).fetchone()
                        if origin != (
                            canonical_language(language),
                            hashlib.sha256(text.encode()).hexdigest(),
                        ):
                            raise ValueError(
                                "Missing or inconsistent corpus origin for manifest text"
                            )
                        signature = _origin_signature(
                            record.get("source"),
                            record.get("source_line"),
                            record.get("genre"),
                            record.get("attribution"),
                        )
                        if not db.execute(
                            "SELECT 1 FROM origin_identities WHERE text_id=? AND signature=?",
                            (text_id, signature),
                        ).fetchone():
                            raise ValueError("Manifest attribution does not match a corpus origin")
                        db.execute("UPDATE origins SET used=1 WHERE text_id=?", (text_id,))
                    font_id = record.get("font_id")
                    font = inventory.get(font_id)
                    if not font or (font["font"], font["face_index"]) != (
                        record.get("font"),
                        record.get("face_index"),
                    ):
                        raise ValueError("Manifest font does not match run inventory")
                    try:
                        db.execute("INSERT INTO seen VALUES(?,?)", (identifier, str(path)))
                        if run["config"]["data"]["deduplicate"]:
                            db.execute(
                                "INSERT INTO pairs VALUES(?,?,?)",
                                (text, font_id, canonical_language(language) if provenance else ""),
                            )
                    except sqlite3.IntegrityError as error:
                        raise ValueError("Duplicate sample, image or text/font pair") from error
                    db.execute("INSERT OR IGNORE INTO labels VALUES(?,?)", (text, split))
                    expected = db.execute(
                        "SELECT split FROM labels WHERE text=?", (text,)
                    ).fetchone()[0]
                    if split != expected:
                        raise ValueError("Text leaks across dataset splits")
                    width, height, bbox = (
                        record.get("width"),
                        record.get("height"),
                        record.get("bbox"),
                    )
                    if (
                        type(width) is not int
                        or type(height) is not int
                        or min(width, height) <= 0
                        or not isinstance(bbox, list)
                        or len(bbox) != 4
                        or any(type(v) is not int for v in bbox)
                        or not 0 <= bbox[0] < bbox[2] <= width
                        or not 0 <= bbox[1] < bbox[3] <= height
                    ):
                        raise ValueError("Invalid image dimensions or text bounding rectangle")
                    limits = run["config"]["generation"]
                    if (
                        width > limits["max_width"]
                        or height > limits["max_height"]
                        or width * height > limits["max_pixels"]
                    ):
                        raise ValueError("Image exceeds configured size limits")
                    if check_images:
                        if not record.get("image_sha256") or sha256(path) != record["image_sha256"]:
                            raise ValueError(f"Image checksum mismatch: {relative}")
                        try:
                            with Image.open(path) as image:
                                image.verify()
                            with Image.open(path) as image:
                                if image.size != (width, height):
                                    raise ValueError("Image dimensions differ from the manifest")
                                expected = {
                                    "png": "PNG",
                                    "jpg": "JPEG",
                                    "webp": "WEBP",
                                    "tif": "TIFF",
                                }[limits["format"]]
                                if (
                                    image.format != expected
                                    or path.suffix != f".{limits['format']}"
                                ):
                                    raise ValueError(
                                        "Image encoding differs from configured format"
                                    )
                                image.load()
                        except (
                            OSError,
                            ValueError,
                            SyntaxError,
                            Image.DecompressionBombError,
                        ) as error:
                            raise ValueError(f"Cannot decode image {relative}: {error}") from error
                    count += 1
                    if count % 10000 == 0:
                        db.commit()
                    yield record, path
            if count != stats["generated"]:
                raise ValueError(f"Manifest count mismatch: {count} != {stats['generated']}")
            if provenance and db.execute("SELECT 1 FROM origins WHERE used=0 LIMIT 1").fetchone():
                raise ValueError("Corpus origins contain unreferenced text identities")
            image_count = sum(1 for p in dataset_files(dataset / "images") if p.name != ".DS_Store")
            if image_count != count:
                raise ValueError("Dataset contains unlisted images or duplicate image references")
        finally:
            db.close()


def verify(dataset: Path, check_images: bool = True) -> dict:
    count = 0
    splits: dict[str, int] = {}
    languages: dict[str, int] = {}
    for record, _ in verified_records(dataset, check_images):
        count += 1
        splits[record["split"]] = splits.get(record["split"], 0) + 1
        language = record.get("language") or "unspecified"
        languages[language] = languages.get(language, 0) + 1
    return {
        "status": "verified",
        "records": count,
        "images_checked": check_images,
        "splits": splits,
        "languages": languages,
        "dataset": str(dataset.resolve()),
    }
