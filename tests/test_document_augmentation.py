"""Composable document recipes preserve text, determinism and bounded buffers."""

import io
import json
import random
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageColor, ImageOps

from synthocr.config import (
    DOCUMENT_LAYERS,
    DOCUMENT_PRESETS,
    Generation,
    augmentation_contrast,
    document_layers,
    resolve_document_settings,
    validate_augmentation,
)
from synthocr.fonts import Font
from synthocr.pipeline import generate
from synthocr.render import Renderer, _document_background
from synthocr.verify import verify

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = (
    ("notonaskharabic/NotoNaskhArabic[wght].ttf", "سڵاو؛ ڕێگای هەولێر، کُردستان ١٢٣", "ckb"),
    ("notosans/NotoSans[wdth,wght].ttf", "Çav baş! Êdî ji Îranê, 123", "kmr"),
)


def settings(**changes):
    return replace(
        Generation(augmentation=1, background="plain", rotation=0, blur=0, contrast=0),
        **changes,
    )


def render(generation, sample=SAMPLES[0], seed=42):
    filename, text, language = sample
    return Renderer(generation, 2).render(
        text, Font(str(ROOT / "fonts/open" / filename)), seed, language
    )


@pytest.mark.parametrize("preset", DOCUMENT_PRESETS)
@pytest.mark.parametrize("sample", SAMPLES, ids=("sorani", "kurmanji"))
def test_realistic_presets_reproduce_pixels_and_record_effective_recipe(preset, sample):
    generation = settings(background_presets=[preset])
    image, info = render(generation, sample)
    repeated, other = render(generation, sample)
    try:
        assert info == other
        assert ImageChops.difference(image, repeated).getbbox() is None
        applied = info["augmentation"]
        recipe = DOCUMENT_PRESETS[preset]
        assert applied["background_preset"] == preset
        assert applied["layers"] == recipe["layers"]
        assert applied["background_color"] == recipe["background_color"]
        assert applied["text_color"] == recipe["text_color"]
        assert applied["texture_strength"] == 0
        assert image.mode == ("RGB" if recipe["color_mode"] == "rgb" else "L")
        assert info["language"] == sample[2]
        assert info["direction"] == ("rtl" if sample[2] == "ckb" else "ltr")
        left, top, right, bottom = info["bbox"]
        assert 0 < left < right < image.width
        assert 0 < top < bottom < image.height
    finally:
        image.close()
        repeated.close()


@pytest.mark.parametrize("layer", DOCUMENT_LAYERS)
def test_every_layer_can_be_combined_and_individually_removed(layer):
    combined = settings(**dict.fromkeys(DOCUMENT_LAYERS, 0.8))
    image, info = render(combined)
    omitted, omitted_info = render(replace(combined, **{layer: 0}))
    try:
        assert ImageChops.difference(image, omitted).getbbox() is not None
        assert info["bbox"] == omitted_info["bbox"]
        assert all(value == 0.8 for value in info["augmentation"]["layers"].values())
        assert omitted_info["augmentation"]["layers"][layer] == 0
        assert info["augmentation"]["rotation"] == omitted_info["augmentation"]["rotation"]
    finally:
        image.close()
        omitted.close()


def test_disabling_inherited_layers_does_not_leave_legacy_paper_texture():
    generation = settings(
        background="paper",
        texture_strength=0.25,
        background_presets=["aged-paper"],
        **dict.fromkeys(DOCUMENT_LAYERS, 0),
    )
    image, info = render(generation)
    try:
        assert image.getpixel((0, 0)) == ImageColor.getrgb(
            DOCUMENT_PRESETS["aged-paper"]["background_color"]
        )
        assert info["augmentation"]["layers"] == dict.fromkeys(DOCUMENT_LAYERS, 0)
        assert info["augmentation"]["texture_strength"] == 0
    finally:
        image.close()


def test_explicit_layer_values_override_only_the_selected_components():
    generation = settings(background_presets=["aged-paper"], stains=0, grain=0.9)
    layers = document_layers(generation, "aged-paper")
    assert layers == {**DOCUMENT_PRESETS["aged-paper"]["layers"], "stains": 0, "grain": 0.9}


def test_custom_palette_and_base_override_the_preset_palette():
    generation = settings(
        background_presets=["office-scan"],
        preset_colors=False,
        color_mode="rgb",
        background="gradient",
        background_color="#dce9f1",
        background_color_end="#f2dbc1",
        text_color="#152d41",
        **dict.fromkeys(DOCUMENT_LAYERS, 0),
    )
    assert resolve_document_settings(generation, "office-scan") == generation
    image, info = render(generation)
    try:
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == ImageColor.getrgb(generation.background_color)
        assert image.getpixel((image.width - 1, image.height - 1)) == ImageColor.getrgb(
            generation.background_color_end
        )
        assert info["augmentation"]["text_color"] == generation.text_color
        assert info["augmentation"]["background"] == "gradient"
    finally:
        image.close()


def test_a_pool_uses_all_selected_presets_deterministically():
    generation = settings(background_presets=list(DOCUMENT_PRESETS))
    renderer = Renderer(generation, 2)
    seen = set()
    for seed in range(60):
        chosen_presets = []
        for repeated in (False, True):
            image, info = renderer.render(
                SAMPLES[1][1], Font(str(ROOT / "fonts/open" / SAMPLES[1][0])), seed, "kmr"
            )
            image.close()
            chosen = info["augmentation"]["background_preset"]
            chosen_presets.append(chosen)
            if not repeated:
                seen.add(chosen)
        assert chosen_presets[0] == chosen_presets[1]
    assert seen == set(DOCUMENT_PRESETS)


@pytest.mark.parametrize("other_layer", ("shadow", "binding_shadow", "scan_banding", "speckles"))
def test_grain_keeps_the_same_random_pattern_when_another_effect_is_enabled(other_layer):
    # With white paper and black shading, independently generated alpha layers
    # compose by multiplication (allowing Pillow's one-level rounding).
    layers = dict.fromkeys(DOCUMENT_LAYERS, 0)
    images = []
    try:
        for additions in (
            {"grain": 0.8},
            {other_layer: 0.8},
            {"grain": 0.8, other_layer: 0.8},
        ):
            image = Image.new("L", (800, 100), 255)
            _document_background(image, {**layers, **additions}, 42, 48)
            images.append(image)
        with ImageChops.multiply(images[0], images[1]) as expected:
            with ImageChops.difference(expected, images[2]) as difference:
                assert difference.getextrema()[1] <= 1
    finally:
        for image in images:
            image.close()


def test_every_document_recipe_has_distinct_pixels_and_visible_ink_cores():
    signatures = set()
    reference, _ = render(settings(text_color="#000000", background_color="#ffffff"))
    opaque = reference.point([255] + [0] * 255)
    outside = ImageOps.invert(opaque)
    try:
        for preset in DOCUMENT_PRESETS:
            generation = settings(background_presets=[preset])
            image, _ = render(generation)
            try:
                signatures.add((image.mode, image.tobytes()))
                palette = resolve_document_settings(generation, preset)
                assert augmentation_contrast(palette) >= 3
                # Background layers must not erase fully covered ink, including
                # the cores of combining marks and Kurdish-specific letters.
                color = ImageColor.getrgb(palette.text_color)
                if image.mode == "L":
                    color = (color[0] * 299 + color[1] * 587 + color[2] * 114 + 500) // 1000
                with Image.new(image.mode, image.size, color) as expected:
                    with ImageChops.difference(image, expected) as difference:
                        # Ignore antialiased pixels; their coverage deliberately
                        # blends ink with the paper beneath it.
                        difference.paste(0, mask=outside)
                        assert difference.getbbox() is None
            finally:
                image.close()
        assert len(signatures) == len(DOCUMENT_PRESETS)
    finally:
        reference.close()
        opaque.close()
        outside.close()


def test_binding_shadow_falls_off_smoothly_from_one_edge():
    image = Image.new("L", (800, 100), 255)
    try:
        _document_background(
            image, {**dict.fromkeys(DOCUMENT_LAYERS, 0), "binding_shadow": 1}, 42, 48
        )
        values = [image.getpixel((x, 50)) for x in range(image.width)]
        assert min(values[0], values[-1]) < 150
        assert max(values[0], values[-1]) == 255
        assert values == sorted(values) or values == sorted(values, reverse=True)
        assert max(abs(a - b) for a, b in zip(values, values[1:], strict=False)) <= 3
    finally:
        image.close()


def test_scan_bands_and_flecks_have_different_spatial_structure():
    images = []
    try:
        for effect in ("scan_banding", "speckles"):
            image = Image.new("L", (800, 100), 255)
            _document_background(image, {**dict.fromkeys(DOCUMENT_LAYERS, 0), effect: 1}, 42, 48)
            images.append(image)
        assert len({images[0].getpixel((0, y)) for y in range(100)}) > 3
        assert all(
            images[0].getpixel((x, y)) == images[0].getpixel((0, y))
            for y in range(100)
            for x in range(800)
        )
        histogram = Counter(images[1].tobytes())
        assert histogram[255] > 0.95 * 800 * 100
        assert any(value < 220 for value in histogram)
    finally:
        for image in images:
            image.close()


def test_document_background_closes_all_temporary_images(monkeypatch):
    created = []
    original_new = Image.Image._new

    def track(self, core):
        result = original_new(self, core)
        created.append(result)
        return result

    image = Image.new("RGB", (800, 100), "white")
    monkeypatch.setattr(Image.Image, "_new", track)
    try:
        assert _document_background(image, dict.fromkeys(DOCUMENT_LAYERS, 1), 42, 48) is image
        assert created
        for temporary in created:
            with pytest.raises(ValueError, match="closed"):
                temporary.getpixel((0, 0))
    finally:
        image.close()


def test_document_noise_buffers_remain_bounded_on_wide_lines(monkeypatch):
    sizes = []
    original = random.Random.randbytes

    def record(self, size):
        sizes.append(size)
        return original(self, size)

    monkeypatch.setattr(random.Random, "randbytes", record)
    sample = (SAMPLES[1][0], "Çav baş! " * 200, "kmr")
    image, _ = render(settings(**dict.fromkeys(DOCUMENT_LAYERS, 1)), sample)
    image.close()
    assert sizes and len(sizes) > 2
    assert max(sizes) <= 256 * 256


@pytest.mark.parametrize("sample", SAMPLES, ids=("sorani", "kurmanji"))
@pytest.mark.parametrize("seed", (0, 17, 42))
def test_strong_composition_keeps_marks_inside_rotated_canvas(sample, seed):
    generation = settings(
        background_presets=["aged-paper"],
        padding=0,
        rotation=10,
        blur=2,
        **dict.fromkeys(DOCUMENT_LAYERS, 1),
    )
    image, info = render(generation, sample, seed)
    try:
        left, top, right, bottom = info["bbox"]
        assert 0 < left < right < image.width
        assert 0 < top < bottom < image.height
        assert image.getpixel((0, 0)) != (255, 255, 255)
    finally:
        image.close()


@pytest.mark.parametrize("format", ("PNG", "JPEG", "WEBP", "TIFF"))
def test_composed_color_documents_decode_in_every_format(format):
    image, info = render(settings(background_presets=["aged-paper"], ruled_lines=0.5, shadow=0.7))
    try:
        with io.BytesIO() as stream:
            image.save(stream, format=format)
            stream.seek(0)
            with Image.open(stream) as decoded:
                decoded.load()
                assert decoded.mode == "RGB"
                assert decoded.size == (info["width"], info["height"])
    finally:
        image.close()


def test_disabling_augmentation_ignores_presets_and_every_layer():
    image, info = render(
        settings(
            augmentation=0,
            background_presets=list(DOCUMENT_PRESETS),
            **dict.fromkeys(DOCUMENT_LAYERS, 1),
        )
    )
    try:
        assert image.mode == "L"
        assert image.getpixel((0, 0)) == 255
        assert image.getextrema() == (0, 255)
        assert info["augmentation"] == {}
    finally:
        image.close()


@pytest.mark.parametrize("value", (-0.1, 1.1, True, "0.5", float("inf"), float("nan")))
@pytest.mark.parametrize("layer", DOCUMENT_LAYERS)
def test_layer_controls_reject_invalid_values(layer, value):
    with pytest.raises(ValueError, match=layer):
        validate_augmentation(settings(**{layer: value}))


@pytest.mark.parametrize("presets", ("office-scan", ["unknown"], ["office-scan"] * 2, [None], [{}]))
def test_invalid_preset_pools_are_rejected(presets):
    with pytest.raises(ValueError, match="background_presets"):
        validate_augmentation(settings(background_presets=presets))


@pytest.mark.parametrize("format", ("png", "jpg", "webp", "tif"))
def test_composed_datasets_verify_and_match_serial_and_worker_outputs(config, format):
    serial = replace(
        config,
        generation=replace(
            config.generation,
            augmentation=1,
            format=format,
            background_presets=list(DOCUMENT_PRESETS),
            stains=0.4,
            ruled_lines=0.5,
        ),
    )
    parallel = replace(serial, generation=replace(serial.generation, output="parallel", workers=2))
    manifests = []
    for current in (serial, parallel):
        assert generate(current)["generated"] == 4
        output = current.path(current.generation.output)
        assert verify(output)["records"] == 4
        with (output / "manifest.jsonl").open(encoding="utf-8") as stream:
            records = [json.loads(line) for line in stream]
        for record in records:
            applied = record["augmentation"]
            assert applied["background_preset"] in DOCUMENT_PRESETS
            assert applied["layers"]["stains"] == 0.4
            assert applied["layers"]["ruled_lines"] == 0.5
        manifests.append(records)
    assert manifests[0] == manifests[1]
