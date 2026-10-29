"""Streaming Unicode preparation; SQLite handles indexing and deduplication on disk."""

import csv
import hashlib
import json
import os
import random
import sqlite3
import tempfile
import unicodedata
from dataclasses import asdict
from pathlib import Path

import regex

from .config import Config
from .corpus_store import (
    add_origin,
    canonical_language,
    corpus_registry,
    create_origins,
    materialize_primary_origin,
    registered_source_languages,
    resolve_source_language,
)
from .downloads import sha256

GRAPHEME = regex.compile(r"\X")
SPACE = regex.compile(r"\s+")
IGNORABLE = regex.compile(r"\A\p{Default_Ignorable_Code_Point}\Z")
# Preserve ZWJ, ZWNJ, variation selectors and bidi controls: these affect shaping.
UNWANTED = dict.fromkeys(
    [0xFEFF, *range(0, 9), *range(14, 32), *range(0x7F, 0x85), *range(0x86, 0xA0)]
)


def required_codepoints(text: str) -> frozenset[int]:
    # Cf includes visible signs such as U+0600 and U+06DD. Ignore only Unicode's
    # actual default-ignorable characters, not the entire format category.
    return frozenset(
        ord(c)
        for c in text
        if not c.isspace()
        and unicodedata.category(c) not in {"Cc", "Cs"}
        and not IGNORABLE.fullmatch(c)
    )


def normalize(text: str, form: str = "NFC") -> str:
    text = text.translate(UNWANTED)
    if form != "none":
        text = unicodedata.normalize(form, text)
    return SPACE.sub(" ", text).strip()


def chunks(text: str, limit: int):
    """Wrap at whitespace when possible, with grapheme-safe hard wrapping."""
    current: list[str] = []
    last_space = -1
    for match in GRAPHEME.finditer(text):
        cluster = match.group()
        if len(current) == limit:
            cut = last_space if last_space > 0 else limit
            result = "".join(current[:cut]).strip()
            if result:
                yield result
            current = current[cut + (cut < limit) :]
            last_space = max((i for i, c in enumerate(current) if c.isspace()), default=-1)
        if not current and cluster.isspace():
            continue
        if cluster.isspace():
            last_space = len(current)
        current.append(cluster)
    result = "".join(current).strip()
    if result:
        yield result


def _bounded_lines(path: Path, limit: int):
    with path.open(encoding="utf-8-sig", errors="strict") as stream:
        line_no = 0
        while line := stream.readline(limit + 1):
            line_no += 1
            if len(line) > limit:
                raise ValueError(f"{path}:{line_no}: line exceeds max_line_chars={limit}")
            yield line


def records(path: Path, raw: Path, limit: int):
    lines = _bounded_lines(path, limit)
    if path.suffix.lower() == ".csv":
        reader = csv.DictReader(lines)
        if not reader.fieldnames or "text" not in reader.fieldnames:
            raise ValueError(f"{path}: CSV requires a text column")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError(f"{path}: CSV headers must be unique")
        old_limit = csv.field_size_limit(limit)
        try:
            for row in reader:
                if None in row:
                    raise ValueError(f"{path}:{reader.line_num}: unexpected CSV columns")
                # DictReader skips blank rows; count embedded newlines backwards
                # from the physical end of this record for an accurate start.
                start_line = reader.line_num - sum(v.count("\n") for v in row.values() if v)
                yield start_line, row
        except csv.Error as error:
            raise ValueError(f"{path}:{reader.line_num}: invalid CSV: {error}") from error
        finally:
            csv.field_size_limit(old_limit)
    else:
        for number, line in enumerate(lines, 1):
            if path.suffix.lower() == ".jsonl":
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict) or not isinstance(row.get("text"), str):
                    raise ValueError(f"{path}:{number}: expected an object with a text string")
            else:
                row = {"text": line}
            yield number, row


def prepare(config: Config, force: bool = False) -> dict:
    config.validate()
    raw = config.path(config.data.raw)
    processed = config.path(config.data.processed)
    files = sorted(p for p in raw.rglob("*") if p.suffix.lower() in {".txt", ".jsonl", ".csv"})
    if not files:
        raise ValueError(f"No TXT, JSONL or CSV sources found in {raw}")
    sources = [{"path": p.relative_to(raw).as_posix(), "sha256": sha256(p)} for p in files]
    registered_languages = registered_source_languages(raw, sources)
    unknown = config.data.source_languages.keys() - {source["path"] for source in sources}
    if unknown:
        raise ValueError(f"source_languages refers to missing raw sources: {sorted(unknown)}")
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "schema": 6,
                "settings": asdict(config.data),
                "sources": sources,
                "registered_languages": registered_languages,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    artifact_names = ["texts.sqlite3", "corpora.json"]
    if config.data.write_jsonl:
        artifact_names.append("texts.jsonl")
    stats_path = processed / "stats.json"
    if not force and all((processed / p).is_file() for p in [*artifact_names, "stats.json"]):
        try:
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
            if (
                isinstance(stats, dict)
                and stats.get("fingerprint") == fingerprint
                and isinstance(stats.get("artifacts"), dict)
                and set(stats["artifacts"]) == set(artifact_names)
                and all(
                    sha256(processed / name) == digest
                    for name, digest in stats["artifacts"].items()
                )
            ):
                return stats | {"cached": True}
        except ValueError, OSError:
            pass  # Rebuild corrupt or truncated derived data from unchanged raw sources.
    processed.mkdir(parents=True, exist_ok=True)
    stats = {
        "schema_version": 3,
        "fingerprint": fingerprint,
        "sources": sources,
        "records": 0,
        "duplicates": 0,
        "empty": 0,
        "raw_lines": 0,
    }
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=processed) as temporary:
        temp = Path(temporary)
        db = sqlite3.connect(temp / "texts.sqlite3")
        try:
            db.execute("PRAGMA cache_size=-4096")
            db.execute("PRAGMA temp_store=FILE")
            db.execute(
                "CREATE TABLE texts (id INTEGER PRIMARY KEY, text TEXT NOT NULL, "
                "source TEXT, line INTEGER, genre TEXT, language TEXT, attribution TEXT)"
            )
            db.execute("CREATE TABLE metadata (fingerprint TEXT NOT NULL)")
            db.execute("INSERT INTO metadata VALUES(?)", (fingerprint,))
            create_origins(db)
            if config.data.deduplicate:
                db.execute("CREATE UNIQUE INDEX unique_text ON texts(language,text)")
            for path in files:
                source = path.relative_to(raw).as_posix()
                for line, row in records(path, raw, config.data.max_line_chars):
                    stats["raw_lines"] += 1
                    value = row.get("text")
                    if not isinstance(value, str):
                        raise ValueError(f"{source}:{line}: text must be a string")
                    if any(unicodedata.category(c) == "Cs" for c in value):
                        raise ValueError(f"{source}:{line}: text contains an unpaired surrogate")
                    text = normalize(value, config.data.normalization)
                    if not required_codepoints(text):
                        stats["empty"] += 1
                        continue
                    genre = row.get("genre") or path.stem
                    source_language = row.get("language")
                    if source_language is not None and not isinstance(source_language, str):
                        raise ValueError(f"{source}:{line}: language must be a string")
                    source_language = source_language or config.data.source_languages.get(
                        source, ""
                    )
                    if not isinstance(genre, str):
                        raise ValueError(f"{source}:{line}: genre and language must be strings")
                    attribution = row.get("attribution", {})
                    if not isinstance(attribution, dict):
                        raise ValueError(f"{source}:{line}: attribution must be an object")
                    declared_language = source_language
                    source_language = resolve_source_language(
                        declared_language, attribution, registered_languages.get(source, "")
                    )
                    language = canonical_language(source_language)
                    for chunk in chunks(text, config.data.max_chars):
                        if not required_codepoints(chunk):
                            stats["empty"] += 1
                            continue
                        provenance = attribution.copy()
                        if source_language != declared_language:
                            provenance.setdefault("original_language", declared_language)
                            provenance.setdefault("source_language", source_language)
                        if language != source_language:
                            provenance.setdefault("source_language", source_language)
                        if provenance:
                            provenance["modified"] = bool(
                                provenance.get("modified") or chunk != value
                            )
                        encoded = json.dumps(provenance, ensure_ascii=False)
                        cursor = db.execute(
                            "INSERT OR IGNORE INTO texts"
                            "(text,source,line,genre,language,attribution) VALUES(?,?,?,?,?,?)",
                            (chunk, source, line, genre, language, encoded),
                        )
                        if cursor.rowcount:
                            text_id = cursor.lastrowid
                            stats["records"] += 1
                        else:
                            stats["duplicates"] += 1
                            text_id, primary_genre = db.execute(
                                "SELECT id,genre FROM texts WHERE language=? AND text=?",
                                (language, chunk),
                            ).fetchone()
                            # Diagnostic probes must not replace natural training text.
                            if primary_genre == "spelling" and genre != "spelling":
                                materialize_primary_origin(db, text_id)
                                db.execute(
                                    "UPDATE texts SET source=?,line=?,genre=?,attribution=? "
                                    "WHERE id=?",
                                    (source, line, genre, encoded, text_id),
                                )
                        add_origin(db, text_id, source, line, genre, source_language, provenance)
                    if stats["raw_lines"] % 10000 == 0:
                        db.commit()
            db.commit()
            if not stats["records"]:
                raise ValueError("No nonempty text remains after preparation")
            stats["genres"] = dict(db.execute("SELECT genre,COUNT(*) FROM texts GROUP BY genre"))
            stats["languages"] = {
                language or "unspecified": count
                for language, count in db.execute(
                    "SELECT language,COUNT(*) FROM texts GROUP BY language"
                )
            }
            stats["origins"] = db.execute("SELECT COUNT(*) FROM origins").fetchone()[0]
            registry = corpus_registry(db, fingerprint, sources)
            stats["corpora"] = registry["corpora"]
            (temp / "corpora.json").write_text(
                json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            if config.data.write_jsonl:
                with (temp / "texts.jsonl").open("w", encoding="utf-8") as output:
                    for identifier, text, source, line, genre, language, attribution in db.execute(
                        "SELECT id,text,source,line,genre,language,attribution FROM texts "
                        "ORDER BY id"
                    ):
                        output.write(
                            json.dumps(
                                {
                                    "id": identifier,
                                    "text": text,
                                    "source": source,
                                    "line": line,
                                    "genre": genre,
                                    "language": language,
                                    "attribution": json.loads(attribution),
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
        finally:
            db.close()
        for path, source_info in zip(files, sources, strict=True):
            if sha256(path) != source_info["sha256"]:
                raise ValueError(f"Raw source changed during preparation: {path}")
        stats["artifacts"] = {name: sha256(temp / name) for name in artifact_names}
        (temp / "stats.json").write_text(
            json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for name in [*artifact_names, "stats.json"]:
            os.replace(temp / name, processed / name)
        if not config.data.write_jsonl:
            (processed / "texts.jsonl").unlink(missing_ok=True)
    return stats


GENRES = (
    "prose",
    "receipt",
    "date",
    "identifier",
    "web",
    "number",
    "address",
    "measurement",
    "arithmetic",
    "contact",
    "table",
)


def generate_text(output: Path, count: int, seed: int, genre: str, language: str) -> dict:
    """Write diverse offline OCR examples atomically, with synthetic provenance."""
    from datetime import date, timedelta
    from importlib.resources import files

    lexicons = json.loads(files("synthocr").joinpath("lexicons.json").read_text(encoding="utf-8"))
    if count <= 0 or genre not in (*GENRES, "mixed") or language not in (*lexicons, "mixed"):
        raise ValueError("Choose a positive count, a supported genre, and a supported language")
    rng = random.Random(seed)
    languages = list(lexicons) if language == "mixed" else [language]
    rng.shuffle(languages)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(output)
    temporary = None

    def money(cents):
        return f"{cents // 100:,}.{cents % 100:02}"

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            for index in range(count):
                locale = languages[index % len(languages)]
                words = lexicons[locale]
                kind = rng.choice(GENRES) if genre == "mixed" else genre
                number = rng.randrange(1, 10_000_000)
                quantity = rng.randrange(1, 100)
                unit = rng.randrange(1, 1_000_000)
                item = rng.choice(words["items"])
                if kind == "prose":
                    text = words["prose"].format(
                        subject=rng.choice(words["subjects"]),
                        verb=rng.choice(words["verbs"]),
                        place=rng.choice(words["places"]),
                    )
                elif kind == "receipt":
                    text = (
                        f"{item} | {words['quantity']}: {quantity} | "
                        f"{words['unit']}: {money(unit)} | "
                        f"{words['total']}: {money(unit * quantity)}"
                    )
                elif kind == "table":
                    text = (
                        f"{number:07} | {item} | {quantity} | "
                        f"{money(unit)} | {money(unit * quantity)}"
                    )
                elif kind == "date":
                    day = date(1990, 1, 1) + timedelta(days=rng.randrange(20000))
                    text = (
                        f"{day.isoformat()} {rng.randrange(24):02}:"
                        f"{rng.randrange(60):02}:{rng.randrange(60):02} UTC+03:00"
                    )
                elif kind == "identifier":
                    prefix = rng.choice(("INV", "ORD", "REF", "DOC"))
                    text = f"{words['invoice']} {prefix}-{number:07}-{rng.randrange(10000):04}"
                elif kind == "web":
                    section = rng.choice(("books", "orders", "items", "search"))
                    text = f"https://example.org/{section}/{number}?lang={locale}&page={quantity}"
                elif kind == "number":
                    text = (
                        f"{rng.choice(('', '-', '+'))}{number:,}.{rng.randrange(10000):04} "
                        f"{rng.choice(('%', 'USD', 'EUR', 'IQD'))}"
                    )
                elif kind == "address":
                    text = (
                        f"{rng.choice(words['streets'])} {rng.randrange(1, 1000)}, "
                        f"{rng.choice(words['cities'])} {rng.randrange(10000, 100000)}"
                    )
                elif kind == "measurement":
                    unit_name = rng.choice(
                        ("kg", "g", "km", "cm", "m²", "m³", "°C", "ms", "kWh", "L")
                    )
                    text = f"{item}: {money(number)} {unit_name}"
                elif kind == "arithmetic":
                    a, b = rng.randrange(-10000, 10000), rng.randrange(1, 1000)
                    operator = rng.choice(("+", "-", "×"))
                    answer = a + b if operator == "+" else a - b if operator == "-" else a * b
                    text = f"{a} {operator} {b} = {answer}"
                else:
                    text = (
                        f"user{number}+{locale}@example.org | "
                        f"+964 7{rng.randrange(1_000_000_000):09}"
                    )
                if locale in {"ckb", "ar", "fa"} and rng.random() < 0.4:
                    digits = "۰۱۲۳۴۵۶۷۸۹" if locale == "fa" else "٠١٢٣٤٥٦٧٨٩"
                    text = text.translate(str.maketrans("0123456789", digits))
                row = {
                    "text": text,
                    "genre": kind,
                    "language": locale,
                    "attribution": {
                        "source": "SynthOCR offline templates v2",
                        "source_id": f"{seed}:{index}",
                        "synthetic": True,
                        "seed": seed,
                        "license": "CC0-1.0",
                        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                        "modified": False,
                    },
                }
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.link(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"output": str(output), "records": count, "seed": seed, "languages": languages}
