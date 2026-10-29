"""Temporary export views must respect byte limits before atomic publication."""

import pytest

from synthocr import pipeline
from synthocr.pipeline import export, generate
from synthocr.verify import dataset_files, verify


@pytest.fixture
def dataset(config):
    generate(config)
    return config.path(config.generation.output)


def apparent_bytes(directory):
    return sum(path.stat().st_size for path in dataset_files(directory))


@pytest.mark.parametrize("format", ["csv", "imagefolder", "tesseract"])
def test_exact_export_limit_counts_unicode_labels_images_provenance_and_metadata(dataset, format):
    baseline = dataset.parent / f"baseline-{format}"
    export(dataset, baseline, format)
    size = apparent_bytes(baseline)
    bounded = dataset.parent / f"bounded-{format}"
    assert export(dataset, bounded, format, max_bytes=size)["records"] == 4
    assert apparent_bytes(bounded) == size
    for path in dataset_files(baseline):
        assert path.read_bytes() == (bounded / path.name).read_bytes()
    rejected = dataset.parent / f"rejected-{format}"
    with pytest.raises(ValueError, match="Export exceeds the configured byte limit"):
        export(dataset, rejected, format, max_bytes=size - 1)
    assert not rejected.exists()
    assert not list(dataset.parent.glob(".export-*"))
    assert verify(dataset)["records"] == 4


@pytest.mark.parametrize("include_origins", [False, True])
def test_export_preflights_images_and_origins_before_creating_a_view(
    dataset, monkeypatch, include_origins
):
    minimum = apparent_bytes(dataset / "images")
    if include_origins:
        minimum += (dataset / "origins.jsonl").stat().st_size
    destination = dataset.parent / "not-created" / "export"

    def unexpected_temporary_directory(**kwargs):
        pytest.fail("An oversized export should be rejected before materializing its view")

    monkeypatch.setattr(pipeline.tempfile, "TemporaryDirectory", unexpected_temporary_directory)
    with pytest.raises(ValueError, match="Export exceeds the configured byte limit"):
        export(dataset, destination, "csv", max_bytes=minimum - 1)
    assert not destination.parent.exists()


@pytest.mark.parametrize("format", ["csv", "imagefolder", "tesseract"])
def test_copy_fallback_also_charges_images_against_export_limit(dataset, monkeypatch, format):
    baseline = dataset.parent / f"linked-{format}"
    export(dataset, baseline, format)
    size = apparent_bytes(baseline)

    def no_hardlinks(source, destination):
        raise OSError("Hardlinks unavailable")

    monkeypatch.setattr(pipeline.os, "link", no_hardlinks)
    copied = dataset.parent / f"copied-{format}"
    assert export(dataset, copied, format, max_bytes=size)["records"] == 4
    assert apparent_bytes(copied) == size
    for image in (dataset / "images").iterdir():
        assert image.read_bytes() == (copied / image.name).read_bytes()
        assert image.stat().st_ino != (copied / image.name).stat().st_ino


@pytest.mark.parametrize("limit", [-1, True, 1.5, "100"])
def test_export_rejects_invalid_limits_before_creating_an_output(dataset, limit):
    output = dataset.parent / "bad-limit"
    with pytest.raises(ValueError, match="max_bytes"):
        export(dataset, output, "csv", max_bytes=limit)
    assert not output.exists()


@pytest.mark.parametrize("format", ["csv", "imagefolder", "tesseract"])
def test_studio_export_quota_returns_413_and_keeps_no_temporary_view(config, dataset, format):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from synthocr.web import Studio

    # Studio storage must not be an ancestor of raw/processed inputs. Moving
    # the complete dataset also exercises its existing portability guarantee.
    output_root = dataset.parent / "studio-output"
    output_root.mkdir()
    dataset = dataset.rename(output_root / dataset.name)
    studio = Studio(config, output_root=output_root)
    try:
        studio.cache_limit = 1
        job = {"id": "a" * 32, "name": dataset.name, "status": "completed"}
        with pytest.raises(HTTPException) as error:
            studio.download(job, format)
        assert error.value.status_code == 413
        assert "export exceeds" in error.value.detail.lower()
        assert not list(studio.metadata.glob(".export-*"))
        assert not list(studio._cache_files())
        assert verify(dataset)["records"] == 4
    finally:
        studio.close()
