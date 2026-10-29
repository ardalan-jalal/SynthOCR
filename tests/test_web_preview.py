"""Studio previews use actual selected corpus labels and bundled diagnostic cases."""

import json
from dataclasses import replace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from fastapi.testclient import TestClient

from synthocr import web
from synthocr.downloads import sha256
from synthocr.text import prepare


@pytest.fixture
def client(config):
    with TestClient(web.create_app(config), base_url="http://127.0.0.1:8000") as value:
        yield value


def test_corpus_preview_needs_prepared_data_and_does_not_prepare_in_request(
    client, config, monkeypatch
):
    def unexpected_prepare(*args, **kwargs):
        pytest.fail("A preview request must never rebuild a corpus")

    monkeypatch.setattr(web, "prepare", unexpected_prepare)
    response = client.post("/api/preview", json={"source": "corpus"})
    assert response.status_code == 400
    assert "synthocr prepare" in response.text and "upload" in response.text
    assert not config.path(config.data.processed).exists()


def test_corpus_preview_selects_real_language_label_readonly_and_wraps_index(
    client, config, monkeypatch
):
    prepare(config)
    path = config.path(config.data.processed) / "texts.sqlite3"
    before = sha256(path), path.stat().st_mtime_ns

    def unexpected_prepare(*args, **kwargs):
        pytest.fail("Prepared previews must not invoke preparation")

    monkeypatch.setattr(web, "prepare", unexpected_prepare)
    for index in [0, 31]:
        response = client.post(
            "/api/preview", json={"source": "corpus", "language": "sorani", "index": index}
        )
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["text"] == "سڵاو جیهان"
        assert preview["source_language"] == "ckb"
        assert preview["source"] == "corpus"
        assert preview["case_id"] is None
    assert (sha256(path), path.stat().st_mtime_ns) == before


def test_corpus_preview_uses_language_aliases_and_excludes_diagnostics(client, config, monkeypatch):
    (config.root / "raw/excluded.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"text": "! diagnostic", "language": "eng", "genre": "spelling"},
                {"text": "! private", "language": "en", "genre": "private"},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    prepare(config)
    studio = client.app.state.studio
    monkeypatch.setattr(
        studio,
        "config",
        replace(config, generation=replace(config.generation, exclude_genres=["private"])),
    )
    response = client.post("/api/preview", json={"source": "corpus", "language": "eng"})
    assert response.status_code == 200, response.text
    assert response.json()["text"] == "A quiet morning"
    assert response.json()["source_language"] == "en"


def test_both_kurdish_corpus_preview_alternates_actual_source_languages(client, config):
    (config.root / "raw/kurmanji.jsonl").write_text(
        '{"text":"Silav dinya","language":"kmr"}\n', encoding="utf-8"
    )
    prepare(config)
    for index, language, text in [(0, "ckb", "سڵاو جیهان"), (1, "kmr", "Silav dinya")]:
        response = client.post(
            "/api/preview", json={"source": "corpus", "language": "both", "index": index}
        )
        assert response.status_code == 200, response.text
        assert response.json()["source_language"] == language
        assert response.json()["text"] == text


def test_corpus_preview_unavailable_language_is_actionable(client, config):
    prepare(config)
    response = client.post("/api/preview", json={"source": "corpus", "language": "de"})
    assert response.status_code == 400
    assert "No usable corpus text" in response.text


def test_corpus_preview_prefers_readable_natural_text_without_removing_other_labels(client, config):
    (config.root / "raw/representative.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"text": "!", "language": "en", "genre": "symbols"},
                {"text": "123", "language": "en", "genre": "number"},
                {"text": "A representative sentence", "language": "en", "genre": "prose"},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    prepared = prepare(config)
    response = client.post("/api/preview", json={"source": "corpus", "language": "en"})
    assert response.status_code == 200
    assert response.json()["text"] == "A representative sentence"
    assert prepare(config)["records"] == prepared["records"] == 7


def test_corrupt_corpus_index_is_an_actionable_error(client, config):
    path = config.path(config.data.processed)
    path.mkdir()
    (path / "texts.sqlite3").write_bytes(b"invalid sqlite")
    response = client.post("/api/preview", json={"source": "corpus"})
    assert response.status_code == 400
    assert "synthocr prepare" in response.text


@pytest.mark.parametrize(
    ("index", "tag", "text", "case_id"),
    [(0, "ckb", "ئاو", "ckb-vowel-initial-01"), (1, "kmr", "çav", "kmr-accented-01")],
)
def test_spelling_preview_uses_actual_bundled_cases_and_alternates_both(
    client, index, tag, text, case_id
):
    response = client.post(
        "/api/preview", json={"source": "spelling", "language": "both", "index": index}
    )
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["source"] == "spelling"
    assert preview["source_language"] == tag
    assert preview["text"] == text
    assert preview["case_id"] == case_id


@pytest.mark.parametrize(
    "body", [{"index": 32}, {"index": -1}, {"index": True}, {"source": "upload"}]
)
def test_preview_source_and_index_are_bounded(client, body):
    response = client.post("/api/preview", json=body)
    assert response.status_code == 422
