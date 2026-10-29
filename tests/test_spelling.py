import json
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from synthocr.cli import main
from synthocr.config import Generation
from synthocr.corpus_store import iter_origins
from synthocr.fonts import Coverage, discover
from synthocr.pipeline import generate
from synthocr.render import Renderer
from synthocr.spelling import spelling_cases, write_spelling
from synthocr.text import GRAPHEME, chunks, normalize, prepare
from synthocr.verify import verify

ROOT = Path(__file__).resolve().parents[1]
CASES = list(spelling_cases())
FONTS = discover([ROOT / "fonts/open"])
COVERAGE = Coverage(128)
RENDERERS = {
    augmentation: Renderer(Generation(augmentation=augmentation), 16) for augmentation in (0, 1)
}


@pytest.mark.parametrize("case", CASES, ids=[row["attribution"]["source_id"] for row in CASES])
@pytest.mark.parametrize("augmentation", [0, 1], ids=["clean", "augmented"])
def test_each_spelling_case_has_a_licensed_font_and_visible_unclipped_render(case, augmentation):
    # Test the actual default preprocessing, including canonical accent forms.
    text = normalize(case["text"])
    assert normalize(case["text"], "none") == case["text"]
    preferred = "notonaskharabic" if case["language"] == "ckb" else "notosans"
    fonts = sorted(FONTS, key=lambda font: Path(font.path).parent.name != preferred)
    face = COVERAGE.choose(fonts, text, 0)
    assert face is not None, case["attribution"]["source_id"]
    image, metadata = RENDERERS[augmentation].render(text, face, 17, case["language"])
    try:
        assert metadata["language"] == case["language"]
        left, top, right, bottom = metadata["bbox"]
        assert 0 < left < right < image.width
        assert 0 < top < bottom < image.height
        low, high = image.getextrema()
        assert high - low > 16
    finally:
        image.close()


def test_suite_covers_core_alphabets_contexts_and_common_spelling_features():
    ids = [case["attribution"]["source_id"] for case in CASES]
    assert len(ids) == len(set(ids))
    assert len({normalize(case["text"]) for case in CASES}) == len(CASES)
    profiles = json.loads((ROOT / "synthocr/font_profiles.json").read_text())
    sorani = {
        chr(int(identifier.rsplit("-", 1)[1], 16))
        for identifier in ids
        if identifier.startswith("ckb-context-")
    }
    kurmanji = {
        chr(int(identifier.rsplit("-", 1)[1], 16))
        for identifier in ids
        if identifier.startswith("kmr-case-")
    }
    assert sorani == set(profiles["sorani"]["letters"])
    assert kurmanji == {letter for letter in profiles["kurmanji"]["letters"] if letter.islower()}
    features = {feature for row in CASES for feature in row["attribution"]["features"]}
    assert {
        "vowel_initial",
        "yeh_vowels",
        "unwritten_short_vowel",
        "reh_rreh",
        "lam_llam",
        "waw_oe_double_waw",
        "heh_ae",
        "compounds",
        "lam_alef_context",
        "ezafe",
        "definiteness",
        "plurals",
        "possessive_suffixes",
        "verb_prefixes",
        "join_controls",
        "combining_marks",
        "digit_sets",
        "formatted_numbers",
        "punctuation",
        "mixed_direction",
        "heh_encoding_variants",
        "keyboard_variants",
        "canonical_accents",
        "accented_letters",
        "isolated",
        "initial",
        "medial",
        "final",
        "lowercase",
        "uppercase",
    } <= features
    assert {case["language"] for case in CASES} == {"ckb", "kmr"}


def test_words_only_export_excludes_diagnostics_and_keeps_its_provenance(tmp_path):
    path = tmp_path / "words.jsonl"
    result = write_spelling(path, words_only=True)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert result["records"] == len(rows) < len(CASES)
    assert all(row["attribution"]["kind"] == "words" for row in rows)
    assert all(row["attribution"]["synthetic"] for row in rows)
    assert all(row["attribution"]["license"] == "CC0-1.0" for row in rows)
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_spelling(path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("language", ["ckb", "kmr"])
def test_frozen_raw_spelling_snapshot_reproduces_exactly(tmp_path, language):
    path = tmp_path / "cases.jsonl"
    result = write_spelling(path, language)
    assert (
        path.read_bytes() == (ROOT / f"data/raw/kurdish-spelling-v1/{language}.jsonl").read_bytes()
    )
    assert set(result["languages"]) == {language}


def test_bad_language_does_not_publish_a_partial_export(tmp_path):
    path = tmp_path / "cases.jsonl"
    with pytest.raises(ValueError, match="ckb, kmr or mixed"):
        write_spelling(path, "ar")
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_spelling_cli_and_preparation_preserve_all_cases_and_feature_tags(config, capsys):
    source = config.root / "raw/cases.jsonl"
    assert main(["spelling", "--output", str(source)]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["records"] == len(CASES)
    before = source.read_bytes()
    # Keep every diagnostic whole for this audit; ordinary datasets can wrap.
    prepared = replace(config, data=replace(config.data, max_chars=200))
    prepare(prepared)
    with sqlite3.connect(config.root / "processed/texts.sqlite3") as db:
        rows = [row for row in iter_origins(db) if row["source"] == "cases.jsonl"]
    assert len(rows) == len(CASES)
    assert {row["attribution"]["source_id"] for row in rows} == {
        row["attribution"]["source_id"] for row in CASES
    }
    assert Counter(
        feature for row in rows for feature in row["attribution"]["features"]
    ) == Counter(feature for row in CASES for feature in row["attribution"]["features"])
    assert source.read_bytes() == before


@pytest.mark.parametrize("text", ["ه‌", "بَّ", "ئ", "ç", "Ç", "ê", "Ş", "û"])
def test_spelling_graphemes_do_not_split_during_wrapping(text):
    value = normalize(text, "none") * 13
    wrapped = list(chunks(value, 3))
    assert "".join(wrapped) == value
    assert all(len(GRAPHEME.findall(row)) <= 3 for row in wrapped)
    assert all(not unicodedata.combining(row[0]) for row in wrapped)


def test_spelling_variants_are_preserved_except_for_canonical_equivalence():
    assert normalize("ک ك ی ي ى ه ھ ە ه‌", "NFC") == "ک ك ی ي ى ه ھ ە ه‌"
    assert normalize("i I ı İ ş ș", "NFC") == "i I ı İ ş ș"
    assert normalize("ç ê î ş û") == "ç ê î ş û"
    assert normalize("ئێمە") == "ئێمە"


def test_generation_suite_covers_every_case_without_reading_or_modifying_user_corpus(config):
    source = config.root / "raw/sample.jsonl"
    original = source.read_bytes()
    selected = replace(
        config,
        data=replace(config.data, max_chars=200, source_languages={"unrelated-missing.txt": "en"}),
        generation=replace(
            config.generation, suite="kurdish-spelling", samples=len(CASES), augmentation=1
        ),
    )
    generate(selected)
    with (config.root / "output/manifest.jsonl").open() as stream:
        rows = [json.loads(line) for line in stream]
    assert {row["attribution"]["source_id"] for row in rows} == {
        row["attribution"]["source_id"] for row in CASES
    }
    assert all(row["source"] == "spelling.jsonl" for row in rows)
    assert source.read_bytes() == original
    assert not (config.root / "processed").exists()
    assert verify(config.root / "output")["records"] == len(CASES)
    parallel = replace(
        selected, generation=replace(selected.generation, workers=2, output="parallel")
    )
    generate(parallel)
    assert (config.root / "parallel/manifest.jsonl").read_bytes() == (
        config.root / "output/manifest.jsonl"
    ).read_bytes()


def test_generation_rejects_unknown_suite(config):
    with pytest.raises(ValueError, match="suite must"):
        replace(config, generation=replace(config.generation, suite="unknown")).validate()


@pytest.mark.parametrize("normalization", ["none", "NFC"])
def test_suite_language_filter_and_normalization_preserve_expected_spelling(config, normalization):
    kurmanji = list(spelling_cases("kmr"))
    selected = replace(
        config,
        data=replace(config.data, normalization=normalization, max_chars=200),
        generation=replace(
            config.generation,
            suite="kurdish-spelling",
            languages=["KMR"],
            samples=len(kurmanji),
        ),
    )
    generate(selected)
    with (config.root / "output/manifest.jsonl").open() as stream:
        actual = {row["attribution"]["source_id"]: row for row in map(json.loads, stream)}
    assert len(actual) == len(kurmanji)
    for original in kurmanji:
        row = actual[original["attribution"]["source_id"]]
        assert row["language"] == "kmr"
        assert row["text"] == normalize(original["text"], normalization)
        assert row["attribution"]["modified"] == (row["text"] != original["text"])
    assert verify(config.root / "output")["records"] == len(kurmanji)
