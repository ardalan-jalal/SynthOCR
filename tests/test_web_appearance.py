"""Public appearance options reach real previews, datasets and gallery samples."""

import base64
import io
import json
import time
from pathlib import Path

import pytest
from PIL import Image, ImageColor

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from fastapi.testclient import TestClient

from synthocr import web
from synthocr.config import AUGMENTATION_FIELDS, DOCUMENT_LAYERS
from synthocr.verify import verify

APPEARANCE = {
    "augmentation": 1,
    "background": "plain",
    "color_mode": "rgb",
    "background_color": "#eacb9d",
    "background_color_end": "#a9cae9",
    "text_color": "#253052",
    "texture_strength": 0,
    "rotation": 0,
    "blur": 0,
    "contrast": 0,
    "background_presets": [],
    "background_weights": {},
    "preset_colors": True,
    **dict.fromkeys(DOCUMENT_LAYERS),
}


@pytest.fixture
def client(config):
    with TestClient(web.create_app(config), base_url="http://127.0.0.1:8000") as value:
        yield value


def decoded_preview(response):
    assert response.status_code == 200, response.text
    payload = response.json()
    image = Image.open(io.BytesIO(base64.b64decode(payload["image"].split(",", 1)[1])))
    image.load()
    return image, payload


def wait_for_job(client, identifier):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{identifier}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] not in web.ACTIVE:
            return job
        time.sleep(0.01)
    pytest.fail("Generation did not finish")


def test_rgb_plain_preview_uses_selected_colors_without_extra_effects(client):
    response = client.post("/api/preview", json={"text": "سڵاو جیهان", **APPEARANCE})
    image, payload = decoded_preview(response)
    try:
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == ImageColor.getrgb(APPEARANCE["background_color"])
        assert ImageColor.getrgb(APPEARANCE["text_color"]) in {
            color for _, color in image.getcolors(maxcolors=256)
        }
        assert payload["text"] == "سڵاو جیهان"
        assert payload["direction"] == "rtl"
        assert payload["augmentation"]["background"] == "plain"
        assert payload["augmentation"]["rotation"] == 0
        assert payload["augmentation"]["blur"] == 0
        assert payload["augmentation"]["contrast"] == 1
    finally:
        image.close()


def test_rgb_gradient_preview_reaches_both_colors(client):
    response = client.post(
        "/api/preview",
        json={"text": "Çav baş!", "language": "kurmanji", **APPEARANCE, "background": "gradient"},
    )
    image, payload = decoded_preview(response)
    try:
        assert image.mode == "RGB"
        for position, key in (
            ((0, 0), "background_color"),
            ((image.width - 1, image.height - 1), "background_color_end"),
        ):
            assert all(
                abs(actual - expected) <= 2
                for actual, expected in zip(
                    image.getpixel(position), ImageColor.getrgb(APPEARANCE[key]), strict=True
                )
            )
        assert payload["augmentation"]["background_color_end"] == APPEARANCE["background_color_end"]
    finally:
        image.close()


@pytest.mark.parametrize("endpoint", ["/api/preview", "/api/jobs"])
@pytest.mark.parametrize("color_mode", ["rgb", "grayscale"])
@pytest.mark.parametrize(
    "changes",
    [
        {"background_color": "#eacb9d", "text_color": "#eacb9d"},
        {"background_color": "#f5f5f5", "text_color": "#e0e0e0"},
        {
            "background": "gradient",
            "background_color": "#000000",
            "background_color_end": "#ffffff",
            "text_color": "#777777",
        },
    ],
    ids=["same-colors", "low-contrast", "gradient-crosses-foreground"],
)
def test_unreadable_background_colors_are_rejected(client, endpoint, color_mode, changes):
    payload = {**APPEARANCE, **changes, "color_mode": color_mode}
    if endpoint == "/api/jobs":
        payload["samples"] = 1
    response = client.post(endpoint, json=payload)
    assert response.status_code == 422, response.text
    assert "3:1 base contrast" in response.text
    assert not client.get("/api/jobs").json()["jobs"]


def test_clean_preview_ignores_inactive_colors_and_produces_grayscale(client):
    image, payload = decoded_preview(
        client.post(
            "/api/preview",
            json={**APPEARANCE, "augmentation": 0, "text_color": APPEARANCE["background_color"]},
        )
    )
    try:
        assert image.mode == "L"
        assert image.getextrema() == (0, 255)
        assert image.getpixel((0, 0)) == 255
        assert payload["augmentation"] == {}
    finally:
        image.close()


@pytest.mark.parametrize("endpoint", ["/api/preview", "/api/jobs"])
@pytest.mark.parametrize(
    "changes",
    [
        {"background": "fabric"},
        {"color_mode": "rgba"},
        {"background_color": "red"},
        {"background_color": "#fff"},
        {"background_color_end": "#invalid"},
        {"text_color": "#000000ff"},
        {"texture_strength": -0.001},
        {"texture_strength": 0.251},
        {"rotation": -0.001},
        {"rotation": 10.001},
        {"blur": -0.001},
        {"blur": 2.001},
        {"contrast": -0.001},
        {"contrast": 0.501},
    ],
)
def test_invalid_options_are_rejected_without_starting_a_job(client, endpoint, changes):
    payload = {**APPEARANCE, **changes}
    if endpoint == "/api/jobs":
        payload["samples"] = 1
    response = client.post(endpoint, json=payload)
    assert response.status_code == 422, response.text
    assert not client.get("/api/jobs").json()["jobs"]


@pytest.mark.parametrize("endpoint", ["/api/preview", "/api/jobs"])
@pytest.mark.parametrize(
    "field", ["augmentation", "texture_strength", "rotation", "blur", "contrast"]
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_json_values_are_rejected_before_generation(client, endpoint, field, value):
    payload = {**APPEARANCE, field: value}
    if endpoint == "/api/jobs":
        payload["samples"] = 1
    response = client.post(
        endpoint,
        content=json.dumps(payload),
        headers={"content-type": "application/json"},
    )
    # Nonfinite numeric literals are not valid JSON. Either a parser rejection
    # or a model-validation rejection must stay a valid client-error response.
    assert response.status_code in {400, 422}, response.text
    assert response.json()["detail"]
    assert not client.get("/api/jobs").json()["jobs"]


@pytest.mark.parametrize("endpoint", ["/api/preview", "/api/jobs"])
@pytest.mark.parametrize(
    "field", ["augmentation", "texture_strength", "rotation", "blur", "contrast"]
)
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_values_in_valid_json_get_validation_errors(client, endpoint, field, value):
    payload = {**APPEARANCE, field: value}
    if endpoint == "/api/jobs":
        payload["samples"] = 1
    response = client.post(endpoint, json=payload)
    assert response.status_code == 422, response.text
    assert not client.get("/api/jobs").json()["jobs"]


@pytest.mark.parametrize("endpoint", ["/api/preview", "/api/jobs"])
@pytest.mark.parametrize(
    "field", ["augmentation", "texture_strength", "rotation", "blur", "contrast"]
)
@pytest.mark.parametrize("value", [True, False, "0.1"])
def test_numeric_controls_require_json_numbers(client, endpoint, field, value):
    payload = {**APPEARANCE, field: value}
    if endpoint == "/api/jobs":
        payload["samples"] = 1
    response = client.post(endpoint, json=payload)
    assert response.status_code == 422, response.text
    assert not client.get("/api/jobs").json()["jobs"]


def test_real_rgb_job_retains_requested_settings_and_gallery_metadata(client):
    appearance = {
        **APPEARANCE,
        "background": "gradient",
        "texture_strength": 0.13,
        "rotation": 3,
        "blur": 0.4,
        "contrast": 0.2,
    }
    response = client.post(
        "/api/jobs",
        json={"name": "appearance-test", "samples": 3, "language": "sorani", **appearance},
    )
    assert response.status_code == 202, response.text
    job = wait_for_job(client, response.json()["id"])
    assert job["status"] == "completed", job
    output = Path(job["output"])
    assert verify(output)["records"] == 3
    run = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert {key: run["config"]["generation"][key] for key in AUGMENTATION_FIELDS} == appearance
    with (output / "manifest.jsonl").open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream]
    gallery_response = client.get(f"/api/jobs/{job['id']}/samples?limit=3")
    assert gallery_response.status_code == 200, gallery_response.text
    gallery = gallery_response.json()["samples"]
    assert len(gallery) == 3
    for record, sample in zip(records, gallery, strict=True):
        actual = record["augmentation"]
        assert actual["background"] == "gradient"
        assert actual["color_mode"] == "rgb"
        assert actual["background_color"] == appearance["background_color"]
        assert actual["background_color_end"] == appearance["background_color_end"]
        assert actual["text_color"] == appearance["text_color"]
        assert -appearance["rotation"] <= actual["rotation"] <= appearance["rotation"]
        assert 0 <= actual["blur"] <= appearance["blur"]
        assert 1 - appearance["contrast"] <= actual["contrast"] <= 1 + appearance["contrast"]
        assert actual["texture_strength"] == 0  # Smooth gradient does not apply paper texture.
        assert sample["augmentation"] == actual
        image_response = client.get(sample["image"])
        assert image_response.status_code == 200
        with Image.open(io.BytesIO(image_response.content)) as image:
            assert image.mode == "RGB"
            image.load()
        thumbnail = client.get(sample["thumbnail"])
        assert thumbnail.status_code == 200
        with Image.open(io.BytesIO(thumbnail.content)) as image:
            image.load()
            assert image.width <= 768 and image.height <= 192
