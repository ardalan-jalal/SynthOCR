import json
import math
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from synthocr.downloads import sha256
from synthocr.experiments import MAX_CASES, PRESETS, SHEET_COLUMNS, SHEET_ROWS, run_experiment
from synthocr.spelling import spelling_cases


def test_experiment_preserves_cases_fonts_and_decodable_evidence(config, tmp_path):
    output = tmp_path / "comparison"
    result = run_experiment(config, output, samples=4, seed=42)
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    catalog = {case["attribution"]["source_id"]: case for case in spelling_cases()}
    assert result["status"] == report["status"] == "complete"
    assert report["images"] == len(rows) == 4 * len(PRESETS)
    assert {row["language"] for row in rows} == {"ckb", "kmr"}
    assert report["manifest_sha256"] == sha256(output / "manifest.jsonl")
    assert report["config_sha256"] == sha256(output / "config.json")
    assert report["cases_sha256"] == sha256(output / "cases.jsonl")
    assert report["contact_sheet_sha256"] == sha256(output / "contact-sheet.png")
    assert report["fonts"]
    assert all(stats["processing_seconds"] >= 0 for stats in report["presets"].values())
    assert all(font["license_file"] == "OFL.txt" for font in report["fonts"].values())
    for case_index in range(4):
        variants = [row for row in rows if row["case_index"] == case_index]
        assert {row["preset"] for row in variants} == {name for name, _ in PRESETS}
        assert len({row["seed"] for row in variants}) == 1
        assert len({row["font_id"] for row in variants}) == 1
        assert len({row["image_sha256"] for row in variants}) >= 4
    for row in rows:
        source = catalog[row["case_id"]]
        assert row["text"] == source["text"]
        assert row["attribution"] == source["attribution"]
        assert row["font_id"] in report["fonts"]
        assert row["image_sha256"] == sha256(output / row["image"])
        with Image.open(output / row["image"]) as image:
            image.load()
            assert image.size == (row["width"], row["height"])
            assert image.width <= config.generation.max_width
            assert image.height <= config.generation.max_height
            assert image.width * image.height <= config.generation.max_pixels
        if row["preset"] != "clean":
            assert row["metrics"]["ink_margin_px"] > 0
        assert row["base_color_contrast"] >= 3
    assert sum(page["cases"] for page in report["contact_sheets"]) == 4
    assert report["contact_sheets"][0]["file"] == "contact-sheet.png"
    assert report["contact_sheets"][0]["sha256"] == report["contact_sheet_sha256"]
    for page in report["contact_sheets"]:
        assert page["sha256"] == sha256(output / page["file"])
        with Image.open(output / page["file"]) as sheet:
            sheet.load()
            assert sheet.size == (page["width"], page["height"])
            assert sheet.width <= SHEET_COLUMNS * 320 + 12
            assert sheet.height <= 54 + SHEET_ROWS * (112 + 24)
    assert not any(Path(value).is_absolute() for value in report["fonts"])


def test_experiment_reproduces_images_and_sources_without_using_raw_corpus(config, tmp_path):
    # Corpus contents and processed caches are deliberately unavailable. This
    # experiment is driven by bundled diagnostic cases, never training text.
    config = replace(config, data=replace(config.data, raw="not-present", processed="absent"))
    run_experiment(config, tmp_path / "first", samples=2, seed=173)
    run_experiment(config, tmp_path / "second", samples=2, seed=173)
    assert (tmp_path / "first/cases.jsonl").read_bytes() == (
        tmp_path / "second/cases.jsonl"
    ).read_bytes()
    assert (tmp_path / "first/manifest.jsonl").read_bytes() == (
        tmp_path / "second/manifest.jsonl"
    ).read_bytes()
    first = json.loads((tmp_path / "first/report.json").read_text(encoding="utf-8"))
    second = json.loads((tmp_path / "second/report.json").read_text(encoding="utf-8"))
    assert first["contact_sheet_sha256"] == second["contact_sheet_sha256"]
    assert first["contact_sheets"] == second["contact_sheets"]


def test_experiment_contact_sheet_remains_bounded_when_more_cases_are_requested(config, tmp_path):
    output = tmp_path / "larger-comparison"
    report = run_experiment(config, output, samples=13, seed=42)
    assert report["images"] == 13 * len(PRESETS)
    assert report["contact_sheet_cases"] == 12
    cases = [
        json.loads(line)
        for line in (output / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    features = {feature for row in cases for feature in row["attribution"]["features"]}
    assert {
        "combining_marks",
        "join_controls",
        "mixed_direction",
        "digit_sets",
        "canonical_accents",
        "capitalization",
    } <= features
    pages = report["contact_sheets"]
    rows_per_case = math.ceil(len(PRESETS) / SHEET_COLUMNS)
    cases_per_page = max(1, SHEET_ROWS // rows_per_case)
    assert len(pages) == math.ceil(12 / cases_per_page)
    assert sum(page["cases"] for page in pages) == 12
    assert [page["first_case_index"] for page in pages] == list(range(0, 12, cases_per_page))
    assert len({page["sha256"] for page in pages}) == len(pages)
    for page in pages:
        with Image.open(output / page["file"]) as sheet:
            sheet.load()
            assert sheet.size == (
                SHEET_COLUMNS * 320 + 12,
                54 + page["cases"] * (24 + rows_per_case * 112),
            )


@pytest.mark.parametrize("samples", [0, -1, MAX_CASES + 1, True, 1.2, "12"])
def test_experiment_rejects_unbounded_or_invalid_sample_counts(config, tmp_path, samples):
    with pytest.raises(ValueError, match="samples"):
        run_experiment(config, tmp_path / "comparison", samples=samples)
    assert not (tmp_path / "comparison").exists()


@pytest.mark.parametrize("seed", [True, 1.2, "42", None])
def test_experiment_rejects_invalid_seed(config, tmp_path, seed):
    with pytest.raises(ValueError, match="seed"):
        run_experiment(config, tmp_path / "comparison", seed=seed)


def test_experiment_refuses_existing_output_without_overwriting_it(config, tmp_path):
    output = tmp_path / "comparison"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("original")
    with pytest.raises(FileExistsError, match="not empty"):
        run_experiment(config, output)
    assert sentinel.read_text() == "original"
    assert sorted(path.name for path in output.iterdir()) == ["keep.txt"]


@pytest.mark.parametrize("relative", ["raw/experiment", "processed/experiment"])
def test_experiment_cannot_write_into_corpus_storage(config, tmp_path, relative):
    with pytest.raises(ValueError, match="separate"):
        run_experiment(config, tmp_path / relative)
    assert not (tmp_path / relative).exists()


def test_experiment_rejects_output_symlink(config, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("This platform does not permit creating symlinks")
    with pytest.raises(ValueError, match="symlink"):
        run_experiment(config, link)
    assert not list(target.iterdir())


def test_experiment_failure_retains_attributed_input_and_incomplete_report(
    config, tmp_path, monkeypatch
):
    from synthocr.experiments import Renderer

    def fail_render(self, text, face, seed, language):
        raise ValueError("Demonstrated render failure")

    monkeypatch.setattr(Renderer, "render", fail_render)
    output = tmp_path / "failed"
    with pytest.raises(ValueError, match="Demonstrated render failure"):
        run_experiment(config, output, samples=2)
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "incomplete"
    assert report["images"] == 0
    assert report["error"] == "Demonstrated render failure"
    sources = [
        json.loads(line)
        for line in (output / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(sources) == 2
    assert all(row["attribution"]["source_id"] for row in sources)
    assert not (output / "contact-sheet.png").exists()
