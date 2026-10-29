"""Corpus identity must use explicit metadata without losing dialect identity."""

import bz2
import json
import sqlite3
from dataclasses import replace

import pytest

from synthocr.corpora import _convert
from synthocr.corpus_store import (
    ALIASES,
    add_origin,
    canonical_language,
    iter_origins,
    registered_source_languages,
    resolve_source_language,
)
from synthocr.downloads import sha256
from synthocr.text import prepare


@pytest.mark.parametrize("alias,canonical", ALIASES.items())
def test_known_source_codes_share_a_canonical_identity(alias, canonical):
    assert canonical_language(alias) == canonical_language(canonical.upper()) == canonical


@pytest.mark.parametrize(
    "tag", ["", "ku", "ckb", "kmr", "sdh", "ku-Arab", "ku-Latn", "ckb-IQ", "kmr-TR", "zz"]
)
def test_languages_dialects_and_subtags_remain_distinct(tag):
    assert canonical_language(tag) == tag.casefold()


@pytest.mark.parametrize(
    "alias,canonical", [("eng-US", "en-us"), ("ara-Arab-IQ", "ar-arab-iq"), ("hye-AM", "hy-am")]
)
def test_known_base_aliases_keep_every_script_and_region_subtag(alias, canonical):
    assert canonical_language(alias) == canonical_language(canonical) == canonical


@pytest.mark.parametrize("specific,macro", [("cmn", "zh"), ("pes", "fa"), ("swh", "sw")])
def test_specific_varieties_are_never_macrolanguage_aliases(specific, macro):
    assert canonical_language(specific) == specific
    assert canonical_language(specific) != canonical_language(macro)
    assert canonical_language(specific + "-Latn") != canonical_language(macro + "-Latn")


@pytest.mark.parametrize("specific,macro", [("cmn", "zh"), ("pes", "fa"), ("swh", "sw")])
def test_only_explicit_reviewed_provenance_recovers_a_specific_variety(specific, macro):
    assert resolve_source_language(macro, {}) == macro
    assert resolve_source_language(macro, {"source_language": specific}) == specific
    assert resolve_source_language(macro, {}, registered=specific) == specific
    assert resolve_source_language(specific, {"source_language": macro}) == specific
    assert resolve_source_language("ckb", {"source_language": specific}) == "ckb"
    assert resolve_source_language("kmr", {}, registered=specific) == "kmr"


@pytest.mark.parametrize("tag", [None, 42, [], " en", "en ", "ckb/kmr", "en_US", "en-", "x"])
def test_bad_source_tags_are_rejected_instead_of_guessed(tag):
    with pytest.raises(ValueError, match="language"):
        canonical_language(tag)


def _replace_sources(config, rows):
    raw = config.root / "raw"
    for path in raw.iterdir():
        path.unlink()
    path = raw / "collection.jsonl"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    return path


def test_one_corpus_per_language_merges_aliases_without_collapsing_dialects(config):
    raw = _replace_sources(
        config,
        [
            {"text": "same", "language": tag, "attribution": {"author": tag}}
            for tag in ("ar", "ara", "AR", "ckb", "kmr", "sdh", "ku", "ckb-IQ", "")
        ],
    )
    before = raw.read_bytes()
    config = replace(config, data=replace(config.data, write_jsonl=False))
    stats = prepare(config)
    assert stats["records"] == 7
    assert stats["duplicates"] == 2
    assert stats["origins"] == 9
    registry_path = config.root / "processed/corpora.json"
    registry = json.loads(registry_path.read_text())
    assert registry["fingerprint"] == stats["fingerprint"]
    corpora = registry["corpora"]
    tags = [row["language"] for row in corpora]
    assert len(tags) == len(set(tags)) == 7
    assert set(tags) == {"ar", "ckb", "kmr", "sdh", "ku", "ckb-iq", ""}
    assert all(row["records"] == row["source_count"] == 1 for row in corpora)
    assert all(row["sources"] == [{"path": raw.name, "sha256": sha256(raw)}] for row in corpora)
    assert not (config.root / "processed/texts.jsonl").exists()
    assert raw.read_bytes() == before
    with sqlite3.connect(config.root / "processed/texts.sqlite3") as db:
        identifier = db.execute("SELECT id FROM texts WHERE language='ar'").fetchone()[0]
        origins = list(iter_origins(db, identifier))
    assert [row["source_language"] for row in origins] == ["ar", "ara", "AR"]
    assert [row["attribution"]["author"] for row in origins] == ["ar", "ara", "AR"]
    assert origins[1]["attribution"]["source_language"] == "ara"
    assert origins[2]["attribution"]["source_language"] == "AR"


def test_duplicate_provenance_is_retained_even_when_diagnostic_primary_is_replaced(config):
    raw = config.root / "raw"
    for path in raw.iterdir():
        path.unlink()
    (raw / "a-probe.jsonl").write_text(
        json.dumps(
            {
                "text": "سڵاو",
                "language": "ckb",
                "genre": "spelling",
                "attribution": {"kind": "words"},
            }
        )
        + "\n"
    )
    (raw / "z-natural.jsonl").write_text(
        json.dumps(
            {"text": "سڵاو", "language": "ckb", "genre": "dialogue", "attribution": {"author": "A"}}
        )
        + "\n"
    )
    stats = prepare(config)
    assert stats["records"] == 1 and stats["origins"] == 2
    assert stats["genres"] == {"dialogue": 1}
    assert stats["corpora"][0]["diagnostic_records"] == 0
    assert stats["corpora"][0]["source_count"] == 2
    with sqlite3.connect(config.root / "processed/texts.sqlite3") as db:
        origins = list(iter_origins(db))
        assert [row["attribution"] for row in origins] == [
            {"kind": "words", "modified": False},
            {"author": "A", "modified": False},
        ]
        assert [row["genre"] for row in origins] == ["spelling", "dialogue"]
        row = db.execute("SELECT source,line,genre,language,attribution FROM texts").fetchone()
        assert row[:4] == ("z-natural.jsonl", 1, "dialogue", "ckb")
        # The primary JSON is shared, rather than duplicated in every origin.
        assert (
            db.execute("SELECT COUNT(*) FROM origins WHERE attribution IS NULL").fetchone()[0] == 1
        )
    exported = json.loads((config.root / "processed/texts.jsonl").read_text())
    assert exported["source"] == "z-natural.jsonl" and exported["genre"] == "dialogue"


def test_primary_json_is_stored_once_and_identical_origins_are_idempotent(config):
    _replace_sources(config, [{"text": "Hello", "language": "en", "attribution": {"author": "A"}}])
    prepare(config)
    with sqlite3.connect(config.root / "processed/texts.sqlite3") as db:
        initial = list(iter_origins(db))
        row = initial[0]
        add_origin(
            db, row["text_id"], row["source"], row["line"], row["genre"], "en", row["attribution"]
        )
        assert list(iter_origins(db)) == initial
        assert db.execute("SELECT attribution FROM origins").fetchone()[0] is None
        with pytest.raises(ValueError, match="existing text"):
            add_origin(db, 999, "x", 1, "prose", "en", {})


def test_default_single_sqlite_store_removes_obsolete_optional_jsonl(config):
    prepare(config)
    jsonl = config.root / "processed/texts.jsonl"
    assert jsonl.exists()
    compact = replace(config, data=replace(config.data, write_jsonl=False))
    stats = prepare(compact)
    assert not jsonl.exists()
    assert set(stats["artifacts"]) == {"texts.sqlite3", "corpora.json"}
    assert prepare(compact)["cached"] is True
    registry = config.root / "processed/corpora.json"
    registry.write_text("broken derived metadata")
    assert "cached" not in prepare(compact)


def test_identical_labels_in_different_languages_retain_both_sources(config):
    _replace_sources(config, [{"text": "123", "language": tag} for tag in ["ckb", "kmr"]])
    stats = prepare(config)
    assert stats["records"] == stats["origins"] == 2
    assert stats["languages"] == {"ckb": 1, "kmr": 1}


def test_new_tatoeba_conversion_preserves_original_source_code_without_rewriting_v1(tmp_path):
    archive = tmp_path / "source.bz2"
    archive.write_bytes(bz2.compress(b"1\tell\tHello\tAuthor\t2020\t2021\n"))
    old, new = tmp_path / "v1.jsonl", tmp_path / "v2.jsonl"
    _convert(archive, old, "ell", 100)
    _convert(archive, new, "ell", 100, conversion_version=2)
    assert json.loads(new.read_text())["language"] == "el"
    assert json.loads(new.read_text())["attribution"]["source_language"] == "ell"
    assert "source_language" not in json.loads(old.read_text())["attribution"]


def test_many_duplicate_origins_stream_without_building_an_attribution_array(config):
    _replace_sources(
        config,
        [
            {"text": "سڵاو", "language": "ckb", "attribution": {"source_id": str(i)}}
            for i in range(2000)
        ],
    )
    compact = replace(config, data=replace(config.data, write_jsonl=False))
    stats = prepare(compact)
    assert stats["records"] == 1 and stats["origins"] == 2000
    with sqlite3.connect(config.root / "processed/texts.sqlite3") as db:
        assert sum(1 for _ in iter_origins(db)) == 2000
        attribution = json.loads(db.execute("SELECT attribution FROM texts").fetchone()[0])
        assert attribution == {"source_id": "0", "modified": False}


@pytest.mark.parametrize("specific,macro", [("cmn", "zh"), ("pes", "fa"), ("swh", "sw")])
def test_frozen_legacy_conversion_is_byte_compatible_but_new_imports_keep_precision(
    tmp_path, specific, macro
):
    archive = tmp_path / "source.bz2"
    archive.write_bytes(bz2.compress(f"1\t{specific}\tHello\tAuthor\t2020\t2021\n".encode()))
    legacy, v2, new = tmp_path / "old.jsonl", tmp_path / "v2.jsonl", tmp_path / "new.jsonl"
    _convert(archive, legacy, specific, 10, conversion_version=1)
    _convert(archive, v2, specific, 10, conversion_version=2)
    _convert(archive, new, specific, 10, conversion_version=3)
    old_row, v2_row, new_row = (
        json.loads(legacy.read_text()),
        json.loads(v2.read_text()),
        json.loads(new.read_text()),
    )
    assert old_row["language"] == macro
    assert "source_language" not in old_row["attribution"]
    assert v2_row["language"] == macro
    assert v2_row["attribution"]["source_language"] == specific
    assert new_row["language"] == new_row["attribution"]["source_language"] == specific


def test_frozen_v2_macro_tag_is_resolved_from_original_source_metadata(config):
    raw = _replace_sources(
        config,
        [
            {"text": "same", "language": "sw", "attribution": {"source_language": "swh"}},
            {"text": "same", "language": "sw"},
            {"text": "same", "language": "swh"},
        ],
    )
    before = raw.read_bytes()
    stats = prepare(config)
    assert stats["languages"] == {"sw": 1, "swh": 1}
    assert stats["origins"] == 3
    assert raw.read_bytes() == before
    with sqlite3.connect(config.root / "processed/texts.sqlite3") as db:
        identifier = db.execute("SELECT id FROM texts WHERE language='swh'").fetchone()[0]
        origins = list(iter_origins(db, identifier))
    assert origins[0]["source_language"] == "swh"
    assert origins[0]["attribution"]["original_language"] == "sw"


def test_only_pinned_catalog_metadata_can_recover_old_source_precision_and_invalidates_cache(
    config,
):
    raw = _replace_sources(config, [{"text": "Hello", "language": "zh"}])
    before = raw.read_bytes()
    initial = prepare(config)
    assert initial["languages"] == {"zh": 1}
    catalog = raw.parent.parent / "corpora.json"
    entry = {"output": raw.name, "language": "cmn", "raw_sha256": sha256(raw)}
    catalog.write_text(json.dumps({"schema_version": 1, "sources": [entry]}))
    resolved = prepare(config)
    assert resolved["languages"] == {"cmn": 1}
    assert resolved["fingerprint"] != initial["fingerprint"]
    assert "cached" not in resolved
    assert prepare(config)["cached"] is True
    with sqlite3.connect(config.root / "processed/texts.sqlite3") as db:
        origin = next(iter_origins(db))
    assert origin["source_language"] == "cmn"
    assert origin["attribution"] == {
        "source_language": "cmn",
        "original_language": "zh",
        "modified": False,
    }
    entry["raw_sha256"] = "0" * 64
    catalog.write_text(json.dumps({"schema_version": 1, "sources": [entry]}))
    conservative = prepare(config)
    assert conservative["languages"] == {"zh": 1}
    assert conservative["fingerprint"] == initial["fingerprint"]
    assert raw.read_bytes() == before


def test_pinned_source_metadata_never_overrides_a_conflicting_explicit_language(config):
    raw = _replace_sources(config, [{"text": "سڵاو", "language": "ckb"}])
    catalog = raw.parent.parent / "corpora.json"
    entry = {"output": raw.name, "language": "pes", "raw_sha256": sha256(raw)}
    catalog.write_text(json.dumps({"sources": [entry]}))
    assert prepare(config)["languages"] == {"ckb": 1}
    with sqlite3.connect(config.root / "processed/texts.sqlite3") as db:
        assert next(iter_origins(db))["source_language"] == "ckb"


def test_registered_source_metadata_is_optional_but_ambiguous_catalogs_are_rejected(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    sources = [{"path": "x.jsonl", "sha256": "1" * 64}]
    assert registered_source_languages(raw, sources) == {}
    catalog = tmp_path / "corpora.json"
    entries = [
        {"output": "x.jsonl", "language": tag, "raw_sha256": "1" * 64} for tag in ("cmn", "pes")
    ]
    catalog.write_text(json.dumps({"sources": entries}))
    with pytest.raises(ValueError, match="Conflicting source languages"):
        registered_source_languages(raw, sources)
