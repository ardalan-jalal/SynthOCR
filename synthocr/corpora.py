"""Versioned Tatoeba imports with author attribution and reproducible source hashes."""

import bz2
import json
import logging
import os
import re
import tempfile
from datetime import date
from pathlib import Path

from .corpus_store import LEGACY_MACROLANGUAGES, canonical_language
from .downloads import download, sha256, within

LOG = logging.getLogger(__name__)
LICENSE = "CC-BY-2.0-FR"
LICENSE_URL = "https://creativecommons.org/licenses/by/2.0/fr/"


def _convert(
    archive: Path, target: Path, language: str, limit: int, conversion_version: int = 1
) -> dict:
    count = unattributed = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            with bz2.open(archive, "rt", encoding="utf-8", errors="strict") as source:
                while line := source.readline(1_000_001):
                    if len(line) > 1_000_000:
                        raise ValueError("Tatoeba source line exceeds 1,000,000 characters")
                    fields = line.rstrip("\r\n").split("\t")
                    if len(fields) != 6 or fields[1] != language or not fields[0].isdigit():
                        raise ValueError("Unexpected Tatoeba detailed TSV record")
                    identifier, _, text, author, added, modified = fields
                    if author in {"", "\\N"}:
                        unattributed += 1
                        continue
                    row = {
                        "text": text,
                        "language": (
                            LEGACY_MACROLANGUAGES.get(language, canonical_language(language))
                            if conversion_version in {1, 2}
                            else canonical_language(language)
                        ),
                        "genre": "dialogue",
                        "attribution": {
                            "source": "Tatoeba",
                            "source_id": identifier,
                            "url": f"https://tatoeba.org/en/sentences/show/{identifier}",
                            "author": author,
                            "license": LICENSE,
                            "license_url": LICENSE_URL,
                            "date_added": added,
                            "date_modified": modified,
                            "modified": False,
                        },
                    }
                    if conversion_version >= 2:
                        row["attribution"]["source_language"] = language
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
                    count += 1
                    if count == limit:
                        break
        if count == 0:
            raise ValueError(f"No attributed Tatoeba records found for {language}")
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"records": count, "skipped_unattributed": unattributed, "raw_sha256": sha256(target)}


def snapshot(
    raw: Path,
    catalog: Path,
    cache: Path,
    languages: list[str],
    snapshot_date: str,
    limit: int = 10000,
) -> dict:
    date.fromisoformat(snapshot_date)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", snapshot_date) or limit < 1:
        raise ValueError("Use an ISO snapshot date and a positive record limit")
    if not languages or any(not re.fullmatch("[a-z]{3}", lang) for lang in languages):
        raise ValueError("Languages must use ISO 639-3 codes, for example ckb or eng")
    document = (
        json.loads(catalog.read_text(encoding="utf-8"))
        if catalog.exists()
        else {"schema_version": 1, "sources": []}
    )
    result = []
    for language in dict.fromkeys(languages):
        key = f"tatoeba-{snapshot_date}-{language}"
        existing = next((s for s in document["sources"] if s["id"] == key), None)
        relative = f"tatoeba/{snapshot_date}/{language}.jsonl"
        target = within(raw, relative)
        if existing:
            if existing["limit"] != limit:
                raise ValueError("Snapshot limit changed; choose a new snapshot date")
            if target.is_file() and sha256(target) == existing["raw_sha256"]:
                result.append(existing | {"cached": True})
                continue
            if target.exists():
                raise ValueError(f"Raw snapshot has changed: {target}; it will not be overwritten")
        elif target.exists():
            raise ValueError(f"Unregistered raw data already exists: {target}")
        url = (
            f"https://downloads.tatoeba.org/exports/per_language/{language}/"
            f"{language}_sentences_detailed.tsv.bz2"
        )
        archive = cache / snapshot_date / f"{language}.tsv.bz2"
        digest = download(
            url,
            existing["archive_sha256"] if existing else None,
            archive,
            128 * 1024 * 1024,
            {"downloads.tatoeba.org"},
        )
        conversion_version = existing.get("conversion_version", 1) if existing else 3
        converted = _convert(archive, target, language, limit, conversion_version)
        if existing and converted["raw_sha256"] != existing["raw_sha256"]:
            # Only remove this failed reproduction, never a preexisting raw snapshot.
            target.unlink()
            raise ValueError("Converted snapshot differs from its catalog checksum")
        entry = {
            "id": key,
            "provider": "Tatoeba",
            "language": language,
            "language_tag": canonical_language(language),
            "conversion_version": conversion_version,
            "snapshot": snapshot_date,
            "url": url,
            "archive_sha256": digest,
            "output": relative,
            "limit": limit,
            "license": LICENSE,
            "license_url": LICENSE_URL,
            **converted,
        }
        if not existing:
            document["sources"].append(entry)
        catalog.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=catalog.parent, delete=False
        ) as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            temp_catalog = Path(stream.name)
        os.replace(temp_catalog, catalog)
        result.append(entry)
        LOG.info("Imported %s: %s attributed sentences", language, entry["records"])
    return {
        "sources": len(result),
        "records": sum(s["records"] for s in result),
        "catalog": str(catalog),
    }
