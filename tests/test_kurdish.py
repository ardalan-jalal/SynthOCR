import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_integrity import mutate
from test_pipeline import ARABIC, read_manifest

from synthocr.cli import main
from synthocr.fonts import Coverage, Font, load_profile
from synthocr.pipeline import generate
from synthocr.render import Renderer
from synthocr.spelling import spelling_cases
from synthocr.text import normalize, prepare, required_codepoints
from synthocr.verify import verify

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def kurdish_config(config):
    (config.root / "raw/kurdish.jsonl").write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=False)
            for row in [
                {"text": "Ez bi kurdî dinivîsim.", "language": "kmr"},
                {"text": "سڵاو ⁦ABC-123⁩ جیهان", "language": "ckb"},
                {"text": "ک ك ی ي ى ه ھ ە ه‌", "language": "ckb"},
                {"text": "٠١٢٣ ۰۱۲۳ 0123", "language": "ckb", "genre": "number"},
                {"text": "سئم | سئ", "language": "ckb", "genre": "spelling"},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    path = config.root / "kurdish.toml"
    path.write_text(
        '[data]\nraw="raw"\nprocessed="processed"\nnormalization="NFKC"\n'
        f'[fonts]\ndirectories=[{json.dumps(str(ROOT / "fonts/open"))}]\ncoverage="off"\n'
        '[generation]\nsamples=4\ndirection="ltr"\nlanguage="ar"\nlanguages=["en"]\n'
        'suite="kurdish-spelling"\n',
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "language,expected", [(None, {"ckb"}), ("kurmanji", {"kmr"}), ("both", {"ckb", "kmr"})]
)
def test_simple_kurdish_command_selects_language_and_verifies(
    config, kurdish_config, capsys, language, expected
):
    args = ["--config", str(kurdish_config), "kurdish", "--output", "easy"]
    if language:
        args.extend(["--language", language])
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["generated"] == result["verification"]["records"] == 4
    assert result["verification"]["status"] == "verified"
    assert set(result["verification"]["languages"]) == expected
    rows = read_manifest(config.root / "easy")
    assert all(row["genre"] != "spelling" for row in rows)
    assert all(row["language"] == row["shaping_language"] for row in rows)
    run = json.loads((config.root / "easy/run.json").read_text())
    assert run["config"]["fonts"]["coverage"] == "strict"
    assert run["config"]["data"]["normalization"] == "NFC"
    assert run["config"]["generation"]["suite"] == ""
    if expected == {"ckb"}:
        assert {row["text"] for row in rows} == {
            "سڵاو جیهان",
            "سڵاو ⁦ABC-123⁩ جیهان",
            "ک ك ی ي ى ه ھ ە ه‌",
            "٠١٢٣ ۰۱۲۳ 0123",
        }
        assert all(row["direction"] == "rtl" for row in rows if row["genre"] != "number")


def test_simple_kurdish_command_chooses_fresh_output_and_keeps_raw(config, kurdish_config, capsys):
    original = {path: path.read_bytes() for path in (config.root / "raw").iterdir()}
    outputs = []
    for _ in range(2):
        assert main(["--config", str(kurdish_config), "kurdish"]) == 0
        result = json.loads(capsys.readouterr().out)
        outputs.append(result["output"])
        assert Path(result["output"]).parent == config.root / "dataset"
    assert outputs[0] != outputs[1]
    assert all(path.read_bytes() == data for path, data in original.items())


@pytest.mark.parametrize("language,count", [("sorani", 176), ("kurmanji", 98), ("both", 274)])
def test_simple_spelling_check_covers_all_cases_and_keeps_contexts_whole(
    config, kurdish_config, capsys, language, count
):
    # The check should not accidentally split contexts or depend on training input.
    with kurdish_config.open("a") as stream:
        stream.write('exclude_genres=["spelling"]\n')
    source = kurdish_config.read_text().replace('normalization="NFKC"', "max_chars=2")
    kurdish_config.write_text(source)
    assert (
        main(["--config", str(kurdish_config), "kurdish", "--check", "--language", language]) == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["spelling_cases_checked"] == result["verification"]["records"] == count
    rows = read_manifest(Path(result["output"]))
    assert len({row["attribution"]["source_id"] for row in rows}) == count
    assert all(row["genre"] == "spelling" for row in rows)


def test_simple_spelling_check_refuses_a_partial_check(config, kurdish_config):
    output = config.root / "too-small"
    assert (
        main(
            [
                "--config",
                str(kurdish_config),
                "kurdish",
                "--check",
                "--samples",
                "10",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert not output.exists()


def test_simple_spelling_check_fails_if_a_required_case_is_missing(
    kurdish_config, capsys, monkeypatch
):
    def cases_with_missing_case():
        yield from spelling_cases()
        yield {
            "text": "سڵاو",
            "language": "ckb",
            "attribution": {"source_id": "required-case-absent-from-generated-input"},
        }

    monkeypatch.setattr("synthocr.cli.spelling_cases", cases_with_missing_case)
    assert main(["--config", str(kurdish_config), "kurdish", "--check"]) == 1
    assert capsys.readouterr().out == ""


def test_simple_kurdish_command_refuses_existing_output(config, kurdish_config, capsys):
    args = ["--config", str(kurdish_config), "kurdish", "--output", "existing"]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    manifest = Path(result["output"]) / "manifest.jsonl"
    before = manifest.read_bytes()
    assert main(args) == 1
    assert manifest.read_bytes() == before


def test_simple_kurdish_command_returns_failure_when_verification_fails(
    config, kurdish_config, monkeypatch
):
    def fail_verification(*args, **kwargs):
        raise ValueError("invalid image")

    monkeypatch.setattr("synthocr.cli.verify", fail_verification)
    assert main(["--config", str(kurdish_config), "kurdish", "--output", "invalid"]) == 1


@pytest.mark.parametrize("genres", ["spelling", [""], [1], ["prose"] * 101])
def test_genre_exclusions_are_validated(config, genres):
    with pytest.raises(ValueError, match="exclude_genres"):
        replace(config, generation=replace(config.generation, exclude_genres=genres)).validate()


def test_genre_filter_works_without_language_filter_and_reproduces_workers(config):
    selected = replace(config, generation=replace(config.generation, exclude_genres=["prose"]))
    generate(selected)
    rows = read_manifest(config.root / "output")
    assert all(row["genre"] != "prose" for row in rows)
    parallel = replace(
        selected, generation=replace(selected.generation, output="parallel", workers=2)
    )
    generate(parallel)
    assert read_manifest(config.root / "parallel") == rows


def test_verifier_rejects_excluded_genres_even_after_checksums_are_updated(config):
    selected = replace(config, generation=replace(config.generation, exclude_genres=["spelling"]))
    generate(selected)
    mutate(config.root / "output", lambda rows: rows[0].update(genre="spelling"))
    with pytest.raises(ValueError, match="excluded genre"):
        verify(config.root / "output")


def test_source_language_metadata_is_explicit_preserves_raw_and_invalidates_cache(config):
    source = config.root / "raw/news.txt"
    original = "ڕۆژێکی نوێ لە هەولێر\n".encode()
    source.write_bytes(original)
    prepare(config)
    annotated = replace(
        config,
        data=replace(config.data, source_languages={"news.txt": "ckb", "sample.jsonl": "ar"}),
    )
    assert not prepare(annotated).get("cached")
    rows = [
        json.loads(line)
        for line in (config.root / "processed/texts.jsonl").read_text().splitlines()
    ]
    assert next(row for row in rows if row["source"] == "news.txt")["language"] == "ckb"
    assert next(row for row in rows if row["text"] == "Hello world")["language"] == "en"
    assert next(row for row in rows if row["text"] == "١٢٣٤ 5678")["language"] == "ar"
    assert source.read_bytes() == original
    assert prepare(annotated)["cached"]


def test_missing_source_metadata_is_rejected_instead_of_silently_ignored(config):
    broken = replace(config, data=replace(config.data, source_languages={"typo.txt": "ckb"}))
    with pytest.raises(ValueError, match="missing raw sources"):
        prepare(broken)


@pytest.mark.parametrize(
    "mapping",
    [[], {"../news.txt": "ckb"}, {"/news.txt": "ckb"}, {"news.txt": 1}, {"news.txt": ""}],
)
def test_invalid_source_language_metadata_is_rejected(config, mapping):
    with pytest.raises(ValueError, match="source_languages"):
        replace(config, data=replace(config.data, source_languages=mapping)).validate()


@pytest.mark.parametrize("languages", ["ckb", [""], ["ckb' OR 1=1"], [None], ["ckb"] * 101])
def test_invalid_language_selection_is_rejected(config, languages):
    with pytest.raises(ValueError, match="languages must"):
        replace(config, generation=replace(config.generation, languages=languages)).validate()


def test_sorani_selection_keeps_original_ids_and_source_language_with_shaping_override(config):
    sorani = replace(
        config, generation=replace(config.generation, languages=["CKB"], language="ar")
    )
    generate(sorani)
    rows = read_manifest(config.root / "output")
    assert {row["text_id"] for row in rows} == {2}
    assert {row["language"] for row in rows} == {"ckb"}
    assert {row["shaping_language"] for row in rows} == {"ar"}
    assert verify(config.root / "output")["languages"] == {"ckb": 4}


def test_multilanguage_selection_is_reproducible_across_worker_counts(config):
    selected = replace(config, generation=replace(config.generation, languages=["ckb", "en"]))
    generate(selected)
    parallel = replace(
        selected, generation=replace(selected.generation, workers=2, output="parallel")
    )
    generate(parallel)
    rows = read_manifest(config.root / "output")
    assert rows == read_manifest(config.root / "parallel")
    assert {row["text_id"] for row in rows} == {1, 2, 4}
    assert {row["language"] for row in rows} == {"ckb", "en"}


def test_empty_language_selection_fails_honestly(config):
    missing = replace(config, generation=replace(config.generation, languages=["kmr"]))
    with pytest.raises(ValueError, match="No processed records match"):
        generate(missing)
    stats = json.loads((config.root / "output/stats.json").read_text())
    assert stats["status"] == "failed" and stats["generated"] == 0
    with pytest.raises(ValueError, match="incomplete"):
        verify(config.root / "output")


def test_verifier_rejects_wrong_language_even_with_updated_checksums(config):
    selected = replace(config, generation=replace(config.generation, languages=["ckb"]))
    generate(selected)
    mutate(config.root / "output", lambda rows: rows[0].update(language="en"))
    with pytest.raises(ValueError, match="requested languages"):
        verify(config.root / "output")


def test_cli_kurdish_language_selection(config, capsys):
    path = config.root / "config.toml"
    path.write_text(
        f'[fonts]\ndirectories=[{json.dumps(str(ROOT / "fonts"))}]\n[data]\nraw="raw"\n'
    )
    assert (
        main(
            [
                "--config",
                str(path),
                "generate",
                "--language",
                "ckb",
                "--samples",
                "3",
                "--output",
                "cli",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["generated"] == 3
    assert verify(config.root / "cli")["languages"] == {"ckb": 3}


def test_sorani_and_kurmanji_profiles_cover_distinct_letters_and_digit_sets():
    sorani, kurmanji = load_profile("sorani"), load_profile("kurmanji")
    assert set("ڕڵێۆەڤگک0123456789٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹") <= set(sorani)
    assert set("çêîşûÇÊÎŞÛ") <= set(kurmanji)
    assert "ڕ" not in kurmanji
    assert not Coverage().missing(Font(str(ARABIC)), required_codepoints(sorani))
    latin = Font(str(ROOT / "fonts/open/notosans/NotoSans[wdth,wght].ttf"))
    assert not Coverage().missing(latin, required_codepoints(kurmanji))


def test_font_audit_can_require_one_suitable_font_instead_of_every_font(capsys):
    assert main(["fonts", "check", "--profile", "sorani", "--require-any"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert 0 < result["fully_supported"] < result["fonts"]


def test_font_audit_fails_when_no_face_covers_sorani(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text(f"[fonts]\ndirectories=[{json.dumps(str(ROOT / 'fonts/open/notosans'))}]\n")
    assert (
        main(["--config", str(path), "fonts", "check", "--profile", "sorani", "--require-any"]) == 1
    )
    assert json.loads(capsys.readouterr().out)["fully_supported"] == 0


@pytest.mark.parametrize(
    "text", ["ڕۆژێکی نوێ، گوڵ و گوێ", "سڵاو\u200cجیهان", "ژمارە ١٢٣ ۱۲۳ 123", "سڵاوَ کوردستان"]
)
def test_sorani_joiners_marks_and_three_digit_sets_render_without_rewriting_labels(config, text):
    source = normalize(text)
    assert source == text
    face = Font(str(ARABIC))
    assert not Coverage().missing(face, required_codepoints(source))
    image, info = Renderer(config.generation, 2).render(source, face, 17, "ckb")
    try:
        assert info["direction"] == "rtl"
        assert info["language"] == "ckb"
        assert image.getextrema()[0] < 200
    finally:
        image.close()
