import csv
import hashlib
import io
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from fontTools.ttLib import TTCollection, TTFont
from PIL import Image, ImageChops

from synthocr.cli import main
from synthocr.config import Generation, load_config
from synthocr.downloads import sha256
from synthocr.fonts import Coverage, Font, _download, check, discover, load_characters
from synthocr.pipeline import _bounded_map, export, generate
from synthocr.render import Renderer, direction_for
from synthocr.text import GENRES, GRAPHEME, chunks, generate_text, normalize, prepare

ROOT = Path(__file__).resolve().parents[1]
ARABIC = ROOT / "fonts/open/notonaskharabic/NotoNaskhArabic[wght].ttf"
LATIN = ROOT / "fonts/open/notosans/NotoSans[wdth,wght].ttf"


def read_manifest(directory):
    return [json.loads(line) for line in (directory / "manifest.jsonl").read_text().splitlines()]


def test_prepare_preserves_raw_numbers_joiners_and_metadata(config):
    raw = config.root / "raw/sample.jsonl"
    original = raw.read_bytes()
    (config.root / "raw/more.txt").write_text("Hello world\n123.45\nمی\u200cروم\n\n")
    stats = prepare(config)
    assert raw.read_bytes() == original
    records = [
        json.loads(line)
        for line in (config.root / "processed/texts.jsonl").read_text().splitlines()
    ]
    assert stats["records"] == len(records) == 7
    assert stats["duplicates"] == 0
    assert {row["language"] for row in records if row["text"] == "Hello world"} == {"", "en"}
    assert {"123.45", "می\u200cروم"} <= {row["text"] for row in records}
    with sqlite3.connect(config.root / "processed/texts.sqlite3") as db:
        assert [row[0] for row in db.execute("SELECT id FROM texts ORDER BY id")] == list(
            range(1, 8)
        )
    assert prepare(config)["cached"] is True
    with raw.open("a") as stream:
        stream.write('{"text":"new"}\n')
    assert prepare(config)["records"] == 8


def test_prepare_csv_and_failure_keeps_previous_processed_data(config):
    source = config.root / "raw/input.csv"
    source.write_text('text,genre,language\n"A, B",receipt,en\n')
    assert prepare(config)["records"] == 5
    database = (config.root / "processed/texts.sqlite3").read_bytes()
    source.write_text("wrong_column\nhello\n")
    with pytest.raises(ValueError, match="text column"):
        prepare(config)
    assert database == (config.root / "processed/texts.sqlite3").read_bytes()


@pytest.mark.parametrize(
    "text", ["e\u0301" * 7, "کتاب\u200cخانه " * 4, "👩\u200d🔬" * 5, "नमस्ते दुनिया", "x" * 13]
)
def test_chunks_are_grapheme_safe(text):
    result = list(chunks(text, 3))
    assert all(len(GRAPHEME.findall(chunk)) <= 3 for chunk in result)
    assert "".join(result).replace(" ", "") == text.replace(" ", "")


def test_normalization_preserves_shaping_controls():
    text = "\ufeff e\u0301\tمی\u200cروم\u200f 👩\u200d🔬\x00 "
    assert normalize(text) == "é می\u200cروم\u200f 👩\u200d🔬"
    assert normalize("e\u0301", "none") == "e\u0301"


@pytest.mark.parametrize(
    "changes",
    [
        {"samples": 0},
        {"workers": 0},
        {"workers": 17},
        {"seed": True},
        {"font_size": 2048},
        {"max_pixels": 20_000_000},
        {"padding": -1},
        {"augmentation": float("nan")},
        {"train_ratio": 0.99, "validation_ratio": 0.1},
        {"direction": "wrong"},
        {"format": "exe"},
        {"output": "raw/images"},
    ],
)
def test_invalid_configuration(config, changes):
    with pytest.raises(ValueError):
        replace(config, generation=replace(config.generation, **changes)).validate()


def test_config_paths_and_unknown_settings(tmp_path, monkeypatch):
    file = tmp_path / "config.toml"
    file.write_text('[data]\nraw="raw"\n')
    monkeypatch.chdir(ROOT)
    assert load_config(file).path("raw") == tmp_path / "raw"
    file.write_text("[generation]\nsampels=10\n")
    with pytest.raises(ValueError, match="sampels"):
        load_config(file)


def test_bounded_source_lines(config):
    smaller = replace(config, data=replace(config.data, max_line_chars=4))
    with pytest.raises(ValueError, match="max_line_chars"):
        prepare(smaller)


def test_coverage_and_original_checker_schema(tmp_path):
    coverage = Coverage(1)
    latin, arabic = Font(str(LATIN)), Font(str(ARABIC))
    assert coverage.choose([latin, arabic], "سڵاو", 0) == arabic
    assert coverage.choose([latin], "سڵاو", 0) is None
    assert coverage.choose([latin], "Hello\u200c", 0) == latin
    coverage.codepoints(latin)
    assert len(coverage.cache) == 1
    file = tmp_path / "chars.json"
    file.write_text('{"0":{"char":"A"},"1":{"char":"س"}}')
    result = check([latin], load_characters(file))
    assert result["results"][0]["missing"] == ["U+0633"]


def test_discover_recurses_deduplicates_and_handles_collection(tmp_path):
    subdir = tmp_path / "nested"
    subdir.mkdir()
    (tmp_path / "first.TTF").write_bytes(ARABIC.read_bytes())
    (subdir / "duplicate.ttf").write_bytes(ARABIC.read_bytes())
    (subdir / "broken.otf").write_bytes(b"broken")
    (subdir / "unsupported.woff").write_bytes(b"not a rendering font")
    assert len(discover([tmp_path])) == 1
    collection = TTCollection()
    with TTFont(ARABIC) as first, TTFont(LATIN) as second:
        collection.fonts = [first, second]
        collection.save(tmp_path / "pair.ttc")
    fonts = discover([tmp_path])
    assert len(fonts) == 3
    assert {f.index for f in fonts if f.path.endswith(".ttc")} == {0, 1}


@pytest.mark.parametrize(
    "text,expected",
    [("سڵاو 2026", "rtl"), ("Hello سڵاو", "ltr"), ("123.45", "ltr"), ("שלום", "rtl")],
)
def test_auto_direction(text, expected):
    assert direction_for(text, "auto") == expected


def test_render_is_deterministic_and_bounds_include_negative_bearings():
    renderer = Renderer(Generation(augmentation=1, max_width=400, max_height=180), 2)
    face = Font(str(ARABIC))
    a, info = renderer.render("سڵاو جیهان " * 6, face, 3, "ckb")
    b, second = renderer.render("سڵاو جیهان " * 6, face, 3, "ckb")
    assert info == second
    assert ImageChops.difference(a, b).getbbox() is None
    assert a.width <= 400 and a.height <= 180
    x0, y0, x1, y1 = info["bbox"]
    assert 0 <= x0 < x1 <= a.width and 0 <= y0 < y1 <= a.height
    for size in (20, 30, 40):
        renderer.font(face, size)
    assert len(renderer.cache) == 2
    a.close()
    b.close()


@pytest.mark.parametrize("format", ["png", "jpg", "webp", "tif"])
def test_generation_formats_and_exports(config, format):
    config = replace(config, generation=replace(config.generation, format=format))
    result = generate(config)
    output = config.root / "output"
    records = read_manifest(output)
    assert result["generated"] == len(records) == 4
    for record in records:
        with Image.open(output / record["image"]) as image:
            assert image.size == (record["width"], record["height"])
        assert record["direction"] in {"rtl", "ltr"}
    with pytest.raises(ValueError, match="must be empty"):
        generate(config)
    for kind in ("csv", "imagefolder", "tesseract"):
        destination = config.root / kind
        assert export(output, destination, kind)["records"] == 4
        if kind == "csv":
            with (destination / "labels.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            assert [row["text"] for row in rows] == [row["text"] for row in records]
        elif kind == "tesseract":
            for record in records:
                assert (destination / f"{record['id']}.gt.txt").read_text() == record["text"]
        else:
            metadata = [
                json.loads(line)
                for line in (destination / "metadata.jsonl").read_text().splitlines()
            ]
            assert len(metadata) == 4
            assert all((destination / row["file_name"]).is_file() for row in metadata)


def test_workers_reproduce_serial_output(config):
    generate(config)
    parallel = replace(config, generation=replace(config.generation, output="parallel", workers=2))
    generate(parallel)
    assert read_manifest(config.root / "output") == read_manifest(config.root / "parallel")
    for record in read_manifest(config.root / "output"):
        a = (config.root / "output" / record["image"]).read_bytes()
        b = (config.root / "parallel" / record["image"]).read_bytes()
        assert a == b


def test_font_variants_have_no_duplicate_pairs_or_split_leakage(config):
    config = replace(config, generation=replace(config.generation, samples=50))
    generate(config)
    records = read_manifest(config.root / "output")
    pairs = {(r["text_id"], r["font"], r["face_index"]) for r in records}
    assert len(pairs) == 50
    for text in {r["text"] for r in records}:
        assert len({r["split"] for r in records if r["text"] == text}) == 1


def test_insufficient_supported_data_fails_honestly(config):
    (config.root / "raw/sample.jsonl").write_text('{"text":"\U00013000"}\n')
    with pytest.raises(ValueError, match="Only 0/4"):
        generate(config)
    stats = json.loads((config.root / "output/stats.json").read_text())
    assert stats["status"] == "failed"
    assert stats["generated"] == 0
    assert stats["unsupported_texts"] == 1
    with pytest.raises(ValueError, match="incomplete"):
        export(config.root / "output", config.root / "export", "csv")


def test_download_checksum_size_and_path_security(tmp_path, monkeypatch):
    monkeypatch.setattr("synthocr.fonts.urlopen", lambda *a, **k: io.BytesIO(b"font data"))
    digest = hashlib.sha256(b"font data").hexdigest()
    _download("https://raw.githubusercontent.com/test/font.ttf", digest, tmp_path / "font.ttf", 100)
    assert (tmp_path / "font.ttf").read_bytes() == b"font data"
    if os.name != "nt":
        # Readable by other users, e.g. the container's non-root account.
        assert (tmp_path / "font.ttf").stat().st_mode & 0o044 == 0o044
    with pytest.raises(ValueError, match="SHA-256"):
        _download(
            "https://raw.githubusercontent.com/test/font.ttf", "bad", tmp_path / "bad.ttf", 100
        )
    with pytest.raises(ValueError, match="exceeds"):
        _download(
            "https://raw.githubusercontent.com/test/font.ttf", digest, tmp_path / "big.ttf", 1
        )
    with pytest.raises(ValueError, match="HTTPS"):
        _download("http://example.org/font.ttf", digest, tmp_path / "evil.ttf", 100)
    assert list(tmp_path.iterdir()) == [tmp_path / "font.ttf"]


def test_export_rejects_manifest_traversal(config):
    generate(config)
    manifest = config.root / "output/manifest.jsonl"
    records = read_manifest(config.root / "output")
    records[0]["image"] = "../raw/sample.jsonl"
    manifest.write_text(json.dumps(records[0]) + "\n")
    stats_path = config.root / "output/stats.json"
    stats = json.loads(stats_path.read_text())
    stats["manifest_sha256"] = sha256(manifest)
    stats_path.write_text(json.dumps(stats))
    with pytest.raises(ValueError, match="within the dataset"):
        export(config.root / "output", config.root / "export", "csv")


def test_genre_templates_are_reproducible_and_do_not_overwrite(tmp_path):
    for name in ("first", "second"):
        generate_text(tmp_path / name, 100, 42, "mixed", "ckb")
    assert (tmp_path / "first").read_bytes() == (tmp_path / "second").read_bytes()
    records = [json.loads(line) for line in (tmp_path / "first").read_text().splitlines()]
    assert {row["genre"] for row in records} == set(GENRES)
    with pytest.raises(FileExistsError):
        generate_text(tmp_path / "first", 1, 42, "mixed", "ckb")


def test_cli_errors_have_nonzero_exit_code(config, capsys):
    assert main(["--config", str(config.root / "missing.toml"), "prepare"]) == 1
    assert main(["--config", str(ROOT / "config.toml"), "generate", "--samples", "0"]) == 1


def test_bounded_map_keeps_order_and_limits_pending_tasks():
    from concurrent.futures import ThreadPoolExecutor

    consumed = 0

    def items():
        nonlocal consumed
        for item in range(20):
            consumed += 1
            yield item

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = _bounded_map(executor, lambda value: value * value, items(), 3)
        assert next(results) == 0
        assert consumed == 4
        assert list(results) == [value * value for value in range(1, 20)]
