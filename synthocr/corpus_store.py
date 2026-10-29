"""Explicit source-language identities for a single corpus per language tag.

Only known code aliases are folded. Script, region and dialect subtags remain
distinct; this module never infers a language from a filename or its text.
"""

import hashlib
import json
import re
import sqlite3
from pathlib import Path

LANGUAGE = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*\Z")
# These are the exact ISO 639-3 source codes used by our imported collections.
# Keep this mapping explicit: generic Kurdish is not Sorani or Kurmanji, and
# an unrecognised language is not guessed from a neighbouring code or script.
ALIASES = {
    "amh": "am",
    "ara": "ar",
    "aze": "az",
    "ben": "bn",
    "deu": "de",
    "ell": "el",
    "eng": "en",
    "fra": "fr",
    "heb": "he",
    "hin": "hi",
    "hye": "hy",
    "ind": "id",
    "ita": "it",
    "jpn": "ja",
    "kat": "ka",
    "kor": "ko",
    "nld": "nl",
    "pol": "pl",
    "por": "pt",
    "rus": "ru",
    "spa": "es",
    "tam": "ta",
    "tha": "th",
    "tur": "tr",
    "uig": "ug",
    "ukr": "uk",
    "urd": "ur",
    "vie": "vi",
}
# These are specific languages within macrolanguages, not interchangeable
# aliases. Historical imports used the broader tag; only explicit provenance
# may recover the precise source identity without rewriting those raw files.
LEGACY_MACROLANGUAGES = {"cmn": "zh", "pes": "fa", "swh": "sw"}


def canonical_language(tag: str) -> str:
    """Return a stable, case-insensitive identity without collapsing dialects."""
    if not isinstance(tag, str) or (tag and not LANGUAGE.fullmatch(tag)):
        raise ValueError("Source language must be empty or a valid language tag")
    value = tag.casefold()
    base, separator, subtags = value.partition("-")
    return ALIASES.get(base, base) + separator + subtags


def resolve_source_language(declared: str, attribution: dict, registered: str = "") -> str:
    """Recover only reviewed historical macro/specific pairs from metadata.

    Arbitrary conflicting language fields cannot override a record's declared
    language. A generic user language remains generic without explicit source
    metadata identifying its specific variety.
    """
    canonical = canonical_language(declared)
    for candidate in (attribution.get("source_language"), registered):
        if not isinstance(candidate, str) or not LANGUAGE.fullmatch(candidate):
            continue
        precise = canonical_language(candidate)
        if LEGACY_MACROLANGUAGES.get(precise) == canonical:
            return candidate
    return declared


def registered_source_languages(raw: Path, sources: list[dict]) -> dict[str, str]:
    """Read explicit source identities only from matching pinned catalog rows.

    The companion catalog is optional for mounted or custom raw collections.
    Paths identify registered source files; they never imply a language.
    """
    catalog = raw.parent / "corpora.json"
    if not catalog.is_file():
        return {}
    document = json.loads(catalog.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("sources"), list):
        raise ValueError("Corpus source catalog must contain a sources list")
    checksums = {source["path"]: source["sha256"] for source in sources}
    result = {}
    for entry in document["sources"]:
        if not isinstance(entry, dict):
            raise ValueError("Corpus source catalog entries must be objects")
        path, language = entry.get("output"), entry.get("language")
        if (
            isinstance(path, str)
            and isinstance(language, str)
            and language in LEGACY_MACROLANGUAGES
            and checksums.get(path) == entry.get("raw_sha256")
            and path in checksums
        ):
            if path in result and result[path] != language:
                raise ValueError(f"Conflicting source languages in corpus catalog: {path}")
            result[path] = language
    return result


def create_origins(db: sqlite3.Connection) -> None:
    """Store provenance separately so repeated labels never build huge arrays."""
    db.execute(
        "CREATE TABLE origins (id INTEGER PRIMARY KEY, text_id INTEGER NOT NULL, "
        "source TEXT NOT NULL, line INTEGER NOT NULL, genre TEXT NOT NULL, "
        "source_language TEXT NOT NULL, attribution TEXT, origin_hash BLOB NOT NULL, "
        "UNIQUE(text_id,origin_hash))"
    )


def add_origin(
    db: sqlite3.Connection,
    text_id: int,
    source: str,
    line: int,
    genre: str,
    source_language: str,
    attribution: dict,
) -> None:
    encoded = json.dumps(attribution, ensure_ascii=False, sort_keys=True)
    first = db.execute(
        "SELECT source,line,genre,attribution FROM texts WHERE id=?", (text_id,)
    ).fetchone()
    if first is None:
        raise ValueError("A corpus origin must reference an existing text")
    # The first origin reuses the attribution already stored on its text row.
    # Repeating that JSON in every origin would almost double the database.
    payload = (
        None
        if first[:3] == (source, line, genre) and json.loads(first[3]) == attribution
        else encoded
    )
    digest = hashlib.sha256(
        json.dumps(
            [source, line, genre, source_language, attribution],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).digest()
    db.execute(
        "INSERT OR IGNORE INTO origins"
        "(text_id,source,line,genre,source_language,attribution,origin_hash) VALUES(?,?,?,?,?,?,?)",
        (
            text_id,
            source,
            line,
            genre,
            source_language,
            payload,
            digest,
        ),
    )


def iter_origins(db: sqlite3.Connection, text_id: int | None = None):
    """Yield all origins, or those of one label, in deterministic insertion order."""
    where, parameters = (
        (" WHERE origins.text_id=?", (text_id,)) if text_id is not None else ("", ())
    )
    cursor = db.execute(
        "SELECT origins.text_id,origins.source,origins.line,origins.genre,source_language,"
        "COALESCE(origins.attribution,texts.attribution) FROM origins "
        "JOIN texts ON texts.id=origins.text_id" + where + " ORDER BY origins.text_id,origins.id",
        parameters,
    )
    for identifier, source, line, genre, language, attribution in cursor:
        yield {
            "text_id": identifier,
            "source": source,
            "line": line,
            "genre": genre,
            "source_language": language,
            "attribution": json.loads(attribution),
        }


def materialize_primary_origin(db: sqlite3.Connection, text_id: int) -> None:
    """Preserve the old attribution before choosing a better primary origin."""
    db.execute(
        "UPDATE origins SET attribution=(SELECT attribution FROM texts WHERE id=?) "
        "WHERE text_id=? AND attribution IS NULL",
        (text_id, text_id),
    )


def corpus_registry(db: sqlite3.Connection, fingerprint: str, sources: list[dict]) -> dict:
    """Describe one logical corpus per language without copying any corpus text."""
    source_index = {source["path"]: source for source in sources}
    corpora = []
    for language, count, diagnostics in db.execute(
        "SELECT language,COUNT(*),SUM(genre='spelling') FROM texts GROUP BY language "
        "ORDER BY language"
    ):
        paths = (
            row[0]
            for row in db.execute(
                "SELECT DISTINCT origins.source FROM origins "
                "JOIN texts ON texts.id=origins.text_id WHERE texts.language=? "
                "ORDER BY origins.source",
                (language,),
            )
        )
        entries = [source_index[path] for path in paths]
        corpora.append(
            {
                "language": language,
                "records": count,
                "diagnostic_records": diagnostics,
                "source_count": len(entries),
                "sources": entries,
            }
        )
    return {"schema_version": 1, "fingerprint": fingerprint, "corpora": corpora}
