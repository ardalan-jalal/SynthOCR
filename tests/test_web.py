"""Real local-studio requests, dataset verification and output confinement."""

import asyncio
import base64
import io
import json
import threading
import time
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from fastapi.testclient import TestClient

from synthocr import web
from synthocr.pipeline import GenerationCancelled, generate
from synthocr.text import prepare
from synthocr.verify import verify


@pytest.fixture
def client(config):
    with TestClient(web.create_app(config), base_url="http://127.0.0.1:8000") as value:
        yield value


def wait_for_job(client, identifier):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{identifier}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] not in web.ACTIVE:
            return job
        time.sleep(0.01)
    pytest.fail("Studio generation did not complete")


def start_job(client, **overrides):
    response = client.post("/api/jobs", json={"samples": 3, **overrides})
    assert response.status_code == 202, response.text
    return wait_for_job(client, response.json()["id"])


def test_info_and_actual_kurdish_preview(client):
    info = client.get("/api/info").json()
    assert info["fonts_count"] > 0
    assert info["raqm"] is True
    assert info["defaults"]["workers"] == 1
    assert info["defaults"]["language"] == "sorani"
    label = "هەڵبژاردن · ماڵەکەم · ١٢٣"
    response = client.post("/api/preview", json={"text": label, "augmentation": 1})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["text"] == label
    assert result["direction"] == "rtl"
    with Image.open(io.BytesIO(base64.b64decode(result["image"].split(",", 1)[1]))) as image:
        assert image.size == (result["width"], result["height"])
        assert image.getextrema()[1] - image.getextrema()[0] > 16


def test_info_uses_one_registry_entry_per_language_without_duplicate_kurdish_choices(
    client, config
):
    (config.root / "raw/precise.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"text": "Hello", "language": "eng"},
                {"text": "Hello", "language": "en"},
                {"text": "你好", "language": "cmn"},
                {"text": "سڵاو", "language": "sdh"},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    prepare(config)
    info = client.get("/api/info").json()
    languages = {row["id"]: row for row in info["languages"]}
    assert len(languages) == len(info["languages"])
    assert "eng" not in languages
    assert "ckb" not in languages and "kmr" not in languages
    assert languages["en"]["records"] == 3
    assert languages["cmn"]["records"] == 1
    assert languages["sdh"]["records"] == 1
    corpora = {row["language"]: row["records"] for row in info["corpora"]}
    assert len(corpora) == len(info["corpora"])
    assert corpora["ckb"] == 1


@pytest.mark.parametrize("language", ["sorani", "kurmanji", "both", "all", "de"])
def test_empty_preview_uses_a_bounded_demo(client, language):
    response = client.post("/api/preview", json={"text": "", "language": language})
    assert response.status_code == 200, response.text
    assert response.json()["text"]


def test_second_studio_cannot_change_running_history_in_same_output_folder(config):
    app = web.create_app(config)
    manager = app.state.studio
    identifier = "a" * 32
    path = manager.metadata / f"{identifier}.json"
    try:
        manager._save({"id": identifier, "name": "running", "status": "generating"})
        before = path.read_bytes()
        with pytest.raises(ValueError, match="Another SynthOCR studio"):
            web.create_app(config)
        assert path.read_bytes() == before
    finally:
        manager.close()
    with TestClient(web.create_app(config), base_url="http://127.0.0.1") as client:
        assert client.get(f"/api/jobs/{identifier}").json()["status"] == "failed"


@pytest.mark.parametrize(
    "body",
    [
        {"name": "../escape"},
        {"name": "a/b"},
        {"name": "a\\b"},
        {"name": ".jobs"},
        {"name": ""},
        {"samples": 0},
        {"samples": 100001},
        {"samples": True},
        {"workers": 5},
        {"font_size": 129},
        {"augmentation": -1},
        {"format": "pickle"},
        {"unknown": True},
        {"language": "../ar"},
        {"train_ratio": 0.9, "validation_ratio": 0.2},
        {"source": "custom", "text": " "},
        {"source": "custom", "language": "both", "text": "hello"},
        {"source": "custom", "language": "all", "text": "hello"},
        {"source": "spelling", "language": "all"},
        {"source": "spelling", "language": "en"},
        {"text": "unexpected"},
        {"source": "custom", "text": "ە" * 102401},
    ],
)
def test_invalid_requests_do_not_start_jobs(client, body):
    response = client.post("/api/jobs", json={"samples": 3, **body})
    assert response.status_code == 422, response.text
    assert not client.get("/api/jobs").json()["jobs"]


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.org",
        "null",
        "http://127.0.0.1:9999",
        "http://127.0.0.1:invalid",
        "http://localhost:8000",
    ],
)
def test_mutations_require_same_local_origin(client, origin):
    response = client.post("/api/jobs", json={"samples": 3}, headers={"Origin": origin})
    assert response.status_code == 403
    assert not client.get("/api/jobs").json()["jobs"]


def test_host_fetch_site_and_payload_limits(client):
    assert client.get("/api/info", headers={"host": "attacker.example"}).status_code == 400
    assert (
        client.post("/api/jobs", json={}, headers={"sec-fetch-site": "cross-site"}).status_code
        == 403
    )
    assert client.post("/api/jobs", content=b"x" * (256 * 1024 + 1)).status_code == 413
    assert client.get("/api/info").headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("format", ["png", "jpg", "webp", "tif"])
def test_real_corpus_job_preserves_languages_and_verifies(client, config, format):
    raw = (config.path(config.data.raw) / "sample.jsonl").read_bytes()
    job = start_job(client, name=f"sorani-{format}", format=format)
    assert job["status"] == "completed", job
    assert job["generated"] == 3 and job["progress"] == 100
    assert job["result"]["verification"]["languages"] == {"ckb": 3}
    assert (config.path(config.data.raw) / "sample.jsonl").read_bytes() == raw
    assert verify(Path(job["output"]))["records"] == 3
    response = client.get(f"/api/jobs/{job['id']}/samples?limit=2&offset=1")
    page = response.json()
    assert page["total"] == 3 and len(page["samples"]) == 2
    assert all(row["language"] == "ckb" for row in page["samples"])
    image = client.get(page["samples"][0]["image"])
    assert image.status_code == 200
    with Image.open(io.BytesIO(image.content)) as decoded:
        decoded.verify()
    thumbnail = client.get(page["samples"][0]["thumbnail"])
    assert thumbnail.status_code == 200
    assert thumbnail.headers["cache-control"] == "private, max-age=3600"
    with Image.open(io.BytesIO(thumbnail.content)) as decoded:
        assert decoded.width <= 768 and decoded.height <= 192
    duplicate = client.post("/api/jobs", json={"samples": 3, "name": job["name"]})
    assert duplicate.status_code == 409
    assert client.get("/api/jobs").json()["jobs"][0]["id"] == job["id"]


def test_custom_text_keeps_exact_source_and_unicode_provenance(client):
    original = "ه‌ە و ک و ي و ی\n\nدەنووسم · ١٢٣\n"
    job = start_job(client, source="custom", text=original)
    assert job["status"] == "completed", job
    dataset = Path(job["output"])
    assert (dataset / "source.txt").read_text(encoding="utf-8") == original
    rows = [json.loads(line) for line in (dataset / "source.jsonl").read_text().splitlines()]
    assert [row["text"] for row in rows] == [original.splitlines()[0], original.splitlines()[2]]
    manifest = client.get(f"/api/jobs/{job['id']}/download?format=manifest")
    assert manifest.status_code == 200
    for row in map(json.loads, manifest.text.splitlines()):
        assert row["language"] == "ckb"
        assert row["attribution"]["source"] == "User supplied text"
        assert row["source"] == "custom.jsonl"


def test_custom_capacity_matches_all_generated_unique_pairs(client):
    original = "ماڵەکەم\nماڵەکەم\n\nدەنووسم\n"
    response = client.post("/api/capacity", json={"text": original, "language": "sorani"})
    assert response.status_code == 200, response.text
    capacity = response.json()
    assert capacity["texts"] == 2
    assert capacity["available_pairs"] > 2
    assert capacity["unsupported_texts"] == 0
    job = start_job(client, source="custom", text=original, samples=capacity["available_pairs"])
    assert job["status"] == "completed", job
    assert job["generated"] == capacity["available_pairs"]
    rows = client.get(f"/api/jobs/{job['id']}/download?format=manifest").text.splitlines()
    pairs = {(row["text"], row["font_id"]) for row in map(json.loads, rows)}
    assert len(pairs) == capacity["available_pairs"]


def test_custom_capacity_wraps_and_normalizes_like_generation(client, config):
    original = "\n" + "شێر " * 200 + "\nCafe\u0301\nCafé\n"
    capacity = client.post("/api/capacity", json={"text": original}).json()
    assert capacity["texts"] > 1
    preview = client.post("/api/preview", json={"text": original}).json()
    assert len(preview["text"]) <= config.data.max_chars
    assert preview["wrapped"] is True
    job = start_job(client, source="custom", text=original, samples=capacity["available_pairs"])
    assert job["status"] == "completed", job
    labels = {
        json.loads(line)["text"]
        for line in (Path(job["output"]) / "manifest.jsonl").read_text().splitlines()
    }
    assert preview["text"] in labels
    assert "Café" in labels and "Cafe\u0301" not in labels
    assert len(labels) == capacity["texts"]


def test_custom_preflight_rejects_excessive_count_without_creating_output(client):
    response = client.post("/api/jobs", json={"source": "custom", "text": "سڵاو"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["available_pairs"] > 0
    assert not client.get("/api/jobs").json()["jobs"]
    assert not list(client.app.state.studio.output_root.glob("sorani-*"))


def test_custom_preflight_does_not_silently_skip_unsupported_lines(client):
    text = "سڵاو\n\U0010ffff\n"
    capacity = client.post("/api/capacity", json={"text": text}).json()
    assert capacity["texts"] == 2 and capacity["unsupported_texts"] == 1
    assert capacity["unsupported_examples"] == ["\U0010ffff"]
    response = client.post("/api/jobs", json={"source": "custom", "text": text, "samples": 1})
    assert response.status_code == 422
    assert response.json()["detail"]["unsupported_texts"] == 1
    assert not client.get("/api/jobs").json()["jobs"]


def test_empty_custom_capacity_is_zero(client):
    assert client.post("/api/capacity", json={"text": " \n"}).json() == {
        "texts": 0,
        "available_pairs": 0,
        "unsupported_texts": 0,
        "unsupported_examples": [],
    }


def test_repeated_capacity_reuses_bounded_counts_not_source_text(client, monkeypatch):
    studio = client.app.state.studio
    expected = client.post("/api/capacity", json={"text": "سڵاو"}).json()

    def unexpected_prepare(config):
        pytest.fail("Unchanged custom text should reuse the preflight counts")

    monkeypatch.setattr(web, "prepare", unexpected_prepare)
    assert client.post("/api/capacity", json={"text": "سڵاو"}).json() == expected
    assert studio._coverage.cache
    assert len(studio._coverage.cache) <= 128
    assert all("سڵاو" not in key for key in studio._capacities)
    monkeypatch.setattr(studio, "_capacity", lambda request: {"texts": len(request.text)})
    for index in range(6):
        studio.capacity(web.CapacityRequest(text=str(index)))
    assert len(studio._capacities) == 4


def test_failed_custom_generation_also_retains_user_source(client, monkeypatch):
    actual = web.generate

    def fail_after_generation(config, **kwargs):
        actual(config, **kwargs)
        raise ValueError("Simulated post-generation failure")

    monkeypatch.setattr(web, "generate", fail_after_generation)
    job = start_job(client, source="custom", text="سڵاو\n")
    assert job["status"] == "failed"
    assert (Path(job["output"]) / "source.txt").read_text() == "سڵاو\n"
    assert client.get(f"/api/jobs/{job['id']}/download").status_code == 409


@pytest.mark.parametrize("format", ["dataset", "csv", "imagefolder", "tesseract"])
def test_downloads_are_valid_zip64_and_use_verified_export(client, format):
    job = start_job(client, source="custom", text="سڵاو\nکتێبەکەم\n")
    response = client.get(f"/api/jobs/{job['id']}/download?format={format}")
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        if format == "dataset":
            assert {"manifest.jsonl", "run.json", "stats.json", "source.txt"}.issubset(names)
        else:
            metadata = json.loads(archive.read("export.json"))
            assert metadata["records"] == 3 and metadata["format"] == format
            if format == "tesseract":
                assert sum(name.endswith(".gt.txt") for name in names) == 3
    # A second request can stream the atomically published archive.
    assert client.get(f"/api/jobs/{job['id']}/download?format={format}").content == response.content
    studio = client.app.state.studio
    assert not studio._leased
    assert not list(studio.metadata.glob(".export-*"))
    assert all(path.suffix == ".zip" for path in studio._cache_files())
    storage = client.get("/api/info").json()["storage"]
    assert storage["current_cache_bytes"] == len(response.content)
    assert storage["cache_limit_bytes"] == 256 * 1024 * 1024


def test_archive_size_preflight_matches_streamed_zip64(client):
    job = start_job(client)
    predicted = web._archive_size(Path(job["output"]))
    response = client.get(f"/api/jobs/{job['id']}/download")
    assert response.status_code == 200
    assert predicted == len(response.content)


def test_download_quota_rejects_before_copying_and_keeps_original_dataset(client):
    job = start_job(client)
    studio = client.app.state.studio
    studio.cache_limit = 1
    response = client.get(f"/api/jobs/{job['id']}/download")
    assert response.status_code == 413
    assert "cache limit" in response.json()["detail"]
    assert not list(studio._cache_files())
    assert not list(studio.metadata.glob(".export-*"))
    assert verify(Path(job["output"]))["records"] == 3
    assert client.get(f"/api/jobs/{job['id']}/download?format=manifest").status_code == 200


@pytest.mark.parametrize("format", ["dataset", "manifest", "csv", "imagefolder", "tesseract"])
def test_prepare_download_then_browser_download_preserves_attribution(client, format):
    job = start_job(client)
    url = f"/api/jobs/{job['id']}/download?format={format}"
    prepared = client.post(url, json={})
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["url"] == url
    assert prepared.json()["filename"].startswith(job["name"])
    assert not client.app.state.studio._leased
    response = client.get(prepared.json()["url"])
    assert response.status_code == 200
    assert "attachment;" in response.headers["content-disposition"]
    if format == "manifest":
        rows = [json.loads(line) for line in response.text.splitlines()]
        original = Path(job["output"]) / "manifest.jsonl"
        assert len(rows) == 3
        assert rows == [json.loads(line) for line in original.read_text().splitlines()]
    else:
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.testzip() is None
            assert "origins.jsonl" in archive.namelist()
    assert not client.app.state.studio._leased


def test_prepare_download_limit_has_actionable_copy_and_labels_remain_available(client):
    job = start_job(client)
    studio = client.app.state.studio
    studio.cache_limit = 1
    response = client.post(f"/api/jobs/{job['id']}/download", json={})
    assert response.status_code == 413
    detail = response.json()["detail"]
    assert "fewer images" in detail and "JSONL" in detail
    assert "--cache" not in detail and "output folder" not in detail
    assert not list(studio._cache_files())
    assert verify(Path(job["output"]))["records"] == 3
    assert (
        client.post(f"/api/jobs/{job['id']}/download?format=manifest", json={}).status_code == 200
    )


def test_prepare_download_refuses_incomplete_data_and_cross_origin_requests(client, monkeypatch):
    def failed_generation(*args, **kwargs):
        raise RuntimeError("Interrupted test generation")

    monkeypatch.setattr(web, "generate", failed_generation)
    job = start_job(client)
    assert job["status"] == "failed"
    url = f"/api/jobs/{job['id']}/download"
    assert client.post(url, json={}).status_code == 409
    assert client.post(url, json={}, headers={"Origin": "https://other.invalid"}).status_code == 403
    assert client.post(url + "?format=unknown", json={}).status_code == 422


def test_disconnected_archive_client_releases_cache_pin(client):
    job = start_job(client)
    studio = client.app.state.studio
    response = studio.download(job, "dataset")
    assert studio._leased

    async def disconnected(message):
        raise OSError("Client disconnected")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(OSError, match="Client disconnected"):
        asyncio.run(
            response({"type": "http", "method": "GET", "headers": []}, receive, disconnected)
        )
    assert not studio._leased


def test_cache_lru_and_in_flight_downloads_have_bounded_disk_use(client):
    studio = client.app.state.studio
    studio.cache_limit = 10
    oldest = studio.cache / "old.zip"
    newest = studio.cache / "new.zip"
    oldest.write_bytes(b"1234")
    newest.write_bytes(b"5678")
    oldest.touch()
    # Explicit timestamps make the LRU check independent of filesystem resolution.
    import os

    os.utime(oldest, ns=(1, 1))
    assert studio._trim_cache(reserve=6) == 4
    assert not oldest.exists() and newest.exists()
    studio._leased[newest] = 1
    with pytest.raises(web.HTTPException) as error:
        studio._trim_cache(reserve=7)
    assert error.value.status_code == 503
    assert newest.read_bytes() == b"5678"
    studio._release_archive(newest)
    assert studio._trim_cache(reserve=7) == 0
    assert not newest.exists()


@pytest.mark.parametrize("size", [0, -1, True, 1.5])
def test_invalid_studio_cache_limit_does_not_claim_output_root(config, size):
    with pytest.raises(ValueError, match="positive"):
        web.create_app(config, cache_mb=size)
    app = web.create_app(config, cache_mb=1)
    app.state.studio.close()


def test_restart_discards_only_derivatives_from_legacy_download_cache(config):
    first = web.create_app(config)
    studio = first.state.studio
    dataset = studio.output_root / "original"
    dataset.mkdir()
    original = dataset / "source.txt"
    original.write_text("User data")
    previous_cache = studio.metadata / ("b" * 32) / "0123456789abcdef"
    previous_cache.mkdir(parents=True)
    (previous_cache / "dataset.zip").write_bytes(b"obsolete")
    stale = studio.cache / "unfinished.tmp"
    stale.write_bytes(b"partial")
    studio.close()
    second = web.create_app(config)
    try:
        assert not previous_cache.exists() and not stale.exists()
        assert original.read_text() == "User data"
    finally:
        second.state.studio.close()


def test_gallery_uses_small_sparse_index_and_invalidates_changed_manifest(client):
    job = start_job(client)
    studio = client.app.state.studio
    manifest = Path(job["output"]) / "manifest.jsonl"
    template = json.loads(manifest.read_text().splitlines()[0])
    with manifest.open("w") as stream:
        for number in range(1000):
            stream.write(
                json.dumps(template | {"id": str(number), "text": f"Line {number}"}) + "\n"
            )
    page = studio.samples(job, 900, 12)
    assert page["samples"][0]["text"] == "Line 900"
    assert len(page["samples"]) == 12
    indices = list(studio.cache.glob("*.index"))
    assert len(indices) == 1 and indices[0].stat().st_size == 8 * 8
    assert studio.samples(job, 995, 12)["samples"][-1]["text"] == "Line 999"
    assert studio.samples(job, 99999, 12)["samples"] == []
    manifest.write_text(json.dumps(template | {"text": "Changed"}) + "\n")
    assert studio.samples(job, 1, 12)["samples"] == []


def test_gallery_thumbnails_are_resized_without_touching_original(client):
    job = start_job(client)
    sample = client.get(f"/api/jobs/{job['id']}/samples").json()["samples"][0]
    path = Path(job["output"]) / "images" / sample["image"].rsplit("/", 1)[1]
    with Image.new("L", (2048, 512), 200) as source:
        source.save(path)
    before = path.read_bytes()
    response = client.get(sample["thumbnail"])
    assert response.status_code == 200
    with Image.open(io.BytesIO(response.content)) as thumbnail:
        assert thumbnail.size == (768, 192)
    assert path.read_bytes() == before


def test_gallery_thumbnail_rejects_oversized_decode_and_symlink(client, tmp_path):
    job = start_job(client)
    sample = client.get(f"/api/jobs/{job['id']}/samples").json()["samples"][0]
    path = Path(job["output"]) / "images" / sample["image"].rsplit("/", 1)[1]
    with Image.new("L", (8192, 2049), 255) as source:
        source.save(path)
    assert client.get(sample["thumbnail"]).status_code == 400
    path.unlink()
    outside = tmp_path / "outside.png"
    with Image.new("L", (10, 10), 255) as source:
        source.save(outside)
    path.symlink_to(outside)
    assert client.get(sample["thumbnail"]).status_code == 404


@pytest.mark.parametrize("sample", [[], {"image": None}, {"image": 7}])
def test_malformed_gallery_rows_return_a_clear_error(client, sample):
    job = start_job(client)
    (Path(job["output"]) / "manifest.jsonl").write_text(json.dumps(sample) + "\n")
    assert client.get(f"/api/jobs/{job['id']}/samples").status_code == 400


def test_corrupt_dataset_download_is_rejected(client):
    job = start_job(client)
    (Path(job["output"]) / "manifest.jsonl").write_text("corrupt\n")
    response = client.get(f"/api/jobs/{job['id']}/download?format=manifest")
    assert response.status_code == 400
    assert "checksum" in response.json()["detail"].lower()


def test_one_active_job_and_cooperative_cancel(client, monkeypatch):
    entered = threading.Event()

    def paused(config, *, progress, cancel):
        progress({"stage": "preparing", "generated": 0, "requested": 3})
        entered.set()
        deadline = time.monotonic() + 5
        while not cancel() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise GenerationCancelled("cancelled as requested")

    monkeypatch.setattr(web, "generate", paused)
    response = client.post("/api/jobs", json={"samples": 3})
    identifier = response.json()["id"]
    assert entered.wait(2)
    assert client.post("/api/jobs", json={"samples": 3}).status_code == 409
    assert client.get(f"/api/jobs/{identifier}/download").status_code == 409
    assert client.post(f"/api/jobs/{identifier}/cancel").status_code == 200
    assert wait_for_job(client, identifier)["status"] == "cancelled"


def test_failed_job_reports_error_and_can_be_retried(client):
    job = start_job(client, language="zz", name="missing-language")
    assert job["status"] == "failed"
    assert "No processed records match" in job["error"]
    assert job["result"] is None
    assert start_job(client)["status"] == "completed"


def test_spelling_rejects_incomplete_count_and_checks_every_case(client):
    response = client.post("/api/jobs", json={"source": "spelling", "samples": 3})
    assert response.status_code == 422
    job = start_job(client, source="spelling", samples=176)
    assert job["status"] == "completed", job
    assert job["result"]["spelling_cases_checked"] == 176


def test_symlinks_cannot_escape_output_root(client, tmp_path):
    root = client.app.state.studio.output_root
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    assert client.post("/api/jobs", json={"name": "linked"}).status_code == 400
    job = start_job(client)
    dataset = Path(job["output"])
    image = next((dataset / "images").glob("*"))
    image.unlink()
    secret = outside / "private.txt"
    secret.write_text("secret")
    image.symlink_to(secret)
    assert client.get(f"/api/jobs/{job['id']}/images/{image.name}").status_code == 404
    assert client.get(f"/api/jobs/{job['id']}/images/private.txt").status_code == 404
    assert client.get("/api/jobs/not-a-job").status_code == 404


def test_restart_recovers_active_metadata_and_bounds_history(config):
    app = web.create_app(config)
    manager = app.state.studio
    for index in range(25):
        identifier = f"{index:032x}"
        manager._save({"id": identifier, "name": f"run-{index}", "status": "preparing"})
    manager.close()
    with TestClient(web.create_app(config), base_url="http://127.0.0.1") as client:
        jobs = client.get("/api/jobs").json()["jobs"]
        assert len(jobs) == 20
        assert all(job["status"] == "failed" for job in jobs)
        assert all("app closed" in job["error"] for job in jobs)
        assert client.get(f"/api/jobs/{0:032x}").json()["status"] == "failed"


def test_output_root_cannot_be_inside_raw_or_processed(config):
    for value in (config.data.raw, config.data.processed):
        with pytest.raises(ValueError, match="separate"):
            web.create_app(config, config.path(value) / "ui")


def test_pipeline_cancellation_records_honest_partial_stats(config):
    event = threading.Event()
    received = []

    def progress(value):
        received.append(value)
        if value["generated"] == 2:
            event.set()

    with pytest.raises(GenerationCancelled):
        generate(config, progress=progress, cancel=event.is_set)
    stats = json.loads((config.path(config.generation.output) / "stats.json").read_text())
    assert stats["status"] == "cancelled" and stats["generated"] == 2
    assert received[0]["stage"] == "preparing"
    with pytest.raises(ValueError, match="incomplete"):
        verify(config.path(config.generation.output))


def test_callback_does_not_change_generated_records(config):
    first = generate(config)
    other = replace(config, generation=replace(config.generation, output="other-output"))
    second = generate(other, progress=lambda value: None, cancel=lambda: False)
    assert (Path(first["output"]) / "manifest.jsonl").read_bytes() == (
        Path(second["output"]) / "manifest.jsonl"
    ).read_bytes()


def test_zip64_more_than_65535_entries_without_central_directory_list(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    entry = source / "کوردی.txt"
    entry.write_text("سڵاو", encoding="utf-8")
    monkeypatch.setattr(web, "dataset_files", lambda root: (entry for _ in range(65536)))
    archive = tmp_path / "many.zip"
    web._stored_zip(source, archive)
    with zipfile.ZipFile(archive) as reader:
        assert len(reader.infolist()) == 65536
        assert reader.read("کوردی.txt").decode() == "سڵاو"


def test_job_files_are_closed_before_they_replace_the_old_version(tmp_path, monkeypatch):
    # Windows refuses to rename a file that is still open (WinError 32).
    opened = []
    original_temporary = web.tempfile.NamedTemporaryFile
    original_replace = web.os.replace

    def tracking_temporary(*args, **kwargs):
        file = original_temporary(*args, **kwargs)
        opened.append(file)
        return file

    def windows_replace(source, target):
        assert all(file.closed for file in opened), "renamed a file that is still open"
        return original_replace(source, target)

    monkeypatch.setattr(web.tempfile, "NamedTemporaryFile", tracking_temporary)
    monkeypatch.setattr(web.os, "replace", windows_replace)
    path = tmp_path / "job.json"
    web._atomic_json(path, {"status": "first"})
    web._atomic_json(path, {"status": "second"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "second"}
    assert [item.name for item in tmp_path.iterdir()] == ["job.json"]
