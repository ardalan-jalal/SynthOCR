"""Focused UI source choices remain explicit while general API tags keep working."""

import json
import time
import unicodedata
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from fastapi.testclient import TestClient

from synthocr import web
from synthocr.text import prepare
from synthocr.verify import verify

SELECTED = ["ckb", "kmr", "ar", "en"]
ROWS = [
    {"text": "سڵاو؛ ڕۆژێکی خۆش بێت", "language": "ckb"},
    {"text": "Roj baş û rojeka xweş!", "language": "kmr"},
    {"text": "القراءة مفيدة كل يوم", "language": "ara"},
    {"text": "Read a new book every day", "language": "eng"},
    {"text": "هذا نص عراقي مستقل", "language": "ar-iq"},
    {"text": "هذا نص عربي فصيح", "language": "arb"},
    {"text": "سلام جهان امروز", "language": "fa"},
]


@pytest.fixture
def client(config):
    (config.root / "raw/focused.jsonl").write_text(
        "".join(json.dumps({**row, "genre": "prose"}, ensure_ascii=False) + "\n" for row in ROWS),
        encoding="utf-8",
    )
    prepare(config)
    with TestClient(web.create_app(config), base_url="http://127.0.0.1:8000") as value:
        yield value


def completed(client, payload):
    response = client.post("/api/jobs", json=payload)
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{identifier}").json()
        if job["status"] not in web.ACTIVE:
            break
        time.sleep(0.01)
    else:
        pytest.fail("Language selection generation did not finish")
    assert job["status"] == "completed", job
    output = Path(job["output"])
    assert verify(output)["records"] == payload["samples"]
    records = [
        json.loads(line)
        for line in (output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    return job, records, output


def upload(client, rows, languages, *, filename="mixed.jsonl"):
    raw = (
        rows
        if isinstance(rows, str)
        else "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    )
    params = [("filename", filename), *(("languages", tag) for tag in languages)]
    return client.post(
        "/api/uploads",
        params=params,
        content=raw.encode(),
        headers={"content-type": "application/octet-stream"},
    )


def test_four_language_corpus_selection_is_exact_and_records_actual_sources(client):
    job, records, output = completed(
        client, {"source": "corpus", "languages": SELECTED, "samples": 12}
    )
    assert job["languages"] == SELECTED
    assert {record["language"] for record in records} == set(SELECTED)
    assert all(record["language"] not in {"ar-iq", "arb", "fa", ""} for record in records)
    run = json.loads((output / "run.json").read_text())
    assert run["config"]["generation"]["languages"] == SELECTED
    assert run["config"]["generation"]["language"] == ""


def test_corpus_previews_cycle_selected_sources_without_guessing_script(client):
    found = []
    for index in range(4):
        response = client.post(
            "/api/preview", json={"source": "corpus", "languages": SELECTED, "index": index}
        )
        assert response.status_code == 200, response.text
        sample = response.json()
        found.append(sample["source_language"])
        assert sample["language"] == SELECTED[index]
        assert sample["direction"] == ("rtl" if SELECTED[index] in {"ckb", "ar"} else "ltr")
    assert found == SELECTED


def test_demo_preview_cycles_chosen_sources_and_legacy_arabic_alias_has_arabic_text(client):
    for index, tag in enumerate(SELECTED):
        response = client.post("/api/preview", json={"languages": SELECTED, "index": index})
        assert response.status_code == 200, response.text
        assert response.json()["source_language"] == tag
        assert response.json()["text"] != "123"
    response = client.post("/api/preview", json={"language": "ara"})
    assert response.status_code == 200, response.text
    assert response.json()["source_language"] == "ar"
    assert response.json()["text"] == "مرحبا بالعالم"


@pytest.mark.parametrize("endpoint", ["/api/jobs", "/api/preview", "/api/capacity"])
@pytest.mark.parametrize(
    "languages",
    [
        [],
        ["ckb", "ckb"],
        ["ara"],
        ["ar-iq"],
        ["fa"],
        ["ckb", "kmr", "ar", "en", "ckb"],
        [True],
        "ckb",
    ],
)
def test_focused_selection_requires_one_to_four_distinct_canonical_tags(
    client, endpoint, languages
):
    response = client.post(endpoint, json={"languages": languages})
    assert response.status_code == 422, response.text
    assert client.get("/api/jobs").json()["jobs"] == []


@pytest.mark.parametrize(
    "endpoint,extra",
    [
        ("/api/jobs", {"source": "custom"}),
        ("/api/capacity", {}),
        ("/api/preview", {"source": "demo"}),
    ],
)
def test_pasted_text_requires_one_declared_source_language(client, endpoint, extra):
    response = client.post(
        endpoint, json={"languages": ["ckb", "ar"], "text": "سڵاو مرحبا", **extra}
    )
    assert response.status_code == 422, response.text
    assert "one explicit source language" in response.text


@pytest.mark.parametrize("endpoint", ["/api/jobs", "/api/preview"])
def test_kurdish_diagnostics_reject_supporting_languages_instead_of_dropping_them(client, endpoint):
    response = client.post(endpoint, json={"source": "spelling", "languages": ["ckb", "ar"]})
    assert response.status_code == 422, response.text
    assert "Spelling" in response.text


def test_mixed_script_sorani_label_preserves_logical_order_marks_joiner_and_source(client):
    label = "سڵاو ثA ١2 اَ‌ب"
    job, records, output = completed(
        client,
        {
            "source": "custom",
            "languages": ["ckb"],
            "text": label,
            "samples": 1,
            "augmentation": 1,
            "background_presets": ["office-scan"],
        },
    )
    assert job["languages"] == ["ckb"]
    assert records[0]["language"] == "ckb"
    assert records[0]["text"] == unicodedata.normalize("NFC", label)
    assert records[0]["direction"] == "rtl"
    assert (output / "source.txt").read_text(encoding="utf-8") == label


def test_single_explicit_selection_overrides_legacy_default_for_capacity_and_custom_generation(
    client,
):
    response = client.post("/api/capacity", json={"languages": ["en"], "text": "Hello world"})
    assert response.status_code == 200, response.text
    assert response.json()["texts"] == 1
    job, records, _ = completed(
        client, {"source": "custom", "languages": ["en"], "text": "Hello world", "samples": 1}
    )
    assert job["languages"] == ["en"]
    assert records[0]["language"] == "en"


def test_labelled_upload_filter_and_generation_keep_selected_and_observed_languages_separate(
    client,
):
    selected = ["ckb", "ar", "en"]
    response = upload(client, ROWS, selected)
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["selected_languages"] == selected
    assert set(document["languages"]) == {"ckb", "kmr", "ar", "en", "ar-iq", "arb", "fa"}
    assert document["records"] == 3
    assert document["preview_language"] == "ckb"
    _, records, output = completed(
        client,
        {"source": "upload", "upload_id": document["id"], "languages": selected, "samples": 3},
    )
    assert {record["language"] for record in records} == set(selected)
    retained = json.loads((output / "source-upload.json").read_text())
    assert retained["selected_languages"] == selected


@pytest.mark.parametrize(
    "filename,raw",
    [
        ("plain.txt", "Hello world\n"),
        ("mixed.jsonl", '{"text":"سڵاو","language":"ckb"}\n{"text":"Hello"}\n'),
        ("mixed.csv", "text,language\nسڵاو,ckb\nHello,\n"),
    ],
)
def test_unlabelled_upload_cannot_assign_one_of_multiple_selected_languages(client, filename, raw):
    response = upload(client, raw, ["ckb", "en"], filename=filename)
    assert response.status_code == 422, response.text
    assert "without language tags" in response.text
    assert not list(client.app.state.studio.uploads.iterdir())


def test_unlabelled_upload_retains_single_explicit_source_when_legacy_default_differs(client):
    response = upload(client, "Hello world\n", ["en"], filename="english.txt")
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["selected_languages"] == document["languages"] == ["en"]
    changed = client.post(
        "/api/jobs",
        json={
            "source": "upload",
            "upload_id": document["id"],
            "languages": ["ckb", "en"],
            "samples": 1,
        },
    )
    assert changed.status_code == 422, changed.text
    _, records, _ = completed(
        client, {"source": "upload", "upload_id": document["id"], "languages": ["en"], "samples": 1}
    )
    assert records[0]["language"] == "en"


@pytest.mark.parametrize(
    "selection", [["ckb", "ckb"], ["ara"], ["fa"], ["ckb", "kmr", "ar", "en", "ckb"]]
)
def test_upload_query_rejects_invalid_focused_selection_before_writing(client, selection):
    response = upload(client, ROWS, selection)
    assert response.status_code == 422, response.text
    assert not list(client.app.state.studio.uploads.iterdir())


@pytest.mark.parametrize(
    "language,expected",
    [("ara", "ar"), ("eng", "en"), ("ar-iq", "ar-iq"), ("arb", "arb"), ("fa", "fa")],
)
def test_legacy_general_api_tags_keep_exact_source_selection(client, language, expected):
    _, records, _ = completed(client, {"source": "corpus", "language": language, "samples": 1})
    assert records[0]["language"] == expected


@pytest.mark.parametrize(
    "weights",
    [{"ar": 1}, {"ckb": -1}, {"ckb": 0, "kmr": 0}],
)
def test_language_shares_must_match_selected_languages(weights):
    with pytest.raises(ValueError, match="Language shares"):
        web.JobRequest(languages=["ckb", "kmr"], language_weights=weights)
    assert web.JobRequest(
        languages=["ckb", "kmr"], language_weights={"ckb": 70, "kmr": 30}
    ).language_weights == {"ckb": 70, "kmr": 30}
