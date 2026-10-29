"""Authored Kurdish spelling examples and explicitly labelled rendering probes."""

import hashlib
import json
import os
import tempfile
from collections import Counter
from importlib.resources import files
from pathlib import Path

LANGUAGES = ("ckb", "kmr", "mixed")


def spelling_cases(language: str = "mixed", words_only: bool = False):
    if language not in LANGUAGES:
        raise ValueError("Spelling cases support ckb, kmr or mixed")
    resource = files("synthocr").joinpath("kurdish_spelling.json").read_bytes()
    catalog = json.loads(resource)
    digest = hashlib.sha256(resource).hexdigest()

    def record(identifier, locale, text, kind, features):
        return {
            "text": text,
            "language": locale,
            "genre": "spelling",
            "attribution": {
                "source": "SynthOCR Kurdish spelling cases",
                "version": catalog["version"],
                "catalog_sha256": digest,
                "source_id": identifier,
                "features": features,
                "kind": kind,
                "synthetic": True,
                "license": "CC0-1.0",
                "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                "modified": False,
            },
        }

    for group in catalog["groups"]:
        if language != "mixed" and group["language"] != language:
            continue
        if words_only and group["kind"] != "words":
            continue
        for index, text in enumerate(group["texts"], 1):
            yield record(
                f"{group['id']}-{index:02}",
                group["language"],
                text,
                group["kind"],
                [group["feature"]],
            )
    if words_only:
        return
    profiles = json.loads(
        files("synthocr").joinpath("font_profiles.json").read_text(encoding="utf-8")
    )
    if language in {"ckb", "mixed"}:
        for letter in profiles["sorani"]["letters"]:
            # Context strings are diagnostics, not invented dictionary words.
            # Non-joining letters correctly stay unconnected where required.
            yield record(
                f"ckb-context-{ord(letter):04x}",
                "ckb",
                f"{letter} | {letter}م | س{letter}م | س{letter}",
                "joining_probe",
                ["core_alphabet", "isolated", "initial", "medial", "final"],
            )
    if language in {"kmr", "mixed"}:
        letters = profiles["kurmanji"]["letters"]
        for letter in letters:
            if letter.islower():
                yield record(
                    f"kmr-case-{ord(letter):04x}",
                    "kmr",
                    f"{letter} {letter.upper()}",
                    "case_probe",
                    ["core_alphabet", "lowercase", "uppercase"],
                )


def write_spelling(output: Path, language: str = "mixed", words_only: bool = False) -> dict:
    """Publish one record per case atomically, without inflating counts by repetition."""
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    languages, features, kinds = Counter(), Counter(), Counter()
    count = 0
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            for row in spelling_cases(language, words_only):
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                count += 1
                languages[row["language"]] += 1
                features.update(row["attribution"]["features"])
                kinds[row["attribution"]["kind"]] += 1
        os.link(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {
        "output": str(output),
        "records": count,
        "languages": dict(languages),
        "features": dict(features),
        "kinds": dict(kinds),
    }
