import bz2
import errno
import io
import json
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

# Shared filesystem fixtures describe the same tiny real multilingual corpus.
from test_pipeline import ARABIC, LATIN, read_manifest

from synthocr.config import Generation
from synthocr.corpora import _convert, snapshot
from synthocr.downloads import download, sha256, within
from synthocr.fonts import Coverage, Font, required_codepoints
from synthocr.pipeline import export, generate
from synthocr.render import Renderer
from synthocr.text import GENRES, generate_text, prepare, records
from synthocr.verify import verify


def reseal(dataset):
    path = dataset / "stats.json"
    value = json.loads(path.read_text())
    value["manifest_sha256"] = sha256(dataset / "manifest.jsonl")
    value["run_sha256"] = sha256(dataset / "run.json")
    path.write_text(json.dumps(value))


def mutate(dataset, change):
    rows = read_manifest(dataset)
    change(rows)
    (dataset / "manifest.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    reseal(dataset)


def test_verifier_accepts_all_completed_images(config):
    generate(config)
    result = verify(config.root / "output")
    assert result["records"] == 4 and result["images_checked"]
    assert sum(result["splits"].values()) == 4


@pytest.mark.parametrize("filename", ["manifest.jsonl", "run.json"])
def test_verifier_rejects_corrupt_metadata(config, filename):
    generate(config)
    with (config.root / "output" / filename).open("a") as stream:
        stream.write(" ")
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify(config.root / "output")


@pytest.mark.parametrize("kind", ["corrupt", "missing", "extra"])
def test_verifier_rejects_bad_images(config, kind):
    generate(config)
    dataset = config.root / "output"
    image = dataset / read_manifest(dataset)[0]["image"]
    if kind == "missing":
        image.unlink()
    elif kind == "corrupt":
        image.write_bytes(b"corrupt image")
    else:
        (dataset / "images/extra.png").write_bytes(image.read_bytes())
    with pytest.raises(ValueError, match="within|checksum|unlisted"):
        verify(dataset)


@pytest.mark.parametrize(
    "field,value",
    [
        ("width", 0),
        ("width", True),
        ("height", -1),
        ("width", 9999999),
        ("bbox", [0, 0, 999999, 1]),
        ("bbox", [2, 0, 1, 1]),
        ("bbox", [0, 0, 1]),
        ("id", "../escape"),
        ("id", "٠١٢"),
        ("split", "invalid"),
        ("font_id", "missing"),
        ("text", ""),
        ("text", "\ufe0f"),
        ("face_index", 999),
    ],
)
def test_verifier_rejects_invalid_annotations(config, field, value):
    generate(config)
    dataset = config.root / "output"
    mutate(dataset, lambda rows: rows[0].update({field: value}))
    with pytest.raises(ValueError):
        verify(dataset)


@pytest.mark.parametrize("field", ["id", "image"])
def test_verifier_rejects_duplicate_identity(config, field):
    generate(config)
    dataset = config.root / "output"
    mutate(dataset, lambda rows: rows[1].update({field: rows[0][field]}))
    with pytest.raises(ValueError, match="Duplicate"):
        verify(dataset)


def test_verifier_rejects_duplicate_pair(config):
    generate(config)
    dataset = config.root / "output"

    def duplicate(rows):
        for field in (
            "text",
            "text_id",
            "language",
            "font",
            "font_id",
            "face_index",
            "split",
            "source",
            "source_line",
            "genre",
            "attribution",
        ):
            rows[1][field] = rows[0][field]

    mutate(dataset, duplicate)
    with pytest.raises(ValueError, match="Duplicate"):
        verify(dataset)


def test_verifier_rejects_split_leakage(config):
    generate(config)
    dataset = config.root / "output"

    def leak(rows):
        for field in (
            "text",
            "text_id",
            "language",
            "source",
            "source_line",
            "genre",
            "attribution",
        ):
            rows[1][field] = rows[0][field]
        rows[1]["split"] = "validation" if rows[0]["split"] == "train" else "train"

    # Disable pair checking to isolate the split invariant even if both selected one font.
    run_path = dataset / "run.json"
    run = json.loads(run_path.read_text())
    run["config"]["data"]["deduplicate"] = False
    run_path.write_text(json.dumps(run))
    mutate(dataset, leak)
    with pytest.raises(ValueError, match="leaks"):
        verify(dataset)


def test_verifier_rejects_truncated_manifest(config):
    generate(config)
    dataset = config.root / "output"
    mutate(dataset, lambda rows: rows.pop())
    with pytest.raises(ValueError, match="count mismatch"):
        verify(dataset)


@pytest.mark.parametrize("kind", ["csv", "imagefolder", "tesseract"])
@pytest.mark.parametrize("existing", [False, True])
def test_failed_export_is_not_published(config, kind, existing):
    generate(config)
    dataset = config.root / "output"
    (dataset / read_manifest(dataset)[-1]["image"]).write_bytes(b"broken")
    destination = config.root / "export"
    if existing:
        destination.mkdir()
    with pytest.raises(ValueError, match="checksum"):
        export(dataset, destination, kind)
    assert not destination.exists() or not list(destination.iterdir())
    assert not list(config.root.glob(".export-*"))


def test_export_copy_fallback_is_exclusive(config, monkeypatch):
    generate(config)

    def no_links(*args):
        raise OSError(errno.EXDEV, "different filesystems")

    monkeypatch.setattr("synthocr.pipeline.os.link", no_links)
    result = export(config.root / "output", config.root / "copied", "imagefolder")
    assert result["records"] == 4
    for row in read_manifest(config.root / "output"):
        assert (config.root / "copied" / Path(row["image"]).name).read_bytes() == (
            config.root / "output" / row["image"]
        ).read_bytes()


@pytest.mark.parametrize("kind", ["csv", "imagefolder", "tesseract"])
def test_attribution_survives_preparation_generation_and_export(config, kind):
    raw = config.root / "raw/sample.jsonl"
    attribution = {
        "source": "Tatoeba",
        "source_id": "123",
        "author": "ExampleAuthor",
        "url": "https://tatoeba.org/en/sentences/show/123",
        "license": "CC-BY-2.0-FR",
        "modified": False,
    }
    raw.write_text(json.dumps({"text": "Hello   world", "attribution": attribution}))
    config = replace(config, generation=replace(config.generation, samples=1))
    generate(config)
    row = read_manifest(config.root / "output")[0]
    assert row["attribution"]["author"] == "ExampleAuthor"
    assert row["attribution"]["modified"] is True
    export(config.root / "output", config.root / "export", kind)
    filename = {
        "csv": "labels.csv",
        "imagefolder": "metadata.jsonl",
        "tesseract": "attribution.jsonl",
    }[kind]
    result = (config.root / "export" / filename).read_text()
    assert "ExampleAuthor" in result and "CC-BY-2.0-FR" in result


@pytest.mark.parametrize("filename", ["texts.sqlite3", "texts.jsonl", "stats.json"])
def test_prepare_rebuilds_corrupt_derived_data(config, filename):
    expected = prepare(config)
    (config.root / "processed" / filename).write_bytes(b"corrupt")
    actual = prepare(config)
    assert actual["records"] == expected["records"]
    assert not actual.get("cached")
    assert prepare(config)["cached"]


def test_source_change_during_prepare_preserves_previous_outputs(config, monkeypatch):
    prepare(config)
    original = sha256(config.root / "processed/texts.sqlite3")
    from synthocr import text

    iterator = text.records

    def changed(path, raw, limit):
        yield from iterator(path, raw, limit)
        with path.open("a") as stream:
            stream.write('\n{"text":"new row"}\n')

    monkeypatch.setattr(text, "records", changed)
    with pytest.raises(ValueError, match="changed during preparation"):
        prepare(config, force=True)
    assert sha256(config.root / "processed/texts.sqlite3") == original


def test_multiline_csv_reports_physical_start_line(tmp_path):
    file = tmp_path / "rows.csv"
    file.write_text('text,language\n"First\nsecond",en\nThird,en\n')
    result = list(records(file, tmp_path, 100))
    assert [line for line, _ in result] == [2, 4]


def test_visible_arabic_format_signs_are_checked():
    assert required_codepoints("\u0600\u06dd\u200c\u200d\ufe0f") == {0x600, 0x6DD}
    assert Coverage().choose([Font(str(LATIN))], "\u0600", 0) is None


def test_coverage_caches_use_compact_unicode_bitmaps():
    import sys

    coverage = Coverage(2)
    font = Font(str(LATIN))
    assert not coverage.missing(font, frozenset(map(ord, "Hello")))
    cjk = Font(str(LATIN.parents[1] / "notosansjp/NotoSansJP[wght].ttf"))
    assert not coverage.missing(cjk, frozenset(map(ord, "図書館")))
    # The old large set representation used megabytes for CJK coverage alone.
    assert sum(sys.getsizeof(value) for value in coverage.cache.values()) < 300000


def test_font_identity_survives_matching_external_basenames(config):
    with tempfile.TemporaryDirectory() as directory:
        parent = Path(directory)
        for name, source in (("first", ARABIC), ("second", LATIN)):
            (parent / name).mkdir()
            (parent / name / "font.ttf").write_bytes(source.read_bytes())
        (config.root / "raw/sample.jsonl").write_text('{"text":"123456"}')
        config = replace(
            config,
            fonts=replace(config.fonts, directories=[str(parent)]),
            generation=replace(config.generation, samples=2),
        )
        generate(config)
        rows = read_manifest(config.root / "output")
        assert len({row["font_id"] for row in rows}) == 2
        assert verify(config.root / "output")["records"] == 2


def test_no_silent_fallback_when_shaping_is_unavailable(monkeypatch):
    monkeypatch.setattr("synthocr.render.features.check_feature", lambda feature: False)
    with pytest.raises(ValueError, match="libraqm"):
        Renderer(Generation())


@pytest.mark.parametrize(
    "url",
    [
        "http://raw.githubusercontent.com/x",
        "https://example.org/x",
        "https://user:password@raw.githubusercontent.com/x",
    ],
)
def test_download_rejects_untrusted_origins(tmp_path, url):
    with pytest.raises(ValueError, match="HTTPS"):
        download(url, "anything", tmp_path / "file", 10, {"raw.githubusercontent.com"})


def test_download_rejects_redirect_escape_and_existing_oversize(tmp_path):
    class Redirect(io.BytesIO):
        def geturl(self):
            return "http://example.org/unsafe"

    with pytest.raises(ValueError, match="redirected"):
        download(
            "https://raw.githubusercontent.com/x",
            None,
            tmp_path / "file",
            10,
            {"raw.githubusercontent.com"},
            opener=lambda *a, **k: Redirect(b"abc"),
        )
    (tmp_path / "file").write_bytes(b"too long")
    with pytest.raises(ValueError, match="exceeds"):
        download(
            "https://raw.githubusercontent.com/x",
            None,
            tmp_path / "file",
            1,
            {"raw.githubusercontent.com"},
        )


def test_catalog_rejects_symlink_escape(tmp_path):
    root = tmp_path / "fonts"
    root.mkdir()
    (root / "outside").symlink_to(tmp_path, target_is_directory=True)
    for name in ("../escape", str(tmp_path / "escape"), "outside/escape"):
        with pytest.raises(ValueError, match="inside"):
            within(root, name)


def test_tatoeba_preserves_text_and_attributes_only_known_authors(tmp_path):
    archive = tmp_path / "data.bz2"
    archive.write_bytes(
        bz2.compress(
            "1\tckb\tسڵاو جیهان\tExampleUser\t2020\t2021\n"
            "2\tckb\tunattributed\t\\N\t2020\t2021\n".encode()
        )
    )
    output = tmp_path / "raw.jsonl"
    result = _convert(archive, output, "ckb", 100)
    assert result["records"] == 1 and result["skipped_unattributed"] == 1
    row = json.loads(output.read_text())
    assert row["text"] == "سڵاو جیهان" and row["attribution"]["author"] == "ExampleUser"
    assert row["attribution"]["modified"] is False
    with pytest.raises(FileExistsError):
        _convert(archive, output, "ckb", 100)


def test_snapshot_is_cached_and_never_overwrites_raw(tmp_path, monkeypatch):
    def fake_download(url, digest, target, *args):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bz2.compress(b"1\teng\tHello\tAuthor\t2020\t2021\n"))
        return sha256(target)

    monkeypatch.setattr("synthocr.corpora.download", fake_download)
    args = (tmp_path / "raw", tmp_path / "catalog.json", tmp_path / "cache", ["eng"], "2026-10-24")
    assert snapshot(*args)["records"] == 1
    assert snapshot(*args)["records"] == 1
    target = tmp_path / "raw/tatoeba/2026-10-24/eng.jsonl"
    target.write_text("changed raw data")
    with pytest.raises(ValueError, match="will not be overwritten"):
        snapshot(*args)
    assert target.read_text() == "changed raw data"


@pytest.mark.parametrize("language", ["en", "ckb", "ar", "fa", "de", "fr", "es", "tr", "mixed"])
def test_new_templates_cover_genres_and_keep_synthetic_provenance(tmp_path, language):
    output = tmp_path / "generated.jsonl"
    generate_text(output, 1000, 42, "mixed", language)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert {row["genre"] for row in rows} == set(GENRES)
    assert all(row["attribution"]["synthetic"] for row in rows)
    if language == "mixed":
        assert len({row["language"] for row in rows}) == 8
