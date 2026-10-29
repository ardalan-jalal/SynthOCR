"""Per-language shares divide a mixed dataset without changing unweighted runs."""

import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from synthocr.cli import parser
from synthocr.pipeline import _interleave, _quotas, generate
from synthocr.verify import verify

ROOT = Path(__file__).resolve().parents[1]


def manifest_languages(config):
    dataset = config.path(config.generation.output)
    lines = (dataset / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["language"] for line in lines]


@pytest.fixture
def bilingual(config):
    raw = config.root / "bilingual-raw"
    raw.mkdir()
    rows = [{"text": f"English line {index}", "language": "en"} for index in range(60)]
    rows += [{"text": f"Ligne française {index}", "language": "fr"} for index in range(60)]
    (raw / "lines.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    return replace(
        config,
        data=replace(config.data, raw=str(raw)),
        fonts=replace(config.fonts, directories=[str(ROOT / "fonts/open/notosans")]),
        generation=replace(config.generation, samples=20, languages=["en", "fr"]),
    )


def test_weights_set_exact_language_counts_when_text_is_sufficient(bilingual):
    config = replace(
        bilingual,
        generation=replace(bilingual.generation, language_weights={"en": 3, "fr": 1}),
    ).validate()
    generate(config)
    verify(config.path(config.generation.output))
    assert Counter(manifest_languages(config)) == {"en": 15, "fr": 5}


def test_weighted_runs_are_reproducible_and_spread_languages_evenly(bilingual, tmp_path):
    runs = []
    for name in ("first", "second"):
        config = replace(
            bilingual,
            generation=replace(
                bilingual.generation,
                output=str(tmp_path / name),
                language_weights={"en": 1, "fr": 1},
            ),
        ).validate()
        generate(config)
        runs.append(manifest_languages(config))
    assert runs[0] == runs[1]
    # Interleaved rather than one language followed by the other.
    assert Counter(runs[0][:10]) == {"en": 5, "fr": 5}


def test_zero_weight_excludes_a_language(bilingual):
    config = replace(
        bilingual,
        generation=replace(bilingual.generation, language_weights={"en": 1, "fr": 0}),
    ).validate()
    generate(config)
    assert set(manifest_languages(config)) == {"en"}


def test_quotas_sum_exactly_and_omitted_weights_default_to_one():
    assert _quotas(["a", "b", "c"], {"a": 1, "b": 1, "c": 1}, 10) == [4, 3, 3]
    assert sum(_quotas(["a", "b"], {"a": 2}, 7)) == 7
    assert _quotas(["a", "b"], {"a": 2}, 6) == [4, 2]


def test_an_exhausted_language_is_filled_from_the_others():
    streams = [iter(["a1", "a2"]), iter(f"b{index}" for index in range(10))]
    drawn = list(_interleave(streams, [5, 5], 10))
    assert len(drawn) == 10
    assert drawn.count("a1") + drawn.count("a2") == 2


@pytest.mark.parametrize(
    "weights",
    [{"de": 1}, {"en": -1}, {"en": float("inf")}, {"en": 0, "fr": 0}, {"en": 1, "EN": 2}],
)
def test_invalid_weights_are_rejected(bilingual, weights):
    with pytest.raises(ValueError, match="language_weights"):
        replace(
            bilingual, generation=replace(bilingual.generation, language_weights=weights)
        ).validate()


def test_cli_collects_language_weights():
    args = parser().parse_args(
        ["generate", "--language", "ckb", "--language", "kmr", "--language-weight", "ckb=70"]
    )
    assert args.language_weights == {"ckb": 70.0}
    with pytest.raises(SystemExit):
        parser().parse_args(
            ["generate", "--language-weight", "ckb=1", "--language-weight", "ckb=2"]
        )
