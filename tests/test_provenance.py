"""Deduplication must retain every source's license and language identity."""

import csv
import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from synthocr.corpus_store import iter_origins
from synthocr.downloads import sha256
from synthocr.pipeline import export, generate
from synthocr.verify import verify

ROOT = Path(__file__).resolve().parents[1]


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )


def reseal(dataset, name, key):
    path = dataset / "stats.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value[key] = sha256(dataset / name)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def attributed_config(config):
    raw = config.root / "attributed-raw"
    raw.mkdir()
    write_jsonl(
        raw / "first.jsonl",
        [
            {
                "text": "chat",
                "language": "eng",
                "genre": "natural",
                "attribution": {
                    "source": "First English collection",
                    "author": "Author One",
                    "license": "CC-BY-4.0",
                },
            },
            {
                "text": "chat",
                "language": "fr",
                "genre": "natural",
                "attribution": {
                    "source": "French collection",
                    "author": "Author Trois",
                    "license": "CC-BY-SA-4.0",
                },
            },
            {
                "text": "Unselected German label",
                "language": "de",
                "genre": "natural",
                "attribution": {"source": "Unused collection", "license": "CC0-1.0"},
            },
        ],
    )
    write_jsonl(
        raw / "second.jsonl",
        [
            {
                "text": "chat",
                "language": "en",
                "genre": "natural",
                "attribution": {
                    "source": "Second English collection",
                    "author": "Author Two",
                    "license": "CC0-1.0",
                },
            }
        ],
    )
    return replace(
        config,
        data=replace(config.data, raw=str(raw)),
        fonts=replace(
            config.fonts,
            directories=[str(ROOT / "fonts/open/notosans"), str(ROOT / "fonts/open/notoserif")],
        ),
        generation=replace(config.generation, samples=4, languages=["en", "fr"]),
    ).validate()


@pytest.fixture
def attributed_dataset(attributed_config):
    generate(attributed_config)
    return attributed_config.path(attributed_config.generation.output)


def test_deduplicated_labels_keep_every_origin_without_rewriting_raw(attributed_config):
    raw = attributed_config.path(attributed_config.data.raw)
    before = {path.name: path.read_bytes() for path in raw.iterdir()}
    stats = generate(attributed_config)
    dataset = attributed_config.path(attributed_config.generation.output)
    manifest = read_jsonl(dataset / "manifest.jsonl")
    origins = read_jsonl(dataset / "origins.jsonl")
    run = json.loads((dataset / "run.json").read_text(encoding="utf-8"))

    assert run["schema_version"] == 3
    assert stats["origins_sha256"] == sha256(dataset / "origins.jsonl")
    assert {path.name: path.read_bytes() for path in raw.iterdir()} == before
    assert len(manifest) == 4
    assert {row["text"] for row in manifest} == {"chat"}
    assert Counter(row["language"] for row in manifest) == {"en": 2, "fr": 2}
    assert len({row["text_id"] for row in manifest}) == 2
    assert len({(row["text"], row["font_id"]) for row in manifest}) == 2
    assert len({row["split"] for row in manifest}) == 1

    # Each selected text identity appears once in the sidecar, with all its
    # sources, even when multiple fonts generate several images for that label.
    text_ids = dict.fromkeys(row["text_id"] for row in manifest)
    with sqlite3.connect(
        attributed_config.path(attributed_config.data.processed) / "texts.sqlite3"
    ) as db:
        expected = [
            origin | {"text_sha256": hashlib.sha256(b"chat").hexdigest()}
            for text_id in text_ids
            for origin in iter_origins(db, text_id)
        ]
    assert origins == expected
    assert len(origins) == 3
    assert {row["source_language"] for row in origins} == {"eng", "en", "fr"}
    assert {row["attribution"]["license"] for row in origins} == {
        "CC-BY-4.0",
        "CC-BY-SA-4.0",
        "CC0-1.0",
    }
    assert all(row["attribution"]["source"] != "Unused collection" for row in origins)
    assert verify(dataset)["records"] == 4


@pytest.mark.parametrize("format", ["csv", "imagefolder", "tesseract"])
def test_exports_preserve_full_origins_and_text_identity(attributed_dataset, format):
    dataset = attributed_dataset
    output = dataset.parent / f"export-{format}"
    export(dataset, output, format)
    assert (output / "origins.jsonl").read_bytes() == (dataset / "origins.jsonl").read_bytes()
    stats = json.loads((output / "export.json").read_text(encoding="utf-8"))
    assert stats["origins_sha256"] == sha256(output / "origins.jsonl")
    assert stats["source_manifest_sha256"] == sha256(dataset / "manifest.jsonl")
    if format == "csv":
        with (output / "labels.csv").open(encoding="utf-8", newline="") as stream:
            labels = list(csv.DictReader(stream))
    else:
        labels = read_jsonl(
            output / ("metadata.jsonl" if format == "imagefolder" else "attribution.jsonl")
        )
    ids = {row["text_id"] for row in read_jsonl(output / "origins.jsonl")}
    assert len(labels) == 4
    assert {int(row["text_id"]) for row in labels} == ids
    assert {row["language"] for row in labels} == {"en", "fr"}


def test_origin_checksum_is_verified(attributed_dataset):
    with (attributed_dataset / "origins.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(" ")
    with pytest.raises(ValueError, match="checksum mismatch: origins.jsonl"):
        verify(attributed_dataset)


@pytest.mark.parametrize("missing", ["file", "checksum"])
def test_new_datasets_require_origins(attributed_dataset, missing):
    if missing == "file":
        (attributed_dataset / "origins.jsonl").unlink()
    else:
        path = attributed_dataset / "stats.json"
        stats = json.loads(path.read_text(encoding="utf-8"))
        del stats["origins_sha256"]
        path.write_text(json.dumps(stats), encoding="utf-8")
    with pytest.raises(ValueError, match="Missing origins"):
        verify(attributed_dataset)


@pytest.mark.parametrize("change", ["missing", "unused", "language", "conflicting_language"])
def test_origin_identities_and_languages_are_verified_after_checksum(attributed_dataset, change):
    path = attributed_dataset / "origins.jsonl"
    origins = read_jsonl(path)
    selected = origins[0]["text_id"]
    if change == "missing":
        origins = [row for row in origins if row["text_id"] != selected]
    elif change == "unused":
        origins.append(origins[0] | {"text_id": 999999})
    elif change == "language":
        for row in origins:
            if row["text_id"] == selected:
                row["source_language"] = "de"
    else:
        origins.append(origins[0] | {"source_language": "de"})
    write_jsonl(path, origins)
    reseal(attributed_dataset, "origins.jsonl", "origins_sha256")
    with pytest.raises(ValueError, match="origin|identit"):
        verify(attributed_dataset)


@pytest.mark.parametrize(
    "field,value",
    [
        ("text_id", 0),
        ("text_id", True),
        ("source", ""),
        ("line", 0),
        ("attribution", []),
        ("text_sha256", None),
        ("text_sha256", "short"),
    ],
)
def test_malformed_origins_are_rejected(attributed_dataset, field, value):
    path = attributed_dataset / "origins.jsonl"
    origins = read_jsonl(path)
    origins[0][field] = value
    write_jsonl(path, origins)
    reseal(attributed_dataset, "origins.jsonl", "origins_sha256")
    with pytest.raises(ValueError, match="origin"):
        verify(attributed_dataset)


@pytest.mark.parametrize("name", ["origins.jsonl", "manifest.jsonl"])
@pytest.mark.parametrize("value", [[], None, 42, "not an object", True])
def test_metadata_rows_must_be_objects_after_checksum(attributed_dataset, name, value):
    path = attributed_dataset / name
    rows = read_jsonl(path)
    rows[0] = value
    write_jsonl(path, rows)
    key = "origins_sha256" if name == "origins.jsonl" else "manifest_sha256"
    reseal(attributed_dataset, name, key)
    with pytest.raises(ValueError, match="object"):
        verify(attributed_dataset)


@pytest.mark.parametrize(
    "operation", ["verify", "metadata_only", "csv", "imagefolder", "tesseract"]
)
def test_origins_cannot_link_outside_the_dataset(attributed_dataset, operation):
    dataset = attributed_dataset
    path = dataset / "origins.jsonl"
    before = path.read_bytes()
    outside = dataset.parent / "external-origins.jsonl"
    outside.write_bytes(before)
    path.unlink()
    path.symlink_to(outside)
    output = dataset.parent / f"symlink-export-{operation}"
    with pytest.raises(ValueError, match="symbolic links"):
        if operation in {"verify", "metadata_only"}:
            verify(dataset, check_images=operation == "verify")
        else:
            export(dataset, output, operation)
    assert outside.read_bytes() == before
    assert not output.exists()
    assert not list(dataset.parent.glob(".export-*"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "fabricated.jsonl"),
        ("source_line", 999999),
        ("genre", "fabricated"),
        ("attribution", {"source": "Fabricated collection", "license": "CC0-1.0"}),
    ],
)
def test_manifest_cannot_fabricate_a_primary_source(attributed_dataset, field, value):
    dataset = attributed_dataset
    path = dataset / "manifest.jsonl"
    rows = read_jsonl(path)
    rows[0][field] = value
    write_jsonl(path, rows)
    reseal(dataset, "manifest.jsonl", "manifest_sha256")
    with pytest.raises(ValueError, match="attribution.*corpus origin"):
        verify(dataset)


def test_manifest_can_select_a_complete_valid_secondary_origin(attributed_dataset):
    dataset = attributed_dataset
    path = dataset / "manifest.jsonl"
    rows = read_jsonl(path)
    english = next(row for row in rows if row["language"] == "en")
    secondary = next(
        origin
        for origin in read_jsonl(dataset / "origins.jsonl")
        if origin["text_id"] == english["text_id"] and origin["source"] != english["source"]
    )
    english.update(
        source=secondary["source"],
        source_line=secondary["line"],
        genre=secondary["genre"],
        attribution=secondary["attribution"],
    )
    write_jsonl(path, rows)
    reseal(dataset, "manifest.jsonl", "manifest_sha256")
    assert verify(dataset)["records"] == 4


def test_manifest_cannot_mix_fields_from_different_valid_origins(attributed_dataset):
    dataset = attributed_dataset
    path = dataset / "manifest.jsonl"
    rows = read_jsonl(path)
    english = next(row for row in rows if row["language"] == "en")
    secondary = next(
        origin
        for origin in read_jsonl(dataset / "origins.jsonl")
        if origin["text_id"] == english["text_id"] and origin["source"] != english["source"]
    )
    # Both values really occur in the sidecar, but never as the same source.
    english["source"] = secondary["source"]
    assert english["attribution"] != secondary["attribution"]
    write_jsonl(path, rows)
    reseal(dataset, "manifest.jsonl", "manifest_sha256")
    with pytest.raises(ValueError, match="attribution.*corpus origin"):
        verify(dataset)


@pytest.mark.parametrize("field", ["text", "text_id"])
def test_manifest_labels_are_bound_to_their_origin_identities(config, field):
    generate(config)
    dataset = config.path(config.generation.output)
    manifest = read_jsonl(dataset / "manifest.jsonl")
    english = [row for row in manifest if row["language"] == "en"]
    assert len(english) == 2
    if field == "text":
        english[0]["text"] = "Changed label"
    else:
        # Both labels have the same language, so the text hash must reject an
        # identity swap even when the language and both origin IDs are valid.
        english[0]["text_id"] = english[1]["text_id"]
    write_jsonl(dataset / "manifest.jsonl", manifest)
    reseal(dataset, "manifest.jsonl", "manifest_sha256")
    with pytest.raises(ValueError, match="corpus origin"):
        verify(dataset)


@pytest.mark.parametrize("format", ["csv", "imagefolder", "tesseract"])
def test_bad_provenance_cannot_publish_an_export(attributed_dataset, format):
    path = attributed_dataset / "origins.jsonl"
    path.write_bytes(path.read_bytes() + b" ")
    output = attributed_dataset.parent / f"bad-export-{format}"
    with pytest.raises(ValueError, match="origins.jsonl"):
        export(attributed_dataset, output, format)
    assert not output.exists()
    assert not list(output.parent.glob(".export-*"))


@pytest.mark.parametrize("format", ["csv", "imagefolder", "tesseract"])
def test_old_schema_two_datasets_remain_usable_without_origins(config, format):
    # The original schema uses unique text/font pairs across source languages.
    # The shared fixture's labels are distinct, as a real schema-two dataset's are.
    generate(config)
    dataset = config.path(config.generation.output)
    run_path = dataset / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["schema_version"] = 2
    run_path.write_text(json.dumps(run), encoding="utf-8")
    reseal(dataset, "run.json", "run_sha256")
    stats_path = dataset / "stats.json"
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    stats.pop("origins_sha256")
    stats_path.write_text(json.dumps(stats), encoding="utf-8")
    (dataset / "origins.jsonl").unlink()

    assert verify(dataset)["records"] == 4
    output = dataset.parent / f"legacy-export-{format}"
    assert export(dataset, output, format)["records"] == 4
    assert not (output / "origins.jsonl").exists()
    exported = json.loads((output / "export.json").read_text(encoding="utf-8"))
    assert "origins_sha256" not in exported
