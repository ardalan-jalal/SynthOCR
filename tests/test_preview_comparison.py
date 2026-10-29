"""Appearance comparisons keep one real sample and bound their decoded buffers."""

import base64
import io
import json
import time
from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from fastapi.testclient import TestClient

from synthocr import web
from synthocr.config import DOCUMENT_LAYERS, DOCUMENT_PRESETS
from synthocr.verify import verify


@pytest.fixture
def client(config):
    with TestClient(web.create_app(config), base_url="http://127.0.0.1:8000") as value:
        yield value


def preview(client, **settings):
    response = client.post(
        "/api/preview",
        json={
            "text": "سڵاو جیهان",
            "augmentation": 1,
            "rotation": 0,
            "blur": 0,
            "contrast": 0,
            **settings,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def decode(sample):
    return Image.open(io.BytesIO(base64.b64decode(sample["image"].split(",", 1)[1])))


def test_comparison_catalog_uses_one_sample_and_selects_requested_after(client):
    settings = {"compare": True, "background_presets": ["office-scan", "aged-paper"]}
    first = preview(client, **settings)
    second = preview(
        client,
        **{**settings, "background_presets": ["notebook"], "grain": 0.4, "shadow": 0.7},
    )
    assert first["before"] == second["before"]
    assert first["text"] == second["text"] == "سڵاو جیهان"
    assert first["font"] == second["font"]
    assert first["seed"] == second["seed"] == 42
    assert first["augmentation"]["background_preset"] == "office-scan"
    assert second["augmentation"]["background_preset"] == "notebook"
    for result in (first, second):
        assert len(result["variants"]) == len(DOCUMENT_PRESETS)
        assert [entry["preset"] for entry in result["variants"]] == list(DOCUMENT_PRESETS)
        assert result["before"]["augmentation"] == {}
        with decode(result["before"]) as clean:
            assert clean.mode == "L"
            assert clean.getpixel((0, 0)) == 255
        for variant in result["variants"]:
            assert variant["text"] == result["text"]
            assert variant["font"] == result["font"]
            assert variant["font_size"] == result["before"]["font_size"]
            assert variant["direction"] == result["before"]["direction"] == "rtl"
            assert variant["name"] == DOCUMENT_PRESETS[variant["preset"]]["label"]
            assert variant["augmentation"]["background_preset"] == variant["preset"]
            with decode(variant) as image:
                assert image.size == (variant["width"], variant["height"])
                assert image.width * image.height <= 524_288
                left, top, right, bottom = variant["bbox"]
                assert 0 < left < right < image.width
                assert 0 < top < bottom < image.height
    assert len({variant["image"] for variant in first["variants"]}) == len(DOCUMENT_PRESETS)


def test_selected_variants_combine_layers_and_preserve_zero_overrides(client):
    settings = {
        "background_presets": ["aged-paper", "notebook"],
        "paper_texture": 0.3,
        "grain": 0.2,
        "shadow": 0.4,
        "stains": 0.5,
        "creases": 0.6,
        "ruled_lines": 0,
        "scan_banding": 0.25,
        "speckles": 0.15,
        "binding_shadow": 0,
    }
    result = preview(client, **settings)
    assert [entry["preset"] for entry in result["variants"]] == settings["background_presets"]
    for variant in result["variants"]:
        assert variant["augmentation"]["layers"] == {
            name: settings[name] for name in DOCUMENT_LAYERS
        }
    assert result["image"] == result["variants"][0]["image"]


def test_long_rotated_comparison_uses_the_same_effective_point_size(client):
    result = preview(
        client,
        text="W" * 30,
        language="kurmanji",
        font_size=128,
        rotation=10,
        blur=2,
        compare=True,
        background_presets=["phone-photo", "notebook"],
    )
    assert result["before"]["font_size"] < 128
    assert {variant["font_size"] for variant in result["variants"]} == {
        result["before"]["font_size"]
    }


def test_comparison_clean_toggle_keeps_exact_sample_and_no_effects(client):
    result = preview(
        client,
        compare=True,
        augmentation=0,
        background_presets=["phone-photo", "photocopy"],
        stains=1,
        shadow=1,
    )
    assert result["image"] == result["before"]["image"]
    assert result["augmentation"] == {}
    assert result["variants"] == []
    # Keep the legacy image plus clean baseline without repeating the same
    # encoded buffer in every hidden selector entry.
    assert json.dumps(result).count("data:image/png;base64,") == 2


def test_legacy_custom_background_still_compares_with_clean_sample(client):
    result = preview(
        client,
        background="plain",
        color_mode="rgb",
        background_color="#ecd7a4",
        text_color="#203040",
    )
    assert len(result["variants"]) == 1
    assert result["variants"][0]["preset"] == ""
    assert result["variants"][0]["image"] == result["image"]
    assert result["image"] != result["before"]["image"]
    with decode(result) as image:
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (236, 215, 164)


def test_comparison_releases_each_image_before_next_render(client, monkeypatch):
    original = web.Renderer
    rendered = []

    class ObservedRenderer(original):
        def render(self, *args, **kwargs):
            if rendered:
                with pytest.raises(ValueError, match="closed image"):
                    rendered[-1].load()
            image, info = super().render(*args, **kwargs)
            rendered.append(image)
            return image, info

    monkeypatch.setattr(web, "Renderer", ObservedRenderer)
    result = preview(client, compare=True, background_presets=["office-scan"])
    assert len(result["variants"]) == len(DOCUMENT_PRESETS)
    assert len(rendered) == len(DOCUMENT_PRESETS) + 1
    for image in rendered:
        with pytest.raises(ValueError, match="closed image"):
            image.load()


def test_registry_and_defaults_come_from_shared_configuration(client):
    response = client.get("/api/info")
    assert response.status_code == 200
    assert response.json()["document_presets"] == DOCUMENT_PRESETS
    defaults = response.json()["defaults"]
    assert defaults["background_presets"] == []
    assert defaults["preset_colors"] is True
    assert all(defaults[name] is None for name in DOCUMENT_LAYERS)


@pytest.mark.parametrize(
    ("language", "text"),
    [("sorani", "ڕۆژێکی خۆش؛ ژمارە ١٢٣"), ("kurmanji", "Roj baş, cîhan! Ê û î.")],
)
def test_entire_catalog_can_be_selected_and_last_recipe_is_reachable(client, language, text):
    last = next(reversed(DOCUMENT_PRESETS))
    selected = [last, *(name for name in DOCUMENT_PRESETS if name != last)]
    result = preview(
        client,
        language=language,
        text=text,
        background_presets=selected,
        scan_banding=0.25,
        speckles=0,
        binding_shadow=None,
    )
    assert [entry["preset"] for entry in result["variants"]] == selected
    assert result["augmentation"]["background_preset"] == last
    for variant in result["variants"]:
        assert variant["text"] == text
        assert variant["augmentation"]["layers"]["scan_banding"] == 0.25
        assert variant["augmentation"]["layers"]["speckles"] == 0
        assert (
            variant["augmentation"]["layers"]["binding_shadow"]
            == DOCUMENT_PRESETS[variant["preset"]]["layers"]["binding_shadow"]
        )
    schema = web.AppearanceRequest.model_json_schema()
    assert schema["properties"]["background_presets"]["maxItems"] == len(DOCUMENT_PRESETS)


@pytest.mark.parametrize("endpoint", ["/api/preview", "/api/jobs"])
@pytest.mark.parametrize(
    "changes",
    [
        {"background_presets": "office-scan"},
        {"background_presets": ["unknown"]},
        {"background_presets": ["notebook", "notebook"]},
        {"background_presets": [*DOCUMENT_PRESETS, "notebook"]},
        {"background_presets": [None]},
        {"preset_colors": "false"},
        {"preset_colors": 1},
        *({name: value} for name in DOCUMENT_LAYERS for value in [-0.01, 1.01, True, "0.3"]),
    ],
)
def test_layer_requests_reject_invalid_values_before_job_start(client, endpoint, changes):
    response = client.post(endpoint, json={"augmentation": 1, **changes})
    assert response.status_code == 422, response.text
    assert not client.get("/api/jobs").json()["jobs"]


@pytest.mark.parametrize("value", [0, "true", None])
def test_comparison_toggle_requires_a_boolean(client, value):
    response = client.post("/api/preview", json={"compare": value})
    assert response.status_code == 422, response.text


def test_multi_preset_job_keeps_pool_and_effective_annotations(client):
    selected = list(DOCUMENT_PRESETS)
    settings = {
        "background_presets": selected,
        "augmentation": 1,
        "grain": 0.2,
        "shadow": 0.4,
        "stains": 0.3,
        "creases": 0.2,
        "ruled_lines": 0.1,
        "scan_banding": 0.3,
        "speckles": 0.15,
        "binding_shadow": 0.25,
    }
    response = client.post(
        "/api/jobs",
        json={"source": "spelling", "language": "both", "samples": 274, **settings},
    )
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{identifier}").json()
        if job["status"] not in web.ACTIVE:
            break
        time.sleep(0.02)
    else:
        pytest.fail("The multi-preset Kurdish job did not finish")
    assert job["status"] == "completed", job
    output = Path(job["output"])
    assert verify(output)["records"] == 274
    run = json.loads((output / "run.json").read_text(encoding="utf-8"))
    generation = run["config"]["generation"]
    assert all(generation[name] == value for name, value in settings.items())
    found, languages = set(), set()
    with (output / "manifest.jsonl").open(encoding="utf-8") as stream:
        for row in map(json.loads, stream):
            augmentation = row["augmentation"]
            found.add(augmentation["background_preset"])
            languages.add(row["language"])
            for name in DOCUMENT_LAYERS:
                expected = settings.get(name)
                if expected is not None:
                    assert augmentation["layers"][name] == expected
    assert found == set(selected)
    assert languages == {"ckb", "kmr"}
