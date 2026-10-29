"""UI starting points remain editable settings accepted by the real renderer."""

import base64
import io
from dataclasses import replace

import pytest
from PIL import Image

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from fastapi.testclient import TestClient

from synthocr.config import AUGMENTATION_FIELDS, DOCUMENT_LAYERS, DOCUMENT_PRESETS, IMAGE_PRESETS
from synthocr.web import AppearanceRequest, JobRequest, create_app


@pytest.fixture
def client(config):
    with TestClient(create_app(config), base_url="http://127.0.0.1:8000") as value:
        yield value


def test_discoverable_presets_are_complete_shared_settings(config, client):
    response = client.get("/api/info")
    assert response.status_code == 200
    catalog = response.json()["image_presets"]
    assert list(catalog) == ["balanced", "clean", "varied"]
    assert response.json()["recommended_preset"] == "balanced"
    assert {preset["label"] for preset in catalog.values()} == {"Balanced", "Clean", "Varied"}
    for preset in catalog.values():
        assert set(preset) == {"label", "description", "settings"}
        assert set(preset["settings"]) == set(AUGMENTATION_FIELDS)
        assert AppearanceRequest(**preset["settings"]).model_dump() == preset["settings"]
        replace(config, generation=replace(config.generation, **preset["settings"])).validate()
        assert all(preset["settings"][name] is None for name in DOCUMENT_LAYERS)
        assert preset["settings"]["preset_colors"] is True
    assert catalog["balanced"]["settings"]["augmentation"] == 0.75
    assert catalog["clean"]["settings"]["augmentation"] == 0
    assert catalog["varied"]["settings"]["augmentation"] == 1
    assert set(catalog["varied"]["settings"]["background_presets"]) == set(DOCUMENT_PRESETS)
    # Choosing a new UI recommendation must not alter requests from API clients
    # which omit appearance settings.
    assert response.json()["defaults"]["augmentation"] == JobRequest().augmentation == 0.35


@pytest.mark.parametrize("preset", ["balanced", "clean", "varied"])
@pytest.mark.parametrize(
    ("language", "text", "direction"),
    [("sorani", "سڵاو جیهان", "rtl"), ("kurmanji", "Roj baş, cîhan!", "ltr")],
)
def test_preset_previews_render_both_kurdish_varieties(client, preset, language, text, direction):
    settings = client.get("/api/info").json()["image_presets"][preset]["settings"]
    response = client.post(
        "/api/preview",
        json={**settings, "language": language, "text": text, "seed": 42},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["text"] == text
    assert result["direction"] == direction
    assert result["before"]["augmentation"] == {}
    with Image.open(io.BytesIO(base64.b64decode(result["image"].split(",", 1)[1]))) as image:
        image.load()
        assert image.size == (result["width"], result["height"])
        assert image.width * image.height <= 524_288
    if preset == "clean":
        assert result["augmentation"] == {}
        assert result["image"] == result["before"]["image"]
    else:
        assert [entry["preset"] for entry in result["variants"]] == settings["background_presets"]
        assert result["image"] != result["before"]["image"]
        for variant in result["variants"]:
            assert (
                variant["augmentation"]["layers"] == DOCUMENT_PRESETS[variant["preset"]]["layers"]
            )


def test_editing_a_preset_is_an_ordinary_custom_preview(client):
    catalog = client.get("/api/info").json()["image_presets"]
    settings = catalog["balanced"]["settings"]
    settings.update(
        background_presets=["book-page"],
        grain=0.4,
        binding_shadow=0,
        scan_banding=0.3,
        speckles=0.2,
    )
    response = client.post("/api/preview", json={**settings, "seed": 42, "text": "سڵاو"})
    assert response.status_code == 200, response.text
    applied = response.json()["augmentation"]
    assert applied["background_preset"] == "book-page"
    assert applied["layers"]["grain"] == 0.4
    assert applied["layers"]["binding_shadow"] == 0
    assert applied["layers"]["scan_banding"] == 0.3
    assert applied["layers"]["speckles"] == 0.2
    assert (
        applied["layers"]["paper_texture"]
        == DOCUMENT_PRESETS["book-page"]["layers"]["paper_texture"]
    )
    assert client.get("/api/info").json()["image_presets"] == IMAGE_PRESETS
