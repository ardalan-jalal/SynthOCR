"""Disk-backed uploads retain original bytes and explicit source languages."""

import asyncio
import hashlib
import json
import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from fastapi.testclient import TestClient

from synthocr import web
from synthocr.verify import verify


@pytest.fixture
def client(config):
    with TestClient(web.create_app(config), base_url="http://127.0.0.1:8000") as value:
        yield value


def upload(client, data, *, filename="sentences.txt", language="sorani"):
    return client.post(
        "/api/uploads",
        params={"filename": filename, "language": language},
        content=data.encode("utf-8") if isinstance(data, str) else data,
        headers={"Content-Type": "application/octet-stream"},
    )


def finished(client, identifier):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{identifier}").json()
        if job["status"] not in web.ACTIVE:
            return job
        time.sleep(0.01)
    pytest.fail("Uploaded generation did not finish")


def test_upload_is_saved_before_generation_and_retains_original_bytes(client):
    original = "\ufeffسڵاو جیهان\r\nسڵاو جیهان\r\n١٢٣\r\n".encode()
    response = upload(client, original, filename="Kurdish text.TXT")
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["bytes"] == len(original)
    assert document["records"] == 2
    assert document["source_records"] == 3
    assert document["languages"] == ["ckb"]
    assert document["preview_language"] == "ckb"
    assert document["preview_text"] == "سڵاو جیهان"
    assert document["sha256"] == hashlib.sha256(original).hexdigest()
    manager = client.app.state.studio
    pending = manager.uploads / document["id"] / "source.txt"
    assert pending.read_bytes() == original
    assert not client.get("/api/jobs").json()["jobs"]
    started = client.post(
        "/api/jobs",
        json={"source": "upload", "upload_id": document["id"], "samples": 3},
    )
    assert started.status_code == 202, started.text
    job = finished(client, started.json()["id"])
    assert job["status"] == "completed", job
    dataset = manager.dataset(job)
    assert (dataset / "source.txt").read_bytes() == original
    assert (
        json.loads((dataset / "source-upload.json").read_text())["filename"] == "Kurdish text.TXT"
    )
    assert not pending.exists()
    assert verify(dataset)["status"] == "verified"
    repeated = client.post(
        "/api/jobs", json={"source": "upload", "upload_id": document["id"], "samples": 1}
    )
    assert repeated.status_code == 409
    assert "saved with dataset" in repeated.text
    downloaded = client.get(f"/api/jobs/{job['id']}/download?format=csv")
    assert downloaded.status_code == 200, downloaded.text
    with zipfile.ZipFile(BytesIO(downloaded.content)) as archive:
        assert archive.read("source.txt") == original
        assert "source-upload.json" in archive.namelist()


@pytest.mark.parametrize("extension", ["csv", "jsonl"])
def test_tagged_upload_preserves_sorani_and_kurmanji(client, extension):
    rows = [
        {"text": "سڵاو جیهان", "language": "ckb"},
        {"text": "Silav dinya", "language": "kmr"},
    ]
    data = (
        "text,language\nسڵاو جیهان,ckb\nSilav dinya,kmr\n"
        if extension == "csv"
        else "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
    )
    response = upload(client, data, filename=f"bilingual.{extension}", language="both")
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["unlabelled_records"] == 0
    assert document["languages"] == ["ckb", "kmr"]
    response = client.post(
        "/api/jobs",
        json={"source": "upload", "upload_id": document["id"], "language": "both", "samples": 2},
    )
    assert response.status_code == 202, response.text
    job = finished(client, response.json()["id"])
    assert job["status"] == "completed", job
    root = client.app.state.studio.dataset(job)
    labels = [json.loads(line) for line in (root / "manifest.jsonl").read_text().splitlines()]
    assert {row["language"] for row in labels} == {"ckb", "kmr"}
    assert (root / f"source.{extension}").read_text() == data


def test_upload_count_matches_normalized_wrapped_chunks(client):
    response = upload(client, "A" * 65 + "\n" + "A" * 65 + "\n", language="en")
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["source_records"] == 2
    # max_chars=30: two identical 30-letter pieces and one 5-letter piece.
    assert document["records"] == 2
    assert len(document["preview_text"]) == 30


def test_upload_inspection_uses_the_same_nfc_as_ui_generation(client, monkeypatch):
    from dataclasses import replace

    studio = client.app.state.studio
    monkeypatch.setattr(
        studio,
        "config",
        replace(studio.config, data=replace(studio.config.data, normalization="none")),
    )
    original = "e\u0301\né\n".encode()
    response = upload(client, original, language="en")
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["records"] == 1
    assert document["preview_text"] == "é"
    assert (studio.uploads / document["id"] / "source.txt").read_bytes() == original


@pytest.mark.parametrize(("language", "expected"), [("sorani", 1), ("all", 1000)])
def test_upload_count_and_preview_use_only_selected_languages(client, language, expected):
    data = (
        "".join(
            json.dumps({"text": f"English sentence {number}", "language": "eng"}) + "\n"
            for number in range(999)
        )
        + json.dumps({"text": "سڵاو جیهان", "language": "ckb"}, ensure_ascii=False)
        + "\n"
    )
    response = upload(client, data, filename="mixed.jsonl", language=language)
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["records"] == expected
    assert document["source_records"] == 1000
    assert document["languages"] == ["ckb", "en"]
    assert document["preview_language"] == ("ckb" if language == "sorani" else "en")
    assert document["preview_text"] == (
        "سڵاو جیهان" if language == "sorani" else "English sentence 0"
    )


@pytest.mark.parametrize("genre", ["spelling", "private"])
def test_upload_count_excludes_diagnostic_and_configured_genres(client, monkeypatch, genre):
    from dataclasses import replace

    studio = client.app.state.studio
    monkeypatch.setattr(
        studio,
        "config",
        replace(
            studio.config, generation=replace(studio.config.generation, exclude_genres=["private"])
        ),
    )
    data = (
        json.dumps({"text": "سڵاو", "language": "ckb", "genre": genre}, ensure_ascii=False) + "\n"
    )
    response = upload(client, data, filename="excluded.jsonl")
    assert response.status_code == 422
    assert "no usable text" in response.text
    assert not list(studio._pending_uploads())


def test_upload_rejects_tagged_file_with_no_selected_language(client):
    response = upload(client, '{"text":"English","language":"en"}\n', filename="english.jsonl")
    assert response.status_code == 422
    assert "selected language" in response.text
    assert not list(client.app.state.studio._pending_uploads())


@pytest.mark.parametrize(("first_genre", "status"), [("private", 422), ("spelling", 201)])
def test_upload_selection_matches_shared_preparation_primary_origin(
    client, monkeypatch, first_genre, status
):
    from dataclasses import replace

    studio = client.app.state.studio
    monkeypatch.setattr(
        studio,
        "config",
        replace(
            studio.config,
            generation=replace(studio.config.generation, exclude_genres=["private"]),
        ),
    )
    data = (
        "\n".join(
            json.dumps({"text": "سڵاو", "language": "ckb", "genre": genre}, ensure_ascii=False)
            for genre in [first_genre, "prose"]
        )
        + "\n"
    )
    response = upload(client, data, filename="overlap.jsonl")
    assert response.status_code == status, response.text
    if status == 201:
        assert response.json()["records"] == 1
        assert response.json()["preview_text"] == "سڵاو"


@pytest.mark.parametrize(
    ("data", "filename", "language"),
    [
        (" ", "empty.txt", "sorani"),
        (b"\xff\xfe", "invalid.txt", "sorani"),
        ('{"text":123}\n', "invalid.jsonl", "sorani"),
        ('{"text":"Hi","language":42}\n', "invalid.jsonl", "sorani"),
        ('{"text":"Hi","language":"bad/tag"}\n', "invalid.jsonl", "sorani"),
        ('{"text":"Hi","attribution":[]}\n', "invalid.jsonl", "sorani"),
        ('{"text":"\\ud800"}\n', "invalid.jsonl", "sorani"),
        ("text,text\nhello,world\n", "invalid.csv", "sorani"),
        ("Hello", "invalid.txt", "both"),
        ("Hello", "invalid.txt", "all"),
        ("Hello", "../escape.txt", "en"),
        ("Hello", "image.png", "en"),
    ],
)
def test_invalid_uploads_leave_no_partial_inputs(client, data, filename, language):
    response = upload(client, data, filename=filename, language=language)
    assert response.status_code in {400, 422}, response.text
    assert not list(client.app.state.studio._pending_uploads())
    assert not client.get("/api/jobs").json()["jobs"]


def test_uploaded_text_cannot_silently_change_language(client):
    document = upload(client, "Hello", language="en").json()
    response = client.post(
        "/api/jobs",
        json={"source": "upload", "upload_id": document["id"], "language": "sorani"},
    )
    assert response.status_code == 422
    assert "upload it again" in response.text
    assert not client.get("/api/jobs").json()["jobs"]


def test_pending_upload_count_is_bounded_and_unused_uploads_can_be_removed(client, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_FILES", 2)
    first = upload(client, "one").json()
    second = upload(client, "two").json()
    assert upload(client, "three").status_code == 413
    assert client.delete(f"/api/uploads/{first['id']}").status_code == 200
    assert upload(client, "three").status_code == 201
    assert (client.app.state.studio.uploads / second["id"]).exists()


def test_upload_byte_quota_handles_chunked_requests_and_removes_partial_file(client, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_BYTES", 16)
    monkeypatch.setattr(web, "UPLOAD_TOTAL_BYTES", 20)
    assert upload(client, "one two\n").status_code == 201
    assert upload(client, "three four\n").status_code == 201
    assert upload(client, "five\n").status_code == 413
    assert len(list(client.app.state.studio._pending_uploads())) == 2
    response = client.post(
        "/api/uploads?filename=large.txt&language=en", content=iter([b"x" * 10, b"x" * 10])
    )
    assert response.status_code == 413
    assert len(list(client.app.state.studio._pending_uploads())) == 2


def test_simultaneous_upload_writes_cannot_exceed_the_global_quota(client, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_TOTAL_BYTES", 20)
    studio = client.app.state.studio
    _, first = studio._begin_upload("one.txt", "en")
    _, second = studio._begin_upload("two.txt", "en")
    barrier = threading.Barrier(2)

    def write(path):
        with path.open("xb", buffering=0) as stream:
            barrier.wait(timeout=5)
            try:
                studio._write_upload_chunk(stream, b"x" * 12)
            except web.HTTPException as error:
                return error.status_code
            return 201

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(write, [first, second])) == [201, 413]
    assert first.stat().st_size + second.stat().st_size == 12


def test_expired_unused_inputs_are_removed_without_touching_datasets(client):
    first = upload(client, "one").json()
    directory = client.app.state.studio.uploads / first["id"]
    os.utime(directory, (time.time() - web.UPLOAD_TTL - 1,) * 2)
    assert upload(client, "two").status_code == 201
    assert not directory.exists()


def test_disconnected_client_after_validation_does_not_leave_an_unused_upload(client):
    class DisconnectedRequest:
        async def stream(self):
            yield b"Hello\n"

        async def is_disconnected(self):
            return True

    studio = client.app.state.studio
    with pytest.raises(web.HTTPException) as cancelled:
        asyncio.run(studio.upload(DisconnectedRequest(), "sentences.txt", "en"))
    assert cancelled.value.status_code == 499
    assert not list(studio._pending_uploads())


def test_upload_used_by_running_generation_cannot_be_deleted(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    generate = web.generate

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return generate(*args, **kwargs)

    monkeypatch.setattr(web, "generate", blocked)
    document = upload(client, "سڵاو").json()
    response = client.post(
        "/api/jobs", json={"source": "upload", "upload_id": document["id"], "samples": 1}
    )
    try:
        assert entered.wait(5)
        removed = client.delete(f"/api/uploads/{document['id']}")
        assert removed.status_code == 409
    finally:
        release.set()
    assert finished(client, response.json()["id"])["status"] == "completed"


def test_upload_keeps_same_local_origin_protection_and_json_body_limit(client):
    forbidden = client.post(
        "/api/uploads?filename=text.txt", content=b"hello", headers={"origin": "https://other.test"}
    )
    assert forbidden.status_code == 403
    too_large_json = client.post("/api/jobs", content=b"x" * (256 * 1024 + 1))
    assert too_large_json.status_code == 413
    info = client.get("/api/info").json()
    assert info["upload_formats"] == ["txt", "jsonl", "csv"]
    assert info["upload_limit_bytes"] == 50 * 1024 * 1024


def test_export_reserves_uploaded_source_bytes_before_materializing_view(client, monkeypatch):
    document = upload(client, ("A" * 5000 + "\n") * 220, language="en").json()
    assert document["bytes"] > 1024 * 1024
    response = client.post(
        "/api/jobs",
        json={"source": "upload", "upload_id": document["id"], "language": "en", "samples": 1},
    )
    assert response.status_code == 202, response.text
    job = finished(client, response.json()["id"])
    assert job["status"] == "completed", job
    monkeypatch.setattr(client.app.state.studio, "cache_limit", 1024 * 1024)

    def unexpected_export(*args, **kwargs):
        pytest.fail("An oversized retained source must be rejected before export materializes")

    monkeypatch.setattr(web, "export", unexpected_export)
    response = client.get(f"/api/jobs/{job['id']}/download?format=csv")
    assert response.status_code == 413
    assert "cache limit" in response.text
    assert not list(client.app.state.studio.metadata.glob(".export-*"))
